import asyncio
import re
import time

import httpx
import pytest

from paperread.book_match import confirm_book
from paperread.cli import Settings
from paperread.credentials import WriterCredentials
from paperread.errors import AppError
from paperread.models import Book,EntryInput,StatsPair
from paperread.web import create_app
from paperread.wereadgear_adapter import WriteResult


def send(app,method,path,**kwargs):
    async def execute():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1:8765",follow_redirects=True) as client:
            return await client.request(method,path,**kwargs)
    return asyncio.run(execute())


def wait_job(app,response):
    identifier=str(response.url).rsplit("/",1)[-1]
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        job=next(item for item in app.state.operations.jobs.snapshots() if item["id"]==identifier)
        if job["state"] not in {"QUEUED","RUNNING"}:
            assert job["state"]=="DONE",job
            return job
        time.sleep(.01)
    raise AssertionError("synthetic job did not finish")


@pytest.mark.parametrize("review_mode",["separate","inline"])
@pytest.mark.parametrize("duration_seconds,trial_delta",[(120,60),(3600,60),(3600,64)])
def test_real_web_routes_use_same_intent_verification_and_remaining_gates(root,tmp_path,monkeypatch,review_mode,duration_seconds,trial_delta):
    # All 'official' objects here are explicit in-memory fixtures. No real Key,
    # browser, HTTP transport or user profile is loaded in this integration test.
    import paperread.wereadgear_adapter as adapters
    from paperread.credentials import CredentialStore
    calls=[]
    class Stats:
        source="official"
        identity_hash="synthetic-read-identity"
        book=0
        total=100
        def close(self): pass
        def pair(self,book_id,timezone):
            return StatsPair(book_id=book_id,account_id="default",book_seconds=self.book,period_seconds=self.total,
                period_key="monthly:synthetic",source="official",identity_hash=self.identity_hash,
                evidence={"synthetic_test_fixture":True})
    stats=Stats()
    profile=WriterCredentials(wr_skey="synthetic-session",wr_vid="synthetic-account",captured_headers={},captured_payload={})
    class Writer:
        source="official"
        def __init__(self,profile): self.identity_hash=profile.identity_hash
        def validate_book(self,book_id): assert book_id=="10001"
        def run_bounded_session(self,book_id,seconds,**kwargs):
            calls.append((book_id,seconds))
            delta=trial_delta if len(calls)==1 else seconds
            stats.book+=delta;stats.total+=delta
            return WriteResult("RESPONSE_RECEIVED",1,seconds)
    def flags(*,live,confirm):
        if not live or not confirm: raise AppError("WRITE_DENIED","explicit flags required")
    monkeypatch.setattr(adapters,"require_live_flags",flags)
    monkeypatch.setattr(adapters,"BoundedWriter",Writer)
    monkeypatch.setattr(CredentialStore,"load",lambda self:profile)
    monkeypatch.setattr(Settings,"stats",lambda self,db,allow_live=False:stats)
    settings=Settings(root,tmp_path/"real-fixture.sqlite","default",False,False)
    with settings.database() as db:
        db.bind(confirm_book(Book(book_id="10001",title="合成集成测试书",author="测试作者",publisher="测试出版社",origin="official"),confirmed=True))
        entry=db.add(EntryInput(book_id="10001",reading_date="2026-11-04",duration_seconds=duration_seconds))
    app=create_app(settings)
    try:
        def page(): return send(app,"GET","/").text
        def token(html): return re.search(r'name="csrf" value="([^"]+)"',html)[1]
        def post(action,**values):
            return send(app,"POST","/action/"+action,data={"csrf":token(page()),"entry_id":entry["entry_id"],**values})
        wait_job(app,post("baseline"))
        section=re.search(r'<form method="post" action="/action/attest">(.*?)</form>',page(),re.S)[1]
        assert "app_book_seconds" not in section and "app_period_seconds" not in section
        shown_id=re.search(r'name="review_snapshot" value="([^"]+)"',section)[1]
        if review_mode=="separate":
            rejected=post("attest",review_snapshot=shown_id)
            assert "请先对照手机核对" in rejected.text and calls==[]
            result=post("attest",review_snapshot=shown_id,review_confirmed="yes")
            assert result.status_code==200,result.text
            assert str(result.url).endswith("#review-"+entry["entry_id"])
            review_panel=re.search(r'<section class="card" id="review-[^"]+">(.*?)</section>',result.text,re.S)[1]
            assert 'role="status"' in review_panel
            assert "核对结果已保存，本次未提交阅读时长" in review_panel
            assert 'href="#sync-'+entry["entry_id"]+'"' in review_panel
            assert calls==[]
        html=page()
        section=re.search(r'<form method="post" action="/action/probe">(.*?)</form>',html,re.S)[1]
        assert "disabled" not in section  # Required fields validate the user's approval; no hidden dead switch.
        grant=re.search(r'name="grant" value="([^"]+)"',section)[1]
        approvals={key:"yes" for key in ("live","confirm","actual_reading","same_account","no_other_reading","same_day_confirmed")}
        body={"grant":grant,"phrase":"10001:60",**approvals}
        if review_mode=="inline":
            body.update(review_in_write="yes",review_snapshot=shown_id)
        # Re-query in another page AFTER the preview is displayed. Unchanged
        # values must not revoke consent or invalidate this approved budget.
        wait_job(app,post("baseline"))
        failed=post("probe",grant=grant,phrase="10001:60",live="yes")
        assert failed.status_code==400 and calls==[]
        if review_mode=="inline":
            stats.total=101
            changed=post("probe",**body)
            checked=wait_job(app,changed)
            assert checked["result"]["status"]=="REVIEW_CHANGED" and calls==[]
            assert "本次没有提交阅读请求" in send(app,"GET",str(changed.url)).text
            with settings.database() as db:
                assert db.conn.execute("SELECT COUNT(*) FROM sync_attempts").fetchone()[0]==0
            section=re.search(r'<form method="post" action="/action/probe">(.*?)</form>',page(),re.S)[1]
            body["grant"]=re.search(r'name="grant" value="([^"]+)"',section)[1]
            body["review_snapshot"]=re.search(r'name="review_snapshot" value="([^"]+)"',section)[1]
        response=post("probe",**body)
        wait_job(app,response)
        assert 'href="/#verify-'+entry["entry_id"]+'"' in send(app,"GET",str(response.url)).text
        with settings.database() as db:
            document=__import__('json').loads(db.conn.execute("SELECT document FROM attestations WHERE entry_id=?",(entry["entry_id"],)).fetchone()[0])
            assert document["review_method"]=="display_confirmation" and "app_book_seconds" not in document
            assert db.entry(entry["entry_id"])["status"]=="AWAITING_VERIFICATION"
            assert db.entry(entry["entry_id"])["verified_seconds"]==0
        pending_page=page()
        assert "存在未决写入" in pending_page and "/action/diagnose" in pending_page
        assert 'id="remaining-'+entry["entry_id"]+'"' in pending_page
        assert '<button disabled>批量同步剩余时长（等待上次写入核验）</button>' in pending_page
        assert 'action="/action/remaining"' not in pending_page
        assert "不用每分钟点击" in pending_page and "不支持瞬间补写一小时" in pending_page
        diagnostic=post("diagnose")
        job=wait_job(app,diagnostic)
        assert job["result"]["status"]=="READ_ONLY_DIAGNOSIS"
        assert not job["result"]["client_review_performed"]
        assert "尚未确认阅读时长入账" in send(app,"GET",str(diagnostic.url)).text
        with settings.database() as db:
            assert db.entry(entry["entry_id"])["verified_seconds"]==0
            assert db.conn.execute("SELECT COUNT(*) FROM write_guard").fetchone()[0]==1
        assert calls==[("10001",60)]
        repeated=post("probe",**body)
        assert str(repeated.url)==str(response.url) and calls==[("10001",60)]
        verification=post("verify",app_book_seconds=str(trial_delta),app_period_seconds=str((101 if review_mode=="inline" else 100)+trial_delta),no_other_reading="yes",same_day_confirmed="yes")
        verified_job=wait_job(app,verification)
        html=page()
        if trial_delta>60:
            assert verified_job["result"]["error_code"]=="DELTA_EXCEEDS_BUDGET"
            assert "本书增加 64 秒，本月增加 64 秒" in html
            assert "当前暂不可提交" in html and 'action="/action/remaining"' not in html
            assert "单书增加 64 秒、月度增加 64 秒" in send(app,"GET",str(verification.url)).text
            with settings.database() as db:
                assert db.entry(entry["entry_id"])["verified_seconds"]==0
                assert db.conn.execute("SELECT COUNT(*) FROM write_guard").fetchone()[0]==1
            assert calls==[("10001",60)]
            return
        assert 'href="/#sync-'+entry["entry_id"]+'"' in send(app,"GET",str(verification.url)).text
        assert 'action="/action/probe"' not in html  # A verified probe must not invite a duplicate trial.
        section=re.search(r'<form method="post" action="/action/remaining">(.*?)</form>',html,re.S)[1]
        remaining_grant=re.search(r'name="grant" value="([^"]+)"',section)[1]
        wait_job(app,post("remaining",grant=remaining_grant,phrase=f"10001:{duration_seconds-60}",**approvals))
        with settings.database() as db:
            final=db.entry(entry["entry_id"])
            assert final["verified_seconds"]==duration_seconds and final["status"]=="VERIFIED"
            assert db.conn.execute("SELECT COUNT(*) FROM sync_attempts").fetchone()[0]==duration_seconds//60
        assert 'action="/action/attest"' not in page()
        writes_before=list(calls)
        stale_review=post("attest",review_snapshot=shown_id,review_confirmed="yes")
        assert stale_review.status_code==400 and "NO_SUBMITTABLE_SECONDS" in stale_review.text
        assert calls==writes_before
        assert calls==[("10001",60)]*(duration_seconds//60)
    finally: app.state.operations.jobs.shutdown()


def test_reopened_real_ui_baseline_recovers_pending_trial_instead_of_failing(root,tmp_path,monkeypatch):
    from paperread.credentials import app_directory
    from paperread.sync_service import SyncService
    import paperread.wereadgear_adapter as adapters
    class Stats:
        source="official"
        identity_hash="synthetic-reader"
        total=11697
        def close(self): pass
        def pair(self,book_id,timezone):
            return StatsPair(book_id=book_id,account_id="default",book_seconds=0,period_seconds=self.total,
                period_key="monthly:synthetic",source="official",identity_hash=self.identity_hash)
    class Writer:
        source="official"
        identity_hash="synthetic-writer"
        calls=0
        def validate_book(self,book_id): pass
        def run_bounded_session(self,*args,**kwargs):
            self.calls+=1
            return WriteResult("UNCERTAIN",1,20,"WRITE_RESPONSE")
    stats,writer=Stats(),Writer()
    monkeypatch.setattr(adapters,"require_live_flags",lambda **kwargs:None)  # This writer is memory-only.
    monkeypatch.setattr(Settings,"stats",lambda *args,**kwargs:stats)
    settings=Settings(root,tmp_path/"recovery.sqlite","default",False,False)
    with settings.database() as db:
        db.bind(confirm_book(Book(book_id="10001",title="合成恢复书",author="作者",publisher="出版社",origin="official"),confirmed=True))
        entry=db.add(EntryInput(book_id="10001",reading_date="2026-11-04",duration_seconds=3600))
        service=SyncService(db,stats,writer,lock_path=app_directory()/"writer.lock")
        service.baseline(entry["entry_id"])
        service.attest(entry["entry_id"],app_book_seconds=0,app_period_seconds=11697,same_account=True,no_other_reading=True)
        attempt=service.start(entry["entry_id"],seconds=60,live=True,confirm=True)
        old_baseline=db.latest_snapshot(entry["entry_id"])[0]
    stats.total=11698
    app=create_app(settings)  # Reopen with an existing durable pending attempt.
    try:
        for _ in range(2):
            page=send(app,"GET","/")
            csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
            response=send(app,"POST","/action/baseline",data={"csrf":csrf,"entry_id":entry["entry_id"]})
            job=wait_job(app,response)
            assert job["error_code"] is None
            assert job["result"]["status"]=="PENDING_REVIEW_REQUIRED"
            result_page=send(app,"GET",str(response.url)).text
            assert "已读取微信读书时长" in result_page and "查询完成" in result_page
            assert "合成恢复书" in result_page and "3 小时" in result_page
            assert "查看上次试验的处理方式" in result_page
            assert "请先只读 verify/reconcile" not in result_page
        page=send(app,"GET","/").text
        assert 'id="recovery-'+entry["entry_id"]+'"' in page
        assert 'action="/action/attest"' not in page  # No known-dead-end button while pending.
        expired=send(app,"GET","/jobs/old-process-task")
        assert expired.status_code==200 and "页面已更新" in expired.text
        with settings.database() as db:
            assert db.latest_snapshot(entry["entry_id"])[0]==old_baseline
            assert db.entry(entry["entry_id"])["verified_seconds"]==0
            assert db.entry(entry["entry_id"])["status"]=="UNCERTAIN"
            assert db.conn.execute("SELECT COUNT(*) FROM write_guard").fetchone()[0]==1
            assert db.conn.execute("SELECT COUNT(*) FROM sync_attempts").fetchone()[0]==1
        assert writer.calls==1
    finally: app.state.operations.jobs.shutdown()
