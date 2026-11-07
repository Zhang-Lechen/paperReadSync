import re
import asyncio

import httpx
import pytest

from paperread.cli import Settings
from paperread.errors import AppError
from paperread.web import create_app


def client(root,tmp_path):
    settings=Settings(root,tmp_path/"mock.sqlite","default",True,False)
    app=create_app(settings)
    class LocalBrowser:
        def request(self, method, path, **kwargs):
            async def execute():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                    base_url="http://127.0.0.1:8765",follow_redirects=True) as browser:
                    return await browser.request(method,path,**kwargs)
            return asyncio.run(execute())

        def get(self,path,**kwargs): return self.request("GET",path,**kwargs)
        def post(self,path,**kwargs): return self.request("POST",path,**kwargs)
    return LocalBrowser(),settings


def csrf_token(response):
    return re.search(r'name="csrf" value="([^"]+)"',response.text)[1]


def test_ui_host_origin_csrf_and_no_automatic_network(root,tmp_path):
    browser,settings=client(root,tmp_path)
    page=browser.get("/")
    assert page.status_code==200
    assert "合成演示" in page.text
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert page.headers["Referrer-Policy"]=="same-origin"
    assert browser.get("/",headers={"Host":"attacker.example"}).status_code==403
    assert browser.post("/action/book",data={"csrf":"bad"}).status_code==400
    assert browser.post("/action/book",headers={"Origin":"https://attacker.example"},data={}).status_code==403
    assert browser.get("/static/app.css").status_code==200


def test_same_origin_native_book_form_returns_to_ledger_with_bound_option(root,tmp_path):
    browser,settings=client(root,tmp_path)
    page=browser.get("/")
    # Chromium uses this policy when serializing Origin for a native form POST.
    # no-referrer previously produced Origin:null and failed before CSRF handling.
    assert page.headers["Referrer-Policy"]=="same-origin"
    result=browser.post("/action/book",headers={"Origin":"http://127.0.0.1:8765"},data={
        "csrf":csrf_token(page),"book_id":"mock-anna-v1","title":"战争与和平",
        "author":"托尔斯泰","translator":"A","publisher":"AAA",
        "corresponding":"yes","confirmed":"yes",
    })
    assert result.status_code==200,result.text
    assert [response.status_code for response in result.history]==[303]
    assert str(result.url)=="http://127.0.0.1:8765/"
    assert '<option value="mock-anna-v1">战争与和平 · A</option>' in result.text
    with settings.database() as db:
        book=db.book("mock-anna-v1")
        assert book.confirmed_by_user and book.corresponding_edition


@pytest.mark.parametrize("origin",["null","https://attacker.example","http://127.0.0.1:8766","http://localhost:8765"])
def test_rejected_origin_cannot_bind_even_with_valid_csrf(root,tmp_path,origin):
    browser,settings=client(root,tmp_path)
    token=csrf_token(browser.get("/"))
    result=browser.post("/action/book",headers={"Origin":origin},data={
        "csrf":token,"book_id":"mock-rejected","title":"示例书","author":"作者",
        "translator":"译者","confirmed":"yes",
    })
    assert result.status_code==403 and result.text=="不允许的来源"
    with settings.database() as db:
        assert db.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]==0


def test_correct_origin_does_not_replace_form_csrf_confirmation(root,tmp_path):
    browser,settings=client(root,tmp_path)
    result=browser.post("/action/book",headers={"Origin":"http://127.0.0.1:8765"},data={
        "csrf":"invalid","book_id":"mock-rejected","title":"示例书","author":"作者",
        "translator":"译者","confirmed":"yes",
    })
    assert result.status_code==400 and "CSRF_INVALID" in result.text
    with settings.database() as db:
        assert db.conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]==0


def test_mock_ui_record_and_synthetic_sync_workflow_escapes_html(root,tmp_path):
    browser,settings=client(root,tmp_path)
    token=csrf_token(browser.get("/"))
    def post(action,**values):
        result=browser.post("/action/"+action,data={"csrf":token,**values})
        assert result.status_code==200,result.text
        return result
    post("book",book_id="mock-A",title="<script>alert(1)</script>",author="测试作者",translator="示例译者",confirmed="yes")
    page=post("entry",book_id="mock-A",date="2026-10-22",minutes="2")
    assert "&lt;script&gt;" in page.text and "<script>alert" not in page.text
    with settings.database() as db:
        identifier=db.entries("default")[0]["entry_id"]
    for action in ("prepare","probe","verify","remaining"):
        post(action,entry_id=identifier)
    with settings.database() as db:
        entry=db.entry(identifier)
        assert entry["status"]=="MOCK_VERIFIED" and entry["verified_seconds"]==120


def test_real_setup_ui_opens_before_first_probe_without_loading_credentials(root,tmp_path):
    settings=Settings(root,tmp_path/"real.sqlite","default",False,False)
    app=create_app(settings)
    assert app is not None
    assert app.state.operations.jobs.snapshots()==[]


def test_ui_cannot_be_bound_to_network_interface(root,tmp_path):
    settings=Settings(root,tmp_path/"mock.sqlite","default",True,False)
    with pytest.raises(AppError,match="LOOPBACK_REQUIRED"):
        create_app(settings,origin="http://0.0.0.0:8765")
