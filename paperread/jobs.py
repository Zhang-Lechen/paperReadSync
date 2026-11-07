"""Explicit, bounded user jobs; no scheduler, no automatic retries or startup jobs."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
import traceback
from uuid import uuid4

from paperread.errors import AppError
from paperread.models import utc_now


class JobManager:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="paperread-user-job")
        self.lock = Lock()
        self.jobs = {}
        self.closed = False

    def submit(self, kind, function, *, token=None):
        with self.lock:
            if self.closed:
                raise AppError("APP_STOPPING", "应用正在停止")
            if token:
                for identifier, job in self.jobs.items():
                    if job.get("token") == token:
                        return identifier  # Repeat form submission is not a second job.
            if any(job["state"] in {"QUEUED", "RUNNING"} for job in self.jobs.values()):
                raise AppError("JOB_BUSY", "已有操作进行中，请等待或取消")
            if len(self.jobs) > 200:
                terminal = [key for key,value in self.jobs.items() if value["state"] not in {"QUEUED","RUNNING"}]
                for key in terminal[:100]:
                    del self.jobs[key]
            identifier = str(uuid4())
            job = {"id":identifier,"kind":kind,"state":"QUEUED","message":"等待执行", "created_at":utc_now(),
                   "result":None,"error_code":None,"cancel_event":Event(),"token":token}
            self.jobs[identifier] = job
        self.executor.submit(self._run, identifier, function)
        return identifier

    def _run(self, identifier, function):
        def progress(message):
            with self.lock:
                self.jobs[identifier]["message"] = message
        with self.lock:
            self.jobs[identifier]["state"] = "RUNNING"
            event = self.jobs[identifier]["cancel_event"]
        try:
            if event.is_set():
                raise AppError("USER_CANCELLED", "已取消，未开始操作")
            result = function(event, progress)
            message = "操作已结束，请检查结果"
            if isinstance(result,dict) and result.get("batch_status"):
                labels={"COMPLETED":"本次批量已全部完成","STOPPED":"同步已中途停止","CANCELLED":"同步已取消"}
                message=(labels[result["batch_status"]]+f"；本次已核验 {result['completed_this_job_seconds']} 秒 / "
                         f"批准 {result['approved_job_seconds']} 秒，尚未核验 {result['unverified_this_job_seconds']} 秒")
            with self.lock:
                self.jobs[identifier].update(state="DONE",message=message,result=result)
        except AppError as exc:
            with self.lock:
                self.jobs[identifier].update(state="CANCELLED" if exc.code=="USER_CANCELLED" else "FAILED",
                                            message=exc.message,error_code=exc.code)
        except Exception as exc:
            # Store only exception metadata, never exception messages or local values.
            source = next((frame for frame in reversed(traceback.extract_tb(exc.__traceback__))
                           if Path(frame.filename).resolve().parent == Path(__file__).resolve().parent), None)
            location = f"{Path(source.filename).name}:{source.lineno}" if source else None
            with self.lock:
                self.jobs[identifier].update(state="FAILED",message=f"程序内部异常（{type(exc).__name__}），详情中可查看发生位置。",
                                            error_code="JOB_FAILED",error_type=type(exc).__name__,error_location=location)

    def snapshots(self):
        with self.lock:
            return [{key:value for key,value in job.items() if key not in {"cancel_event","token"}}
                    for job in reversed(list(self.jobs.values()))]

    def by_token(self, token):
        with self.lock:
            return next((key for key,value in self.jobs.items() if value.get("token")==token),None)

    def cancel(self, identifier):
        with self.lock:
            if identifier not in self.jobs:
                raise AppError("JOB_NOT_FOUND", "未找到该操作")
            self.jobs[identifier]["cancel_event"].set()
            self.jobs[identifier]["message"] = "正在停止，已发出的请求仍需核验"

    def shutdown(self):
        with self.lock:
            self.closed = True
            for job in self.jobs.values():
                job["cancel_event"].set()
        self.executor.shutdown(wait=False, cancel_futures=True)
