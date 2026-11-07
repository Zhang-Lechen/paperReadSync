"""Loopback UI with explicit local approval and the shared bounded-write service."""

from datetime import datetime
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from paperread.book_match import confirm_book
from paperread.errors import AppError
from paperread.models import Book, EntryInput

HERE = Path(__file__).resolve().parent


def require_real_ui_gate(settings):
    # Setup/search/local ledger must be usable BEFORE the first live probe.
    # The actual live gates belong to SyncService and the per-request confirmation.
    with settings.database():
        pass


def create_app(settings, *, origin="http://127.0.0.1:8765", operations=None):
    require_real_ui_gate(settings)
    parsed = urlparse(origin)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise AppError("LOOPBACK_REQUIRED", "Web 只允许监听 127.0.0.1")
    from paperread.web_operations import WebOperations
    operations = operations or WebOperations(settings)

    @asynccontextmanager
    async def lifespan(app):
        upkeep = asyncio.create_task(run_in_threadpool(operations.maintain_login))
        try:
            yield
        finally:
            await upkeep
            operations.jobs.shutdown()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, debug=False, lifespan=lifespan)
    app.state.operations = operations
    csrf = secrets.token_urlsafe(32)
    templates = Jinja2Templates(directory=str(HERE/"templates"))

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        if request.headers.get("host") != parsed.netloc:
            return HTMLResponse("不允许的主机",status_code=403)
        if request.headers.get("origin") not in (None,origin):
            return HTMLResponse("不允许的来源",status_code=403)
        response = await call_next(request)
        # no-referrer turns native POST navigation's Origin into "null" in
        # Chromium. Preserve same-origin form submissions while still withholding
        # referrers from other sites; never allow an opaque/null Origin instead.
        response.headers.update({"Cache-Control":"no-store","Referrer-Policy":"same-origin",
            "X-Content-Type-Options":"nosniff", "Content-Security-Policy":
            "default-src 'none'; style-src 'self'; script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    @app.get("/static/app.css")
    def stylesheet():
        return FileResponse(HERE/"static/app.css",media_type="text/css")

    @app.get("/static/app.js")
    def interface_script():
        return FileResponse(HERE/"static/app.js",media_type="application/javascript")

    @app.get("/")
    def index(request: Request):
        from zoneinfo import ZoneInfo
        with settings.database() as db:
            managed_books = db.managed_books(settings.account)
            books = [item["book"] for item in managed_books if not item["removed"]]
            records = []
            for row in db.entries(settings.account):
                book = db.book(row["book_id"])
                data = {**row,"title":book.title,"translator":book.translator,"publisher":book.publisher,
                        "remaining":row["available_seconds"],
                        "completed":row["verified_seconds"] >= row["duration_seconds"]}
                if not settings.mock:
                    from paperread.credentials import CredentialStore
                    data["measurement_changed"] = False
                    data["write_pending"] = bool(db.conn.execute("SELECT 1 FROM write_guard").fetchone())
                    pending = db.conn.execute("""SELECT s.attempt_id,s.requested_seconds FROM sync_attempts s
                        JOIN write_guard w ON w.attempt_id=s.attempt_id WHERE s.entry_id=? AND s.purpose='probe' AND s.credited_seconds=0""",
                        (row["entry_id"],)).fetchone()
                    data["pending_probe"] = dict(pending) if pending else None
                    detail = db.conn.execute("""SELECT s.requested_seconds,s.error_code,
                        s.before_book_seconds,s.after_book_seconds,s.before_period_seconds,s.after_period_seconds
                        FROM sync_attempts s JOIN write_guard w ON w.attempt_id=s.attempt_id
                        WHERE s.entry_id=?""",(row["entry_id"],)).fetchone()
                    data["pending_detail"] = dict(detail) if detail else None
                    data["context_ready"] = bool(db.route(settings.account,row["book_id"])) and CredentialStore(settings.account,book_id=row["book_id"]).path.is_file()
                    try:
                        data["baseline_id"],data["baseline"] = db.latest_snapshot(row["entry_id"])
                    except AppError:
                        data["baseline_id"] = None
                        data["baseline"] = None
                    review = db.conn.execute("SELECT snapshot_id FROM attestations WHERE entry_id=?",(row["entry_id"],)).fetchone()
                    data["review_ready"] = bool(review and review[0]==data["baseline_id"])
                    data["probe_verified"] = bool(db.conn.execute("SELECT 1 FROM sync_attempts WHERE entry_id=? AND purpose='probe' AND verification_status='VERIFIED' LIMIT 1",(row["entry_id"],)).fetchone())
                    data["current_stats"] = data["baseline"]
                    if data["write_pending"]:
                        observed = db.conn.execute("""SELECT pair_json FROM remote_snapshots WHERE entry_id=?
                            AND role IN ('baseline','diagnostic','observation','after','recovery_observation') ORDER BY rowid DESC LIMIT 1""",
                            (row["entry_id"],)).fetchone()
                        if observed:
                            from paperread.models import StatsPair
                            data["current_stats"] = StatsPair.model_validate_json(observed[0])
                            active_before = db.conn.execute("SELECT s.before_json FROM sync_attempts s JOIN write_guard w ON s.attempt_id=w.attempt_id WHERE s.entry_id=?",(row["entry_id"],)).fetchone()
                            if active_before:
                                from paperread.verifier import book_time_metric
                                data["measurement_changed"] = book_time_metric(StatsPair.model_validate_json(active_before[0])) != book_time_metric(data["current_stats"])
                    data["probe_grant"] = operations.offer(row,"probe")
                    data["remaining_grant"] = operations.offer(row,"remaining")
                    from paperread.sync_service import direct_sync_preview, reserved_retry_preview
                    data["direct_grant"] = operations.offer(row,"sync",preview=direct_sync_preview(db,row))
                    data["retry_grant"] = operations.offer(row,"retry",preview=reserved_retry_preview(db,row)) if row["reserved_seconds"] else None
                records.append(data)
            candidates = [Book.model_validate_json(row[0]) for row in db.conn.execute("SELECT document FROM candidates ORDER BY rowid DESC LIMIT 30")]
            query = db.conn.execute("SELECT value FROM metadata WHERE key='last_search'").fetchone()
            query = query[0] if query else ""
            query = request.query_params.get("query", query)[:500]
            if query:
                candidates = [book for book in candidates if query.casefold() in book.title.casefold()]
            shelf_cache = db.shelf_candidates(settings.account)
            shelf_loaded = shelf_cache is not None
            shelf_books = shelf_cache or []
            shelf_query = request.query_params.get("shelf_query", "")[:500].strip()
            if shelf_query:
                shelf_books = [book for book in shelf_books if shelf_query.casefold() in book.title.casefold()]
        ledger_filter = request.query_params.get("ledger", "unfinished")
        if ledger_filter not in {"unfinished", "completed", "all"}:
            ledger_filter = "unfinished"
        counts = {"all":len(records), "completed":sum(row["completed"] for row in records)}
        counts["unfinished"] = counts["all"] - counts["completed"]
        jobs = operations.jobs.snapshots()
        return templates.TemplateResponse(request=request,name="index.html",context={
            "csrf":csrf,"books":books,"managed_books":managed_books,"records":records,"mock":settings.mock,
            "view_scope":("mock:" if settings.mock else "real:")+settings.account,
            "ledger_filter":ledger_filter,"ledger_counts":counts,
            "today":datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            "connections":operations.connection_status() if not settings.mock else {},
            "key_notice":request.query_params.get("notice") == "read_key_required",
            "job_expired_notice":request.query_params.get("notice") == "job_expired",
            "review_notice": {"review_changed":"服务器读数已更新，请查看本书当前显示的时长，再勾选确认。",
                "review_missing":"请先对照手机核对显示的时长，再勾选确认。"}.get(request.query_params.get("notice")),
            "review_entry":request.query_params.get("entry"),
            "search_open":request.query_params.get("search")=="1",
            "shelf_open":request.query_params.get("shelf")=="1",
            "shelf_books":shelf_books,"shelf_loaded":shelf_loaded,"shelf_query":shelf_query,
            "candidates":candidates,"query":query,"jobs":jobs,
            "active_jobs":[job for job in jobs if job["state"] in {"RUNNING", "QUEUED"}],
            "history_jobs":[job for job in jobs if job["state"] not in {"RUNNING", "QUEUED"}]})

    @app.get("/health")
    def health():
        # Local, no network and no credentials: distinguish stale processes,
        # other account aliases and unreadable storage on future support calls.
        import os
        import sys
        from paperread import __version__
        from paperread.credentials import ApiKeyStore, read_key_status
        return {"status":"ok", "pid":os.getpid(), "software_version":__version__,
            "account":settings.account, "mock":settings.mock, "project_root":str(settings.root),
            "python":sys.executable, "module":str(HERE),
            "read_key":read_key_status(settings.account),
            "key_file_exists":ApiKeyStore(settings.account).exists()}

    @app.get("/jobs/{identifier}")
    def job_view(identifier: str, request: Request):
        job = next((item for item in operations.jobs.snapshots() if item["id"]==identifier),None)
        if not job:
            return RedirectResponse("/?notice=job_expired",status_code=303)
        return templates.TemplateResponse(request=request,name="job.html",context={
            "job":job,"csrf":csrf,"active":job["state"] in {"RUNNING","QUEUED"}})

    async def form(request):
        if request.headers.get("content-type","").split(";")[0] != "application/x-www-form-urlencoded":
            raise AppError("FORM_INVALID", "仅支持本地表单")
        body=await request.body()
        if len(body)>20000:
            raise AppError("FORM_INVALID", "表单过大")
        try:
            values={k:v[0] for k,v in parse_qs(body.decode("utf-8"),max_num_fields=25).items() if len(v)==1}
        except ValueError:
            raise AppError("FORM_INVALID", "表单无效") from None
        if not secrets.compare_digest(values.pop("csrf",""),csrf):
            raise AppError("CSRF_INVALID", "表单确认已失效，请刷新页面")
        return values

    def handle(action, value):
        from paperread.cli import sync_service
        with settings.database() as db:
            if action in {"book-remove", "book-restore"}:
                db.set_book_removed(settings.account, value.get("book_id", ""), removed=action=="book-remove")
                return
            if not settings.mock and action!="entry":
                return operations.handle(action,value)
            if action == "book":
                if value.get("confirmed") != "yes":
                    raise AppError("BOOK_UNCONFIRMED", "请核对并明确确认版本")
                identifier = value.get("book_id","")
                if settings.mock and not identifier.startswith("mock-"):
                    raise AppError("MOCK_ID_REQUIRED", "演示书籍 ID 请以 mock- 开头")
                book=Book(book_id=identifier,title=value.get("title",""),author=value.get("author",""),
                          translator=value.get("translator",""),publisher=value.get("publisher",""),
                          origin="mock" if settings.mock else "manual")
                db.bind(confirm_book(book,confirmed=True,corresponding_edition=value.get("corresponding")=="yes"))
                return
            if action == "entry":
                minutes = int(value.get("minutes","0"))
                if not 1<=minutes<=1440:
                    raise AppError("DURATION_INVALID", "分钟数应为 1 至 1440 的整数")
                entry=EntryInput(book_id=value.get("book_id",""),account_id=settings.account,
                    reading_date=value.get("date",""),duration_seconds=minutes*60,note=value.get("note",""))
                db.add(entry,separate_session=value.get("new_session")=="yes")
                return
            if not settings.mock:
                raise AppError("CLI_CONFIRMATION_REQUIRED", "真实写入与核验请使用 CLI 的完整确认流程")
            identifier=value.get("entry_id","")
            entry=db.entry(identifier,settings.account)
            with sync_service(settings,db,writer=True) as service:
                if action=="prepare":
                    baseline=service.baseline(identifier)
                    service.attest(identifier,app_book_seconds=baseline["book_seconds"],
                        app_period_seconds=baseline["period_seconds"],same_account=True,no_other_reading=True)
                elif action=="probe":
                    service.start(identifier,seconds=min(60,entry["duration_seconds"]-entry["verified_seconds"]))
                elif action=="verify":
                    pair=service.stats.pair(entry["book_id"],entry["timezone"])
                    service.verify(identifier,app_book_seconds=pair.book_seconds,app_period_seconds=pair.period_seconds,
                                   no_other_reading=True,same_day_confirmed=True)
                elif action=="remaining":
                    budget=entry["duration_seconds"]-entry["verified_seconds"]
                    for offset in range(0,budget,60):
                        service.start(identifier,seconds=min(60,budget-offset),purpose="remaining")
                        result=service.verify(identifier,no_other_reading=True,same_day_confirmed=True)
                        if result["verification_status"]!="VERIFIED":
                            raise AppError("UNVERIFIED", "当前批次尚未核验，已停止")
                else:
                    raise AppError("ACTION_INVALID", "未知操作")

    @app.post("/action/{action}")
    async def post_action(action: str,request: Request):
        value = {}
        try:
            value=await form(request)
            result = await run_in_threadpool(handle,action,value)
        except AppError as exc:
            if action=="attest" and exc.code in {"REVIEW_CHANGED","REVIEW_CONFIRMATION_REQUIRED"}:
                from uuid import UUID
                try:
                    identifier=str(UUID(value.get("entry_id","")))
                except ValueError:
                    identifier=""
                notice="review_changed" if exc.code=="REVIEW_CHANGED" else "review_missing"
                return RedirectResponse("/?"+urlencode({"notice":notice,"entry":identifier})+"#review-"+identifier,status_code=303)
            if action in {"search", "shelf", "book"} and exc.code in {"READ_LOGIN_REQUIRED", "LOCAL_LOGIN_REQUIRED"}:
                params = {"notice":"read_key_required"}
                if action == "search":
                    params["query"] = value.get("query", "")[:500]
                return RedirectResponse("/?"+urlencode(params)+"#read-authorization",status_code=303)
            from html import escape
            return HTMLResponse("<meta charset='utf-8'><p>"+escape(str(exc))+"</p><a href='/'>返回记录</a>",status_code=400)
        except (ValidationError,ValueError):
            return HTMLResponse("输入无效；请检查版本、正整数分钟、日期与时区。",status_code=400)
        except Exception:
            return HTMLResponse("本地操作失败；请检查记录和待核验意图，不要重复提交。",status_code=503)
        destination = "/jobs/"+result if isinstance(result,str) else "/"
        if not isinstance(result,str):
            if action=="search":
                destination="/?search=1#book-search"
            elif action=="shelf":
                destination="/?shelf=1#book-shelf"
            elif action in {"book-remove", "book-restore"}:
                destination="/#book-management"
            elif action=="attest" and isinstance(result,dict) and result.get("status")=="READY":
                from uuid import UUID
                destination="/#review-"+str(UUID(value["entry_id"]))
            elif value.get("entry_id"):
                from uuid import UUID
                try:
                    destination="/#entry-"+str(UUID(value["entry_id"]))
                except ValueError:
                    pass
        return RedirectResponse(destination,status_code=303)

    return app
