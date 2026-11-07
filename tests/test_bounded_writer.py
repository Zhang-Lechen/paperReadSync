import ast
import json
from types import SimpleNamespace

import httpx
import pytest

from paperread.credentials import WriterCredentials
from paperread.errors import AppError
from paperread.wereadgear_adapter import BoundedWriter


def credentials(book_id="A"):
    return WriterCredentials(wr_skey="synthetic-private-session", wr_vid="synthetic-private-account",
        captured_headers={"origin":"https://weread.qq.com","referer":"https://weread.qq.com/",
                          "content-type":"application/json", "user-agent":"user-approved-agent"},
        captured_payload={"b":book_id,"c":"chapter-A","sm":"同一章节测试内容","co":0,"pr":1,
                          "ps":"synthetic-ps","pc":"synthetic-pc","appId":"synthetic-app","ci":0})


class Clock:
    now=1000.0
    def monotonic(self): return self.now
    def wall_time(self): return self.now
    def sleep(self, seconds): self.now += seconds


def writer(monkeypatch, handler):
    import paperread.upstream_protocol as protocol
    clock=Clock()
    monkeypatch.setattr(protocol,"time",SimpleNamespace(time=clock.wall_time))
    client=httpx.Client(transport=httpx.MockTransport(handler))
    return BoundedWriter(credentials(),client=client,monotonic=clock.monotonic,
                          wall_time=clock.wall_time,sleep=clock.sleep)


def test_fixed_book_measured_time_and_bounded_total(monkeypatch):
    calls=[]
    def handle(request):
        calls.append(json.loads(request.content))
        assert str(request.url)=="https://weread.qq.com/web/book/read"
        return httpx.Response(200,json={"succ":1})
    result=writer(monkeypatch,handle).run_bounded_session("A",60,live=True,confirm=True,test_transport=True)
    assert result.transport_status == "RESPONSE_RECEIVED"
    assert result.elapsed_seconds == 60
    assert {call["b"] for call in calls} == {"A"}
    assert {call["c"] for call in calls} == {"chapter-A"}
    assert sum(call["rt"] for call in calls) == 60
    assert calls[0]["ct"] == 1020  # No initial fabricated +30 seconds.
    assert all(call["s"] and call["sg"] for call in calls)


def test_timeout_after_first_send_never_retries(monkeypatch):
    calls=[]
    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("private",request=request)
    result=writer(monkeypatch,handle).run_bounded_session("A",60,live=True,confirm=True,test_transport=True)
    assert result.transport_status == "UNCERTAIN" and len(calls)==1
    assert result.error_code == "WRITE_NETWORK"


@pytest.mark.parametrize("response", [httpx.Response(200,json={"succ":0,"synckey":"x"}),
    httpx.Response(200,json={}), httpx.Response(200,text="private not json"),
    httpx.Response(403,text="private"), httpx.Response(429,text="private")])
def test_failed_or_ambiguous_response_stops_without_fallback(monkeypatch,response):
    calls=[]
    def handle(request):
        calls.append(request)
        return response
    result=writer(monkeypatch,handle).run_bounded_session("A",60,live=True,confirm=True,test_transport=True)
    assert result.transport_status == "UNCERTAIN"
    assert len(calls)==1


def test_missing_confirmation_wrong_book_and_ci_all_send_zero(monkeypatch):
    calls=[]
    value=writer(monkeypatch,lambda r:calls.append(r))
    with pytest.raises(AppError,match="WRITE_DENIED"):
        value.run_bounded_session("A",60,live=True,confirm=False,test_transport=True)
    with pytest.raises(AppError,match="BOOK_MISMATCH"):
        value.run_bounded_session("B",60,live=True,confirm=True,test_transport=True)
    with pytest.raises(AppError,match="CI_WRITE_DENIED"):
        value.run_bounded_session("A",60,live=True,confirm=True)
    assert calls == []


def test_cancel_before_first_request_and_after_send(monkeypatch):
    calls=[]
    value=writer(monkeypatch,lambda r: calls.append(r) or httpx.Response(200,json={"succ":1}))
    value.sleep=lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt())
    result=value.run_bounded_session("A",60,live=True,confirm=True,test_transport=True)
    assert result.transport_status == "CANCELLED" and calls==[]


def test_protocol_methods_are_unchanged_from_pinned_upstream(root):
    from paperread import upstream_protocol
    original=ast.parse((root/"vendor/WeReadGears/src/api_reader.py").read_text(encoding="utf-8"))
    extracted=ast.parse((root/"paperread/upstream_protocol.py").read_text(encoding="utf-8"))
    old=next(n for n in original.body if isinstance(n,ast.ClassDef) and n.name=="ApiReader")
    new=next(n for n in extracted.body if isinstance(n,ast.ClassDef) and n.name=="UpstreamPayload")
    for method in new.body:
        counterpart=next(n for n in old.body if isinstance(n,ast.FunctionDef) and n.name==method.name)
        assert ast.dump(method,include_attributes=False) == ast.dump(counterpart,include_attributes=False)


def test_canonical_api_book_id_uses_its_bound_route_in_every_write(monkeypatch):
    calls=[]
    value=writer(monkeypatch,lambda request: calls.append(json.loads(request.content)) or httpx.Response(200,json={"succ":1}))
    original=value.credentials
    value.credentials=original.model_copy(update={"api_book_id":"10001","web_book_id":"route-A",
        "mapping_source":"official_reader_link","captured_payload":{**original.captured_payload,"b":"route-A"},
        "captured_headers":{**original.captured_headers,"referer":"https://weread.qq.com/web/reader/route-A"}})
    result=value.run_bounded_session("10001",60,live=True,confirm=True,test_transport=True)
    assert result.transport_status=="RESPONSE_RECEIVED"
    assert {call["b"] for call in calls}=={"route-A"}
    with pytest.raises(AppError,match="BOOK_MISMATCH"):
        value.run_bounded_session("10002",60,live=True,confirm=True,test_transport=True)
    assert len(calls)==3


def test_user_cancel_event_stops_before_first_network_request(monkeypatch):
    from threading import Event
    event=Event();event.set()
    calls=[]
    value=writer(monkeypatch,lambda request:calls.append(request))
    result=value.run_bounded_session("A",60,live=True,confirm=True,test_transport=True,cancel_event=event)
    assert result.transport_status=="CANCELLED" and result.requests_sent==0 and calls==[]


@pytest.mark.parametrize("response,code,category", [
    (httpx.Response(200,json={}),"WRITE_RESPONSE","EMPTY_OBJECT"),
    (httpx.Response(200,text="synthetic-private-body"),"WRITE_RESPONSE","INVALID_JSON"),
    (httpx.Response(200,json=[]),"WRITE_RESPONSE","UNEXPECTED_JSON_TYPE"),
    (httpx.Response(200,json={"succ":0}),"WRITE_RESPONSE","SUCCESS_NOT_CONFIRMED"),
    (httpx.Response(200,json={"errCode":-2012,"errMsg":"synthetic-private-body"}),"WRITE_AUTH_EXPIRED","AUTH_EXPIRED"),
    (httpx.Response(200,json={"succ":1,"errcode":-1}),"WRITE_REMOTE_ERROR","REMOTE_ERROR"),
    (httpx.Response(200,json={"errCode":"synthetic-private-body"}),"WRITE_RESPONSE","INVALID_ERROR_CODE"),
    (httpx.Response(429,text="synthetic-private-body"),"WRITE_HTTP","HTTP_ERROR"),
])
def test_failure_diagnostics_are_specific_private_and_never_retry(monkeypatch,response,code,category):
    calls=[]
    result=writer(monkeypatch,lambda r:calls.append(r) or response).run_bounded_session(
        "A",60,live=True,confirm=True,test_transport=True)
    assert result.transport_status=="UNCERTAIN" and result.error_code==code
    assert result.requests_sent==1 and result.elapsed_seconds==20 and len(calls)==1
    assert result.response_diagnostics["category"]==category
    assert "synthetic-private-body" not in json.dumps(result.response_diagnostics)


def test_diagnostics_never_copy_remote_messages_headers_or_success_keys():
    from paperread.wereadgear_adapter import classify_write_response
    error,details=classify_write_response(httpx.Response(200,json={"errCode":-1,
        "errMsg":"synthetic-private-message","Cookie":"synthetic-private-cookie",
        "synckey":"synthetic-private-key","nested":{"token":"synthetic-private-nested"}},
        headers={"set-cookie":"synthetic-private-header"}))
    assert error=="WRITE_REMOTE_ERROR"
    assert details=={"http_status":200,"category":"REMOTE_ERROR","remote_codes":{"errCode":-1}}


def test_session_config_drives_signature_without_modifying_pinned_protocol(monkeypatch):
    import hashlib
    from pydantic import SecretStr
    calls=[]
    value=writer(monkeypatch,lambda request:calls.append(json.loads(request.content)) or httpx.Response(200,json={"succ":1}))
    value._session_token=SecretStr("synthetic-current-config")
    result=value.run_bounded_session("A",20,live=True,confirm=True,test_transport=True)
    assert result.transport_status=="RESPONSE_RECEIVED" and len(calls)==1
    body=calls[0]
    assert body["sg"]==hashlib.sha256((str(body["ts"])+str(body["rn"])+"synthetic-current-config").encode()).hexdigest()
    assert "synthetic-current-config" not in json.dumps(body)


@pytest.mark.parametrize("latency",[0.25,0.75,1.25])
def test_network_response_time_cannot_overflow_budget_or_drop_final_seconds(monkeypatch,latency):
    calls=[]
    value=None
    def handle(request):
        body=json.loads(request.content)
        calls.append(body)
        elapsed=value.monotonic()-1000
        assert sum(part['rt'] for part in calls)<=int(elapsed)
        clock=value.monotonic.__self__
        clock.now+=latency
        return httpx.Response(200,json={"succ":1})
    value=writer(monkeypatch,handle)
    result=value.run_bounded_session("A",60,live=True,confirm=True,test_transport=True)
    assert result.transport_status=="RESPONSE_RECEIVED" and result.error_code is None
    assert sum(body['rt'] for body in calls)==60
    assert len(calls)==3
