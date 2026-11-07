"""Reproducible integration evidence. No real account actions or credential imports."""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

from paperread.redaction import redact

ROOT=Path(__file__).resolve().parents[1]


def scan(root=ROOT):
    paths=subprocess.run(["git","ls-files","--cached","--others","--exclude-standard","-z"],
                         cwd=root,capture_output=True,check=True).stdout.decode().split("\0")
    patterns={
        "official_api_key_shape":re.compile(r"\bwrk-[A-Za-z0-9_-]{16,}\b"),
        "nonempty_environment_assignment":re.compile(r'''(?m)^[ \t]*(?:WEREAD_API_KEY|wr_skey|wr_vid)[ \t]*=[ \t]*["']?([^\r\n"' \t#]+)'''),
        "serialized_credential_value":re.compile(r'''["'](?:wr_skey|wr_vid|WEREAD_API_KEY|Authorization|Cookie)["'][ \t]*:[ \t]*["']([^"'\r\n]+)["']''',re.I),
    }
    findings=[]
    count=0
    for relative in filter(None,paths):
        path=root/relative
        if not path.is_file(): continue
        raw=path.read_bytes()
        if b"\0" in raw: continue
        count+=1
        contents=raw.decode("utf-8-sig",errors="replace")
        for name,pattern in patterns.items():
            for match in pattern.finditer(contents):
                candidate=match[1] if match.lastindex else match[0]
                # Source expressions such as wr_skey=value[...] contain no secret
                # value. Bare "Bearer " is only a protocol prefix, never a token.
                if name=="nonempty_environment_assignment" and path.suffix==".py" and not re.match(
                    r'''^[ \t]*(?:WEREAD_API_KEY|wr_skey|wr_vid)[ \t]*=[ \t]*["']''',match[0]):
                    continue
                if candidate.strip()=="Bearer":
                    continue
                if any(marker in candidate for marker in ("<",">","$","REDACTED","synthetic-")):
                    continue
                findings.append({"file":relative,"line":contents[:match.start()].count("\n")+1,"detector":name})
    return {"status":"FAIL" if findings else "PASS","checked_text_files":count,
            "scope":"Git-visible project artifacts; ignored credentials/data/vendor/venv never read",
            "findings":findings,"limitations":"Format scan cannot identify arbitrary unmarked secret strings; synthetic canary tests provide additional coverage."}


def main():
    commands=[["-m","pytest","-q"],["-m","compileall","-q","paperread","scripts"],
              ["-m","paperread","doctor"],["-m","pip","check"],["scripts/mock_demo.py"]]
    evidence={"captured_at":datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "evidence_scope":"This local check run only; real account attempts and outcomes are in reports/live-probe.md",
        "python_version":sys.version.split()[0],"real_account_verification":"NOT_RUN",
        "real_reading_seconds_submitted":0,"checks":[]}
    failed=False
    for command in commands:
        result=subprocess.run([sys.executable,*command],cwd=ROOT,capture_output=True,text=True,
            encoding="utf-8",errors="replace",timeout=60,env={**os.environ,"PYTHONIOENCODING":"utf-8"})
        output={"command":".venv\\Scripts\\python "+" ".join(command),"exit_code":result.returncode,
                "stdout":redact(result.stdout.strip()),"stderr":redact(result.stderr.strip())}
        evidence["checks"].append(output)
        print(output["command"]+": "+str(result.returncode))
        print(output["stdout"])
        failed=failed or result.returncode!=0
    audit=scan()
    evidence["sensitive_format_scan"]=audit
    (ROOT/"reports/final-evidence.json").write_text(json.dumps(evidence,ensure_ascii=True,indent=2)+"\n",encoding="utf-8")
    print("Sensitive format scan: "+audit["status"])
    return int(failed or audit["status"]!="PASS")


if __name__=="__main__":
    sys.exit(main())
