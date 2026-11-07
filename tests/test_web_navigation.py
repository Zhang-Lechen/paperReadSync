"""Keep the ledger view across real browser form navigation using offline ASGI routes."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import sys
from urllib.parse import urlparse

import httpx
import pytest

from paperread.book_match import confirm_book
from paperread.cli import Settings
from paperread.models import Book, EntryInput
from paperread.web import create_app


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the installed Windows Edge browser")
def test_return_from_diagnosis_restores_ledger_view(root, tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    settings = Settings(root, tmp_path / "navigation.sqlite", "default", False, False)
    with settings.database() as db:
        entries = []
        for number in range(3):
            book_id = str(10001 + number)
            db.bind(confirm_book(Book(book_id=book_id, title=f"导航合成书 {number}", author="作者",
                publisher="出版社", origin="official"), confirmed=True))
            entry = db.add(EntryInput(book_id=book_id, reading_date="2026-11-04", duration_seconds=3600))
            entries.append(entry["entry_id"])
        db.conn.execute("UPDATE reading_entries SET verified_seconds=3600 WHERE entry_id=?", (entries[1],))

    app = create_app(settings)

    def diagnose(action, values):
        assert action == "diagnose"
        return app.state.operations.jobs.submit("diagnose", lambda event, progress: {
            "status":"READ_ONLY_DIAGNOSIS", "entry_id":values["entry_id"],
            "after_book_seconds":3600, "after_period_seconds":7200,
        })

    monkeypatch.setattr(app.state.operations, "handle", diagnose)
    origin = "http://127.0.0.1:8765"

    def send(request):
        async def execute():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=origin) as client:
                return await client.request(request.method, request.url,
                    content=request.post_data_buffer, headers=request.headers)
        return asyncio.run(execute())

    try:
        with ThreadPoolExecutor(max_workers=1) as executor, playwright.sync_playwright() as engine:
            try:
                browser = engine.chromium.launch(channel="msedge", headless=True)
            except playwright.Error as exc:
                pytest.skip(f"Edge unavailable: {exc}")
            try:
                page = browser.new_page(viewport={"width":1280, "height":800})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def dispatch(route):
                    if urlparse(route.request.url).netloc != "127.0.0.1:8765":
                        route.abort()
                        return
                    response = executor.submit(send, route.request).result(timeout=10)
                    route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

                page.route("**/*", dispatch)
                page.goto(origin)
                page.select_option("#ledger-filter", "all")
                page.locator(f'[data-entry-target="entry-{entries[0]}"] .entry-link').click()
                advanced = page.locator("#advanced-" + entries[0])
                advanced.locator(":scope > summary").click()
                page.locator("#verify-" + entries[0] + " > summary").click()
                page.locator("#remaining-" + entries[0] + " > summary").click()
                diagnostic = advanced.locator('form[action="/action/diagnose"] button')
                diagnostic.scroll_into_view_if_needed()
                before_scroll = page.evaluate("scrollY")
                diagnostic.click()
                page.wait_for_url("**/jobs/*")
                page.get_by_role("link", name="回到阅读账本", exact=True).click()
                page.wait_for_url("**/?ledger=all#entry-*")
                page.wait_for_function("expected => Math.abs(scrollY - expected) < 3", arg=before_scroll)
                assert page.locator("#entry-" + entries[0]).is_visible()
                assert advanced.evaluate("element => element.open")
                assert page.locator("#verify-" + entries[0]).evaluate("element => element.open")
                assert not page.locator("#remaining-" + entries[0]).evaluate("element => element.open")
                assert page.locator("#ledger-filter").input_value() == "all"

                # Browser Back must restore the same view as the result page's return link.
                diagnostic.click()
                page.wait_for_url("**/jobs/*")
                page.go_back()
                assert advanced.evaluate("element => element.open")
                assert page.locator("#entry-" + entries[0]).is_visible()
                page.wait_for_function("expected => Math.abs(scrollY - expected) < 3", arg=before_scroll)

                # Explicit links to a different entry/step override the saved selection.
                page.goto(origin + "/#review-" + entries[1])
                assert page.locator("#entry-" + entries[1]).is_visible()
                assert page.locator("#advanced-" + entries[1]).evaluate("element => element.open")
                page.select_option("#ledger-filter", "unfinished")
                page.goto(origin + "/#entry-" + entries[1])
                assert page.locator("#ledger-filter").input_value() == "completed"
                assert page.locator("#entry-" + entries[1]).is_visible()

                # When an operation completes the selected entry, returning keeps it visible.
                page.select_option("#ledger-filter", "unfinished")
                page.locator(f'[data-entry-target="entry-{entries[0]}"] .entry-link').click()
                diagnostic.click()
                page.wait_for_url("**/jobs/*")
                with settings.database() as db:
                    db.conn.execute("UPDATE reading_entries SET verified_seconds=3600 WHERE entry_id=?", (entries[0],))
                page.get_by_role("link", name="回到阅读账本", exact=True).click()
                assert page.locator("#ledger-filter").input_value() == "completed"
                assert page.locator("#entry-" + entries[0]).is_visible()

                # A fresh tab retains the normal unfinished-only default.
                fresh_page = browser.new_page()
                fresh_page.route("**/*", dispatch)
                fresh_page.goto(origin)
                assert fresh_page.locator("#ledger-filter").input_value() == "unfinished"
                assert fresh_page.locator(".sync-card:visible").count() == 0
                with settings.database() as db:
                    assert db.conn.execute("SELECT COUNT(*) FROM sync_attempts").fetchone()[0] == 0
                assert errors == []
            finally:
                browser.close()
    finally:
        app.state.operations.jobs.shutdown()
