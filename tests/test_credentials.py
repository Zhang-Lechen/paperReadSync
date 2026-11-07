import json
import os

import pytest

from paperread.credentials import CredentialStore, WriterCredentials, import_upstream_file
from paperread.errors import AppError


def test_credential_model_repr_and_invalid_import_never_expose_values(tmp_path):
    value=WriterCredentials(wr_skey="synthetic-private-value",wr_vid="synthetic-private-value",
                             captured_headers={"Cookie":"synthetic-private-value"},captured_payload={"ps":"synthetic-private-value"})
    assert "synthetic-private-value" not in repr(value)
    path=tmp_path/"invalid.json"
    path.write_text('{"wr_skey":"synthetic-private-value"}',encoding="utf-8")
    with pytest.raises(AppError) as exc:
        import_upstream_file(path)
    assert "synthetic-private-value" not in str(exc.value)


@pytest.mark.skipif(os.name!="nt", reason="Windows current-user DPAPI")
def test_windows_dpapi_store_roundtrip_with_synthetic_values(tmp_path):
    store=CredentialStore("synthetic-test",directory=tmp_path/"private")
    value=WriterCredentials(wr_skey="synthetic-private-value",wr_vid="synthetic-private-account",
                             captured_headers={},captured_payload={},captured_cookies={"wr_rt":"synthetic-private-extra-session"})
    store.save(value)
    assert b"synthetic-private-value" not in store.path.read_bytes()
    assert store.load().wr_skey.get_secret_value()=="synthetic-private-value"
    assert store.load().captured_cookies["wr_rt"].get_secret_value()=="synthetic-private-extra-session"
    assert b"synthetic-private-extra-session" not in store.path.read_bytes()


@pytest.mark.skipif(os.name!="nt", reason="Windows DPAPI")
def test_encrypted_api_key_and_browser_state_are_isolated_from_real_profile(tmp_path):
    from paperread.credentials import ApiKeyStore,PrivateJsonStore,official_key
    key=ApiKeyStore("default")
    key.save_key("wrk-synthetic-private-value")
    assert b"wrk-synthetic-private-value" not in key.path.read_bytes()
    assert official_key()=="wrk-synthetic-private-value"
    browser=PrivateJsonStore("default","browser")
    browser.save({"cookies":[{"name":"wr_skey","value":"synthetic-private-value"}]})
    assert b"synthetic-private-value" not in browser.path.read_bytes()
    assert browser.load()["cookies"][0]["value"]=="synthetic-private-value"


@pytest.mark.skipif(os.name!="nt", reason="Windows DPAPI")
def test_read_key_status_and_request_resolver_agree_after_reopening(monkeypatch):
    from paperread.credentials import ApiKeyStore,official_key,read_key_status
    assert read_key_status()["available"] is False
    ApiKeyStore().save_key("wrk-synthetic-restart-private-value")
    # Fresh instances and empty environment simulate reopening the application.
    monkeypatch.setenv("WEREAD_API_KEY", "   ")
    assert ApiKeyStore().load_key()=="wrk-synthetic-restart-private-value"
    assert official_key()=="wrk-synthetic-restart-private-value"
    status=read_key_status()
    assert status=={"available":True,"source":"encrypted","error_code":None}
    assert "synthetic-restart-private-value" not in str(status)


def test_corrupt_key_file_is_not_reported_as_connected():
    from paperread.credentials import ApiKeyStore,read_key_status
    store=ApiKeyStore()
    store.directory.mkdir(parents=True)
    store.path.write_bytes(b"synthetic-invalid-encrypted-file")
    assert store.exists()
    assert read_key_status()=={"available":False,"source":None,"error_code":"LOCAL_LOGIN_REQUIRED"}
