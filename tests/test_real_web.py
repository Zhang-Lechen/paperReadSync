import asyncio
import re
from uuid import uuid4

import httpx
import pytest

from paperread.book_match import confirm_book
from paperread.cli import Settings
from paperread.errors import AppError
from paperread.models import Book,EntryInput
from paperread.web import create_app


def request(app,method,path,**kwargs):
    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://127.0.0.1:8765",follow_redirects=True) as client:
            return await client.request(method,path,**kwargs)
    return asyncio.run(call())


def test_real_ui_exposes_setup_and_explicit_write_controls_without_network(root,tmp_path):
    settings=Settings(root,tmp_path/"real.sqlite","default",False,False)
    with settings.database() as db:
        db.bind(confirm_book(Book(book_id="10001",title="合成书",author="测试作者",publisher="测试出版社",origin="official"),confirmed=True))
        item=db.add(EntryInput(book_id="10001",reading_date="2026-11-04",duration_seconds=3600))
    app=create_app(settings)
    try:
        response=request(app,"GET","/")
        assert response.status_code==200
        for phrase in ("扫码登录微信读书","从微信读书搜索","准备本书上下文","查询微信读书当前时长","先在第 5 步查询时长"):
            assert phrase in response.text
        assert app.state.operations.jobs.snapshots()==[]
    finally: app.state.operations.jobs.shutdown()


@pytest.mark.parametrize("values",[{}, {"live":"yes"},{"confirm":"yes"},{"live":"yes","confirm":"yes","phrase":"10001:60"}])
def test_real_write_rejections_never_enqueue_a_network_job(root,tmp_path,values):
    settings=Settings(root,tmp_path/"real.sqlite","default",False,False)
    with settings.database() as db:
        db.bind(confirm_book(Book(book_id="10001",title="合成书",author="作者",publisher="出版社",origin="official"),confirmed=True))
        entry=db.add(EntryInput(book_id="10001",reading_date="2026-11-04",duration_seconds=3600))
    app=create_app(settings)
    try:
        page=request(app,"GET","/")
        token=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        result=request(app,"POST","/action/probe",data={"csrf":token,"entry_id":entry["entry_id"],**values})
        assert result.status_code==400
        assert app.state.operations.jobs.snapshots()==[]
        with settings.database() as db:
            assert db.conn.execute("SELECT COUNT(*) FROM sync_attempts").fetchone()[0]==0
    finally: app.state.operations.jobs.shutdown()


def test_browser_login_in_tests_is_explicitly_denied_before_browser_launch(root,tmp_path):
    from paperread.browser_session import authorize
    from threading import Event
    with pytest.raises(AppError,match="CI_BROWSER_DENIED"):
        asyncio.run(authorize("default",cancel_event=Event(),progress=lambda text:None))


def test_missing_key_search_returns_to_authorization_and_preserves_query(root,tmp_path):
    settings=Settings(root,tmp_path/"real.sqlite","default",False,False)
    app=create_app(settings)
    try:
        page=request(app,"GET","/")
        assert re.search(r'<button\s+disabled>从微信读书搜索</button>',page.text)
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        response=request(app,"POST","/action/search",data={"csrf":csrf,"query":"十八家诗钞"})
        assert response.status_code==200
        assert "#read-authorization" in str(response.url)
        assert 'value="十八家诗钞"' in response.text
        assert "微信扫码只完成第 2 步登录，不能替代 Key" in response.text
        assert "READ_LOGIN_REQUIRED:" not in response.text
        assert app.state.operations.jobs.snapshots()==[]
    finally: app.state.operations.jobs.shutdown()


@pytest.mark.skipif(__import__('os').name!='nt',reason="Windows DPAPI")
def test_health_and_page_use_actual_key_resolver_without_exposing_key(root,tmp_path):
    from paperread.credentials import ApiKeyStore
    ApiKeyStore().save_key("wrk-synthetic-web-private-value")
    settings=Settings(root,tmp_path/"real.sqlite","default",False,False)
    app=create_app(settings)
    try:
        response=request(app,"GET","/health")
        assert response.status_code==200
        data=response.json()
        assert data["read_key"]["available"] is True and data["key_file_exists"] is True
        page=request(app,"GET","/")
        assert "已保存官方 Key" in page.text
        assert not re.search(r'<button\s+disabled>从微信读书搜索</button>',page.text)
        assert "wrk-synthetic-web-private-value" not in response.text+page.text
    finally: app.state.operations.jobs.shutdown()
