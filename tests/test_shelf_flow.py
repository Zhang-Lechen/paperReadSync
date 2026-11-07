import json
import re

import httpx
import pytest

from paperread.cli import Settings
from paperread.credentials import CredentialStore, WriterCredentials
from paperread.errors import AppError
from paperread.models import Book
from paperread.official_stats import OfficialStats
from paperread.web import create_app
from paperread.wereadgear_adapter import WriteResult
from test_direct_sync import approval
from test_official_stats import current_base
from test_real_web_flow import send, wait_job


@pytest.fixture
def shelf_app(root, tmp_path, monkeypatch):
    state = {"books": [{"bookId": "CB_imported_1", "title": "导入书 <测试>", "author": "",
                        "readerLink": "https://weread.qq.com/web/reader/importedRoute"}],
             "book_seconds": 100, "total_seconds": 200, "calls": [], "writes": [], "prepared": []}

    def gateway(request):
        body = json.loads(request.content)
        state["calls"].append(body["api_name"])
        if body["api_name"] == "/shelf/sync":
            return httpx.Response(200, json={"books": state["books"]})
        if body["api_name"] == "/book/getprogress":
            return httpx.Response(200, json={"bookId": body["bookId"],
                "book": {"readingTime": state["book_seconds"]}})
        if body["api_name"] == "/readdata/detail":
            return httpx.Response(200, json={"baseTime": current_base(), "totalReadTime": state["total_seconds"]})
        pytest.fail("Unexpected API " + body["api_name"])

    stats = OfficialStats(root=root, enabled=True, account_id="default", key="synthetic-key",
        client=httpx.Client(transport=httpx.MockTransport(gateway)))
    monkeypatch.setattr(Settings, "stats", lambda *args, **kwargs: stats)
    import paperread.browser_session as browser
    def prepare(account_id, **kwargs):
        state["prepared"].append(kwargs["book"])
        return {"status": "SYNTHETIC_CONTEXT"}
    monkeypatch.setattr(browser, "run_authorization", prepare)

    import paperread.wereadgear_adapter as adapters
    profile = WriterCredentials(wr_skey="synthetic-session", wr_vid="synthetic-account",
        captured_headers={}, captured_payload={})
    monkeypatch.setattr(CredentialStore, "load", lambda self: profile)
    class Writer:
        source = "official"
        def __init__(self, credentials):
            self.identity_hash = credentials.identity_hash
        def validate_book(self, book_id):
            assert book_id == "CB_imported_1"
        def run_bounded_session(self, book_id, seconds, **kwargs):
            self.validate_book(book_id)
            state["writes"].append(seconds)
            state["book_seconds"] += seconds
            state["total_seconds"] += seconds
            return WriteResult("RESPONSE_RECEIVED", 3, seconds)
    def flags(*, live, confirm):
        if not live or not confirm:
            raise AppError("WRITE_DENIED", "Explicit flags required")
    monkeypatch.setattr(adapters, "BoundedWriter", Writer)
    monkeypatch.setattr(adapters, "require_live_flags", flags)
    settings = Settings(root, tmp_path / "shelf.sqlite", "default", False, False)
    app = create_app(settings)
    try:
        yield app, settings, state
    finally:
        app.state.operations.jobs.shutdown()
        stats.client.close()


def post(app, action, **values):
    token = re.search(r'name="csrf" value="([^"]+)"', send(app, "GET", "/").text)[1]
    return send(app, "POST", "/action/" + action, data={"csrf": token, **values})


def test_shelf_select_prepare_and_sync_uses_original_imported_book(shelf_app):
    app, settings, state = shelf_app
    assert state["calls"] == []
    page = post(app, "shelf")
    assert page.status_code == 200 and str(page.url).endswith("?shelf=1#book-shelf")
    assert "导入书 &lt;测试&gt;" in page.text and 'name="source" value="shelf"' in page.text
    rejected = post(app, "book", source="shelf", book_id="CB_imported_1", confirmed="yes")
    assert rejected.status_code == 400 and "EDITION_UNKNOWN" in rejected.text
    selected = post(app, "book", source="shelf", book_id="CB_imported_1", confirmed="yes", corresponding="yes")
    assert selected.status_code == 200
    with settings.database() as db:
        book = db.book("CB_imported_1")
        assert book.discovery_source == "shelf" and book.confirmed_by_user
    post(app, "entry", book_id=book.book_id, date="2026-11-04", minutes="2")
    wait_job(app, post(app, "context", book_id=book.book_id))
    assert state["prepared"] == [book.model_copy(update={"confirmed_by_user": False,
        "confirmed_at": None, "corresponding_edition": False})]
    assert state["writes"] == [] and set(state["calls"]) == {"/shelf/sync"}
    with settings.database() as db:
        entry_id = db.entries("default")[0]["entry_id"]
    result = wait_job(app, send(app, "POST", "/action/sync", data=approval(app, entry_id)))
    assert result["result"]["verified_seconds"] == 120
    assert state["writes"] == [60, 60]
    assert state["book_seconds"] == 220 and state["total_seconds"] == 320
    with settings.database() as db:
        assert db.entry(entry_id)["status"] == "VERIFIED"
        assert not db.conn.execute("SELECT 1 FROM write_guard").fetchone()


@pytest.mark.parametrize("link", ["", "weixin://reader/imported", "https://evil.test/web/reader/A"])
def test_unsupported_shelf_reader_stops_before_browser_or_write(shelf_app, link):
    app, settings, state = shelf_app
    state["books"][0]["readerLink"] = link
    post(app, "book", source="shelf", book_id="CB_imported_1", confirmed="yes", corresponding="yes")
    response = post(app, "context", book_id="CB_imported_1")
    identifier = str(response.url).rsplit("/", 1)[-1]
    app.state.operations.jobs.executor.shutdown(wait=True)
    job = next(job for job in app.state.operations.jobs.snapshots() if job["id"] == identifier)
    assert job["state"] == "FAILED" and job["error_code"] == "BOOK_WEB_UNAVAILABLE"
    assert state["prepared"] == [] and state["writes"] == []


def test_refresh_filter_empty_and_account_isolation(shelf_app):
    app, settings, state = shelf_app
    state["books"] = [{"bookId": f"CB_{number}", "title": f"书架书 {number}", "author": "作者"}
                       for number in range(40)]
    post(app, "shelf")
    filtered = send(app, "GET", "/?shelf=1&shelf_query=书架书%2039")
    assert 'name="book_id" value="CB_39"' in filtered.text
    assert 'name="book_id" value="CB_0"' not in filtered.text
    assert "没有匹配书名" in send(app, "GET", "/?shelf=1&shelf_query=missing").text
    with settings.database() as db:
        assert len(db.shelf_candidates("default")) == 40
        assert db.shelf_candidates("other") is None
        db.cache_shelf("other", [Book(book_id="other", title="其他账号书", author="作者")])
    state["books"] = []
    assert "当前账号书架未返回可选书籍" in post(app, "shelf").text
    with settings.database() as db:
        assert db.shelf_candidates("default") == []
        assert len(db.shelf_candidates("other")) == 1
    state["books"] = None
    assert post(app, "shelf").status_code == 400
    with settings.database() as db:
        assert db.shelf_candidates("default") == []


def test_removed_shelf_book_cannot_be_bound_from_stale_cache(shelf_app):
    app, settings, state = shelf_app
    post(app, "shelf")
    state["books"] = []
    response = post(app, "book", source="shelf", book_id="CB_imported_1", confirmed="yes", corresponding="yes")
    assert response.status_code == 400 and "BOOK_NOT_FOUND" in response.text
    with settings.database() as db:
        assert db.managed_books("default") == []
