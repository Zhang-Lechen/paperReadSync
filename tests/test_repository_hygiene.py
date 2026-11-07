import subprocess


def test_credentials_and_runtime_artifacts_are_ignored(root, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".gitignore").write_bytes((root / ".gitignore").read_bytes())
    ignored = [".env", ".env.local", "data/ledger.sqlite", "shared/credentials/u/default.json",
               "logs/debug.txt", "log/weread.txt", "vendor/WeReadGears/src/main.py",
               ".venv/pyvenv.cfg", "paperread/__pycache__/cli.pyc"]
    result = subprocess.run(
        ["git", "-C", str(tmp_path), "check-ignore", "--stdin", "-z"],
        input=("\0".join(ignored) + "\0").encode(), capture_output=True, check=True,
    )
    assert result.stdout.decode().split("\0")[:-1] == ignored
    for visible in (".env.example", "paperread/cli.py", "reports/phase-0.md"):
        result = subprocess.run(["git", "-C", str(tmp_path), "check-ignore", visible],
                                capture_output=True)
        assert result.returncode == 1
