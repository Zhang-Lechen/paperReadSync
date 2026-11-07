import importlib.util
import json
import subprocess
import sys

import pytest

from paperread.upstream import check_upstreams


def test_pinned_sources_match_reviewed_patch(root):
    assert all(item["status"] == "PASS" for item in check_upstreams(root))


def test_quarantine_fails_before_imports_or_network(root, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(root / "vendor" / "WeReadGears"))
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("src", None)
    with pytest.raises(RuntimeError, match="PAPERREAD_UPSTREAM_QUARANTINED"):
        __import__("src.main")
    assert "src.api_reader" not in sys.modules
    assert "src.browser" not in sys.modules
    assert list(tmp_path.iterdir()) == []


def test_direct_upstream_entry_is_blocked_before_dependency_imports(root, tmp_path):
    entry = root / "vendor" / "WeReadGears" / "src" / "main.py"
    result = subprocess.run([sys.executable, str(entry)], cwd=tmp_path,
                            capture_output=True, timeout=15)
    assert result.returncode != 0
    assert b"PAPERREAD_UPSTREAM_QUARANTINED" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_upstream_logger_discards_messages_args_and_tracebacks(root, tmp_path, capsys):
    path = root / "vendor" / "WeReadGears" / "src" / "utils" / "logger.py"
    spec = importlib.util.spec_from_file_location("isolated_upstream_logger", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    logger = module.logger
    logger.setup("audit-test", str(tmp_path / "upstream.log"), "DEBUG")
    for method in ("debug", "info", "warning", "error", "critical", "exception"):
        getattr(logger, method)("wr_skey=%s", "synthetic-never-log-this", exc_info=True)
    logger.logger.error("synthetic-never-log-this")
    captured = capsys.readouterr()
    assert "synthetic-never-log-this" not in captured.out + captured.err
    assert list(tmp_path.iterdir()) == []


def test_upstream_config_defaults_are_disabled(root):
    path = root / "vendor" / "WeReadGears"
    example = (path / "config.example.yaml").read_text(encoding="utf-8")
    defaults = (path / "src" / "config.py").read_text(encoding="utf-8")
    assert "schedule:\n  enabled: false" in example
    assert "daemon:\n  enabled: false" in example
    assert '"schedule": {\n                "enabled": False' in defaults
    assert '"daemon": {\n                "enabled": False' in defaults
    source = (path / "src" / "api_reader.py").read_text(encoding="utf-8")
    assert "[DIAG]" not in source
    assert "[DIAG-RESP]" not in source


def test_bootstrap_is_idempotent(root):
    # This subprocess is only allowed to verify already-present checkouts.
    assert (root / "vendor" / "WeReadGears" / ".git").is_dir()
    assert (root / "vendor" / "WeChatReading" / ".git").is_dir()
    result = subprocess.run([sys.executable, str(root / "scripts" / "bootstrap_upstream.py")],
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.count(b"PASS") == 2


def test_doctor_detects_modified_source(root, monkeypatch):
    import paperread.upstream as upstream
    original = upstream._git

    def changed(path, *args):
        if args[0] == "diff":
            return b"unreviewed modification"
        return original(path, *args)

    monkeypatch.setattr(upstream, "_git", changed)
    assert all(item["status"] == "MISMATCH" for item in check_upstreams(root))


def test_fresh_bootstrap_applies_patch_from_local_repositories_only(root, tmp_path):
    spec = importlib.util.spec_from_file_location("bootstrap", root / "scripts" / "bootstrap_upstream.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lock = json.loads((root / "upstream.lock.json").read_text(encoding="utf-8"))
    for item in lock["repositories"]:
        local_repository = root / "vendor" / item["name"]
        assert (local_repository / ".git").is_dir()
        item["url"] = str(local_repository)
        if item["patch"]:
            destination = tmp_path / item["patch"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((root / item["patch"]).read_bytes())
    manifest = tmp_path / "upstream.lock.json"
    manifest.write_text(json.dumps(lock), encoding="utf-8")
    module.bootstrap(tmp_path)
    assert all(item["status"] == "PASS" for item in check_upstreams(tmp_path))
    module.bootstrap(tmp_path)
    changed = tmp_path / "vendor" / "WeReadGears" / "src" / "__init__.py"
    changed.write_text("# unexpected local edit\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="modifications differ"):
        module.bootstrap(tmp_path)
    assert changed.read_text(encoding="utf-8") == "# unexpected local edit\n"
