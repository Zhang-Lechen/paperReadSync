import json
from uuid import uuid4

from typer.testing import CliRunner

from paperread.cli import app


def test_cli_synthetic_probe_verify_and_remaining(root,tmp_path):
    runner=CliRunner()
    base=["--root",str(root),"--db",str(tmp_path/"mock.sqlite"),"--mock"]
    entry_id=str(uuid4())
    commands=[
        ["search","安娜"],
        ["bind","mock-anna-A","--confirm"],
        ["add","--book-id","mock-anna-A","--date","2026-10-22","--minutes","60","--entry-id",entry_id],
        ["baseline",entry_id],
        ["attest",entry_id,"--app-book-seconds","0","--app-period-seconds","0","--same-account","--no-other-reading"],
        ["probe",entry_id,"--seconds","60"],
        ["verify",entry_id,"--app-book-seconds","60","--app-period-seconds","60","--no-other-reading","--same-day-confirmed"],
    ]
    for command in commands:
        result=runner.invoke(app,base+command)
        assert result.exit_code==0,(command,result.output,result.exception)
    preview=runner.invoke(app,base+["preview",entry_id])
    assert json.loads(preview.output)["remaining_seconds"]==3540
    result=runner.invoke(app,base+["submit",entry_id,"--remaining","--no-other-reading","--same-day-confirmed"])
    assert result.exit_code==0,result.output
    audit=runner.invoke(app,base+["audit",entry_id])
    value=json.loads(audit.output)
    assert value["entry"]["status"]=="MOCK_VERIFIED"
    assert value["entry"]["verified_seconds"]==3600
    assert len(value["attempts"])==60
    assert sum(attempt["requested_seconds"] for attempt in value["attempts"])==3600
    result=runner.invoke(app,base+["submit",entry_id,"--remaining"])
    assert result.exit_code==0 and json.loads(result.output)["network_writes"]==0


def test_live_commands_without_both_flags_send_zero_requests(root,tmp_path,monkeypatch):
    import httpx
    calls=[]
    monkeypatch.setattr(httpx.HTTPTransport,"handle_request",lambda self,request:calls.append(request))
    for suffix in ([],["--live"],["--confirm"],["--live","--confirm"]):
        result=CliRunner().invoke(app,["--root",str(root),"--db",str(tmp_path/"real.sqlite"),
                                    "probe",str(uuid4()),*suffix])
        assert result.exit_code!=0
    assert calls==[]
