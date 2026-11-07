import json
from uuid import uuid4

from typer.testing import CliRunner

from paperread.cli import app


def test_full_offline_ledger_workflow_and_export(root, tmp_path):
    args = ["--root", str(root), "--db", str(tmp_path / "ledger.sqlite")]
    runner = CliRunner()
    bound = runner.invoke(app, args + ["bind", "A", "--title", "安娜·卡列尼娜", "--author", "托尔斯泰",
                                       "--translator", "译者 A", "--confirm"])
    assert bound.exit_code == 0, bound.output
    entry_id = str(uuid4())
    added = runner.invoke(app, args + ["add", "--book-id", "A", "--date", "2026-10-22", "--minutes", "60", "--entry-id", entry_id])
    assert added.exit_code == 0, added.output
    assert json.loads(added.output)["duration_seconds"] == 3600
    listed = runner.invoke(app, args + ["list"])
    assert len(json.loads(listed.output)) == 1
    preview = runner.invoke(app, args + ["preview", entry_id])
    assert json.loads(preview.output)["remaining_seconds"] == 3600
    output = tmp_path / "export.json"
    result = runner.invoke(app, args + ["export", "--format", "json", "--output", str(output)])
    assert result.exit_code == 0
    result = runner.invoke(app, args + ["import", str(output)])
    assert result.exit_code == 0, result.output
    assert len(json.loads(runner.invoke(app, args + ["list"]).output)) == 1
    csv = runner.invoke(app, args + ["export"])
    assert "verified_seconds" in csv.output


def test_multiple_mock_candidates_are_not_automatically_bound(root, tmp_path):
    args = ["--root", str(root), "--db", str(tmp_path / "mock.sqlite"), "--mock"]
    runner = CliRunner()
    result = runner.invoke(app, args + ["search", "安娜"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["candidates"]) == 2
    assert payload["selection"] == "NONE"
    result = runner.invoke(app, args + ["add", "--book-id", "mock-anna-A", "--date", "2026-10-22", "--minutes", "60"])
    assert result.exit_code == 1
    assert "BOOK_UNBOUND" in result.output


def test_unexpected_cli_error_does_not_dump_private_message(root,monkeypatch):
    from paperread.cli import Settings
    def failed(self):
        raise RuntimeError("wr_skey=synthetic-private-value")
    monkeypatch.setattr(Settings,"database",failed)
    result=CliRunner().invoke(app,["--root",str(root),"list"])
    assert result.exit_code==1 and "INTERNAL_ERROR" in result.output
    assert "synthetic-private-value" not in result.output
