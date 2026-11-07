from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from paperread.book_match import confirm_book
from paperread.db import Database
from paperread.errors import AppError
from paperread.file_lock import WriterLock
from paperread.mock_adapter import MockStats
from paperread.models import Book, EntryInput
from paperread.sync_service import SyncService
from paperread.wereadgear_adapter import MockWriter, WriteResult


@pytest.fixture
def sync(tmp_path):
    with Database(tmp_path / "mock.sqlite", mock=True) as db:
        db.bind(confirm_book(Book(book_id="mock-A", title="合成书 A", author="作者", publisher="示例", origin="mock"), confirmed=True))
        entry = db.add(EntryInput(book_id="mock-A", reading_date="2026-10-22", duration_seconds=3600))
        stats = MockStats(db)
        writer = MockWriter(stats)
        service = SyncService(db, stats, writer, lock_path=tmp_path / "writer.lock")
        service.baseline(entry["entry_id"])
        service.attest(entry["entry_id"], app_book_seconds=0, app_period_seconds=0, same_account=True, no_other_reading=True)
        yield service, entry["entry_id"], writer, stats


def verify(service, entry_id, stats):
    pair=stats.pair("mock-A", "Asia/Shanghai")
    return service.verify(entry_id, app_book_seconds=pair.book_seconds, app_period_seconds=pair.period_seconds,
                          no_other_reading=True, same_day_confirmed=True)


def test_transport_success_is_awaiting_and_probe_is_part_of_same_budget(sync):
    service, entry_id, writer, stats = sync
    first=service.start(entry_id, seconds=60)
    assert first["status"] == "AWAITING_VERIFICATION"
    assert first["verified_seconds"] == 0
    result=verify(service, entry_id, stats)
    assert result["verified_seconds"] == 60 and result["remaining_seconds"] == 3540
    assert result["status"] == "MOCK_PARTIALLY_VERIFIED"
    assert writer.calls == [("mock-A",60)]


def test_expired_session_preflight_creates_no_intent_and_consumes_no_probe(sync):
    service, entry_id, writer, stats = sync
    def expired():
        raise AppError("SESSION_EXPIRED","synthetic expired session")
    writer.validate_session=expired
    with pytest.raises(AppError,match="SESSION_EXPIRED"):
        service.start(entry_id,seconds=60)
    assert writer.calls==[] and not service.pending_path.exists() and not service._active()
    assert service.db.conn.execute("SELECT COUNT(*) FROM sync_attempts").fetchone()[0]==0
    assert service._entry(entry_id)["verified_seconds"]==0


def test_uncertain_recovery_reserves_full_budget_and_never_claims_failure_or_success(sync):
    service, entry_id, writer, stats = sync
    writer.run_bounded_session=lambda *a,**k:WriteResult("UNCERTAIN",1,20,"WRITE_RESPONSE")
    first=service.start(entry_id,seconds=60)
    phrase=f"RESERVE:{first['attempt_id']}:60"
    with pytest.raises(AppError,match="RESERVATION_CONFIRMATION"):
        service.withdraw_uncertain(entry_id,phrase=phrase)
    assert service._active() and service._entry(entry_id)["available_seconds"]==3600
    stats.advance("mock-B",1)  # Unknown total activity is not credited to the target.
    result=service.withdraw_uncertain(entry_id,phrase=phrase,reserve_confirmed=True)
    assert result["reserved_seconds"]==60 and result["available_seconds"]==3540
    assert result["verified_seconds"]==0 and result["verification_status"]=="WITHDRAWN_UNVERIFIED"
    assert not service._active() and not service.pending_path.exists()
    assert service._attempt(first["attempt_id"])["transport_status"]=="UNCERTAIN"
    # Reconciliation does not credit or release the reservation, even if the
    # server later changes; old and new activity can no longer be separated.
    service.reconcile(entry_id)
    assert service._entry(entry_id)["reserved_seconds"]==60
    writer.run_bounded_session=MockWriter(stats).run_bounded_session
    service.baseline(entry_id)
    pair=stats.pair("mock-A","Asia/Shanghai")
    service.attest(entry_id,app_book_seconds=pair.book_seconds,app_period_seconds=pair.period_seconds,
                   same_account=True,no_other_reading=True)
    service.start(entry_id,seconds=60)
    result=verify(service,entry_id,stats)
    assert result["verified_seconds"]==60 and result["available_seconds"]==3480
    for _ in range(58):
        service.start(entry_id,seconds=60,purpose="remaining")
        result=service.verify(entry_id,no_other_reading=True,same_day_confirmed=True)
    assert result["verified_seconds"]==3540 and result["reserved_seconds"]==60 and result["available_seconds"]==0
    assert result["status"]!="MOCK_VERIFIED"  # Remaining uncertainty cannot become success.
    with pytest.raises(AppError,match="BUDGET_EXCEEDED"):
        service.start(entry_id,seconds=1,purpose="remaining")


def test_reservation_rejects_changed_book_and_retains_original_intent(sync):
    service, entry_id, writer, stats = sync
    writer.run_bounded_session=lambda *a,**k:WriteResult("UNCERTAIN",1,20,"WRITE_RESPONSE")
    first=service.start(entry_id,seconds=60)
    stats.advance("mock-A",1)
    with pytest.raises(AppError,match="RECONCILE_REQUIRED"):
        service.withdraw_uncertain(entry_id,phrase=f"RESERVE:{first['attempt_id']}:60",reserve_confirmed=True)
    assert service._active() and service.pending_path.exists()
    assert service._entry(entry_id)["reserved_seconds"]==0


def test_same_attempt_id_never_sends_twice(sync):
    service, entry_id, writer, stats = sync
    attempt_id=str(uuid4())
    service.start(entry_id, seconds=60, attempt_id=attempt_id)
    service.start(entry_id, seconds=60, attempt_id=attempt_id)
    assert len(writer.calls) == 1
    with pytest.raises(AppError, match="ATTEMPT_ID_CONFLICT"):
        service.start(entry_id, seconds=30, attempt_id=attempt_id)


def test_second_session_blocked_until_independent_verification(sync):
    service, entry_id, writer, stats = sync
    service.start(entry_id, seconds=60)
    with pytest.raises(AppError, match="RECONCILE_REQUIRED"):
        service.start(entry_id, seconds=60, purpose="remaining")
    assert len(writer.calls) == 1


def test_baseline_during_uncertain_attempt_reads_stats_without_replacing_approved_evidence(sync):
    service, entry_id, writer, stats = sync
    calls=[]
    writer.run_bounded_session=lambda *a,**k:calls.append(1) or WriteResult("UNCERTAIN",1,20,"WRITE_RESPONSE")
    result=service.start(entry_id,seconds=60)
    old_entry=service._entry(entry_id)
    old_attempt=service._attempt(result["attempt_id"])
    old_baseline=service.db.latest_snapshot(entry_id)
    old_attestation=dict(service.db.conn.execute("SELECT * FROM attestations WHERE entry_id=?",(entry_id,)).fetchone())
    old_marker=service.pending_path.read_bytes()
    stats.advance("mock-B",1)
    for _ in range(2):
        observed=service.baseline(entry_id)
        assert observed["status"]=="PENDING_REVIEW_REQUIRED"
        assert observed["book_seconds"]==0 and observed["period_delta"]==1
        assert observed["original_baseline_preserved"] and observed["reading_requests_sent_this_operation"]==0
    assert service._entry(entry_id)==old_entry
    assert service._attempt(result["attempt_id"])==old_attempt
    assert service.db.latest_snapshot(entry_id)==old_baseline
    assert dict(service.db.conn.execute("SELECT * FROM attestations WHERE entry_id=?",(entry_id,)).fetchone())==old_attestation
    assert service.pending_path.read_bytes()==old_marker and service._active()
    assert calls==[1]
    with pytest.raises(AppError,match="RECONCILE_REQUIRED"):
        service.start(entry_id,seconds=60)


def test_other_pending_record_does_not_prevent_readonly_statistics(sync):
    service, entry_id, writer, stats = sync
    service.start(entry_id,seconds=60)
    other=service.db.add(EntryInput(book_id="mock-A",reading_date="2026-10-23",duration_seconds=3600))
    result=service.baseline(other["entry_id"])
    assert result["status"]=="STATISTICS_ONLY" and result["reading_requests_sent_this_operation"]==0
    assert service._entry(other["entry_id"])["status"]=="LOCAL_ONLY"
    with pytest.raises(AppError,match="BASELINE_REQUIRED"):
        service.db.latest_snapshot(other["entry_id"])
    assert len(writer.calls)==1 and service._active()


def test_direct_remaining_does_not_require_probe_or_manual_phone_readings(sync):
    service, entry_id, writer, stats = sync
    service.authorize_direct(entry_id,confirmed=True)
    service.start(entry_id, seconds=60, purpose="remaining")
    result=service.verify(entry_id, no_other_reading=True, same_day_confirmed=True)
    assert result["verified_seconds"] == 60 and result["verification_status"]=="VERIFIED"
    assert result["automatic_verification"] and not result["client_confirmed"]
    assert service.db.conn.execute("SELECT COUNT(*) FROM sync_attempts WHERE purpose='probe'").fetchone()[0]==0


def test_full_synthetic_budget_and_completed_entry_cannot_write_again(sync):
    service, entry_id, writer, stats = sync
    service.start(entry_id, seconds=60)
    verify(service, entry_id, stats)
    for _ in range(59):
        service.start(entry_id, seconds=60, purpose="remaining")
        result=service.verify(entry_id, no_other_reading=True, same_day_confirmed=True)
        assert result["verification_status"] == "VERIFIED"
    assert result["status"] == "MOCK_VERIFIED"
    assert result["verified_seconds"] == 3600
    assert result["remaining_seconds"] == 0
    last=service.start(entry_id, seconds=60, purpose="remaining")
    assert last["network_writes"] == 0
    assert len(writer.calls) == 60


def test_external_book_activity_changes_total_and_blocks_attribution(sync):
    service, entry_id, writer, stats = sync
    service.start(entry_id, seconds=60)
    stats.advance("mock-B", 60)
    result=verify(service, entry_id, stats)
    assert result["verified_seconds"] == 0
    assert result["error_code"] == "ATTRIBUTION_UNPROVEN"
    assert service._active()


def test_partial_credit_keeps_intent_and_delayed_remainder_only_credits_difference(sync):
    service, entry_id, writer, stats = sync
    def partial(book_id, seconds, **kwargs):
        stats.advance(book_id,30)
        return WriteResult("RESPONSE_RECEIVED",1,60)
    writer.run_bounded_session=partial
    service.start(entry_id,seconds=60)
    result=verify(service,entry_id,stats)
    assert result["status"] == "PARTIAL" and result["verified_seconds"] == 30
    assert service._active()
    stats.advance("mock-A",30)
    result=verify(service,entry_id,stats)
    assert result["verified_seconds"] == 60
    assert not service._active()
    verify(service,entry_id,stats)
    assert service._entry(entry_id)["verified_seconds"] == 60


def test_timeout_after_server_receipt_reconciles_without_resend(sync):
    service, entry_id, writer, stats = sync
    calls=[]
    def timed_out(book_id, seconds, **kwargs):
        calls.append(book_id)
        stats.advance(book_id,seconds)
        return WriteResult("UNCERTAIN",1,60,"WRITE_NETWORK")
    writer.run_bounded_session=timed_out
    result=service.start(entry_id,seconds=60)
    assert result["status"] == "UNCERTAIN"
    result=service.reconcile(entry_id,app_book_seconds=60,app_period_seconds=60,no_other_reading=True,same_day_confirmed=True)
    assert result["verified_seconds"] == 60
    assert calls == ["mock-A"]


def test_readonly_diagnosis_preserves_uncertain_intent_and_never_assumes_client_review(sync):
    service, entry_id, writer, stats = sync
    calls=[]
    def failed(book_id, seconds, **kwargs):
        calls.append(book_id)
        return WriteResult("UNCERTAIN",1,20,"WRITE_RESPONSE")  # Historical result without details.
    writer.run_bounded_session=failed
    original=service.start(entry_id,seconds=60)
    stats.advance("mock-B",1)
    before=service._entry(entry_id)
    for _ in range(2):
        result=service.diagnose(entry_id)
        assert result["status"]=="READ_ONLY_DIAGNOSIS"
        assert result["entry_status"]=="UNCERTAIN" and result["comparison"]=="ATTRIBUTION_UNPROVEN"
        assert result["book_delta"]==0 and result["period_delta"]==1
        assert result["requests_sent"]==1 and result["elapsed_seconds"]==20
        assert result["write_error_code"]=="WRITE_RESPONSE" and not result["response_diagnostics_available"]
        assert result["reading_requests_sent_this_operation"]==0 and not result["client_review_performed"]
    assert calls==["mock-A"] and service._entry(entry_id)==before
    assert service._active() and service.pending_path.exists()
    assert service._attempt(original["attempt_id"])["verification_status"]=="UNVERIFIED"
    with pytest.raises(AppError,match="RECONCILE_REQUIRED"):
        service.start(entry_id,seconds=60)


def test_reconciliation_keeps_original_write_diagnostics(sync):
    service, entry_id, writer, stats = sync
    details={"http_status":200,"category":"EMPTY_OBJECT"}
    writer.run_bounded_session=lambda *a,**k:WriteResult("UNCERTAIN",1,20,"WRITE_RESPONSE",details)
    service.start(entry_id,seconds=60)
    result=service.reconcile(entry_id)
    assert result["error_code"]=="CLIENT_REVIEW_REQUIRED"
    assert result["write_error_code"]=="WRITE_RESPONSE" and result["response_diagnostics"]==details
    assert result["verified_seconds"]==0 and service._active()


def test_restart_preserves_intent_and_does_not_autoresend(sync):
    service, entry_id, writer, stats = sync
    service.start(entry_id, seconds=60)
    with Database(service.db.path, mock=True) as reopened:
        new_stats=MockStats(reopened)
        new_writer=MockWriter(new_stats)
        restored=SyncService(reopened,new_stats,new_writer,lock_path=service.lock_path)
        assert restored._active()
        with pytest.raises(AppError,match="RECONCILE_REQUIRED"):
            restored.start(entry_id,seconds=60)
        assert new_writer.calls == []
        result=restored.reconcile(entry_id,app_book_seconds=60,app_period_seconds=60,no_other_reading=True,same_day_confirmed=True)
        assert result["verified_seconds"] == 60
        assert new_writer.calls == []


def test_crash_before_result_save_requires_manual_client_and_uses_persisted_cap(sync):
    service, entry_id, writer, stats = sync
    service.start(entry_id,seconds=60)
    earlier=(datetime.now(timezone.utc)-timedelta(seconds=61)).isoformat()
    service.db.conn.execute("UPDATE sync_attempts SET result_json=NULL,started_at=?,transport_status='UNCERTAIN'", (earlier,))
    failed=service.reconcile(entry_id,no_other_reading=True,same_day_confirmed=True)
    assert failed["verified_seconds"] == 0
    result=service.reconcile(entry_id,app_book_seconds=60,app_period_seconds=60,no_other_reading=True,same_day_confirmed=True)
    assert result["verified_seconds"] == 60
    assert len(writer.calls) == 1


def test_cancellation_before_first_request_can_release_intent(sync):
    service, entry_id, writer, stats = sync
    writer.run_bounded_session=lambda *a,**k:WriteResult("CANCELLED",0,0,"USER_CANCELLED")
    result=service.start(entry_id,seconds=60)
    assert result["status"] == "CANCELLED"
    assert not service._active() and not service.pending_path.exists()


def test_os_writer_lock_and_changed_baseline(sync):
    service, entry_id, writer, stats = sync
    with WriterLock(service.lock_path):
        with pytest.raises(AppError,match="WRITER_BUSY"):
            service.start(entry_id,seconds=60)
    stats.advance("mock-B",1)
    with pytest.raises(AppError,match="BASELINE_CHANGED"):
        service.start(entry_id,seconds=60)
    assert writer.calls == []


def test_old_display_is_rechecked_against_fresh_server_data_before_writing(sync):
    service, entry_id, writer, stats = sync
    _,pair=service.db.latest_snapshot(entry_id)
    stale=pair.model_copy(update={"captured_at":(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()})
    service.db.save_snapshot(entry_id,stale,"baseline")
    result=service.start(entry_id,seconds=60)
    before=service._attempt(result["attempt_id"])["before_json"]
    assert json.loads(before)["captured_at"]!=stale.captured_at
    assert result["transport_status"]=="RESPONSE_RECEIVED"
    assert writer.calls==[("mock-A",60)]


def test_stale_remote_measurement_still_cannot_create_a_write_intent(sync):
    service,entry_id,writer,stats=sync
    _,pair=service.db.latest_snapshot(entry_id)
    stale=pair.model_copy(update={"captured_at":(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()})
    stats.pair=lambda *args:stale
    with pytest.raises(AppError,match="BASELINE_STALE"):
        service.start(entry_id,seconds=60)
    assert writer.calls==[] and not service._active()


def test_display_review_needs_no_retyped_app_numbers_and_refreshes_unchanged_old_snapshot(sync):
    service,entry_id,writer,stats=sync
    stats.advance("mock-B",11705)
    service.baseline(entry_id)
    _,pair=service.db.latest_snapshot(entry_id)
    old=pair.model_copy(update={"captured_at":(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()})
    shown_id=service.db.save_snapshot(entry_id,old,"baseline")
    with pytest.raises(AppError,match="REVIEW_CONFIRMATION_REQUIRED"):
        service.confirm_baseline(entry_id,snapshot_id=shown_id)
    assert service._entry(entry_id)["status"]=="LOCAL_ONLY"
    result=service.confirm_baseline(entry_id,snapshot_id=shown_id,review_confirmed=True)
    assert result["status"]=="READY" and result["review_method"]=="display_confirmation"
    row=service.db.conn.execute("SELECT * FROM attestations WHERE entry_id=?",(entry_id,)).fetchone()
    document=json.loads(row["document"])
    assert document["displayed_snapshot_id"]==shown_id
    assert document["displayed_period_seconds"]==11705
    assert "app_book_seconds" not in document and "app_period_seconds" not in document
    current_id,current=service.db.latest_snapshot(entry_id)
    assert current_id==row["snapshot_id"] and current_id!=shown_id
    service._fresh(current)
    assert writer.calls==[] and not service._active()


def test_display_review_does_not_approve_changed_counters_even_with_same_displayed_minute(sync):
    service,entry_id,writer,stats=sync
    stats.advance("mock-B",11705)
    service.baseline(entry_id)
    shown_id,_=service.db.latest_snapshot(entry_id)
    stats.advance("mock-B",1)
    with pytest.raises(AppError,match="REVIEW_CHANGED"):
        service.confirm_baseline(entry_id,snapshot_id=shown_id,review_confirmed=True)
    assert service.db.latest_snapshot(entry_id)[1].period_seconds==11706
    assert not service.db.conn.execute("SELECT 1 FROM attestations WHERE entry_id=?",(entry_id,)).fetchone()
    with pytest.raises(AppError,match="REVIEW_CHANGED"):
        service.confirm_baseline(entry_id,snapshot_id=shown_id,review_confirmed=True)
    assert writer.calls==[] and service._entry(entry_id)["verified_seconds"]==0


@pytest.mark.parametrize("same,paused,code",[(False,True,"ACCOUNT_CONFIRMATION_REQUIRED"),(True,False,"ACTIVITY_CONFIRMATION_REQUIRED")])
def test_numeric_review_reports_the_actual_missing_confirmation(sync,same,paused,code):
    service,entry_id,writer,stats=sync
    with pytest.raises(AppError,match=code):
        service.attest(entry_id,app_book_seconds=0,app_period_seconds=0,same_account=same,no_other_reading=paused)
    assert service._entry(entry_id)["status"]=="LOCAL_ONLY" and writer.calls==[]


def test_legacy_minute_in_seconds_field_has_specific_feedback_and_redacted_audit(sync):
    service,entry_id,writer,stats=sync
    stats.advance("mock-B",11705)
    service.baseline(entry_id)
    with pytest.raises(AppError,match="CLIENT_READINGS_MISMATCH") as exc:
        service.attest(entry_id,app_book_seconds=0,app_period_seconds=195,same_account=True,no_other_reading=True)
    assert "195 秒" in exc.value.message and "11705 秒" in exc.value.message
    assert "账号和活动确认已通过" in exc.value.message
    event=service.db.conn.execute("SELECT details FROM audit_events WHERE event='CLIENT_REVIEW_REJECTED' ORDER BY event_id DESC LIMIT 1").fetchone()
    assert json.loads(event[0])["app_period_seconds"]==195
    assert service._entry(entry_id)["status"]=="LOCAL_ONLY" and writer.calls==[]
    # Explicit numeric minute mode remains exact at its stated display precision.
    service.attest(entry_id,app_book_seconds=0,app_period_seconds=11700,same_account=True,no_other_reading=True,app_resolution_seconds=60)
    assert service._entry(entry_id)["status"]=="READY"


def test_requery_keeps_original_consent_when_values_are_unchanged(sync):
    service,entry_id,writer,stats=sync
    original=dict(service.db.conn.execute("SELECT * FROM attestations WHERE entry_id=?",(entry_id,)).fetchone())
    updated=service._entry(entry_id)["updated_at"]
    for _ in range(2):
        result=service.baseline(entry_id)
        assert result["review_preserved"] and not result["needs_client_attestation"]
        review=dict(service.db.conn.execute("SELECT * FROM attestations WHERE entry_id=?",(entry_id,)).fetchone())
        assert review["snapshot_id"]==result["snapshot_id"]
        assert review["document"]==original["document"]
        assert review["confirmed_at"]==original["confirmed_at"]
        assert service._entry(entry_id)["updated_at"]==updated
    assert service._entry(entry_id)["status"]=="READY" and writer.calls==[]
    stats.advance("mock-B",1)
    changed=service.baseline(entry_id)
    assert not changed["review_preserved"] and changed["needs_client_attestation"]
    assert not service.db.conn.execute("SELECT 1 FROM attestations WHERE entry_id=?",(entry_id,)).fetchone()
    with pytest.raises(AppError,match="ATTESTATION_REQUIRED"):
        service.start(entry_id,seconds=60)
    assert writer.calls==[]


def pending_route_fixture(service, entry_id, writer):
    service.db.save_route("default","mock-A","route-A","https://weread.qq.com/web/reader/route-A",writer.identity_hash)
    service.db.bind(confirm_book(Book(book_id="mock-B",title="合成书 B",author="作者",publisher="示例",origin="mock"),confirmed=True))
    other=service.db.add(EntryInput(book_id="mock-B",reading_date="2026-10-22",duration_seconds=60))
    attempt=service.start(entry_id,seconds=60)
    return other, attempt


def test_prepare_other_book_keeps_pending_attempt_original_route_and_write_block(sync):
    from paperread.browser_session import recovery_identity
    service,entry_id,writer,stats=sync
    other,attempt=pending_route_fixture(service,entry_id,writer)
    before=service._attempt(attempt["attempt_id"])
    old_route=dict(service.db.route("default","mock-A"))
    marker=service.pending_path.read_bytes()
    assert recovery_identity(service.db,"default",service.db.book("mock-B"),service.pending_path)==writer.identity_hash
    service.db.save_route("default","mock-B","route-B","https://weread.qq.com/web/reader/route-B",writer.identity_hash)
    assert service.db.route("default","mock-B")["web_book_id"]=="route-B"
    assert dict(service.db.route("default","mock-A"))==old_route
    assert service._attempt(attempt["attempt_id"])==before
    assert service.pending_path.read_bytes()==marker and service._active()[0]==attempt["attempt_id"]
    with pytest.raises(AppError,match="RECONCILE_REQUIRED"):
        service.start(other["entry_id"],seconds=60,purpose="remaining")
    assert writer.calls==[("mock-A",60)]


@pytest.mark.parametrize("account,identity",[("other","mock-writer-identity"),("default","synthetic-other-account")])
def test_pending_other_book_preparation_cannot_switch_account(sync,account,identity):
    service,entry_id,writer,stats=sync
    pending_route_fixture(service,entry_id,writer)
    with pytest.raises(AppError,match="RECOVERY_ACCOUNT_MISMATCH"):
        service.db.save_route(account,"mock-B","route-B","https://weread.qq.com/web/reader/route-B",identity)
    assert service.db.route(account,"mock-B") is None
    assert service.pending_path.exists() and service._active()


def test_pending_original_book_route_is_preserved_and_cannot_be_replaced(sync):
    service,entry_id,writer,stats=sync
    pending_route_fixture(service,entry_id,writer)
    old_route=dict(service.db.route("default","mock-A"))
    service.db.save_route("default","mock-A","route-A",old_route["official_reader_link"],writer.identity_hash)
    assert dict(service.db.route("default","mock-A"))==old_route
    with pytest.raises(AppError,match="RECOVERY_CONTEXT_MISMATCH"):
        service.db.save_route("default","mock-A","route-C","https://weread.qq.com/web/reader/route-C",writer.identity_hash)
    assert dict(service.db.route("default","mock-A"))==old_route
    assert service.pending_path.exists() and service._active()


def test_cross_book_reservation_keeps_previous_verified_credit_in_original_entry(sync):
    from paperread.sync_service import direct_sync_preview
    service,entry_id,writer,stats=sync
    service.start(entry_id,seconds=60)
    verify(service,entry_id,stats)
    writer.run_bounded_session=lambda *args,**kwargs:WriteResult("UNCERTAIN",1,20,"WRITE_RESPONSE")
    pending=service.start(entry_id,seconds=60,purpose="remaining")
    service.db.bind(confirm_book(Book(book_id="mock-B",title="合成书 B",author="作者",publisher="示例",origin="mock"),confirmed=True))
    other=service.db.add(EntryInput(book_id="mock-B",reading_date="2026-10-22",duration_seconds=120))
    preview=direct_sync_preview(service.db,other)
    assert preview["seconds"]==120 and preview["reservation"]["entry_id"]==entry_id
    service.reserve_for_direct(entry_id,attempt_id=pending["attempt_id"],seconds=60,confirmed=True)
    original=service._entry(entry_id)
    assert original["verified_seconds"]==60 and original["reserved_seconds"]==60
    assert original["available_seconds"]==3480 and original["status"]=="PARTIALLY_VERIFIED"
    assert service._entry(other["entry_id"])["available_seconds"]==120
    assert not service._active() and not service.pending_path.exists()


def test_cross_account_entry_cannot_reserve_another_accounts_pending_write(sync):
    from paperread.sync_service import direct_sync_preview
    service,entry_id,writer,stats=sync
    pending_route_fixture(service,entry_id,writer)
    other=service.db.add(EntryInput(book_id="mock-B",account_id="other",reading_date="2026-10-22",duration_seconds=120))
    preview=direct_sync_preview(service.db,other)
    assert preview["blocked"] and preview["reservation"] is None
    assert service._active() and service.pending_path.exists()


def test_explicit_same_book_phone_activity_reconciliation_excludes_surplus_from_paper_credit(sync):
    service,entry_id,writer,stats=sync
    service.start(entry_id,seconds=60,purpose='remaining')
    stats.advance('mock-A',5)
    strict=service.verify(entry_id,no_other_reading=True,same_day_confirmed=True)
    assert strict['error_code']=='DELTA_EXCEEDS_BUDGET' and strict['verified_seconds']==0
    result=service.reconcile(entry_id,no_other_reading=False,same_day_confirmed=True,concurrent_reading_confirmed=True)
    assert result['verification_status']=='VERIFIED' and result['verified_seconds']==60
    assert result['book_delta']==65 and result['period_delta']==65 and result['excluded_surplus_seconds']==5
    assert result['confirmed_concurrent_reading'] and not result['automatic_verification']
    assert writer.calls==[('mock-A',60)] and not service._active()
    service.reconcile(entry_id,concurrent_reading_confirmed=True,same_day_confirmed=True)
    assert service._entry(entry_id)['verified_seconds']==60
    evidence=json.loads(service.db.conn.execute('SELECT result_json FROM sync_attempts').fetchone()[0])
    assert evidence['raw_comparison']=='DELTA_EXCEEDS_BUDGET' and not evidence['no_other_reading']


@pytest.mark.parametrize('case',['other_book','uncertain_transport','missing_day'])
def test_concurrent_activity_confirmation_cannot_bypass_unrelated_or_unproven_writes(sync,case):
    service,entry_id,writer,stats=sync
    if case=='uncertain_transport':
        def uncertain(book_id,seconds,**kwargs):
            stats.advance(book_id,seconds)
            return WriteResult('UNCERTAIN',1,60,'WRITE_NETWORK')
        writer.run_bounded_session=uncertain
    service.start(entry_id,seconds=60,purpose='remaining')
    stats.advance('mock-B' if case=='other_book' else 'mock-A',5)
    result=service.reconcile(entry_id,no_other_reading=False,same_day_confirmed=case!='missing_day',
        concurrent_reading_confirmed=True)
    assert result['verified_seconds']==0 and result['verification_status']!='VERIFIED'
    assert service._active() and service.pending_path.exists()
