import itertools

import httpx
import pytest
from typer.testing import CliRunner

from paperread.cli import app
from paperread.network import offline_client
from paperread.safety import SafetyError, SafetyPolicy


@pytest.mark.parametrize("live,confirm", itertools.product((False, True), repeat=2))
def test_flags_alone_cannot_replace_interactive_confirmation(live, confirm):
    requests = []
    transport = httpx.MockTransport(lambda request: requests.append(request))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(SafetyError):
            SafetyPolicy().require_write(live=live, confirm=confirm)
            client.post("https://weread.qq.com/web/book/read", json={"b": "test-A"})
    assert requests == []


@pytest.mark.parametrize("method,url", [
    ("POST", "https://weread.qq.com/web/book/read"),
    ("POST", "https://i.weread.qq.com/api/agent/gateway"),
    ("GET", "https://weread.qq.com/"),
    ("PUT", "http://127.0.0.1:8000/read"),
])
def test_transport_blocks_all_remote_and_loopback_requests(method, url):
    with offline_client() as client:
        with pytest.raises(SafetyError, match="NETWORK_DISABLED"):
            client.request(method, url)


@pytest.mark.parametrize("section,key,value", [
    ("schedule", "enabled", "true"), ("daemon", "enabled", "true"),
    ("safety", "live_writes_enabled", "true"),
    ("safety", "browser_fallback", "true"),
    ("safety", "automatic_retry", "true"),
    ("safety", "mode", '"live"'), ("schedule", "enabled", '"false"'),
    ("schedule", "enabled", "0"),
])
def test_unsafe_config_fails_closed(root, tmp_path, section, key, value):
    original = (root / "config.toml").read_text(encoding="utf-8")
    before, after = original.split("[" + section + "]", 1)
    current = '"offline"' if key == "mode" else "false"
    after = after.replace(f"{key} = {current}", f"{key} = {value}", 1)
    path = tmp_path / "unsafe.toml"
    path.write_text(before + "[" + section + "]" + after, encoding="utf-8")
    with pytest.raises(SafetyError, match="CONFIG_UNSAFE"):
        SafetyPolicy.load(path)


@pytest.mark.parametrize("contents", ["", "[schedule]\nenabled=false", "invalid TOML", "WEREAD_API_KEY = 'synthetic-never-log-this'\n"])
def test_incomplete_or_malformed_config_has_safe_errors(tmp_path, contents):
    path = tmp_path / "config.toml"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(SafetyError) as exc:
        SafetyPolicy.load(path)
    assert "synthetic-never-log-this" not in str(exc.value)


def test_missing_config_is_blocked(tmp_path):
    with pytest.raises(SafetyError, match="CONFIG_INVALID"):
        SafetyPolicy.load(tmp_path / "absent.toml")


def test_cli_start_and_restart_have_zero_requests_and_no_background_files(root, tmp_path, monkeypatch):
    requests = []

    def spy(self, request):
        requests.append(request)
        raise AssertionError("doctor must not use HTTP")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", spy)
    monkeypatch.setattr(httpx.MockTransport, "handle_request", spy)
    # Presence of a credential must never silently enable live operation.
    monkeypatch.setenv("WEREAD_API_KEY", "synthetic-never-log-this")
    monkeypatch.chdir(tmp_path)
    for _ in range(2):
        result = CliRunner().invoke(app, ["doctor", "--root", str(root)])
        assert result.exit_code == 0, result.output
        assert '"mode": "offline"' in result.output
        assert '"remote_verification": "NOT_RUN"' in result.output
        assert "synthetic-never-log-this" not in result.output
    assert requests == []
    assert list(tmp_path.iterdir()) == []


def test_probe_rejects_invalid_uuid_and_never_defaults_to_live(root):
    result = CliRunner().invoke(app, ["probe", "test-entry", "--live", "--confirm"])
    assert result.exit_code != 0


def test_doctor_rejects_missing_checkout(root, tmp_path):
    for name in ("config.toml", "upstream.lock.json"):
        (tmp_path / name).write_bytes((root / name).read_bytes())
    result = CliRunner().invoke(app, ["doctor", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "MISSING" in result.output


@pytest.mark.parametrize("lock", ['{}', '[]', '{"schema_version":1,"repositories":[]}'])
def test_invalid_source_lock_cannot_report_pass(root, tmp_path, lock):
    (tmp_path / "config.toml").write_bytes((root / "config.toml").read_bytes())
    (tmp_path / "upstream.lock.json").write_text(lock, encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "BLOCKED" in result.output
