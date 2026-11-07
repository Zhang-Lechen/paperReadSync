"""Explicit retries release quarantined budgets once and preserve their evidence."""

import json
import re

import pytest

from paperread.cli import sync_service
from paperread.errors import AppError
from paperread.sync_service import direct_sync_preview, reserved_retry_preview
from paperread.book_match import confirm_book
from paperread.models import Book, EntryInput
from test_direct_sync import direct, old_trial, approval as direct_approval
from test_real_web_flow import send, wait_job


def reserve_old_trial(settings, entry_id, stats):
    old_trial(settings,entry_id,stats)
    with settings.database() as db, sync_service(settings,db,writer=True,allow_live=True,entry_id=entry_id) as service:
        reservation = direct_sync_preview(db,db.entry(entry_id))["reservation"]
        service.reserve_for_direct(entry_id,attempt_id=reservation["attempt_id"],seconds=reservation["seconds"],confirmed=True)
        # Represent the other batches which were already verified before this retry.
        db.conn.execute("UPDATE reading_entries SET verified_seconds=3536 WHERE entry_id=?",(entry_id,))
        return dict(db.conn.execute("SELECT * FROM sync_attempts WHERE entry_id=?",(entry_id,)).fetchone())


def approval(app, entry_id):
    page = send(app,"GET","/").text
    section = next(form for form in re.findall(r'<form method="post" action="/action/retry">(.*?)</form>',page,re.S)
                   if f'name="entry_id" value="{entry_id}"' in form)
    return {"csrf":re.search(r'name="csrf" value="([^"]+)"',section)[1],
            "grant":re.search(r'name="grant" value="([^"]+)"',section)[1],
            "entry_id":entry_id,"retry_confirmed":"yes"}


def test_retry_completes_reserved_seconds_once_and_keeps_original_evidence(direct):
    app,settings,entry_id,stats = direct
    old = reserve_old_trial(settings,entry_id,stats)
    data = approval(app,entry_id)
    stale_preview = approval(app,entry_id)
    response = send(app,"POST","/action/retry",data=data)
    job = wait_job(app,response)
    result = job["result"]
    assert result["batch_status"] == "COMPLETED" and result["verified_seconds"] == 3600
    assert result["reserved_seconds"] == result["available_seconds"] == 0
    assert result["approved_job_seconds"] == 64 and result["tolerance_seconds"] == 5
    assert stats.calls == [60,60,4]
    with settings.database() as db:
        saved = dict(db.conn.execute("SELECT * FROM sync_attempts WHERE attempt_id=?",(old["attempt_id"],)).fetchone())
        for key in ("requested_seconds","credited_seconds","before_json","after_json","verification_status"):
            assert saved[key] == old[key]
        assert json.loads(saved["result_json"])["reservation_released_for_retry"] is True
        assert db.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event='RESERVATION_RELEASED_FOR_RETRY'").fetchone()[0] == 1
    repeated = send(app,"POST","/action/retry",data=data)
    assert str(repeated.url) == str(response.url)
    assert send(app,"POST","/action/retry",data=stale_preview).status_code == 400
    assert stats.calls == [60,60,4]


def test_retry_requires_its_own_confirmation(direct):
    app,settings,entry_id,stats = direct
    reserve_old_trial(settings,entry_id,stats)
    data = approval(app,entry_id)
    data.pop("retry_confirmed")
    data["sync_confirmed"] = "yes"
    assert send(app,"POST","/action/retry",data=data).status_code == 400
    with settings.database() as db:
        assert db.entry(entry_id)["reserved_seconds"] == 64
    assert stats.calls == [60] and app.state.operations.jobs.snapshots() == []


def test_failed_auth_or_statistics_preserves_reservation(direct,monkeypatch):
    app,settings,entry_id,stats = direct
    reserve_old_trial(settings,entry_id,stats)
    data = approval(app,entry_id)
    def unavailable(*args,**kwargs):
        raise AppError("SESSION_EXPIRED","synthetic expired login")
    monkeypatch.setattr(stats,"pair",unavailable)
    response = send(app,"POST","/action/retry",data=data)
    identifier = str(response.url).rsplit("/",1)[-1]
    import time
    deadline = time.monotonic()+5
    while time.monotonic()<deadline:
        job = next(item for item in app.state.operations.jobs.snapshots() if item["id"]==identifier)
        if job["state"] not in {"QUEUED","RUNNING"}:
            break
        time.sleep(.01)
    assert job["state"] == "FAILED" and job["error_code"] == "SESSION_EXPIRED"
    with settings.database() as db:
        assert db.entry(entry_id)["reserved_seconds"] == 64
    assert stats.calls == [60]


def test_release_and_review_are_atomic_and_recoverable(direct):
    app,settings,entry_id,stats = direct
    old = reserve_old_trial(settings,entry_id,stats)
    import sqlite3
    with settings.database() as db, sync_service(settings,db,writer=True,allow_live=True,entry_id=entry_id) as service:
        preview = reserved_retry_preview(db,db.entry(entry_id))
        db.conn.execute("""CREATE TRIGGER fail_release BEFORE INSERT ON audit_events
            WHEN NEW.event='RESERVATION_RELEASED_FOR_RETRY' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
        with pytest.raises(sqlite3.IntegrityError):
            service.authorize_reserved_retry(entry_id,preview=preview,confirmed=True)
        assert db.entry(entry_id)["reserved_seconds"] == 64
        assert db.conn.execute("SELECT result_json FROM sync_attempts WHERE attempt_id=?",(old["attempt_id"],)).fetchone()[0] == old["result_json"]
        db.conn.execute("DROP TRIGGER fail_release")
        service.authorize_reserved_retry(entry_id,preview=preview,confirmed=True)
    # A restart between approval and writing leaves a resumable available balance.
    with settings.database() as db:
        assert db.entry(entry_id)["reserved_seconds"] == 0
        assert db.entry(entry_id)["available_seconds"] == 64
        assert reserved_retry_preview(db,db.entry(entry_id))["seconds"] == 0
    assert stats.calls == [60]


def test_normal_sync_accepts_four_seconds_of_statistics_offset(direct):
    app,settings,entry_id,stats = direct
    with settings.database() as db:
        db.bind(confirm_book(Book(book_id="20002",title="合成误差测试书",author="作者",publisher="出版社",origin="official"),confirmed=True))
        entry=db.add(EntryInput(book_id="20002",reading_date="2026-11-04",duration_seconds=120))
    stats.next_delta=64
    job=wait_job(app,send(app,"POST","/action/sync",data=direct_approval(app,entry["entry_id"])))
    assert job["result"]["batch_status"]=="COMPLETED"
    assert job["result"]["verified_seconds"]==120
    assert stats.calls==[60,60]
    with settings.database() as db:
        first=json.loads(db.conn.execute("SELECT result_json FROM sync_attempts WHERE entry_id=? ORDER BY attempt_no",(entry["entry_id"],)).fetchone()[0])
        assert first["comparison"]=="DUAL_DELTA_WITH_TOLERANCE" and first["book_delta"]==64


def test_confirmed_unsent_failure_releases_balance_without_reservation(direct):
    app,settings,entry_id,stats = direct
    from paperread.wereadgear_adapter import WriteResult
    with settings.database() as db, sync_service(settings,db,writer=True,allow_live=True,entry_id=entry_id) as service:
        service.authorize_direct(entry_id,confirmed=True)
        service.writer.run_bounded_session=lambda *args,**kwargs:WriteResult("UNCERTAIN",0,0,"CLOCK_OR_BUDGET")
        result=service.start(entry_id,seconds=60,purpose="remaining",live=True,confirm=True)
        assert result["status"]=="FAILED_CONFIRMED"
        assert not service._active() and not service.pending_path.exists()
        assert db.entry(entry_id)["reserved_seconds"]==0
        assert db.entry(entry_id)["available_seconds"]==3600
