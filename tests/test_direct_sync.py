"""User-approved direct jobs with memory-only official adapters; never use real accounts."""

import json
import re

import pytest

from paperread.book_match import confirm_book
from paperread.cli import Settings, sync_service
from paperread.credentials import CredentialStore, WriterCredentials
from paperread.errors import AppError
from paperread.models import Book, EntryInput, StatsPair
from paperread.web import create_app
from paperread.wereadgear_adapter import WriteResult
from test_real_web_flow import send, wait_job


@pytest.fixture
def direct(root,tmp_path,monkeypatch):
    import paperread.wereadgear_adapter as adapters
    class Stats:
        source="official"
        identity_hash="synthetic-reader"
        book=13515
        total=11843
        other_book=700
        next_delta=None
        calls=[]
        book_calls=[]
        def close(self): pass
        def pair(self,book_id,timezone):
            return StatsPair(book_id=book_id,account_id="default",book_seconds=self.book if book_id=="10001" else self.other_book,period_seconds=self.total,
                period_key="monthly:synthetic",source=self.source,identity_hash=self.identity_hash,
                evidence={"book_time_field":"book.readingTime","synthetic_fixture":True})
    stats=Stats()
    profile=WriterCredentials(wr_skey="synthetic-session",wr_vid="synthetic-account",captured_headers={},captured_payload={})
    class Writer:
        source="official"
        def __init__(self,profile): self.identity_hash=profile.identity_hash
        def validate_book(self,book_id): assert book_id in {"10001","20002"}
        def run_bounded_session(self,book_id,seconds,**kwargs):
            stats.calls.append(seconds)
            stats.book_calls.append((book_id,seconds))
            delta=seconds if stats.next_delta is None else stats.next_delta
            stats.next_delta=None
            if book_id=="10001": stats.book+=delta
            else: stats.other_book+=delta
            stats.total+=delta
            return WriteResult("RESPONSE_RECEIVED",3,seconds)
    def flags(*,live,confirm):
        if not live or not confirm:
            raise AppError("WRITE_DENIED","Explicit user approval required")
    monkeypatch.setattr(adapters,"require_live_flags",flags)
    monkeypatch.setattr(adapters,"BoundedWriter",Writer)
    monkeypatch.setattr(CredentialStore,"load",lambda self:profile)
    monkeypatch.setattr(Settings,"stats",lambda *args,**kwargs:stats)
    settings=Settings(root,tmp_path/"direct.sqlite","default",False,False)
    with settings.database() as db:
        db.bind(confirm_book(Book(book_id="10001",title="合成直接同步书",author="作者",publisher="出版社",origin="official"),confirmed=True))
        entry=db.add(EntryInput(book_id="10001",reading_date="2026-11-04",duration_seconds=3600))
    app=create_app(settings)
    try:
        yield app,settings,entry["entry_id"],stats
    finally:
        app.state.operations.jobs.shutdown()


def approval(app,entry_id):
    html=send(app,"GET","/").text
    section=next(part for part in re.findall(r'<form method="post" action="/action/sync">(.*?)</form>',html,re.S)
                 if f'name="entry_id" value="{entry_id}"' in part)
    return {"csrf":re.search(r'name="csrf" value="([^"]+)"',section)[1],
            "grant":re.search(r'name="grant" value="([^"]+)"',section)[1],
            "entry_id":entry_id,"sync_confirmed":"yes"}


def test_one_approval_syncs_one_hour_without_probe_baseline_or_phone_input(direct):
    app,settings,entry_id,stats=direct
    data=approval(app,entry_id)
    response=send(app,"POST","/action/sync",data=data)
    result=wait_job(app,response)
    assert result["result"]["verified_seconds"]==3600
    assert stats.calls==[60]*60
    with settings.database() as db:
        assert db.conn.execute("SELECT COUNT(*) FROM sync_attempts WHERE purpose='probe'").fetchone()[0]==0
        review=json.loads(db.conn.execute("SELECT document FROM attestations").fetchone()[0])
        assert review["review_method"]=="direct_sync_authorization" and not review["app_reviewed_by_user"]
        assert "app_book_seconds" not in review
        assert not db.conn.execute("SELECT 1 FROM write_guard").fetchone()
    repeated=send(app,"POST","/action/sync",data=data)
    assert str(repeated.url)==str(response.url) and stats.calls==[60]*60


def old_trial(settings,entry_id,stats):
    stats.next_delta=64
    with settings.database() as db, sync_service(settings,db,writer=True,allow_live=True,entry_id=entry_id) as service:
        service.baseline(entry_id)
        service.attest(entry_id,app_book_seconds=stats.book,app_period_seconds=stats.total,
                       same_account=True,no_other_reading=True)
        service.start(entry_id,seconds=60,live=True,confirm=True)
        result=service.verify(entry_id,app_book_seconds=226*60,app_period_seconds=198*60,
            app_resolution_seconds=60,no_other_reading=True,same_day_confirmed=True)
        assert result["client_confirmed"] and result["error_code"]=="DELTA_EXCEEDS_BUDGET"


def test_correct_226_198_review_can_quarantine_64_then_direct_sync_without_repeating_old_trial(direct):
    app,settings,entry_id,stats=direct
    old_trial(settings,entry_id,stats)
    html=send(app,"GET","/").text
    assert "手机数字填错" in html and "1 分 4 秒" in html
    data=approval(app,entry_id)
    result=wait_job(app,send(app,"POST","/action/sync",data=data))
    assert result["result"]["verified_seconds"]==3536
    assert result["result"]["reserved_seconds"]==64
    assert result["result"]["available_seconds"]==0
    assert stats.calls==[60]+[60]*58+[56]  # The original approved minute is not replayed.
    with settings.database() as db:
        old=db.conn.execute("SELECT * FROM sync_attempts ORDER BY attempt_no LIMIT 1").fetchone()
        assert old["requested_seconds"]==60 and old["credited_seconds"]==0
        assert old["verification_status"]=="WITHDRAWN_UNVERIFIED"
        assert json.loads(old["result_json"])["reserved_seconds"]==64
        assert db.entry(entry_id)["status"]!="VERIFIED"


def test_missing_approval_creates_no_job_or_write(direct):
    app,settings,entry_id,stats=direct
    data=approval(app,entry_id)
    del data["sync_confirmed"]
    response=send(app,"POST","/action/sync",data=data)
    assert response.status_code==400 and stats.calls==[]
    assert app.state.operations.jobs.snapshots()==[]


def test_direct_sync_stops_after_unattributed_excess_without_replaying_batch(direct):
    app,settings,entry_id,stats=direct
    stats.next_delta=76
    response=send(app,"POST","/action/sync",data=approval(app,entry_id))
    result=wait_job(app,response)["result"]
    assert result["error_code"]=="DELTA_EXCEEDS_BUDGET" and stats.calls==[60]
    assert result["automatic_verification"] and not result["client_confirmed"]
    assert result["batch_status"]=="STOPPED" and result["approved_job_seconds"]==3600
    page=send(app,"GET",str(response.url)).text
    assert "同步已中途停止" in page and "本次尚未核验" in page
    with settings.database() as db:
        assert db.entry(entry_id)["verified_seconds"]==0
        assert db.conn.execute("SELECT 1 FROM write_guard").fetchone()
    assert "先保留旧写入 1 分 16 秒" in send(app,"GET","/").text


def test_changed_old_reading_reservation_requires_updated_preview_without_new_write(direct):
    app,settings,entry_id,stats=direct
    old_trial(settings,entry_id,stats)
    data=approval(app,entry_id)
    stats.book+=1;stats.total+=1
    response=send(app,"POST","/action/sync",data=data)
    import time
    deadline=time.monotonic()+5
    identifier=str(response.url).rsplit("/",1)[-1]
    while time.monotonic()<deadline:
        job=next(j for j in app.state.operations.jobs.snapshots() if j["id"]==identifier)
        if job["state"] not in {"RUNNING","QUEUED"}:
            break
        time.sleep(.01)
    assert job["error_code"]=="APPROVAL_EXPIRED" and stats.calls==[60]
    with settings.database() as db:
        assert db.entry(entry_id)["reserved_seconds"]==0
        assert db.conn.execute("SELECT 1 FROM write_guard").fetchone()
    assert "1 分 5 秒" in send(app,"GET","/").text


def test_new_book_can_reserve_old_book_in_place_without_deducting_its_own_budget(direct):
    app,settings,old_id,stats=direct
    old_trial(settings,old_id,stats)
    with settings.database() as db:
        db.bind(confirm_book(Book(book_id="20002",title="合成新书",author="作者",publisher="出版社",origin="official"),confirmed=True))
        new=db.add(EntryInput(book_id="20002",reading_date="2026-11-04",duration_seconds=120))
        original_before=db.conn.execute("SELECT before_json FROM sync_attempts WHERE entry_id=?",(old_id,)).fetchone()[0]
    data=approval(app,new["entry_id"])
    html=send(app,"GET","/").text
    assert "不会从当前书的同步额度中扣除" in html
    assert "保留旧书待查额度，并开始当前书 2 分 0 秒" in html
    missing_approval={key:value for key,value in data.items() if key!="sync_confirmed"}
    assert send(app,"POST","/action/sync",data=missing_approval).status_code==400
    with settings.database() as db:
        assert db.entry(old_id)["reserved_seconds"]==0
        assert db.conn.execute("SELECT 1 FROM write_guard").fetchone()
    assert stats.book_calls==[("10001",60)]
    result=wait_job(app,send(app,"POST","/action/sync",data=data))["result"]
    assert result["entry_id"]==new["entry_id"] and result["verified_seconds"]==120
    assert result["reserved_seconds"]==0 and result["status"]=="VERIFIED"
    assert stats.book_calls==[("10001",60),("20002",60),("20002",60)]
    with settings.database() as db:
        old=db.entry(old_id)
        assert old["verified_seconds"]==0 and old["reserved_seconds"]==64 and old["available_seconds"]==3536
        attempt=db.conn.execute("SELECT * FROM sync_attempts WHERE entry_id=?",(old_id,)).fetchone()
        assert attempt["before_json"]==original_before
        assert attempt["verification_status"]=="WITHDRAWN_UNVERIFIED" and attempt["credited_seconds"]==0
        assert not db.conn.execute("SELECT 1 FROM write_guard").fetchone()
    repeated=send(app,"POST","/action/sync",data=data)
    assert len(stats.calls)==3 and repeated.status_code==200
