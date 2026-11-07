import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

from paperread.browser_session import recovery_identity
from paperread.credentials import WriterCredentials
from paperread.errors import AppError
from paperread.models import Book
from paperread.web_session import config_token, validate_session


def profile():
    return WriterCredentials(wr_skey="synthetic-current-session",wr_vid="synthetic-current-account",
        captured_headers={"user-agent":"synthetic-browser","referer":"https://weread.qq.com/web/reader/A",
                          "x-wrpa-0":"synthetic-private-proof"}, captured_payload={},
        captured_cookies={"wr_skey":"synthetic-obsolete-session","wr_vid":"synthetic-obsolete-account",
                          "wr_rt":"synthetic-extra-session"})


@pytest.mark.parametrize("code",[-2010,-2012,-2013])
def test_expired_server_session_is_detected_without_reading_post_or_secret_output(code):
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.method=="GET" and request.url.path=="/web/getConfig"
        assert "x-wrpa-0" not in request.headers
        return httpx.Response(200,json={"errCode":code,"errMsg":"synthetic-private-server-text"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AppError,match="SESSION_EXPIRED") as error:
            validate_session(profile(),client=client)
    assert len(calls)==1 and "synthetic-private" not in str(error.value)


def test_real_config_is_private_and_cookie_identity_is_not_overwritten_by_older_capture():
    def handler(request):
        cookie=request.headers["cookie"]
        assert "synthetic-current-session" in cookie and "synthetic-extra-session" in cookie
        assert "synthetic-obsolete" not in cookie
        return httpx.Response(200,json={"token":"synthetic-private-config"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        token=validate_session(profile(),client=client)
    assert token.get_secret_value()=="synthetic-private-config"
    assert "synthetic-private-config" not in repr(token)


@pytest.mark.parametrize("value",[{},[],{"token":""},{"errCode":-1,"token":"synthetic-private-config"}])
def test_missing_or_failed_config_never_falls_back_to_historical_key(value):
    with pytest.raises(AppError): config_token(value)


def test_timeout_during_auth_check_does_not_retry():
    calls=[]
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic-private",request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AppError,match="SESSION_NETWORK"):
            validate_session(profile(),client=client)
    assert len(calls)==1


def test_preparation_requires_original_database_account_but_allows_other_book_and_keeps_intent(tmp_path):
    import sqlite3
    db=SimpleNamespace(path=tmp_path/"ledger.sqlite",conn=sqlite3.connect(":memory:"))
    db.conn.row_factory=sqlite3.Row
    db.conn.executescript("""CREATE TABLE write_guard(attempt_id TEXT);
        CREATE TABLE sync_attempts(attempt_id TEXT,entry_id TEXT);
        CREATE TABLE reading_entries(entry_id TEXT,account_id TEXT,book_id TEXT);
        CREATE TABLE attestations(entry_id TEXT,writer_identity_hash TEXT);
        INSERT INTO write_guard VALUES('attempt-A');
        INSERT INTO sync_attempts VALUES('attempt-A','entry-A');
        INSERT INTO reading_entries VALUES('entry-A','default','10001');
        INSERT INTO attestations VALUES('entry-A','synthetic-original-identity');""")
    marker=tmp_path/"writer.pending.json"
    marker.write_text(json.dumps({"attempt_id":"attempt-A","database":str(db.path.resolve())}))
    try:
        original=marker.read_text()
        assert recovery_identity(db,"default",None,marker)=="synthetic-original-identity"
        assert recovery_identity(db,"default",Book(book_id="10001",title="合成书",author="作者"),marker)=="synthetic-original-identity"
        with pytest.raises(AppError,match="RECOVERY_ACCOUNT_MISMATCH"):
            recovery_identity(db,"other",None,marker)
        assert recovery_identity(db,"default",Book(book_id="10002",title="其他书",author="作者"),marker)=="synthetic-original-identity"
        assert marker.read_text()==original
        marker.write_text(json.dumps({"attempt_id":"attempt-A","database":str(tmp_path/"other.sqlite")}))
        with pytest.raises(AppError,match="RECONCILE_REQUIRED"):
            recovery_identity(db,"default",None,marker)
        assert db.conn.execute("SELECT COUNT(*) FROM write_guard").fetchone()[0]==1
    finally: db.conn.close()
