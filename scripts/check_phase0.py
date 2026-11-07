"""Run offline phase-0 checks and persist only sanitized diagnostic evidence."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

from paperread.redaction import redact

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if not all((ROOT / "vendor" / name / ".git").is_dir()
               for name in ("WeReadGears", "WeChatReading")):
        print("BLOCKED: prepare pinned upstream sources before running offline checks")
        return 1
    commands = [
        ["-m", "pytest", "-q"],
        ["-m", "paperread", "doctor"],
        ["scripts/bootstrap_upstream.py"],
        ["-m", "pip", "check"],
    ]
    evidence = {
        # Shanghai uses UTC+08:00 on the audit date; Windows lacks tzdata here.
        "captured_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "timezone": "Asia/Shanghai", "python_version": sys.version.split()[0],
        "real_account_verification": "NOT_RUN",
        "credentials_loaded_by_application": False, "checks": [],
    }
    failed = False
    for command in commands:
        try:
            result = subprocess.run(
                [sys.executable, *command], cwd=ROOT, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=60,
            )
            check = {
                "command": ".venv\\Scripts\\python " + " ".join(command),
                "exit_code": result.returncode,
                "stdout": redact(result.stdout.strip()),
                "stderr": redact(result.stderr.strip()),
            }
        except (OSError, subprocess.SubprocessError):
            check = {"command": " ".join(command), "exit_code": 1,
                     "stdout": "", "stderr": "CHECK_PROCESS_FAILED"}
        evidence["checks"].append(check)
        print(check["command"] + ": " + str(check["exit_code"]))
        print(check["stdout"])
        failed = failed or check["exit_code"] != 0
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "phase-0-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=True, indent=2) + "\n", encoding="utf-8",
    )
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
