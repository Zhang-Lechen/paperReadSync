"""Real UI operations reuse the CLI service, encrypted stores and write gates."""

from dataclasses import asdict
import json
import os
import secrets
from threading import Lock
import time

from paperread.book_match import confirm_book
from paperread.credentials import ApiKeyStore, CredentialStore, PrivateJsonStore, app_directory, read_key_status
from paperread.errors import AppError
from paperread.file_lock import WriterLock
from paperread.jobs import JobManager
from paperread.models import Book


def checked_integer(values, key):
    raw = values.get(key, "")
    if not isinstance(raw, str) or not raw.isdigit():
        raise AppError("INPUT_INVALID", "请填写客户端实际显示的非负整数秒数")
    return int(raw)


def app_values(values):
    unit=values.get("app_unit","seconds")
    if unit not in ("seconds","minutes"):
        raise AppError("INPUT_INVALID", "客户端显示单位只能选择秒或整分钟")
    resolution=60 if unit=="minutes" else 1
    return {"app_book_seconds":checked_integer(values,"app_book_seconds")*resolution,
            "app_period_seconds":checked_integer(values,"app_period_seconds")*resolution,
            "app_resolution_seconds":resolution}


class WebOperations:
    def __init__(self, settings, jobs=None):
        self.settings = settings
        self.jobs = jobs or JobManager()
        self.grants = {}
        self.grant_lock = Lock()
        self.session_status = "尚未检查；使用前会自动检查并尝试续期"

    def maintain_login(self):
        """One bounded startup check, independent of reading jobs and health."""
        if self.settings.mock or not PrivateJsonStore(self.settings.account, "browser").exists():
            return
        self.session_status = "正在检查已保存的登录"
        try:
            with WriterLock(app_directory()/"writer.lock"), self.settings.database() as db:
                from paperread.browser_session import recovery_identity
                from paperread.session_manager import SessionManager
                expected = recovery_identity(db, self.settings.account, None,
                                             app_directory()/"writer.pending.json")
                status = SessionManager(self.settings.account).ensure_browser(expected_identity=expected)
            self.session_status = "登录已自动续期" if status == "RENEWED" else "登录检查通过"
        except AppError as exc:
            self.session_status = ("有操作进行中，将在使用前检查登录" if exc.code == "WRITER_BUSY"
                                   else exc.message)
        except Exception:
            self.session_status = "登录检查未完成；使用前将再次检查"

    def connection_status(self):
        key = read_key_status(self.settings.account)
        return {"key": key["available"], "key_status":key,
                "browser": PrivateJsonStore(self.settings.account, "browser").exists(),
                "session_status":self.session_status}

    def offer(self, entry, purpose, *, preview=None):
        seconds = preview["seconds"] if preview else (min(60, entry["available_seconds"]) if purpose == "probe" else entry["available_seconds"])
        with self.grant_lock:
            # Retain grants briefly so browser refresh/back cannot accidentally replay.
            self.grants = {key:value for key,value in self.grants.items() if time.monotonic()-value["created"]<600}
            token = secrets.token_urlsafe(24)
            self.grants[token] = {"entry_id":entry["entry_id"],"book_id":entry["book_id"],"purpose":purpose,
                "seconds":seconds,"updated_at":entry["updated_at"],"created":time.monotonic(),"preview":preview}
        return {"token":token,"seconds":seconds,"phrase":f"{entry['book_id']}:{seconds}",
                "reservation":preview["reservation"] if preview else None,"blocked":preview["blocked"] if preview else None}

    def _safe_no_pending(self, db):
        if (db.conn.execute("SELECT 1 FROM write_guard").fetchone() or
                (app_directory()/"writer.pending.json").exists()):
            raise AppError("RECONCILE_REQUIRED", "有未决写入，请先核验/对账再更换授权或上下文")

    def _stats(self, db):
        return self.settings.stats(db, allow_live=True)

    def handle(self, action, values):
        if self.settings.mock:
            raise AppError("MODE_CONFLICT", "真实操作不能使用 Mock 数据库")
        if action == "key":
            with WriterLock(app_directory()/"writer.lock"), self.settings.database() as db:
                self._safe_no_pending(db)
                ApiKeyStore(self.settings.account).save_key(values.get("api_key", ""))
            return None
        if action == "search":
            query = values.get("query", "").strip()
            with self.settings.database() as db:
                reader = self._stats(db)
                try:
                    books = reader.search(query)
                    db.cache_candidates(books)
                    db.conn.execute("INSERT OR REPLACE INTO metadata VALUES('last_search',?)", (query,))
                finally:
                    reader.close()
            return None
        if action == "book":
            if values.get("confirmed") != "yes":
                raise AppError("BOOK_UNCONFIRMED", "请明确确认这个电子版本")
            with self.settings.database() as db:
                reader = self._stats(db)
                try:
                    info = reader.book_info(values.get("book_id", ""))
                    db.bind(confirm_book(info, confirmed=True, corresponding_edition=values.get("corresponding")=="yes"))
                finally:
                    reader.close()
            return None
        if action in ("login", "context"):
            return self._browser_job(values.get("book_id") if action=="context" else None)
        if action == "cancel":
            self.jobs.cancel(values.get("job_id", ""))
            return None
        identifier = values.get("entry_id", "")
        with self.settings.database() as db:
            entry = db.entry(identifier, self.settings.account)
        if action == "baseline":
            def task(event, progress):
                from paperread.cli import sync_service
                with self.settings.database() as db, sync_service(self.settings, db, allow_live=True) as service:
                    progress("正在查询这本书和本月所有书的当前阅读时长；若有上次试验会一并检查")
                    return service.baseline(identifier)
            return self.jobs.submit("baseline", task)
        if action == "attest":
            if entry["available_seconds"] <= 0:
                raise AppError("NO_SUBMITTABLE_SECONDS", "当前没有可提交余额，无需准备同步。核对当前读数不能解除历史待查保留额度；请返回账本查看记录。")
            from paperread.cli import sync_service
            if "review_snapshot" in values:
                with self.settings.database() as db, sync_service(self.settings,db,writer=True,
                        allow_live=True,entry_id=identifier) as service:
                    return service.confirm_baseline(identifier,snapshot_id=values.get("review_snapshot"),
                        review_confirmed=values.get("review_confirmed")=="yes")
            if values.get("app_reviewed")!="yes":
                raise AppError("APP_REVIEW_REQUIRED", "请先在微信读书 App 核对单书/月度统计口径")
            with self.settings.database() as db, sync_service(self.settings, db, writer=True,
                    allow_live=True, entry_id=identifier) as service:
                return service.attest(identifier,**app_values(values),
                    same_account=values.get("same_account")=="yes", no_other_reading=values.get("no_other_reading")=="yes")
        if action == "diagnose":
            def task(event, progress):
                from paperread.cli import sync_service
                with self.settings.database() as db, sync_service(self.settings, db, allow_live=True) as service:
                    progress("正在只读诊断上次试验，不提交阅读时长、不解锁或替代 App 核对")
                    return service.diagnose(identifier)
            return self.jobs.submit("diagnose", task)
        if action == "reserve":
            from paperread.cli import sync_service
            with self.settings.database() as db, sync_service(self.settings,db,allow_live=True) as service:
                return service.withdraw_uncertain(identifier,phrase=values.get("phrase",""),
                    reserve_confirmed=values.get("reserve_confirmed")=="yes")
        if action in ("verify", "reconcile"):
            options = {"no_other_reading":values.get("no_other_reading")=="yes",
                       "same_day_confirmed":values.get("same_day_confirmed")=="yes"}
            if values.get("app_book_seconds", "") or values.get("app_period_seconds", ""):
                options.update(**app_values(values))
            def task(event,progress):
                from paperread.cli import sync_service
                with self.settings.database() as db, sync_service(self.settings,db,allow_live=True) as service:
                    progress("只读查询服务器并对账，不会重新发送阅读请求")
                    method = service.reconcile if action=="reconcile" else service.verify
                    return method(identifier,**options)
            return self.jobs.submit(action,task)
        if action in ("probe", "remaining", "sync", "retry"):
            return self._write_job(entry, action, values)
        raise AppError("ACTION_INVALID", "未知操作")

    def _browser_job(self, book_id):
        def task(event,progress):
            from paperread.browser_session import run_authorization
            with self.settings.database() as db:
                book = None
                if book_id:
                    current = db.book(book_id)
                    reader = self._stats(db)
                    try:
                        fresh = reader.book_info(book_id)
                    finally:
                        reader.close()
                    for key in ("title", "author", "translator", "publisher", "isbn"):
                        if getattr(current,key) != getattr(fresh,key):
                            raise AppError("BOOK_METADATA_CHANGED", "版本信息已变化，请重新核对后绑定")
                    book = fresh
                return run_authorization(self.settings.account,cancel_event=event,progress=progress,
                    lock_path=app_directory()/"writer.lock",book=book,database=db)
        return self.jobs.submit("context" if book_id else "login",task)

    def _write_job(self, entry, purpose, values):
        from paperread.wereadgear_adapter import require_live_flags
        direct = purpose in {"sync", "retry"}
        confirmation = "retry_confirmed" if purpose=="retry" else "sync_confirmed"
        approved = values.get(confirmation)=="yes" if direct else values.get("confirm")=="yes"
        require_live_flags(live=approved if direct else values.get("live")=="yes",confirm=approved)
        if not approved or (not direct and any(values.get(key)!="yes" for key in ("actual_reading", "same_account", "no_other_reading", "same_day_confirmed"))):
            raise AppError("READING_CONFIRMATION", "请明确确认实际纸质阅读、同一账号、暂停其他活动及同步自然日")
        token = values.get("grant", "")
        with self.grant_lock:
            grant = self.grants.get(token)
        if not grant or grant["entry_id"]!=entry["entry_id"] or grant["purpose"]!=purpose or time.monotonic()-grant["created"]>300:
            raise AppError("APPROVAL_EXPIRED", "同步预览已失效，请刷新页面重新确认")
        if not direct and values.get("phrase") != f"{grant['book_id']}:{grant['seconds']}":
            raise AppError("CONFIRMATION_MISMATCH", "请输入页面显示的精确书籍 ID 与本次秒数")
        if grant["updated_at"] != entry["updated_at"]:
            # If this token has already launched a job, return that job without replay.
            existing = self.jobs.by_token(token)
            if existing:
                return existing
            raise AppError("APPROVAL_EXPIRED", "记录状态已变化，请重新检查预览")
        budget = grant["seconds"]
        if direct and grant["preview"] and grant["preview"]["blocked"]:
            raise AppError("RECOVERY_REQUIRED",grant["preview"]["blocked"])
        if budget<=0:
            raise AppError("ALREADY_COMPLETED", "本条记录已无剩余额度，不会新增请求")
        identifier=entry["entry_id"]
        attempt_id = __import__("uuid").uuid4()
        def task(event,progress):
            from paperread.cli import sync_service
            with self.settings.database() as db, sync_service(self.settings,db,writer=True,
                    allow_live=True,entry_id=identifier) as service:
                if direct:
                    if event.is_set():
                        return {"status":"CANCELLED","entry_id":identifier,"completed_this_job_seconds":0}
                    if purpose=="retry":
                        service.authorize_reserved_retry(identifier,preview=grant["preview"],confirmed=approved)
                    else:
                        reservation=grant["preview"]["reservation"] if grant["preview"] else None
                        if reservation:
                            # The old allowance belongs to its original entry,
                            # even when this approval starts a different book.
                            service.reserve_for_direct(reservation["entry_id"],attempt_id=reservation["attempt_id"],
                                seconds=reservation["seconds"],confirmed=approved)
                        if service._entry(identifier)["available_seconds"]!=budget:
                            raise AppError("APPROVAL_EXPIRED","可用时长已变化，请刷新并确认同步预览")
                        service.authorize_direct(identifier,confirmed=approved)
                elif values.get("review_in_write")=="yes":
                    # The visible --confirm checkbox explicitly includes review
                    # of the displayed phone readings in this form version.
                    try:
                        service.confirm_baseline(identifier,snapshot_id=values.get("review_snapshot"),
                            review_confirmed=values.get("confirm")=="yes")
                    except AppError as exc:
                        if exc.code=="REVIEW_CHANGED":
                            return {"status":"REVIEW_CHANGED","entry_id":identifier,"notice":exc.message,
                                "reading_requests_sent_this_operation":0}
                        raise
                if purpose=="probe":
                    progress(f"正在执行最多 {budget} 秒试验；取消后只允许对账")
                    return service.start(identifier,seconds=budget,attempt_id=attempt_id,
                        live=True,confirm=True,cancel_event=event)
                from paperread.verifier import WEB_TOLERANCE_SECONDS
                return service.run_remaining(identifier,budget_seconds=budget,live=True,confirm=True,
                    cancel_event=event,progress=progress,tolerance_seconds=WEB_TOLERANCE_SECONDS if direct else 0)
        return self.jobs.submit(purpose,task,token=token)
