"""Exercise the complete synthetic CLI workflow, never a real account."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    with TemporaryDirectory(prefix="paperread-mock-") as directory:
        base = [sys.executable, "-m", "paperread", "--root", str(ROOT), "--mock", "--db", str(Path(directory)/"demo.sqlite")]
        identifier = str(uuid4())
        env = {**os.environ, "PYTHONIOENCODING":"utf-8"}
        def run(*args):
            result = subprocess.run([*base,*args],cwd=ROOT,capture_output=True,text=True,encoding="utf-8",env=env,timeout=60)
            if result.returncode:
                print("Mock command failed: " + args[0],file=sys.stderr)
                raise RuntimeError("Mock demo failed; no real requests were sent")
            return result.stdout
        run("search","安娜")
        run("bind","mock-anna-A","--confirm")
        run("add","--book-id","mock-anna-A","--date","2026-10-22","--minutes","60","--entry-id",identifier)
        run("baseline",identifier)
        run("attest",identifier,"--app-book-seconds","0","--app-period-seconds","0","--same-account","--no-other-reading")
        run("probe",identifier,"--seconds","60")
        run("verify",identifier,"--app-book-seconds","60","--app-period-seconds","60","--no-other-reading","--same-day-confirmed")
        after_probe=json.loads(run("preview",identifier))
        run("submit",identifier,"--remaining","--no-other-reading","--same-day-confirmed")
        audit=json.loads(run("audit",identifier))
        repeated=json.loads(run("submit",identifier,"--remaining"))
        assert after_probe["remaining_seconds"]==3540
        assert audit["entry"]["status"]=="MOCK_VERIFIED"
        assert audit["entry"]["verified_seconds"]==3600
        assert sum(a["requested_seconds"] for a in audit["attempts"])==3600
        assert repeated["network_writes"]==0
        evidence={"source":"mock","real_account_verification":"NOT_RUN", "captured_at":datetime.now(timezone.utc).isoformat(),
            "entry_id":identifier,"paper_date":"2026-10-22","duration_seconds":3600,
            "probe_verified_seconds":60,"remaining_after_probe":after_probe["remaining_seconds"],
            "final_synthetic_verified_seconds":3600,"attempt_count":len(audit["attempts"]),
            "total_authorized_seconds":sum(a["requested_seconds"] for a in audit["attempts"]),
            "final_status":audit["entry"]["status"],"repeated_submit_network_writes":0}
        (ROOT/"reports/mock-demo.json").write_text(json.dumps(evidence,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(evidence,indent=2))


if __name__=="__main__":
    main()
