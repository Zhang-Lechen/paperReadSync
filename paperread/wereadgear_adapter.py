"""Bounded, fixed-book protocol reuse; no login, random selection or fallback."""

from dataclasses import dataclass
import hashlib
import os
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx

from paperread.credentials import WriterCredentials
from paperread.errors import AppError
from paperread.upstream_protocol import UpstreamPayload

READ_URL = "https://weread.qq.com/web/book/read"


def require_live_flags(*, live: bool, confirm: bool):
    if not live or not confirm:
        raise AppError("WRITE_DENIED", "真实写入必须同时显式指定 --live --confirm")
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS") or os.environ.get("PYTEST_CURRENT_TEST"):
        raise AppError("CI_WRITE_DENIED", "自动测试/CI 环境禁止真实网络写入")


@dataclass(frozen=True)
class WriteResult:
    transport_status: str
    requests_sent: int = 0
    elapsed_seconds: int = 0
    error_code: str | None = None
    response_diagnostics: dict | None = None


def classify_write_response(response):
    """Keep only fixed categories and bounded numeric codes, never remote text/headers."""
    diagnostics = {"http_status": response.status_code}
    if response.status_code != 200:
        return "WRITE_HTTP", {**diagnostics, "category": "HTTP_ERROR"}
    try:
        value = response.json()
    except ValueError:
        return "WRITE_RESPONSE", {**diagnostics, "category": "INVALID_JSON"}
    if not isinstance(value, dict):
        return "WRITE_RESPONSE", {**diagnostics, "category": "UNEXPECTED_JSON_TYPE"}
    if not value:
        return "WRITE_RESPONSE", {**diagnostics, "category": "EMPTY_OBJECT"}
    errors = {}
    for field in ("errCode", "errcode", "err_code"):
        if field not in value:
            continue
        code = value[field]
        if type(code) is not int or not -(2**31) <= code < 2**31:
            return "WRITE_RESPONSE", {**diagnostics, "category": "INVALID_ERROR_CODE"}
        errors[field] = code
    if errors:
        diagnostics["remote_codes"] = errors
    if any(code != 0 for code in errors.values()):
        expired = any(code in (-2012, -2010) for code in errors.values())
        return ("WRITE_AUTH_EXPIRED" if expired else "WRITE_REMOTE_ERROR"), {
            **diagnostics, "category": "AUTH_EXPIRED" if expired else "REMOTE_ERROR"}
    if value.get("succ") != 1:
        return "WRITE_RESPONSE", {**diagnostics, "category": "SUCCESS_NOT_CONFIRMED"}
    return None, {**diagnostics, "category": "SUCCESS_RESPONSE"}


class BoundedWriter:
    source = "official"

    def __init__(self, credentials: WriterCredentials, *, client: httpx.Client | None = None,
                 monotonic=time.monotonic, wall_time=time.time, sleep=time.sleep):
        self.credentials = credentials
        self.identity_hash = credentials.identity_hash
        self._owned = client is None
        self._client = client
        self.monotonic, self.wall_time, self.sleep = monotonic, wall_time, sleep
        self._session_token = None
        self.session_manager = None

    def validate_session(self):
        from paperread.web_session import validate_session
        if self.session_manager is not None:
            self.credentials, self._session_token, _ = self.session_manager.ensure(
                self.credentials, client=self._client)
        else:
            self._session_token = validate_session(self.credentials, client=self._client)

    def web_id(self, book_id: str):
        if self.credentials.api_book_id is None:
            return book_id  # Legacy same-ID contexts; validation below still rejects mismatches.
        if self.credentials.api_book_id != book_id or not self.credentials.web_book_id:
            raise AppError("BOOK_MISMATCH", "授权上下文属于其他官方书目")
        return self.credentials.web_book_id

    def __repr__(self):
        return "BoundedWriter(<private>)"

    def validate_book(self, book_id: str):
        data = self.credentials.captured_payload
        required = ("b", "c", "sm", "co", "pr", "ps", "pc", "appId", "ci")
        if any(key not in data or data[key] is None for key in required):
            raise AppError("CHAPTER_CONTEXT", "缺少同源书籍/章节上下文，已停止；不会自动打开阅读器")
        web_id = self.web_id(book_id)
        if data["b"] != web_id:
            raise AppError("BOOK_MISMATCH", "本地已授权上下文不是指定书籍；不会自动切书")
        if not all(isinstance(data[key], str) and data[key].strip() for key in ("b", "c", "sm", "ps", "pc", "appId")):
            raise AppError("CHAPTER_CONTEXT", "书籍章节或身份上下文无效")
        if type(data["ci"]) is not int or data["ci"] < 0:
            raise AppError("CHAPTER_CONTEXT", "章节索引无效")
        headers = self.credentials.captured_headers
        for key in ("origin", "referer"):
            value = next((v for k, v in headers.items() if k.lower() == key), "")
            parsed = urlparse(value)
            if parsed.scheme != "https" or parsed.netloc != "weread.qq.com":
                raise AppError("HEADER_CONTEXT", "本地请求上下文来源无效")
        if self.credentials.api_book_id is not None:
            from paperread.book_routes import reader_route
            referer = next((v for k,v in headers.items() if k.lower()=="referer"), "")
            if (reader_route(referer) != web_id or self.credentials.mapping_source != "official_reader_link"):
                raise AppError("BOOK_ROUTE_MISMATCH", "书目、网页路由和章节上下文未形成可信映射")
        if not self.credentials.wr_skey.get_secret_value() or not self.credentials.wr_vid.get_secret_value():
            raise AppError("WRITER_LOGIN_REQUIRED", "写入凭证为空")

    def _builder(self, book_id: str):
        value = UpstreamPayload()
        data = self.credentials.captured_payload
        value.data = dict(data)
        value.credentials = SimpleNamespace(user_info={"captured_payload": dict(data)},
            user_id=self.credentials.wr_vid.get_secret_value(), wr_vid=self.credentials.wr_vid.get_secret_value())
        web_id = self.web_id(book_id)
        value.last_book_id, value.last_chapter_id = web_id, data["c"]
        value.last_chapter_index = data["ci"]
        value._book_chapter_cache = {web_id: {"heartbeat_context": {key: data[key] for key in ("sm", "co", "pr")}}}
        return value

    def run_bounded_session(self, book_id: str, seconds: int, *, live: bool, confirm: bool,
                            test_transport: bool = False, cancel_event=None) -> WriteResult:
        # Test injection is allowed only with an actual in-memory MockTransport.
        in_memory = (self._client is not None and isinstance(self._client._transport, httpx.MockTransport))
        if not (test_transport and in_memory):
            require_live_flags(live=live, confirm=confirm)
        elif not live or not confirm:
            raise AppError("WRITE_DENIED", "测试写入也需要显式确认")
        self.validate_book(book_id)
        if type(seconds) is not int or not 1 <= seconds <= 60:
            raise AppError("BUDGET_INVALID", "单次有界会话只能批准 1 至 60 整秒")
        if not (test_transport and in_memory) and self._session_token is None:
            self.validate_session()
        builder = self._builder(book_id)
        web_id = self.web_id(book_id)
        allowed = {"accept", "accept-language", "content-type", "origin", "referer", "user-agent",
                   "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "x-wrpa-0"}
        headers = {k: v for k, v in self.credentials.captured_headers.items() if k.lower() in allowed}
        from paperread.web_session import session_cookies
        cookies = session_cookies(self.credentials)
        # Lazy creation ensures rejected calls never construct a live transport.
        client = self._client or httpx.Client(timeout=10, follow_redirects=False, trust_env=False,
                                             transport=httpx.HTTPTransport(retries=0))
        started = self.monotonic()
        started_wall = int(self.wall_time())
        last_wall = started_wall
        reported = sent = 0
        try:
            while reported < seconds:
                remaining = seconds - (self.monotonic() - started)
                if remaining>0 and cancel_event is not None:
                    if cancel_event.wait(min(20, remaining)):
                        return WriteResult("UNCERTAIN" if sent else "CANCELLED", sent,
                            int(self.monotonic()-started), "USER_CANCELLED")
                elif remaining>0:
                    self.sleep(min(20, remaining))
                elapsed = self.monotonic() - started
                # Protocol uses current wall time, not a fabricated historical timestamp.
                # Injected clocks in tests replace the upstream module's time.time only in tests.
                builder._prepare_payload(last_wall)
                # Wall-clock rounding and response time must not turn the last
                # interval into an over-budget request (or silently omit a tail).
                # Report only whole, actually elapsed monotonic seconds, once.
                if (elapsed<0 or builder.data["ct"]<last_wall
                        or abs((builder.data["ct"]-started_wall)-elapsed)>2):
                    return WriteResult("UNCERTAIN",sent,int(elapsed),"CLOCK_OR_BUDGET")
                span = min(seconds-reported,max(0,int(elapsed)-reported))
                if span<=0:
                    return WriteResult("UNCERTAIN",sent,int(elapsed),"CLOCK_OR_BUDGET")
                builder.data["rt"] = span
                builder.data.pop("s",None)
                if self._session_token is not None:
                    # Keep upstream pure methods intact, but use the config of
                    # this authenticated session rather than a historical key.
                    builder.data.pop("s", None)
                    builder.data["sg"] = hashlib.sha256((str(builder.data["ts"])+str(builder.data["rn"])+
                        self._session_token.get_secret_value()).encode()).hexdigest()
                builder.data["s"] = builder._calculate_hash(builder._encode_data(builder.data))
                payload = dict(builder.data)
                if cancel_event is not None and cancel_event.is_set():
                    return WriteResult("UNCERTAIN" if sent else "CANCELLED", sent, int(elapsed), "USER_CANCELLED")
                if payload["b"] != web_id:
                    return WriteResult("UNCERTAIN", sent, int(elapsed), "BOOK_MISMATCH")
                sent += 1  # Count before send: a timeout can happen after server receipt.
                # Build a fresh request with the original approved identity each time.
                # The client's response cookie jar must not silently switch identities.
                request = httpx.Request("POST", READ_URL, headers=headers, cookies=cookies, json=payload)
                response = client.send(request, follow_redirects=False)
                error, diagnostics = classify_write_response(response)
                if error:
                    return WriteResult("UNCERTAIN", sent, int(self.monotonic()-started), error, diagnostics)
                reported += span
                last_wall = payload["ct"]
            return WriteResult("RESPONSE_RECEIVED", sent, int(self.monotonic()-started))
        except KeyboardInterrupt:
            return WriteResult("UNCERTAIN" if sent else "CANCELLED", sent, int(self.monotonic()-started), "USER_CANCELLED")
        except httpx.HTTPError:
            return WriteResult("UNCERTAIN", sent, int(self.monotonic()-started), "WRITE_NETWORK")
        finally:
            if self._owned:
                client.close()


class MockWriter:
    source = "mock"
    identity_hash = "mock-writer-identity"

    def __init__(self, stats):
        self.stats = stats
        self.calls = []

    def validate_book(self, book_id):
        if not book_id.startswith("mock-"):
            raise AppError("BACKEND_MISMATCH", "Mock 写入仅支持示例书籍")

    def run_bounded_session(self, book_id, seconds, *, live=False, confirm=False):
        self.validate_book(book_id)
        self.calls.append((book_id, seconds))
        self.stats.advance(book_id, seconds)
        return WriteResult("RESPONSE_RECEIVED", 1, seconds)
