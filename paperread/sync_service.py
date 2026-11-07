"""Durable intent, conservative reconciliation, independent attribution and budgets."""

from dataclasses import asdict
from datetime import datetime, timezone
import json
import time
from pathlib import Path
from uuid import UUID, uuid4

from paperread.db import digest
from paperread.errors import AppError
from paperread.file_lock import WriterLock
from paperread.models import State, StatsPair, utc_now
from paperread.verifier import compare, book_time_metric, Verdict


def uncertainty_reservation(attempt, after):
    """Exclude the whole approval and any larger observed book increase; never credit it."""
    if attempt["credited_seconds"]:
        raise AppError("RECOVERY_REQUIRED", "上次写入已部分归属，请先只读对账")
    before = StatsPair.model_validate_json(attempt["before_json"])
    if any(getattr(before,key)!=getattr(after,key) for key in ("book_id","account_id","identity_hash","source","period_key")):
        raise AppError("RECOVERY_REQUIRED", "旧写入的账户或统计周期与当前不一致，请先对账")
    amount = attempt["requested_seconds"]
    if book_time_metric(before)==book_time_metric(after):
        delta = after.book_seconds-before.book_seconds
        if delta < 0:
            raise AppError("COUNTER_CONFLICT", "旧写入的单书计数下降，请先对账")
        amount = max(amount,delta)
    return amount


def direct_sync_preview(db, entry):
    """Local preview only. It cannot send requests, retire intent or invent an App review."""
    active = db.conn.execute("SELECT s.* FROM sync_attempts s JOIN write_guard w ON w.attempt_id=s.attempt_id").fetchone()
    result = {"seconds":entry["available_seconds"],"reservation":None,"blocked":None}
    if not active:
        return result
    previous_entry = db.entry(active["entry_id"])
    previous_book = db.book(previous_entry["book_id"])
    if previous_entry["account_id"]!=entry["account_id"]:
        return {**result,"blocked":f"另一个账号的《{previous_book.title}》有未决写入，当前不能替它处理。"}
    observation = db.conn.execute("SELECT pair_json FROM remote_snapshots WHERE attempt_id=? ORDER BY rowid DESC LIMIT 1",
                                  (active["attempt_id"],)).fetchone()
    try:
        after = StatsPair.model_validate_json(observation[0] if observation else active["before_json"])
        amount = uncertainty_reservation(active,after)
    except AppError as exc:
        return {**result,"blocked":exc.message}
    if amount>previous_entry["available_seconds"]:
        return {**result,"blocked":"已观察到的旧写入超过可用额度，不能继续提交。"}
    same_entry = previous_entry["entry_id"]==entry["entry_id"]
    return {**result,"seconds":entry["available_seconds"]-(amount if same_entry else 0),
            "reservation":{"attempt_id":active["attempt_id"],"entry_id":previous_entry["entry_id"],
                "book_title":previous_book.title,"same_entry":same_entry,"seconds":amount}}


def reserved_retry_preview(db, entry):
    attempts = db.reserved_attempts(entry["entry_id"])
    return {"seconds":min(sum(item["reserved_seconds"] for item in attempts),
                          entry["duration_seconds"]-entry["verified_seconds"]),
            "attempts":[item["attempt_id"] for item in attempts], "reservation":None,
            "blocked":"当前有未结束的写入，请先完成或处理该批次后再重新提交。"
                if db.conn.execute("SELECT 1 FROM write_guard").fetchone() else None}


class SyncService:
    def __init__(self, db, stats, writer=None, *, account_id="default", lock_path: Path,
                 settlement_clock=None, settlement_wait=None):
        self.db, self.stats, self.writer = db, stats, writer
        self.account_id = account_id
        self.lock_path = lock_path
        self.pending_path = lock_path.with_suffix(".pending.json")
        self.mock = stats.source == "mock"
        self.settlement_clock = settlement_clock or time.monotonic
        self.settlement_wait = settlement_wait
        backend = db.conn.execute("SELECT value FROM metadata WHERE key='backend'").fetchone()[0]
        if (backend == "mock") != self.mock:
            raise AppError("BACKEND_MISMATCH", "Mock 统计不能用于真实数据库核验")

    def _entry(self, entry_id):
        return self.db.entry(str(entry_id), self.account_id)

    def _active(self):
        return self.db.conn.execute("SELECT attempt_id FROM write_guard WHERE slot=1").fetchone()

    def _attempt(self, attempt_id):
        row = self.db.conn.execute("SELECT * FROM sync_attempts WHERE attempt_id=?", (str(attempt_id),)).fetchone()
        if not row:
            raise AppError("ATTEMPT_NOT_FOUND", "未找到同步尝试")
        return dict(row)

    def _marker(self):
        if not self.pending_path.exists():
            return None
        try:
            value = json.loads(self.pending_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value["attempt_id"], str):
                raise ValueError()
            return value
        except (OSError, ValueError, KeyError):
            raise AppError("RECOVERY_REQUIRED", "本机写意图文件异常，需先人工检查；未发送请求") from None

    def baseline(self, entry_id):
        # Reading current statistics remains useful during recovery. Never
        # replace the approved pre-write snapshot of an unresolved attempt.
        with WriterLock(self.lock_path):
            entry = self._entry(entry_id)
            active = self._active()
            if active:
                attempt = self._attempt(active[0])
                if attempt["entry_id"] == entry["entry_id"]:
                    result = self._diagnose_locked(entry, attempt)
                    return {**result, "status":"PENDING_REVIEW_REQUIRED",
                        "book_title":self.db.book(entry["book_id"]).title,
                        "book_seconds":result["after_book_seconds"],
                        "period_seconds":result["after_period_seconds"],
                        "original_baseline_preserved":True,"needs_recovery":True}
            if active or self.pending_path.exists():
                pair = self.stats.pair(entry["book_id"],entry["timezone"])
                self._validate_pair(entry,pair)
                with self.db.transaction():
                    self.db.save_snapshot(entry["entry_id"],pair,"observation")
                return {"status":"STATISTICS_ONLY","source":pair.source,"entry_id":entry["entry_id"],
                    "book_title":self.db.book(entry["book_id"]).title,
                    "book_seconds":pair.book_seconds,"period_seconds":pair.period_seconds,
                    "captured_at":pair.captured_at,"needs_recovery":True,
                    "reading_requests_sent_this_operation":0,"original_baseline_preserved":True}
            return self._new_baseline_locked(entry)

    def _new_baseline_locked(self, entry):
        try:
            pair = self.stats.pair(entry["book_id"], entry["timezone"])
        except AppError:
            with self.db.transaction():
                self.db.set_state(entry["entry_id"], State.BLOCKED)
            raise
        self._validate_pair(entry, pair)
        review = self.db.conn.execute("SELECT * FROM attestations WHERE entry_id=?",(entry["entry_id"],)).fetchone()
        preserve_review = False
        if review and review["identity_hash"] == pair.identity_hash:
            original = self.db.conn.execute("SELECT pair_json FROM remote_snapshots WHERE snapshot_id=? AND entry_id=?",
                (review["snapshot_id"],entry["entry_id"])).fetchone()
            if original:
                try:
                    preserve_review = self._same_statistics(StatsPair.model_validate_json(original[0]),pair)
                except ValueError:
                    pass
        with self.db.transaction():
            snapshot_id = self.db.save_snapshot(entry["entry_id"], pair, "baseline")
            if preserve_review:
                # This is still the person's original review, not a new consent.
                self.db.conn.execute("UPDATE attestations SET snapshot_id=? WHERE entry_id=?",(snapshot_id,entry["entry_id"]))
            else:
                self.db.conn.execute("DELETE FROM attestations WHERE entry_id=?", (entry["entry_id"],))
            if entry["verified_seconds"] < entry["duration_seconds"]:
                target = (State.PARTIALLY_VERIFIED if entry["verified_seconds"] else State.READY) if preserve_review else State.LOCAL_ONLY
                if entry["status"] != target:
                    self.db.set_state(entry["entry_id"],target)
            self.db.event("BASELINE_SAVED", entry["entry_id"], details={"source": pair.source,"review_preserved":preserve_review})
        return {"snapshot_id": snapshot_id, "entry_id":entry["entry_id"], "source": pair.source, "book_seconds": pair.book_seconds,
                "period_seconds": pair.period_seconds, "captured_at": pair.captured_at,
                "status": "BASELINE_SAVED", "needs_client_attestation": not preserve_review,"review_preserved":preserve_review,
                "book_title":self.db.book(entry["book_id"]).title}

    @staticmethod
    def _same_statistics(left, right):
        return all(getattr(left,k)==getattr(right,k) for k in
            ("book_id","account_id","book_seconds","period_seconds","period_key","identity_hash","source")) and book_time_metric(left)==book_time_metric(right)

    def _validate_pair(self, entry, pair):
        expected = "mock" if self.mock else "official"
        if (pair.source != expected or pair.account_id != entry["account_id"] or pair.book_id != entry["book_id"]
                or pair.identity_hash != self.stats.identity_hash):
            raise AppError("IDENTITY_MISMATCH", "统计来源、账户或书籍不一致")

    def _review_context(self, entry_id):
        entry = self._entry(entry_id)
        if self._active() or self.pending_path.exists():
            raise AppError("RECONCILE_REQUIRED", "请先处理上次试验，当前确认不会覆盖原记录")
        snapshot_id, pair = self.db.latest_snapshot(entry["entry_id"])
        self._validate_pair(entry, pair)
        return entry, snapshot_id, pair

    def _reject_review(self, entry, code, message, details=None):
        with self.db.transaction():
            self.db.conn.execute("DELETE FROM attestations WHERE entry_id=?",(entry["entry_id"],))
            state = ("MOCK_PARTIALLY_VERIFIED" if self.mock else State.PARTIALLY_VERIFIED) if entry["verified_seconds"] else State.LOCAL_ONLY
            self.db.set_state(entry["entry_id"],state)
            self.db.event("CLIENT_REVIEW_REJECTED",entry["entry_id"],details={"reason":code,**(details or {})})
        raise AppError(code,message)

    def _save_review(self, entry, snapshot_id, pair, document, *, retry_preview=None):
        if self.writer is None:
            raise AppError("WRITER_LOGIN_REQUIRED", "本地写入凭证未准备，请先准备这本书")
        self.writer.validate_book(entry["book_id"])
        with self.db.transaction():
            if retry_preview is not None:
                current = self._entry(entry["entry_id"])
                if reserved_retry_preview(self.db,current) != retry_preview:
                    raise AppError("APPROVAL_EXPIRED", "待查额度已变化，请刷新后重新确认")
                for attempt in self.db.reserved_attempts(entry["entry_id"]):
                    result = json.loads(attempt["result_json"] or "{}")
                    result.update(reservation_released_for_retry=True, retry_approved_at=utc_now(),
                                  retry_authorization_id=document["retry_authorization_id"])
                    self.db.conn.execute("UPDATE sync_attempts SET result_json=? WHERE attempt_id=?",
                                         (json.dumps(result),attempt["attempt_id"]))
                    self.db.event("RESERVATION_RELEASED_FOR_RETRY",entry["entry_id"],attempt["attempt_id"],
                                  {"seconds":attempt["reserved_seconds"],"duplicate_risk_accepted":True,
                                   "retry_authorization_id":document["retry_authorization_id"]})
            if snapshot_id is None:
                snapshot_id = self.db.save_snapshot(entry["entry_id"],pair,"baseline")
            self.db.conn.execute("INSERT OR REPLACE INTO attestations VALUES(?,?,?,?,?,?)",
                (entry["entry_id"],snapshot_id,pair.identity_hash,self.writer.identity_hash,utc_now(),json.dumps(document)))
            self.db.set_state(entry["entry_id"],State.READY)
            self.db.event("CLIENT_BASELINE_ATTESTED",entry["entry_id"],details={"review_method":document["review_method"]})
        return {"status":"READY","source":pair.source,"review_method":document["review_method"]}

    def attest(self, entry_id, *, app_book_seconds: int, app_period_seconds: int,
               same_account: bool, no_other_reading: bool, app_resolution_seconds: int = 1):
        # Numeric CLI/legacy forms remain supported, with precise feedback.
        with WriterLock(self.lock_path):
            entry,snapshot_id,pair = self._review_context(entry_id)
            self._fresh(pair)
            if not same_account:
                self._reject_review(entry,"ACCOUNT_CONFIRMATION_REQUIRED","尚未确认手机 App、扫码登录和 Key 是同一账号")
            if not no_other_reading:
                self._reject_review(entry,"ACTIVITY_CONFIRMATION_REQUIRED","尚未确认已暂停其他阅读和听书")
            if type(app_resolution_seconds) is not int or app_resolution_seconds not in (1,60):
                self._reject_review(entry,"APP_UNIT_INVALID","请选择按秒或整分钟显示")
            details = {"expected_book_seconds":pair.book_seconds,"expected_period_seconds":pair.period_seconds,
                "app_book_seconds":app_book_seconds if type(app_book_seconds) is int else None,
                "app_period_seconds":app_period_seconds if type(app_period_seconds) is int else None,
                "app_resolution_seconds":app_resolution_seconds}
            if not self._client_matches(pair,app_book_seconds,app_period_seconds,app_resolution_seconds):
                if type(app_book_seconds) is not int or type(app_period_seconds) is not int:
                    self._reject_review(entry,"APP_VALUE_INVALID","阅读时长应填写非负整数",details)
                field = "这一本书" if app_book_seconds != pair.book_seconds//app_resolution_seconds*app_resolution_seconds else "本月所有书"
                actual = app_book_seconds if field=="这一本书" else app_period_seconds
                expected = pair.book_seconds if field=="这一本书" else pair.period_seconds
                self._reject_review(entry,"CLIENT_READINGS_MISMATCH",
                    f"{field}填写的读数折合 {actual} 秒；服务器为 {expected} 秒（{expected//60} 整分钟）。请核对秒与分钟单位。账号和活动确认已通过。",details)
            return self._save_review(entry,snapshot_id,pair,{"app_book_seconds":app_book_seconds,
                "app_period_seconds":app_period_seconds,"app_resolution_seconds":app_resolution_seconds,
                "same_account":True,"no_other_reading":True,"source":pair.source,"review_method":"numeric"})

    def confirm_baseline(self, entry_id, *, snapshot_id, review_confirmed=False):
        """User reviews the displayed values; do not invent typed App readings."""
        with WriterLock(self.lock_path):
            entry,current_id,shown = self._review_context(entry_id)
            if not review_confirmed:
                self._reject_review(entry,"REVIEW_CONFIRMATION_REQUIRED","请先在手机核对页面上的时长，并确认同一账号及暂停其他阅读")
            if snapshot_id != current_id:
                original = self.db.conn.execute("SELECT pair_json FROM remote_snapshots WHERE snapshot_id=? AND entry_id=? AND role='baseline'",
                    (snapshot_id,entry["entry_id"])).fetchone()
                if not original:
                    raise AppError("REVIEW_CHANGED","页面上的读数已更新，请核对当前显示后确认")
                original_pair = StatsPair.model_validate_json(original[0])
                if not self._same_statistics(original_pair,shown):
                    raise AppError("REVIEW_CHANGED","页面上的读数已变化，请核对新值后确认")
                shown = original_pair
            fresh = self.stats.pair(entry["book_id"],entry["timezone"])
            self._validate_pair(entry,fresh)
            self._fresh(fresh)
            if not self._same_statistics(shown,fresh):
                with self.db.transaction():
                    self.db.save_snapshot(entry["entry_id"],fresh,"baseline")
                    self.db.conn.execute("DELETE FROM attestations WHERE entry_id=?",(entry["entry_id"],))
                    self.db.set_state(entry["entry_id"],State.LOCAL_ONLY if not entry["verified_seconds"] else State.PARTIALLY_VERIFIED)
                raise AppError("REVIEW_CHANGED","等待核对期间服务器读数发生了变化，页面已更新；请核对新读数后确认")
            # Re-read instead of rejecting an unchanged display just because the
            # person needed more than 120 seconds to open their phone.
            return self._save_review(entry,None,fresh,{"review_method":"display_confirmation",
                "displayed_snapshot_id":snapshot_id,"displayed_book_seconds":shown.book_seconds,
                "displayed_period_seconds":shown.period_seconds,"app_reviewed_by_user":True,
                "same_account":True,"no_other_reading":True,"source":fresh.source})

    @staticmethod
    def _client_matches(pair, book_seconds, period_seconds, resolution):
        # Explicit UI display precision, never a tolerance on server attribution.
        # Minute mode compares displayed whole minutes (truncated), while every
        # credited second still comes from exact, independently equal API deltas.
        if type(resolution) is not int or resolution not in (1,60) or type(book_seconds) is not int or type(period_seconds) is not int:
            return False
        return (book_seconds,period_seconds)==(pair.book_seconds//resolution*resolution,
                                               pair.period_seconds//resolution*resolution)

    @staticmethod
    def _fresh(pair):
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(pair.captured_at)).total_seconds()
        except (TypeError, ValueError):
            age = -1
        if not 0 <= age <= 120:
            raise AppError("BASELINE_STALE", "基线已过期，请重新只读采集并核对客户端")

    def _grant(self, entry, purpose):
        attestation = self.db.conn.execute("SELECT * FROM attestations WHERE entry_id=?", (entry["entry_id"],)).fetchone()
        if not attestation or self.writer is None:
            raise AppError("ATTESTATION_REQUIRED", "需先 baseline，再核对客户端并 attest")
        if attestation["identity_hash"] != self.stats.identity_hash or attestation["writer_identity_hash"] != self.writer.identity_hash:
            raise AppError("IDENTITY_MISMATCH", "读/写凭证与人工确认时已不同，请停止并重新核对账户")
        if self.writer.source != self.stats.source:
            raise AppError("BACKEND_MISMATCH", "写入和核验适配器来源不一致")
        if purpose == "probe":
            probe = self.db.conn.execute("SELECT 1 FROM sync_attempts WHERE entry_id=? AND purpose='probe' AND verification_status!='WITHDRAWN_UNVERIFIED' LIMIT 1", (entry["entry_id"],)).fetchone()
            if probe:
                raise AppError("PROBE_ALREADY_ATTEMPTED", "已有试验尝试；请先只读对账，不能重复试验")

    def authorize_direct(self, entry_id, *, confirmed=False):
        """One explicit sync approval; no claim that the user typed/reviewed App counters."""
        if not confirmed:
            raise AppError("READING_CONFIRMATION", "请确认本次书籍、实际阅读、账号及同步额度")
        with WriterLock(self.lock_path):
            entry = self._entry(entry_id)
            if self._active() or self._marker():
                raise AppError("RECONCILE_REQUIRED", "上次写入尚未处理，不会重复发送")
            pair = self.stats.pair(entry["book_id"],entry["timezone"])
            self._validate_pair(entry,pair)
            self._fresh(pair)
            return self._save_review(entry,None,pair,{"review_method":"direct_sync_authorization",
                "same_account":True,"no_other_reading":True,"actual_paper_reading":True,
                "current_day_confirmed":True,"source":pair.source,"app_reviewed_by_user":False})

    def authorize_reserved_retry(self, entry_id, *, preview, confirmed=False):
        if not confirmed:
            raise AppError("READING_CONFIRMATION", "请确认重新提交待查时长，并接受可能重复累计")
        with WriterLock(self.lock_path):
            entry = self._entry(entry_id)
            if self._active() or self._marker():
                raise AppError("RECONCILE_REQUIRED", "请先处理当前未结束的写入")
            if preview["seconds"] <= 0 or reserved_retry_preview(self.db,entry) != preview:
                raise AppError("APPROVAL_EXPIRED", "待查额度已变化，请刷新后重新确认")
            if self.writer is None:
                raise AppError("WRITER_LOGIN_REQUIRED", "请先准备本书的登录上下文")
            self.writer.validate_book(entry["book_id"])
            if hasattr(self.writer,"validate_session"):
                self.writer.validate_session()
            pair = self.stats.pair(entry["book_id"],entry["timezone"])
            self._validate_pair(entry,pair)
            self._fresh(pair)
            return self._save_review(entry,None,pair,{"review_method":"reserved_retry_authorization",
                "retry_authorization_id":str(uuid4()),"duplicate_risk_accepted":True,
                "same_account":True,"no_other_reading":True,"actual_paper_reading":True,
                "current_day_confirmed":True,"source":pair.source,"app_reviewed_by_user":False},
                retry_preview=preview)

    def reserve_for_direct(self, entry_id, *, attempt_id, seconds, confirmed=False):
        """Explicitly quarantine an old attempt before a newly approved direct job."""
        if not confirmed or type(seconds) is not int:
            raise AppError("RESERVATION_CONFIRMATION", "请确认页面显示的旧写入保留额度")
        with WriterLock(self.lock_path):
            entry = self._entry(entry_id)
            active = self._active()
            if not active or active[0]!=attempt_id:
                raise AppError("APPROVAL_EXPIRED", "旧写入状态已变化，请刷新同步预览")
            attempt = self._attempt(attempt_id)
            marker = self._marker()
            if (attempt["entry_id"]!=entry["entry_id"] or not marker or marker["attempt_id"]!=attempt_id
                    or marker.get("database")!=str(self.db.path.resolve())):
                raise AppError("RECOVERY_REQUIRED", "旧写入意图不一致，未改变额度")
            review = self.db.conn.execute("SELECT writer_identity_hash FROM attestations WHERE entry_id=?",
                                          (entry["entry_id"],)).fetchone()
            if not review or self.writer is None or review[0]!=self.writer.identity_hash:
                raise AppError("IDENTITY_MISMATCH", "当前书的登录账号与旧写入账号不同，未处理旧额度")
            after = self.stats.pair(entry["book_id"],entry["timezone"])
            self._validate_pair(entry,after)
            required = uncertainty_reservation(attempt,after)
            if required!=seconds:
                with self.db.transaction():
                    self.db.save_snapshot(entry["entry_id"],after,"diagnostic",attempt_id)
                raise AppError("APPROVAL_EXPIRED", "旧写入的最新读数已变化，请刷新并确认新的保留额度；未提交阅读请求")
            if seconds>entry["available_seconds"]:
                raise AppError("BUDGET_EXCEEDED", "保留额度超过本条记录余额")
            result = json.loads(attempt["result_json"] or "{}")
            result.update(reserved_seconds=seconds,reservation_method="direct_sync_quarantine",
                          reservation_confirmed_by_user=True)
            with self.db.transaction():
                self.db.save_snapshot(entry["entry_id"],after,"recovery_observation",attempt_id)
                self.db.conn.execute("UPDATE sync_attempts SET verification_status='WITHDRAWN_UNVERIFIED',result_json=? WHERE attempt_id=?",
                                     (json.dumps(result),attempt_id))
                self.db.conn.execute("DELETE FROM write_guard WHERE attempt_id=?",(attempt_id,))
                self.db.conn.execute("DELETE FROM attestations WHERE entry_id=?",(entry["entry_id"],))
                self.db.set_state(entry["entry_id"],State.PARTIALLY_VERIFIED if entry["verified_seconds"] else State.LOCAL_ONLY)
                self.db.event("UNCERTAIN_BUDGET_RESERVED",entry["entry_id"],attempt_id,
                    {"reserved_seconds":seconds,"approved_seconds":attempt["requested_seconds"],
                     "credited_seconds":0,"confirmed_by_user":True,"method":"direct_sync_quarantine"})
            self._clear_marker(attempt_id)
        return self.summary(attempt_id)

    def start(self, entry_id, *, seconds: int, purpose="probe", attempt_id=None,
              live=False, confirm=False, cancel_event=None):
        entry = self._entry(entry_id)
        attempt_id = str(UUID(str(attempt_id))) if attempt_id else str(uuid4())
        existing = self.db.conn.execute("SELECT * FROM sync_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if existing:
            if existing["entry_id"] != entry["entry_id"] or existing["requested_seconds"] != seconds or existing["purpose"] != purpose:
                raise AppError("ATTEMPT_ID_CONFLICT", "尝试 ID 已用于其他操作")
            return self.summary(attempt_id)
        if entry["verified_seconds"] == entry["duration_seconds"]:
            return {"status": entry["status"], "network_writes": 0, "remaining_seconds": 0}
        if not self.mock:
            from paperread.wereadgear_adapter import require_live_flags
            require_live_flags(live=live, confirm=confirm)
        if type(seconds) is not int or not 1 <= seconds <= 60 or purpose not in ("probe", "remaining"):
            raise AppError("BUDGET_INVALID", "单次批准额度为 1 至 60 整秒")
        with WriterLock(self.lock_path):
            if self._marker():
                raise AppError("RECONCILE_REQUIRED", "本机存在未决写入，需使用原数据库对账")
            if self._active():
                raise AppError("RECONCILE_REQUIRED", "数据库存在未完成意图，不会重发")
            self._grant(entry, purpose)
            self.writer.validate_book(entry["book_id"])
            if hasattr(self.writer, "validate_session"):
                # Read-only auth check must fail before durable intent and before
                # any reading POST, so an expired session does not consume a probe.
                self.writer.validate_session()
            _, approved = self.db.latest_snapshot(entry["entry_id"])
            fresh = self.stats.pair(entry["book_id"], entry["timezone"])
            self._validate_pair(entry, fresh)
            self._fresh(fresh)
            if not self._same_statistics(approved,fresh):
                raise AppError("BASELINE_CHANGED", "批准后统计已改变，无法排除其他阅读；请重新核对")
            with self.db.transaction():
                # Reread inside the transaction to prevent concurrent budget consumption.
                entry = self._entry(entry_id)
                if seconds > entry["available_seconds"]:
                    raise AppError("BUDGET_EXCEEDED", "请求额度超过本条记录尚未核验的余额")
                if self._active():
                    raise AppError("WRITER_BUSY", "数据库已有活动写意图")
                no = self.db.conn.execute("SELECT COALESCE(MAX(attempt_no),0)+1 FROM sync_attempts WHERE entry_id=?", (entry["entry_id"],)).fetchone()[0]
                started = utc_now()
                request_hash = digest(json.dumps({"entry_id": entry["entry_id"], "attempt_id": attempt_id,
                    "book_id": entry["book_id"], "seconds": seconds, "purpose": purpose}, sort_keys=True))
                self.db.conn.execute(
                    """INSERT INTO sync_attempts(attempt_id,entry_id,attempt_no,requested_seconds,before_book_seconds,
                    before_period_seconds,started_at,transport_status,verification_status,request_hash,before_json,purpose)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (attempt_id, entry["entry_id"], no, seconds, fresh.book_seconds, fresh.period_seconds,
                     started, "INTENT_PERSISTED", "UNVERIFIED", request_hash, fresh.model_dump_json(), purpose),
                )
                self.db.conn.execute("INSERT INTO write_guard VALUES(1,?)", (attempt_id,))
                self.db.set_state(entry["entry_id"], State.PROBING if purpose == "probe" else State.SYNCING)
                self.db.conn.execute("UPDATE reading_entries SET sync_at=? WHERE entry_id=?", (started, entry["entry_id"]))
                self.db.save_snapshot(entry["entry_id"], fresh, "before", attempt_id)
                self.db.event("WRITE_INTENT", entry["entry_id"], attempt_id, {"requested_seconds": seconds, "purpose": purpose})
            marker = {"attempt_id": attempt_id, "database": str(self.db.path.resolve())}
            temporary = self.pending_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(marker), encoding="utf-8")
            temporary.replace(self.pending_path)
            try:
                options = {"live": live, "confirm": confirm}
                if cancel_event is not None:
                    options["cancel_event"] = cancel_event
                result = self.writer.run_bounded_session(entry["book_id"], seconds, **options)
                unsent = result.requests_sent == 0 and result.transport_status != "RESPONSE_RECEIVED"
                state = (State.AWAITING_VERIFICATION if result.transport_status == "RESPONSE_RECEIVED" else
                         State.CANCELLED if unsent and result.transport_status == "CANCELLED" else
                         State.FAILED_CONFIRMED if unsent else State.UNCERTAIN)
                result_json = json.dumps(asdict(result))
                with self.db.transaction():
                    self.db.conn.execute(
                        "UPDATE sync_attempts SET transport_status=?,finished_at=?,error_code=?,result_json=? WHERE attempt_id=?",
                        (result.transport_status, utc_now(), result.error_code, result_json, attempt_id),
                    )
                    self.db.set_state(entry["entry_id"], state)
                    if unsent:
                        self.db.conn.execute("DELETE FROM write_guard WHERE attempt_id=?", (attempt_id,))
                        self.db.conn.execute("UPDATE sync_attempts SET verification_status='CANCELLED' WHERE attempt_id=?", (attempt_id,))
                        self.db.event("WRITE_NOT_SENT",entry["entry_id"],attempt_id,{"released_seconds":seconds})
                if unsent:
                    self.pending_path.unlink()
            except BaseException:
                with self.db.transaction():
                    self.db.conn.execute("UPDATE sync_attempts SET transport_status='UNCERTAIN',error_code='INTERRUPTED' WHERE attempt_id=?", (attempt_id,))
                    self.db.set_state(entry["entry_id"], State.UNCERTAIN)
                raise
        return self.summary(attempt_id)

    def summary(self, attempt_id):
        attempt = self._attempt(attempt_id)
        entry = self._entry(attempt["entry_id"])
        result = json.loads(attempt["result_json"]) if attempt["result_json"] else {}
        return {"entry_id": entry["entry_id"], "attempt_id": attempt["attempt_id"], "source": self.stats.source,
                "status": entry["status"], "transport_status": attempt["transport_status"],
                "verification_status": attempt["verification_status"], "requested_seconds": attempt["requested_seconds"],
                "verified_seconds": entry["verified_seconds"], "remaining_seconds": entry["duration_seconds"] - entry["verified_seconds"],
                "reserved_seconds": entry["reserved_seconds"], "available_seconds": entry["available_seconds"],
                "error_code": attempt["error_code"], "write_error_code": result.get("error_code"),
                "requests_sent": result.get("requests_sent"), "elapsed_seconds": result.get("elapsed_seconds"),
                "response_diagnostics": result.get("response_diagnostics"),
                "comparison": result.get("comparison"), "book_delta": result.get("book_delta"),
                "tolerance_seconds":result.get("tolerance_seconds",0),
                "period_delta": result.get("period_delta"),"client_confirmed":result.get("client_confirmed",False),
                "automatic_verification":result.get("automatic_verification",False),
                "confirmed_concurrent_reading":result.get("confirmed_concurrent_reading",False),
                "excluded_surplus_seconds":result.get("excluded_surplus_seconds",0)}

    def diagnose(self, entry_id):
        """Observe an existing attempt without client attestations, credit or unlocking."""
        entry = self._entry(entry_id)
        row = self.db.conn.execute("SELECT attempt_id FROM sync_attempts WHERE entry_id=? ORDER BY attempt_no DESC LIMIT 1",
                                  (entry["entry_id"],)).fetchone()
        if not row:
            raise AppError("ATTEMPT_NOT_FOUND", "没有写入尝试可诊断")
        with WriterLock(self.lock_path):
            return self._diagnose_locked(entry, self._attempt(row[0]))

    def _diagnose_locked(self, entry, attempt):
        before = StatsPair.model_validate_json(attempt["before_json"])
        after = self.stats.pair(entry["book_id"], entry["timezone"])
        self._validate_pair(entry, after)
        result = json.loads(attempt["result_json"]) if attempt["result_json"] else {}
        verdict = compare(before, after, requested_seconds=attempt["requested_seconds"],
                          elapsed_seconds=result.get("elapsed_seconds", 0))
        evidence = {**self.summary(attempt["attempt_id"]), "status": "READ_ONLY_DIAGNOSIS",
            "entry_status": entry["status"], "captured_at": after.captured_at,
            "before_book_seconds": before.book_seconds, "after_book_seconds": after.book_seconds,
            "before_period_seconds": before.period_seconds, "after_period_seconds": after.period_seconds,
            "book_delta": None if verdict.code=="MEASUREMENT_CHANGED" else after.book_seconds-before.book_seconds,
            "period_delta": after.period_seconds-before.period_seconds, "comparison": verdict.code,
            "before_book_time_field":book_time_metric(before),"after_book_time_field":book_time_metric(after),
            "client_review_performed": False, "reading_requests_sent_this_operation": 0,
            "response_diagnostics_available": result.get("response_diagnostics") is not None}
        with self.db.transaction():
            self.db.save_snapshot(entry["entry_id"], after, "diagnostic", attempt["attempt_id"])
            self.db.event("READ_ONLY_DIAGNOSIS", entry["entry_id"], attempt["attempt_id"], evidence)
        return evidence

    def _clear_marker(self, attempt_id):
        marker = self._marker()
        if marker and marker["attempt_id"] == attempt_id and marker.get("database") == str(self.db.path.resolve()):
            self.pending_path.unlink()

    def withdraw_uncertain(self, entry_id, *, phrase, reserve_confirmed=False):
        """Explicitly reserve the WHOLE old approval; never assert a failed write or free its budget."""
        entry = self._entry(entry_id)
        with WriterLock(self.lock_path):
            active = self._active()
            if not active:
                raise AppError("ATTEMPT_NOT_FOUND", "没有待保留的未决试验")
            attempt = self._attempt(active[0])
            if attempt["entry_id"] != entry["entry_id"] or attempt["purpose"] != "probe" or attempt["credited_seconds"] != 0:
                raise AppError("RECOVERY_REQUIRED", "只能保留本记录尚未入账的小额试验额度")
            if (not reserve_confirmed or phrase != f"RESERVE:{attempt['attempt_id']}:{attempt['requested_seconds']}"):
                raise AppError("RESERVATION_CONFIRMATION", "请本人明确确认保留旧试验全部额度；不会将其释放为可写余额")
            marker = self._marker()
            if not marker or marker["attempt_id"] != attempt["attempt_id"] or marker.get("database") != str(self.db.path.resolve()):
                raise AppError("RECOVERY_REQUIRED", "未决意图与数据库不一致，未改变任何额度")
            before = StatsPair.model_validate_json(attempt["before_json"])
            after = self.stats.pair(entry["book_id"],entry["timezone"])
            self._validate_pair(entry, after)
            if before.period_key != after.period_key or (book_time_metric(before)==book_time_metric(after) and after.book_seconds != before.book_seconds):
                raise AppError("RECONCILE_REQUIRED", "单书读数或统计周期已变化，请先对账；未保留或释放额度")
            with self.db.transaction():
                self.db.save_snapshot(entry["entry_id"],after,"recovery_observation",attempt["attempt_id"])
                self.db.conn.execute("UPDATE sync_attempts SET verification_status='WITHDRAWN_UNVERIFIED' WHERE attempt_id=?",(attempt["attempt_id"],))
                self.db.conn.execute("DELETE FROM write_guard WHERE attempt_id=?",(attempt["attempt_id"],))
                self.db.conn.execute("DELETE FROM attestations WHERE entry_id=?",(entry["entry_id"],))
                self.db.set_state(entry["entry_id"],State.LOCAL_ONLY)
                self.db.event("UNCERTAIN_BUDGET_RESERVED",entry["entry_id"],attempt["attempt_id"],
                    {"reserved_seconds":attempt["requested_seconds"],"credited_seconds":0,
                     "book_delta":after.book_seconds-before.book_seconds if book_time_metric(before)==book_time_metric(after) else None,
                     "before_book_time_field":book_time_metric(before),"after_book_time_field":book_time_metric(after),
                     "period_delta":after.period_seconds-before.period_seconds,"confirmed_by_user":True})
            self._clear_marker(attempt["attempt_id"])
        return {**self.summary(attempt["attempt_id"]),"status":"UNCERTAIN_BUDGET_RESERVED",
                "notice":"旧额度仍被冻结且未入账；需重新读取基线并本人批准新试验，程序不会自动重发"}

    def verify(self, entry_id, *, app_book_seconds=None, app_period_seconds=None,
               no_other_reading=False, same_day_confirmed=False, app_resolution_seconds=1,
               concurrent_reading_confirmed=False, tolerance_seconds=0):
        entry = self._entry(entry_id)
        row = self.db.conn.execute("SELECT attempt_id FROM sync_attempts WHERE entry_id=? ORDER BY attempt_no DESC LIMIT 1", (entry["entry_id"],)).fetchone()
        if not row:
            raise AppError("ATTEMPT_NOT_FOUND", "没有待核验尝试；只读基线不代表同步")
        attempt = self._attempt(row[0])
        if attempt["verification_status"] == "WITHDRAWN_UNVERIFIED":
            with WriterLock(self.lock_path):
                self._clear_marker(attempt["attempt_id"])
            return self.summary(attempt["attempt_id"])
        if attempt["verification_status"] in ("VERIFIED", "CANCELLED"):
            with WriterLock(self.lock_path):
                self._clear_marker(attempt["attempt_id"])
            return self.summary(attempt["attempt_id"])
        with WriterLock(self.lock_path):
            marker = self._marker()
            if marker and (marker["attempt_id"] != attempt["attempt_id"] or marker.get("database") != str(self.db.path.resolve())):
                raise AppError("RECONCILE_REQUIRED", "本机未决意图属于其他数据库/尝试")
            before = StatsPair.model_validate_json(attempt["before_json"])
            try:
                after = self.stats.pair(entry["book_id"], entry["timezone"])
                self._validate_pair(entry, after)
            except AppError as exc:
                with self.db.transaction():
                    self.db.set_state(entry["entry_id"], State.UNCERTAIN)
                    self.db.conn.execute("UPDATE sync_attempts SET error_code=? WHERE attempt_id=?", (exc.code, attempt["attempt_id"]))
                raise
            result = json.loads(attempt["result_json"]) if attempt["result_json"] else {}
            # After a crash, API deltas need explicit client evidence in addition to
            # the persisted authorized cap and elapsed wall interval; no resend.
            elapsed = result.get("elapsed_seconds", 0)
            crashed = not elapsed
            if crashed:
                elapsed = min(attempt["requested_seconds"], max(0, int((
                    datetime.now(timezone.utc)-datetime.fromisoformat(attempt["started_at"])).total_seconds())))
            verdict = compare(before, after, requested_seconds=attempt["requested_seconds"], elapsed_seconds=elapsed,
                              tolerance_seconds=tolerance_seconds if not crashed and attempt["transport_status"]=="RESPONSE_RECEIVED" else 0)
            raw_comparison = verdict.code
            # This is explicit, human-assisted reconciliation of known concurrent
            # SAME-BOOK reading, never an automatic tolerance for excess counters.
            # Credit at most the successfully sent, elapsed, approved batch; keep
            # every observed surplus second separate from the paper entry.
            known_concurrent = (concurrent_reading_confirmed is True and not crashed
                and attempt["purpose"]=="remaining" and attempt["transport_status"]=="RESPONSE_RECEIVED"
                and result.get("requests_sent",0)>0 and elapsed>=attempt["requested_seconds"]
                and verdict.code=="DELTA_EXCEEDS_BUDGET")
            excluded_surplus = 0
            if known_concurrent:
                excluded_surplus = after.book_seconds-before.book_seconds-attempt["requested_seconds"]
                verdict = Verdict("DUAL_DELTA_WITH_CONFIRMED_CONCURRENT_READING",attempt["requested_seconds"],True)
            client = (no_other_reading and same_day_confirmed and self._client_matches(after,
                      app_book_seconds,app_period_seconds,app_resolution_seconds))
            needs_client = attempt["purpose"] == "probe" or crashed
            failure = verdict.code
            allowed = verdict.attributable_seconds > 0 and (client or not needs_client)
            if not no_other_reading and not known_concurrent:
                allowed, failure = False, "OTHER_ACTIVITY_NOT_EXCLUDED"
            if not same_day_confirmed:
                allowed, failure = False, "DAY_BUCKET_NOT_CONFIRMED"
            if needs_client and not client:
                allowed, failure = False, "CLIENT_REVIEW_REQUIRED"
            if verdict.code == "MEASUREMENT_CHANGED":
                allowed, failure = False, "MEASUREMENT_CHANGED"
            with self.db.transaction():
                entry = self._entry(entry_id)
                current = self._attempt(attempt["attempt_id"])
                snapshot_id = self.db.save_snapshot(entry["entry_id"], after, "after", attempt["attempt_id"])
                status = State.AWAITING_VERIFICATION if verdict.code == "NO_REMOTE_CHANGE" else State.UNCERTAIN
                credited = current["credited_seconds"]
                complete = allowed and verdict.complete
                if allowed:
                    if verdict.attributable_seconds < credited:
                        raise AppError("COUNTER_CONFLICT", "本次已归属读数下降，保留意图并停止")
                    increment = verdict.attributable_seconds - credited
                    if entry["verified_seconds"] + increment + entry["reserved_seconds"] > entry["duration_seconds"]:
                        raise AppError("BUDGET_EXCEEDED", "核验增量超过记录额度")
                    self.db.conn.execute("UPDATE reading_entries SET verified_seconds=verified_seconds+? WHERE entry_id=?", (increment, entry["entry_id"]))
                    credited = verdict.attributable_seconds
                    if complete:
                        total = entry["verified_seconds"] + increment
                        status = ("MOCK_VERIFIED" if self.mock else State.VERIFIED) if total == entry["duration_seconds"] else (
                            "MOCK_PARTIALLY_VERIFIED" if self.mock else State.PARTIALLY_VERIFIED)
                    else:
                        # Preserve unresolved intent: delayed remainder may still arrive.
                        status = State.PARTIAL
                evidence = {**result, "client_confirmed": client, "automatic_verification":not needs_client and not known_concurrent, "comparison": verdict.code,
                            "tolerance_seconds":tolerance_seconds,
                            "raw_comparison":raw_comparison,"confirmed_concurrent_reading":known_concurrent,
                            "excluded_surplus_seconds":excluded_surplus,
                            "crash_reconciliation": crashed,
                            "client_resolution_seconds":app_resolution_seconds if client else None,
                            "book_delta": None if verdict.code=="MEASUREMENT_CHANGED" else after.book_seconds-before.book_seconds,
                            "before_book_time_field":book_time_metric(before),"after_book_time_field":book_time_metric(after),
                            "period_delta": after.period_seconds-before.period_seconds, "source": after.source,
                            "no_other_reading": no_other_reading and not known_concurrent,
                            "day_bucket_confirmed": same_day_confirmed}
                self.db.conn.execute(
                    """UPDATE sync_attempts SET after_book_seconds=?,after_period_seconds=?,after_json=?,result_json=?,
                    verification_status=?,credited_seconds=?,error_code=? WHERE attempt_id=?""",
                    (after.book_seconds, after.period_seconds, after.model_dump_json(), json.dumps(evidence),
                     "VERIFIED" if complete else "PARTIAL" if allowed else "UNVERIFIED", credited,
                     None if complete else failure, attempt["attempt_id"]),
                )
                self.db.set_state(entry["entry_id"], status)
                self.db.event("REMOTE_COMPARED", entry["entry_id"], attempt["attempt_id"], evidence)
                if complete:
                    self.db.conn.execute("DELETE FROM write_guard WHERE attempt_id=?", (attempt["attempt_id"],))
                    baseline_id = self.db.save_snapshot(entry["entry_id"], after, "baseline")
                    self.db.conn.execute("UPDATE attestations SET snapshot_id=?,confirmed_at=? WHERE entry_id=?",
                                         (baseline_id, utc_now(), entry["entry_id"]))
            if complete and marker:
                self.pending_path.unlink()
        return self.summary(attempt["attempt_id"])

    def reconcile(self, entry_id, **confirmations):
        # Intentionally has no path to writer.run_bounded_session.
        return self.verify(entry_id, **confirmations)

    def verify_until_settled(self, entry_id, *, cancel_event=None, progress=None,
                            wait_seconds=60, poll_seconds=3, tolerance_seconds=0):
        """Wait for two separately refreshed counters; NEVER call the writer here."""
        deadline = self.settlement_clock()+wait_seconds
        queries = 0
        while True:
            result = self.verify(entry_id,no_other_reading=True,same_day_confirmed=True,tolerance_seconds=tolerance_seconds)
            queries += 1
            result = {**result,"settlement_queries":queries}
            if result["verification_status"]=="VERIFIED":
                return result
            if cancel_event is not None and cancel_event.is_set():
                return {**result,"settlement_cancelled":True}
            # A stale book/period cache may trail by one heartbeat. Wait only
            # while BOTH counters remain within the original approved budget.
            deltas = (result.get("book_delta"),result.get("period_delta"))
            retryable = (result.get("comparison") in {"NO_REMOTE_CHANGE","ATTRIBUTION_UNPROVEN","DUAL_DELTA_CONFIRMED"}
                and result.get("error_code") in {"NO_REMOTE_CHANGE","ATTRIBUTION_UNPROVEN","DUAL_DELTA_CONFIRMED"}
                and all(type(value) is int and 0<=value<=result["requested_seconds"]+tolerance_seconds for value in deltas))
            remaining = deadline-self.settlement_clock()
            if not retryable or remaining<=0:
                return {**result,"settlement_timed_out":retryable}
            if progress:
                progress(f"已发送本批时长，正在等服务器统计更新：单书 +{deltas[0]} 秒 / 月度 +{deltas[1]} 秒；仅查询，不重发")
            delay = min(poll_seconds,remaining)
            if self.settlement_wait is not None:
                self.settlement_wait(delay)
            elif cancel_event is not None:
                cancel_event.wait(delay)
            else:
                time.sleep(delay)
            if cancel_event is not None and cancel_event.is_set():
                return {**result,"settlement_cancelled":True}

    def run_remaining(self, entry_id, *, budget_seconds, live=False, confirm=False,
                      cancel_event=None, progress=None, tolerance_seconds=0):
        """One approved batch job, with read-only settlement between writes."""
        initial = self._entry(entry_id)
        if type(budget_seconds) is not int or not 0<budget_seconds<=initial["available_seconds"]:
            raise AppError("BUDGET_EXCEEDED","本次任务额度超过可提交余额")
        self._grant(initial,"remaining")
        result = {}

        def finish(state):
            current = self._entry(entry_id)
            completed = current["verified_seconds"]-initial["verified_seconds"]
            value = {**result,"entry_id":initial["entry_id"],"source":self.stats.source,
                "status":current["status"],"verified_seconds":current["verified_seconds"],
                "remaining_seconds":current["duration_seconds"]-current["verified_seconds"],
                "reserved_seconds":current["reserved_seconds"],"available_seconds":current["available_seconds"],
                "batch_status":state,"approved_job_seconds":budget_seconds,
                "completed_this_job_seconds":completed,"unverified_this_job_seconds":budget_seconds-completed}
            value["tolerance_seconds"] = tolerance_seconds
            with self.db.transaction():
                self.db.event("BATCH_"+state,initial["entry_id"],details={
                    key:value.get(key) for key in ("approved_job_seconds","completed_this_job_seconds",
                        "unverified_this_job_seconds","error_code","comparison")})
            return value

        while self._entry(entry_id)["verified_seconds"]-initial["verified_seconds"]<budget_seconds:
            if cancel_event is not None and cancel_event.is_set():
                return finish("CANCELLED")
            consumed = self._entry(entry_id)["verified_seconds"]-initial["verified_seconds"]
            seconds = min(60,budget_seconds-consumed)
            if progress:
                progress(f"本次已核验 {consumed//60} 分 {consumed%60} 秒 / 批准 {budget_seconds//60} 分 {budget_seconds%60} 秒；正在处理下一批")
            try:
                result = self.start(entry_id,seconds=seconds,purpose="remaining",live=live,
                                    confirm=confirm,cancel_event=cancel_event)
                if result["transport_status"]!="RESPONSE_RECEIVED":
                    return finish("CANCELLED" if cancel_event is not None and cancel_event.is_set() else "STOPPED")
                result = self.verify_until_settled(entry_id,cancel_event=cancel_event,progress=progress,
                                                  tolerance_seconds=tolerance_seconds)
            except AppError as exc:
                result = {**result,"error_code":exc.code,"batch_error":exc.message}
                return finish("STOPPED")
            if result["verification_status"]!="VERIFIED":
                return finish("CANCELLED" if result.get("settlement_cancelled") else "STOPPED")
        return finish("COMPLETED")

    def audit(self, entry_id):
        entry = self._entry(entry_id)
        attempts = [dict(row) for row in self.db.conn.execute(
            "SELECT attempt_id,requested_seconds,transport_status,verification_status,error_code,credited_seconds,before_book_seconds,after_book_seconds,before_period_seconds,after_period_seconds,started_at,finished_at FROM sync_attempts WHERE entry_id=? ORDER BY attempt_no", (entry["entry_id"],))]
        return {"entry": {k:v for k,v in entry.items() if k not in {"input_json","input_hash"}},
                "book": self.db.book(entry["book_id"]).model_dump(), "attempts": attempts,
                "source": self.stats.source, "remote_exactly_once_guaranteed": False}
