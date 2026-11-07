from threading import Event
import time

from paperread.errors import AppError
from paperread.jobs import JobManager


def finished(manager,identifier):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        result=next(row for row in manager.snapshots() if row["id"]==identifier)
        if result["state"] not in {"QUEUED","RUNNING"}: return result
        time.sleep(.01)
    raise AssertionError("test job did not finish")


def test_job_token_is_idempotent_and_snapshot_never_contains_control_token():
    jobs=JobManager();calls=[]
    try:
        identifier=jobs.submit("test",lambda event,progress:calls.append(1) or {"safe":True},token="synthetic-control-token")
        result=finished(jobs,identifier)
        repeated=jobs.submit("test",lambda event,progress:calls.append(2),token="synthetic-control-token")
        assert repeated==identifier and calls==[1]
        assert "synthetic-control-token" not in str(jobs.snapshots())
        assert result["state"]=="DONE"
    finally: jobs.shutdown()


def test_job_cancellation_and_private_error_are_sanitized():
    jobs=JobManager()
    try:
        def task(event,progress):
            progress("等待测试取消")
            event.wait(2)
            raise AppError("USER_CANCELLED","已取消")
        identifier=jobs.submit("test",task)
        jobs.cancel(identifier)
        assert finished(jobs,identifier)["state"]=="CANCELLED"
        def fails(event,progress): raise RuntimeError("wr_skey=synthetic-private-value")
        identifier=jobs.submit("test",fails)
        failed=finished(jobs,identifier)
        assert failed["state"]=="FAILED"
        assert failed["error_type"]=="RuntimeError"
        assert failed["error_location"].startswith("jobs.py:")
        assert "synthetic-private-value" not in str(jobs.snapshots())
    finally: jobs.shutdown()
