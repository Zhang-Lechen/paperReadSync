import asyncio
import json

import httpx
import pytest

from paperread.credentials import CredentialStore, PrivateJsonStore, WriterCredentials
from paperread.errors import AppError
from paperread.session_manager import SessionManager, browser_cookies


def profile(key="synthetic-old"):
    return WriterCredentials(wr_skey=key, wr_vid="synthetic-account", captured_headers={},
        captured_payload={"b":"synthetic-route"}, api_book_id="123", web_book_id="synthetic-route")


def state(key="synthetic-old", identity="synthetic-account"):
    return {"origins":[{"origin":"https://weread.qq.com", "localStorage":[]}], "cookies":[
        {"name":name,"value":value,"domain":".weread.qq.com","path":"/", "expires":1,
         "httpOnly":True,"secure":True,"sameSite":"Lax"}
        for name,value in (("wr_skey",key),("wr_vid",identity),("wr_rt","synthetic-refresh"))]}


@pytest.fixture
def manager(tmp_path):
    value = SessionManager("default", directory=tmp_path, book_id="123")
    value.browser.save(state())
    value.book.save(profile())
    return value


def response(request, *, valid=False):
    if request.method == "GET":
        assert request.url.path == "/web/getConfig"
        if valid or "synthetic-new" in request.headers["cookie"]:
            return httpx.Response(200, json={"token":"synthetic-token"})
        return httpx.Response(200, json={"errCode":-2012})
    assert request.url.path == "/web/login/renewal"
    assert json.loads(request.content) == {"rq":"%2Fweb%2Fbook%2Fread", "ql":False}
    return httpx.Response(200, json={"succ":1}, headers=[
        ("set-cookie","wr_skey=synthetic-new; Path=/; Secure; HttpOnly"),
        ("set-cookie","wr_rt=synthetic-new-refresh; Path=/; Max-Age=7200; Secure")])


def test_valid_session_only_checks_once_without_rewriting_stores(manager):
    before = [store.path.read_bytes() for store in (manager.book, manager.browser)]
    calls = []
    def handler(request):
        calls.append(request)
        return response(request, valid=True)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        updated, token, status = manager.ensure(profile(), client=client)
    assert status == "VALID" and len(calls) == 1 and calls[0].method == "GET"
    assert before == [store.path.read_bytes() for store in (manager.book, manager.browser)]


def test_expired_session_renews_once_validates_and_survives_new_manager(manager):
    calls = []
    def handler(request):
        calls.append((request.method,request.url.path))
        return response(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        updated, token, status = manager.ensure(profile(), client=client)
        assert SessionManager("default", directory=manager.book.directory, book_id="123").ensure_browser(client=client) == "VALID"
    assert status == "RENEWED" and updated.identity_hash == profile().identity_hash
    assert updated.captured_payload == profile().captured_payload
    assert updated.web_book_id == profile().web_book_id
    assert manager.book.load().wr_skey.get_secret_value() == "synthetic-new"
    saved = manager.browser.load()
    assert saved["origins"] == state()["origins"]
    assert browser_cookies(saved)["wr_rt"] == "synthetic-new-refresh"
    assert next(item for item in saved["cookies"] if item["name"] == "wr_skey")["expires"] == -1
    assert calls == [("GET","/web/getConfig"),("GET","/web/getConfig"),
        ("POST","/web/login/renewal"),("GET","/web/getConfig"),("GET","/web/getConfig")]
    for store in (manager.book,manager.browser):
        assert b"synthetic-new" not in store.path.read_bytes()
    assert "synthetic-token" not in repr(token) and "synthetic-new" not in repr(updated)


def test_other_book_reuses_newer_shared_auth_without_second_renewal(manager):
    manager.browser.save(state("synthetic-new"))
    calls=[]
    def handler(request):
        calls.append(request.method)
        return response(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        updated, _, status = manager.ensure(profile(),client=client)
    assert status == "REUSED" and calls == ["GET","GET"]
    assert manager.book.load().wr_skey.get_secret_value() == "synthetic-new"


@pytest.mark.parametrize("kind",["account", "foreign-domain", "missing", "revalidation", "network", "rate", "refused", "redirect", "conflict"])
def test_failed_renewal_never_persists_or_sends_reading_request(manager,kind):
    before = [store.path.read_bytes() for store in (manager.book,manager.browser)]
    calls=[]
    def handler(request):
        calls.append(request)
        if request.method == "GET":
            if kind == "revalidation":
                return httpx.Response(200,json={"errCode":-2012})
            return response(request)
        if kind == "network":
            raise httpx.ReadTimeout("synthetic-private",request=request)
        if kind in {"rate","refused","redirect"}:
            return httpx.Response({"rate":429,"refused":403,"redirect":302}[kind])
        cookies = ["wr_skey=synthetic-new; Path=/"]
        if kind == "account": cookies.append("wr_vid=synthetic-other-account; Path=/")
        if kind == "foreign-domain": cookies = ["wr_skey=synthetic-new; Domain=evil.example; Path=/"]
        if kind == "missing": cookies = []
        if kind == "conflict": cookies.append("wr_skey=synthetic-conflict; Domain=.weread.qq.com; Path=/")
        return httpx.Response(200,json={},headers=[("set-cookie",item) for item in cookies])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AppError) as error:
            manager.ensure(profile(),client=client)
    assert "synthetic-" not in str(error.value)
    assert sum(item.method == "POST" for item in calls) == 1
    assert all(item.url.path in {"/web/getConfig","/web/login/renewal"} for item in calls)
    assert before == [store.path.read_bytes() for store in (manager.book,manager.browser)]


def test_different_shared_account_stops_before_network(manager):
    manager.browser.save(state(identity="synthetic-other"))
    with httpx.Client(transport=httpx.MockTransport(lambda r:pytest.fail("No network allowed"))) as client:
        with pytest.raises(AppError,match="SESSION_ACCOUNT_MISMATCH"):
            manager.ensure(profile(),client=client)
        with pytest.raises(AppError,match="RECOVERY_ACCOUNT_MISMATCH"):
            manager.ensure_browser(expected_identity=profile().identity_hash,client=client)


@pytest.mark.parametrize("status",[403,429,500])
def test_validation_refusal_does_not_trigger_renewal(manager,status):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AppError): manager.ensure(profile(),client=client)
    assert len(calls) == 1 and calls[0].method == "GET"


def test_writer_preflight_uses_renewed_credentials_and_injected_transport(manager):
    from paperread.wereadgear_adapter import BoundedWriter
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        writer=BoundedWriter(profile(),client=client)
        writer.session_manager=manager
        writer.validate_session()
        assert writer.credentials.wr_skey.get_secret_value() == "synthetic-new"
        assert writer.identity_hash == profile().identity_hash
        assert writer._session_token.get_secret_value() == "synthetic-token"


def test_startup_checks_auth_only_and_preserves_ledger(root,tmp_path,monkeypatch):
    from paperread.cli import Settings
    from paperread.web_operations import WebOperations
    from paperread.credentials import app_directory
    monkeypatch.setattr("paperread.web_operations.app_directory", app_directory)
    settings=Settings(root,tmp_path/"ledger.sqlite","default",False,False)
    with settings.database() as db:
        before = "\n".join(db.conn.iterdump())
    store=PrivateJsonStore("default","browser")
    store.save(state())
    calls=[]
    original=SessionManager.ensure_browser
    def handler(request):
        calls.append(request)
        return response(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(SessionManager,"ensure_browser",lambda self,**kwargs:original(self,client=client,**kwargs))
        operations=WebOperations(settings)
        operations.maintain_login()
        assert operations.session_status == "登录已自动续期"
        with settings.database() as db:
            assert "\n".join(db.conn.iterdump()) == before
        assert not (app_directory()/"writer.pending.json").exists()
        operations.jobs.shutdown()
    assert len(calls) == 3 and sum(item.method == "POST" for item in calls) == 1


def test_busy_startup_skips_network(root,tmp_path,monkeypatch):
    from paperread.cli import Settings
    from paperread.web_operations import WebOperations
    from paperread.credentials import app_directory
    from paperread.file_lock import WriterLock
    monkeypatch.setattr("paperread.web_operations.app_directory", app_directory)
    PrivateJsonStore("default","browser").save(state())
    monkeypatch.setattr(SessionManager,"ensure_browser",lambda *a,**k:pytest.fail("Busy check must not call network"))
    operations=WebOperations(Settings(root,tmp_path/"ledger.sqlite","default",False,False))
    with WriterLock(app_directory()/"writer.lock"):
        operations.maintain_login()
    assert "操作进行中" in operations.session_status
    operations.jobs.shutdown()


def test_lifespan_runs_one_check_without_blocking_startup(root,tmp_path,monkeypatch):
    from paperread.cli import Settings
    from paperread.web import create_app
    from paperread.web_operations import WebOperations
    settings=Settings(root,tmp_path/"mock.sqlite","default",True,False)
    operations=WebOperations(settings)
    calls=[]
    monkeypatch.setattr(operations,"maintain_login",lambda:calls.append("check"))
    app=create_app(settings,operations=operations)
    async def run():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
    asyncio.run(run())
    assert calls == ["check"]
