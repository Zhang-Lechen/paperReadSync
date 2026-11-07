"""Interactive browser authorization, adapted from the audited WeReadGears flow.

Unlike the upstream capture flow, EVERY reading-time request is aborted before
network dispatch. No page turning, fingerprint overrides, curl files or traces.
The user scans the actual WeRead login page in a visible temporary browser.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse

from paperread.book_routes import approved_reader_link, validate_captured_mapping
from paperread.credentials import CredentialStore, PrivateJsonStore, WriterCredentials
from paperread.errors import AppError
from paperread.file_lock import WriterLock
from paperread.models import utc_now

READ_PATHS = ("/web/book/read", "/web/book/recordReadingTime", "/web/book/addReadingTime")
AUTH_PREFIXES = ("/web/login/", "/web/auth/", "/web/qrcode/")
READONLY_POST_PATHS = {"/web/book/chapterInfos", "/web/book/getProgress", "/web/book/info", "/web/shelf/sync"}


def browser_request_allowed(url, method):
    parsed = urlparse(url)
    path = parsed.path
    if any(path.lower().startswith(value.lower()) for value in READ_PATHS):
        return False
    if parsed.scheme not in ("https", "data", "blob", "about"):
        return False
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if parsed.netloc == "weread.qq.com":
        return (path.startswith(AUTH_PREFIXES) or path in READONLY_POST_PATHS or
                path.startswith("/web/book/chapter/"))
    # Authentication redirects/QR exchanges only; no telemetry or arbitrary POST.
    return parsed.hostname in {"open.weixin.qq.com", "long.open.weixin.qq.com", "lp.open.weixin.qq.com"}


def _channel():
    for value in ("PAPERREAD_BROWSER_CHANNEL",):
        selected = os.environ.get(value)
        if selected in ("msedge", "chrome", "chromium"):
            return selected
    if os.name == "nt":
        if Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe").is_file():
            return "msedge"
        if Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe").is_file():
            return "chrome"
    return "chromium"


class CaptureGuard:
    """Testable dispatch barrier: capture a matching heartbeat, ALWAYS abort it."""
    def __init__(self, book=None):
        self.book = book
        self.captured = None
        self.blocked = 0

    async def __call__(self, route):
        request = route.request
        parsed = urlparse(request.url)
        if any(parsed.path.lower().startswith(value.lower()) for value in READ_PATHS):
            self.blocked += 1
            try:
                if (self.book is not None and self.captured is None and request.method=="POST"
                        and parsed.scheme=="https" and parsed.netloc=="weread.qq.com"):
                    data = json.loads(request.post_data or "{}")
                    headers = await request.all_headers()
                    route_id = validate_captured_mapping(self.book.book_id,self.book.reader_link,
                        request.frame.url,data,headers.get("referer", ""))
                    self.captured = (data,headers,route_id)
            except Exception:
                pass
            finally:
                await route.abort()
            return
        if browser_request_allowed(request.url,request.method):
            await route.continue_()
        else:
            await route.abort()


async def authorize(account_id, *, cancel_event, progress, book=None, database=None, timeout=300,
                    private_directory=None, recovery_identity_hash=None):
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS") or os.environ.get("PYTEST_CURRENT_TEST"):
        raise AppError("CI_BROWSER_DENIED", "自动化测试/CI 禁止启动真实授权浏览器")
    if os.environ.get("DEBUG") or os.environ.get("PWDEBUG"):
        raise AppError("BROWSER_DEBUG_UNSAFE", "浏览器调试环境可能记录凭证，请关闭当前进程的 DEBUG/PWDEBUG 后重试")
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise AppError("BROWSER_DEPENDENCY", "请安装 requirements.lock 中的浏览器依赖") from None
    session_store = PrivateJsonStore(account_id, "browser", private_directory)
    if book is not None and session_store.exists():
        from paperread.session_manager import SessionManager
        progress("正在检查已保存的登录；过期时尝试正常续期，不发送阅读请求")
        await asyncio.to_thread(SessionManager(account_id, directory=private_directory).ensure_browser,
                                expected_identity=recovery_identity_hash)
    # Login must actually show a fresh QR flow; reusing saved (expired) cookies
    # previously made "re-login" finish immediately without repairing anything.
    state = session_store.load() if book is not None and session_store.exists() else None
    if book is not None:
        approved_reader_link(book.reader_link)
        if not state:
            raise AppError("LOCAL_LOGIN_REQUIRED", "请先点击扫码登录微信读书")
    guard = CaptureGuard(book)
    async with async_playwright() as playwright:
        browser = None
        try:
            channel = _channel()
            options = {"headless": False}
            if channel != "chromium":
                options["channel"] = channel
            browser = await playwright.chromium.launch(**options)
            context = await browser.new_context(storage_state=state, service_workers="block", accept_downloads=False)

            await context.route("**/*", guard)
            await context.route_web_socket("**/*", lambda socket: socket.close())
            page = await context.new_page()
            target = book.reader_link if book is not None else "https://weread.qq.com/"
            progress("请在打开的浏览器扫码登录；所有阅读计时请求均已拦截" if book is None else
                     "正在准备这本书的章节上下文；计时请求只捕获，不发送")
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                # Some aborted reading requests make the reader report a navigation
                # error while leaving a usable page; inspect cookies/capture below.
                if page.is_closed():
                    raise AppError("BROWSER_CLOSED", "浏览器已关闭，未提交阅读请求") from None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cancel_event.is_set():
                    raise AppError("USER_CANCELLED", "已取消浏览器授权/准备")
                if page.is_closed():
                    raise AppError("BROWSER_CLOSED", "浏览器已关闭，未提交阅读请求")
                cookies = await context.cookies("https://weread.qq.com/")
                identity = {item["name"]: item["value"] for item in cookies}
                authenticated = bool(identity.get("wr_skey") and identity.get("wr_vid"))
                if book is None and authenticated:
                    if recovery_identity_hash and hashlib.sha256(identity["wr_vid"].encode()).hexdigest() != recovery_identity_hash:
                        raise AppError("RECOVERY_ACCOUNT_MISMATCH", "扫码账号与未决试验不同，未保存新授权且未清除写入意图")
                    from paperread.web_session import CONFIG_URL, config_token
                    checked = await context.request.get(CONFIG_URL, timeout=15000, max_redirects=0)
                    if checked.status != 200:
                        raise AppError("SESSION_HTTP", "新扫码会话未通过官方校验，未保存授权")
                    config_token(await checked.json())
                    session_store.save(await context.storage_state())
                    if database is not None:
                        with database.transaction():
                            if not recovery_identity_hash:
                                database.conn.execute("DELETE FROM book_routes WHERE account_id=?", (account_id,))
                            database.event("BROWSER_REAUTHORIZED_FOR_RECOVERY" if recovery_identity_hash else
                                "BROWSER_AUTHORIZED_CONTEXTS_INVALIDATED",details={"account_id":account_id})
                    return {"status": "BROWSER_REAUTHORIZED_FOR_RECOVERY" if recovery_identity_hash else "BROWSER_AUTHORIZED",
                            "session_validated": True, "pending_intent_preserved": bool(recovery_identity_hash), "reading_requests_forwarded": 0,
                            "reading_requests_blocked": guard.blocked}
                if book is not None and authenticated and guard.captured is not None:
                    data, headers, route_id = guard.captured
                    value = WriterCredentials(wr_skey=identity["wr_skey"], wr_vid=identity["wr_vid"],
                        captured_cookies=identity,
                        captured_headers={key:value for key,value in headers.items()
                            if key.lower() not in {"cookie", "host", "content-length", "connection"}},
                        captured_payload=data, api_book_id=book.book_id, web_book_id=route_id,
                        context_captured_at=utc_now(), mapping_source="official_reader_link")
                    from paperread.wereadgear_adapter import BoundedWriter
                    BoundedWriter(value).validate_book(book.book_id)
                    if recovery_identity_hash:
                        if value.identity_hash != recovery_identity_hash:
                            raise AppError("RECOVERY_ACCOUNT_MISMATCH", "当前登录账号与未决写入的账号不同，未保存新授权")
                    preserve_pending_route = False
                    if database is not None:
                        # Preparing another book is read-only and uses its own
                        # credential file. Protect the pending book's mapping,
                        # without treating a different book as a different user.
                        preserve_pending_route = database.validate_route_update(account_id,book.book_id,route_id,book.reader_link,value.identity_hash)
                    from paperread.web_session import CONFIG_URL, config_token
                    checked = await context.request.get(CONFIG_URL, timeout=15000, max_redirects=0)
                    if checked.status != 200:
                        raise AppError("SESSION_HTTP", "本书会话未通过官方校验，未保存授权")
                    config_token(await checked.json())
                    CredentialStore(account_id, private_directory, book_id=book.book_id).save(value)
                    session_store.save(await context.storage_state())
                    if database is not None:
                        database.save_route(account_id, book.book_id, route_id, book.reader_link, value.identity_hash)
                        if recovery_identity_hash:
                            database.event("CONTEXT_REAUTHORIZED_FOR_RECOVERY" if preserve_pending_route else
                                "CONTEXT_PREPARED_WITH_PENDING_WRITE", details={"book_id":book.book_id})
                    return {"status":"CONTEXT_READY", "book_id":book.book_id, "book_title":book.title, "web_book_id":route_id,
                            "pending_intent_preserved":bool(recovery_identity_hash),
                            "reading_requests_forwarded":0, "reading_requests_blocked":guard.blocked}
                await asyncio.sleep(0.5)
            raise AppError("BROWSER_TIMEOUT", "等待授权/章节上下文超时；已关闭浏览器且未发送阅读请求")
        finally:
            if browser is not None:
                await browser.close()


def run_authorization(account_id, *, cancel_event, progress, lock_path, **kwargs):
    # Same global writer lock prevents browser context changes during live writing.
    with WriterLock(lock_path):
        expected = recovery_identity(kwargs.get("database"), account_id,
                                     kwargs.get("book"), lock_path.with_suffix(".pending.json"))
        factory = asyncio.ProactorEventLoop if os.name == "nt" else asyncio.new_event_loop
        try:
            with asyncio.Runner(loop_factory=factory) as runner:
                return runner.run(authorize(account_id, cancel_event=cancel_event, progress=progress,
                                            recovery_identity_hash=expected, **kwargs))
        except AppError:
            raise
        except Exception:
            raise AppError("BROWSER_FAILED", "浏览器授权失败；请检查本机 Edge/Chrome 与网络，未输出凭证") from None


def recovery_identity(database, account_id, book, marker_path):
    """An unresolved attempt pins the account, not which book may be prepared."""
    active = database.conn.execute("SELECT attempt_id FROM write_guard").fetchone() if database else None
    if not active and not marker_path.exists():
        return None
    if not database or not active:
        raise AppError("RECONCILE_REQUIRED", "未决意图未与当前数据库匹配，禁止更换授权")
    if marker_path.exists():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("attempt_id") != active[0] or marker.get("database") != str(database.path.resolve()):
            raise AppError("RECONCILE_REQUIRED", "未决意图属于其他数据库，禁止更换授权")
    row = database.conn.execute("""SELECT e.account_id,e.book_id,a.writer_identity_hash FROM sync_attempts s
        JOIN reading_entries e ON e.entry_id=s.entry_id JOIN attestations a ON a.entry_id=e.entry_id
        WHERE s.attempt_id=?""", (active[0],)).fetchone()
    if not row or not row["writer_identity_hash"] or row["account_id"] != account_id:
        raise AppError("RECOVERY_ACCOUNT_MISMATCH", "当前账号与未决写入的原账号不一致，不能切换授权")
    return row["writer_identity_hash"]
