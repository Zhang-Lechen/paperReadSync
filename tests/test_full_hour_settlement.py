"""Actual payload builder + HTTP adapters, virtual elapsed hour and lagging server stats.

All traffic uses MockTransport. No real account, Key, browser or outside socket.
"""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from paperread.book_match import confirm_book
from paperread.db import Database
from paperread.models import Book,EntryInput
from paperread.official_stats import OfficialStats
from paperread.sync_service import SyncService
from paperread.wereadgear_adapter import BoundedWriter
from test_bounded_writer import credentials


class Clock:
    now=1000.0
    def monotonic(self): return self.now
    def wall_time(self): return self.now
    def sleep(self,seconds): self.now+=seconds


def setup(root,tmp_path,monkeypatch,*,duration=3600,forever=False):
    import json
    import paperread.upstream_protocol as protocol
    import paperread.wereadgear_adapter as adapter
    clock=Clock()
    monkeypatch.setattr(protocol,"time",SimpleNamespace(time=clock.wall_time))
    def flags(**values): assert values=={"live":True,"confirm":True}
    monkeypatch.setattr(adapter,"require_live_flags",flags)
    now=datetime.now(ZoneInfo("Asia/Shanghai"))
    base=int(now.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp())
    state={"book":0,"period":1000,"reads":[],"lag_until":0,"lag_count":0,"pairs":0}
    delayed_totals={60} if forever else {300,900,1800,3540}
    def handler(request):
        clock.sleep(.125)  # Every HTTP call has observable elapsed latency.
        if request.url.path=="/web/getConfig":
            return httpx.Response(200,json={"token":"synthetic-session-config"})
        body=json.loads(request.content)
        if request.url.path=="/web/book/read":
            if clock.now<state["lag_until"]:
                raise AssertionError("A new write was sent before delayed statistics reconciled")
            assert body["b"]=="route-A" and body["c"]=="chapter-A"
            state["reads"].append(body)
            state["book"]+=body["rt"];state["period"]+=body["rt"]
            if state["book"] in delayed_totals:
                state["lag_until"]=float('inf') if forever else clock.now+6
                state["lag_count"]+=1
            return httpx.Response(200,json={"succ":1})
        assert request.url.path=="/api/agent/gateway"
        if body["api_name"]=="/book/getprogress":
            visible=state["book"]-(20 if clock.now<state["lag_until"] else 0)
            state["pairs"]+=1
            return httpx.Response(200,json={"bookId":"10001","book":{"readingTime":visible,"recordReadingTime":0},"timestamp":int(clock.now)})
        assert body["api_name"]=="/readdata/detail"
        return httpx.Response(200,json={"baseTime":base,"totalReadTime":state["period"]})
    client=httpx.Client(transport=httpx.MockTransport(handler))
    stats=OfficialStats(root=root,enabled=True,account_id="default",key="synthetic-read-key",client=client)
    profile=credentials("route-A")
    profile=profile.model_copy(update={"api_book_id":"10001","web_book_id":"route-A",
        "mapping_source":"official_reader_link","captured_headers":{**profile.captured_headers,
            "referer":"https://weread.qq.com/web/reader/route-A"}})
    class MemoryTransportWriter(BoundedWriter):
        def validate_session(self):
            from paperread.web_session import validate_session
            self._session_token=validate_session(self.credentials,client=client)
        def run_bounded_session(self,*args,**kwargs):
            return super().run_bounded_session(*args,test_transport=True,**kwargs)
    writer=MemoryTransportWriter(profile,client=client,monotonic=clock.monotonic,wall_time=clock.wall_time,sleep=clock.sleep)
    db=Database(tmp_path/"hour.sqlite")
    db.bind(confirm_book(Book(book_id="10001",title="合成整小时协议测试",author="作者",publisher="出版社",origin="official"),confirmed=True))
    entry=db.add(EntryInput(book_id="10001",reading_date=now.date(),duration_seconds=duration))
    service=SyncService(db,stats,writer,lock_path=tmp_path/"writer.lock",
        settlement_clock=clock.monotonic,settlement_wait=clock.sleep)
    service.authorize_direct(entry['entry_id'],confirmed=True)
    return db,service,entry['entry_id'],clock,state,client


def test_complete_hour_with_real_protocol_network_latency_and_repeated_book_cache_lag(root,tmp_path,monkeypatch):
    db,service,entry_id,clock,state,client=setup(root,tmp_path,monkeypatch)
    try:
        result=service.run_remaining(entry_id,budget_seconds=3600,live=True,confirm=True)
        assert result['batch_status']=='COMPLETED'
        assert result['verified_seconds']==3600 and result['unverified_this_job_seconds']==0
        assert result['completed_this_job_seconds']==3600 and result['available_seconds']==0
        assert sum(body['rt'] for body in state['reads'])==3600 and len(state['reads'])==180
        assert state['lag_count']==4 and clock.now-1000>=3600
        assert db.conn.execute('SELECT COUNT(*) FROM sync_attempts').fetchone()[0]==60
        assert db.conn.execute("SELECT COUNT(*) FROM sync_attempts WHERE verification_status!='VERIFIED'").fetchone()[0]==0
        assert not service._active() and not service.pending_path.exists()
        comparisons=[row[0] for row in db.conn.execute("SELECT details FROM audit_events WHERE event='REMOTE_COMPARED'")]
        assert any('ATTRIBUTION_UNPROVEN' in row for row in comparisons)
    finally:
        client.close();db.close()


def test_persistent_mismatch_waits_without_rewriting_then_reports_partial_job_not_success(root,tmp_path,monkeypatch):
    db,service,entry_id,clock,state,client=setup(root,tmp_path,monkeypatch,duration=120,forever=True)
    try:
        result=service.run_remaining(entry_id,budget_seconds=120,live=True,confirm=True)
        assert result['batch_status']=='STOPPED' and result['settlement_timed_out']
        assert result['approved_job_seconds']==120 and result['completed_this_job_seconds']==0
        assert result['unverified_this_job_seconds']==120 and result['verified_seconds']==0
        assert result['book_delta']==40 and result['period_delta']==60
        assert result['settlement_queries']>1 and len(state['reads'])==3
        assert service._active() and service.pending_path.exists()
    finally:
        client.close();db.close()
