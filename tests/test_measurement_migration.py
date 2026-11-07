import asyncio
import httpx

from paperread.book_match import confirm_book
from paperread.db import Database
from paperread.models import Book,EntryInput,StatsPair
from paperread.sync_service import SyncService
from paperread.wereadgear_adapter import WriteResult


def test_old_attempt_is_not_falsely_verified_after_correcting_field(root,tmp_path,monkeypatch):
    import paperread.wereadgear_adapter as adapters
    # Explicit memory-only adapters. No network, browser or actual account.
    monkeypatch.setattr(adapters,"require_live_flags",lambda **kwargs:None)
    class Stats:
        source="official"
        identity_hash="synthetic-reader"
        corrected=False
        total=1000
        def pair(self,book_id,timezone):
            return StatsPair(book_id=book_id,account_id="default",book_seconds=60 if self.corrected else 0,
                period_seconds=self.total,period_key="monthly:synthetic",source=self.source,identity_hash=self.identity_hash,
                evidence={"book_time_field":"book.readingTime","book":{"readingTime":60}} if self.corrected else
                         {"book":{"recordReadingTime":0}})
    stats=Stats()
    class Writer:
        source="official"
        identity_hash="synthetic-writer"
        calls=0
        def validate_book(self,book_id): pass
        def run_bounded_session(self,*args,**kwargs):
            self.calls+=1;stats.total=1060
            return WriteResult("RESPONSE_RECEIVED",3,60)
    writer=Writer()
    with Database(tmp_path/"real-fixture.sqlite") as db:
        db.bind(confirm_book(Book(book_id="A",title="合成书",author="作者",publisher="出版社",origin="official"),confirmed=True))
        entry=db.add(EntryInput(book_id="A",reading_date="2026-11-04",duration_seconds=3600))
        service=SyncService(db,stats,writer,lock_path=tmp_path/"writer.lock")
        service.baseline(entry["entry_id"])
        service.attest(entry["entry_id"],app_book_seconds=0,app_period_seconds=1000,same_account=True,no_other_reading=True)
        result=service.start(entry["entry_id"],seconds=60,live=True,confirm=True)
        original=service._attempt(result["attempt_id"])["before_json"]
        stats.corrected=True
        observed=service.diagnose(entry["entry_id"])
        assert observed["book_delta"] is None and observed["period_delta"]==60
        assert observed["comparison"]=="MEASUREMENT_CHANGED"
        verified=service.verify(entry["entry_id"],app_book_seconds=60,app_period_seconds=1060,
            no_other_reading=True,same_day_confirmed=True)
        assert verified["error_code"]=="MEASUREMENT_CHANGED" and verified["verified_seconds"]==0
        assert service._attempt(result["attempt_id"])["before_json"]==original
        assert writer.calls==1 and service._active() and service.pending_path.exists()
        from paperread.cli import Settings
        from paperread.web import create_app
        app=create_app(Settings(root,db.path,"default",False,False))
        async def view():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://127.0.0.1:8765") as client:
                return await client.get("/")
        try:
            response=asyncio.run(view())
            assert response.status_code==200
            assert "旧版字段读取错误" in response.text
            assert "历史累计 1 分 0 秒" in response.text
        finally:app.state.operations.jobs.shutdown()
