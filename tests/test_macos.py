"""macOS integration contracts with an isolated in-memory Keychain API."""
from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from paperread import credentials, macos_keychain
from paperread.credentials import ApiKeyStore, CredentialStore, PrivateJsonStore, WriterCredentials
from paperread.errors import AppError


@pytest.fixture
def keychain(keychain_backend, monkeypatch):
    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    return keychain_backend


def test_keychain_roundtrip_update_isolation_and_no_plaintext_files(keychain, tmp_path):
    key = ApiKeyStore(directory=tmp_path / "profile")
    assert not key.exists()
    key.save_key("wrk-synthetic-private-value")
    assert key.exists()
    assert ApiKeyStore(directory=key.directory).load_key() == "wrk-synthetic-private-value"
    key.save_key("wrk-synthetic-updated-value")
    assert key.load_key() == "wrk-synthetic-updated-value"
    assert not ApiKeyStore("other", key.directory).exists()
    assert not ApiKeyStore(directory=tmp_path / "other").exists()
    browser = PrivateJsonStore("default", "browser", key.directory)
    browser.save({"cookies": [{"value": "synthetic-cookie"}]})
    assert browser.load()["cookies"][0]["value"] == "synthetic-cookie"
    assert key.load_key() == "wrk-synthetic-updated-value"
    assert not list(tmp_path.rglob("*.dpapi"))
    if os.name != "nt":
        assert key.directory.stat().st_mode & 0o777 == 0o700
    assert keychain[2] and keychain[3]


def test_writer_book_mapping_and_legacy_fallback(keychain, tmp_path):
    value = WriterCredentials(wr_skey="synthetic-key", wr_vid="synthetic-account",
                              captured_headers={}, captured_payload={"b": "123"},
                              captured_cookies={"wr_rt": "synthetic-cookie"})
    CredentialStore("test", tmp_path).save(value)
    book = CredentialStore("test", tmp_path, book_id="123")
    assert book.load().captured_cookies["wr_rt"].get_secret_value() == "synthetic-cookie"
    with pytest.raises(AppError, match="准备同步上下文"):
        CredentialStore("test", tmp_path, book_id="other").load()
    book.save(value)
    assert book.load().wr_skey.get_secret_value() == "synthetic-key"


def test_keychain_denied_fails_closed_and_redacts_errors(keychain, tmp_path):
    keychain[1].SecKeychainFindGenericPassword = lambda *args: -25293
    key = ApiKeyStore(directory=tmp_path)
    with pytest.raises(AppError) as error:
        key.save_key("wrk-synthetic-private-value")
    assert error.value.code == "PRIVATE_STORAGE"
    assert "synthetic-private-value" not in str(error.value)
    assert not list(tmp_path.iterdir())
    with pytest.raises(AppError):
        key.exists()


def test_macos_browser_selection(monkeypatch):
    from paperread import browser_session
    monkeypatch.setattr(browser_session.sys, "platform", "darwin")
    monkeypatch.delenv("PAPERREAD_BROWSER_CHANNEL", raising=False)
    monkeypatch.setattr(Path, "is_file", lambda path: "Google Chrome.app" in str(path))
    assert browser_session._channel() == "chrome"
    monkeypatch.setenv("PAPERREAD_BROWSER_CHANNEL", "chromium")
    assert browser_session._channel() == "chromium"
    monkeypatch.delenv("PAPERREAD_BROWSER_CHANNEL")
    monkeypatch.setattr(Path, "is_file", lambda path: False)
    assert browser_session._channel() == "chromium"


def test_macos_application_directory(monkeypatch, tmp_path):
    # The autouse fixture redirects storage; inspect the original implementation
    # in a separate module without touching the user's home directory.
    import importlib.util
    spec = importlib.util.spec_from_file_location("isolated_credentials", credentials.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix", environ={}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert module.app_directory() == tmp_path / "Library/Application Support/PaperReadSync"


def test_posix_launcher_forwards_interrupt_to_isolated_child(monkeypatch):
    import importlib.util
    import signal
    spec = importlib.util.spec_from_file_location("start_app", Path(__file__).parents[1] / "scripts/start_app.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.sys, "argv", ["start_app.py", "--no-browser"])
    checks = iter([False, True])
    monkeypatch.setattr(launcher, "running_here", lambda url: next(checks))
    calls = []
    class Process:
        def poll(self):
            return None
        def wait(self, timeout=None):
            if timeout is None:
                raise KeyboardInterrupt
            return 0
        def send_signal(self, value):
            calls.append(value)
    def launch(arguments, **options):
        assert options["start_new_session"] is True
        assert options["cwd"] == launcher.ROOT
        assert arguments[arguments.index("--root") + 1] == str(launcher.ROOT)
        return Process()
    monkeypatch.setattr(launcher.subprocess, "Popen", launch)
    assert launcher.main() == 0
    assert calls == [signal.SIGINT]
