from datetime import datetime
import json
from zoneinfo import ZoneInfo

import httpx
import pytest

from paperread.errors import AppError
from paperread.official_stats import GATEWAY, OfficialStats


def adapter(root, handler):
    return OfficialStats(root=root, enabled=True, account_id="test", key="synthetic-private-value",
                         client=httpx.Client(transport=httpx.MockTransport(handler)))


def current_base():
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    return int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def test_reads_are_explicit_and_never_request_local_history(root, monkeypatch):
    with pytest.raises(AppError, match="READ_ONLY_REQUIRED"):
        OfficialStats(root=root, enabled=False, account_id="test")
    monkeypatch.delenv("WEREAD_API_KEY", raising=False)
    with pytest.raises(AppError, match="READ_LOGIN_REQUIRED"):
        OfficialStats(root=root, enabled=True, account_id="test")


def test_pair_uses_book_seconds_and_total_not_buckets_or_rank(root):
    calls = []
    def handle(request):
        assert str(request.url) == GATEWAY
        body = json.loads(request.content)
        calls.append(body)
        assert body["skill_version"] == "1.0.5"
        assert "params" not in body
        if body["api_name"] == "/book/getprogress":
            return httpx.Response(200, json={"bookId": "A", "book": {"readingTime": 123,"recordReadingTime": 0}, "timestamp": 123456})
        return httpx.Response(200, json={"baseTime": current_base(), "totalReadTime": 900,
                                         "readTimes": {"truncated": 1}, "readLongest": [],
                                         "Authorization": "synthetic-private-value"})
    value = adapter(root, handle).pair("A", "Asia/Shanghai")
    assert value.book_seconds == 123 and value.period_seconds == 900
    assert value.evidence["book_time_field"]=="book.readingTime"
    assert value.evidence["book"]["readingTime"]==123
    assert "recordReadingTime" not in value.evidence["book"]
    assert len(calls) == 2
    assert "synthetic-private-value" not in value.model_dump_json()
    assert value.source == "official"


@pytest.mark.parametrize("status,code", [(401,"READ_AUTH"),(403,"READ_AUTH"),(429,"READ_RATE_LIMIT"),(500,"READ_HTTP"),(302,"READ_HTTP")])
def test_http_failures_never_retry_or_expose_response(root, status, code):
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="wr_skey=synthetic-private-value")
    with pytest.raises(AppError, match=code) as exc:
        adapter(root, handle).request("/book/info", bookId="A")
    assert len(calls) == 1
    assert "synthetic-private-value" not in str(exc.value)


@pytest.mark.parametrize("body,code", [([],"READ_SCHEMA"),({"errcode":-1,"errmsg":"private"},"READ_REMOTE_ERROR"),
                                     ({"upgrade_info":{"message":"private"}},"UPGRADE_REQUIRED")])
def test_schema_and_upgrade_fail_closed(root, body, code):
    with pytest.raises(AppError, match=code):
        adapter(root, lambda request: httpx.Response(200,json=body)).request("/book/info",bookId="A")


def test_malformed_json_and_timeout(root):
    with pytest.raises(AppError, match="READ_SCHEMA"):
        adapter(root, lambda r: httpx.Response(200,text="not json private")).request("/book/info",bookId="A")
    def timeout(request):
        raise httpx.ReadTimeout("wr_skey=private", request=request)
    with pytest.raises(AppError, match="READ_NETWORK"):
        adapter(root, timeout).request("/book/info",bookId="A")


@pytest.mark.parametrize("value", [{}, {"bookId":"A","book":{}}, {"bookId":"A","book":{"recordReadingTime":60}},
                                  {"bookId":"A","book":{"readingTime":True,"recordReadingTime":100}},
                                  {"bookId":"A","book":{"readingTime":-1,"recordReadingTime":100}}])
def test_missing_or_invalid_single_book_seconds_blocks_baseline(root, value):
    def handle(request):
        if json.loads(request.content)["api_name"] == "/book/getprogress":
            return httpx.Response(200,json=value)
        return httpx.Response(200,json={"baseTime":current_base(),"totalReadTime":10})
    with pytest.raises(AppError, match="READ_SCHEMA"):
        adapter(root, handle).pair("A","Asia/Shanghai")


def test_read_gateway_whitelist_prevents_other_post_actions(root):
    calls=[]
    stats=adapter(root,lambda request:calls.append(request))
    with pytest.raises(AppError, match="READ_API_DENIED"):
        stats.request("/shelf/add", bookId="A")
    assert calls == []


def test_search_accepts_scope17_and_detail_retains_edition(root):
    def handle(request):
        value=json.loads(request.content)
        if value["api_name"] == "/store/search":
            assert value["scope"] == 10
            return httpx.Response(200,json={"results":[{"scope":17,"books":[{"bookInfo":{
                "bookId":"A","title":"安娜","author":"作者","publisher":"出版社"}}]}]})
        return httpx.Response(200,json={"bookId":"A","title":"安娜","author":"作者","translator":"译者","isbn":"123"})
    stats=adapter(root,handle)
    assert stats.search("安娜")[0].book_id == "A"
    assert stats.book_info("A").translator == "译者"
