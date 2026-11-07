"""Local list management must preserve sync evidence and account boundaries."""

import asyncio
import re

import httpx
import pytest

from paperread.book_match import confirm_book
from paperread.cli import Settings
from paperread.db import Database
from paperread.errors import AppError
from paperread.models import Book, EntryInput
from paperread.web import create_app


def request(app, method, path, **kwargs):
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                base_url="http://127.0.0.1:8765", follow_redirects=True) as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(send())


@pytest.mark.parametrize("mock", [False, True])
def test_remove_restore_preserves_entries_and_requires_csrf(root, tmp_path, mock):
    settings = Settings(root, tmp_path / "books.sqlite", "default", mock, False)
    book_id = "mock-A" if mock else "10001"
    with settings.database() as db:
        db.bind(confirm_book(Book(book_id=book_id, title="可恢复的书", author="作者",
            publisher="出版社", origin="mock" if mock else "official"), confirmed=True))
        entry = db.add(EntryInput(book_id=book_id, reading_date="2026-10-22", duration_seconds=60))
        db.conn.execute("UPDATE reading_entries SET verified_seconds=30 WHERE entry_id=?", (entry["entry_id"],))
        before = db.entry(entry["entry_id"])
    app = create_app(settings)
    try:
        page = request(app, "GET", "/")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        rejected = request(app, "POST", "/action/book-remove", data={"book_id":book_id})
        assert rejected.status_code == 400
        for action, removed in [("book-remove", True), ("book-restore", False)]:
            result = request(app, "POST", "/action/" + action, data={"csrf":token, "book_id":book_id})
            assert result.status_code == 200 and str(result.url).endswith("#book-management")
            picker = re.search(r'<select required id="entry-book".*?</select>', result.text, re.S)[0]
            assert (f'value="{book_id}"' in picker) is not removed
            with settings.database() as db:
                assert db.entry(entry["entry_id"]) == before
                assert db.managed_books("default")[0]["removed"] is removed
                assert db.managed_books("another-account")[0]["removed"] is False
                if removed:
                    with pytest.raises(AppError, match="BOOK_REMOVED"):
                        db.add(EntryInput(book_id=book_id, reading_date="2026-10-23", duration_seconds=60))
        assert app.state.operations.jobs.snapshots() == []
    finally:
        app.state.operations.jobs.shutdown()


def test_completed_filter_does_not_hide_zero_available_reservations(root, tmp_path):
    from paperread.mock_adapter import MockStats
    from paperread.sync_service import SyncService
    from paperread.wereadgear_adapter import MockWriter, WriteResult

    settings = Settings(root, tmp_path / "filter.sqlite", "default", True, False)
    with settings.database() as db:
        db.bind(confirm_book(Book(book_id="mock-A", title="筛选测试", author="作者",
            publisher="出版社", origin="mock"), confirmed=True))
        complete = db.add(EntryInput(book_id="mock-A", reading_date="2026-10-21", duration_seconds=60))
        db.conn.execute("UPDATE reading_entries SET verified_seconds=60,status='MOCK_VERIFIED' WHERE entry_id=?",
                        (complete["entry_id"],))
        pending = db.add(EntryInput(book_id="mock-A", reading_date="2026-10-22", duration_seconds=60))
        stats = MockStats(db)
        writer = MockWriter(stats)
        writer.run_bounded_session = lambda *args, **kwargs: WriteResult("UNCERTAIN", 1, 20, "WRITE_RESPONSE")
        service = SyncService(db, stats, writer, lock_path=tmp_path / "writer.lock")
        service.baseline(pending["entry_id"])
        service.attest(pending["entry_id"], app_book_seconds=0, app_period_seconds=0,
                       same_account=True, no_other_reading=True)
        attempt = service.start(pending["entry_id"], seconds=60)
        service.withdraw_uncertain(pending["entry_id"],
            phrase=f"RESERVE:{attempt['attempt_id']}:60", reserve_confirmed=True)
        assert db.entry(pending["entry_id"])["available_seconds"] == 0
    app = create_app(settings)
    try:
        for query, expected in [("", ["unfinished"]), ("?ledger=completed", ["completed"]),
                                 ("?ledger=all", ["completed", "unfinished"]),
                                 ("?ledger=invalid", ["unfinished"])]:
            page = request(app, "GET", "/" + query)
            rows = re.findall(r'<tr data-ledger-state="([^"]+)"([^>]*)>', page.text)
            assert [state for state, attrs in rows if "hidden" not in attrs] == expected
            assert "待查保留 60 秒" in page.text
    finally:
        app.state.operations.jobs.shutdown()


def test_v2_migration_preserves_book_and_ledger(tmp_path):
    path = tmp_path / "v2.sqlite"
    with Database(path) as db:
        db.bind(confirm_book(Book(book_id="A", title="旧数据库", author="作者", publisher="出版社"), confirmed=True))
        entry = db.add(EntryInput(book_id="A", reading_date="2026-10-22", duration_seconds=60))
        db.conn.execute("DROP TABLE removed_books")
        db.conn.execute("PRAGMA user_version=2")
    with Database(path) as db:
        assert db.entry(entry["entry_id"])["duration_seconds"] == 60
        assert db.managed_books("default")[0]["removed"] is False
        db.set_book_removed("default", "A", removed=True)
    with Database(path) as db:
        assert db.managed_books("default")[0]["removed"] is True


def test_history_is_folded_and_retains_more_than_five_jobs(root, tmp_path, monkeypatch):
    settings = Settings(root, tmp_path / "history.sqlite", "default", False, False)
    app = create_app(settings)
    history = [{"id":str(number), "kind":"context", "state":"DONE", "message":f"历史任务 {number}",
                "created_at":"2026-11-05T00:00:00Z"} for number in range(8)]
    active = {"id":"active", "kind":"context", "state":"RUNNING", "message":"准备中"}
    monkeypatch.setattr(app.state.operations.jobs, "snapshots", lambda: [active, *history])
    try:
        page = request(app, "GET", "/")
        tag, contents = re.search(r'(<details class="job-history"[^>]*>)(.*?)</details>', page.text, re.S).groups()
        assert " open" not in tag
        assert all(f"历史任务 {number}" in contents for number in range(8))
        assert "/jobs/active" not in contents
        assert 'href="/jobs/active"' in page.text
    finally:
        app.state.operations.jobs.shutdown()
