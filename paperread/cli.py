"""Offline by default. Official reads and bounded live sessions require explicit flags."""

import csv
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from functools import wraps
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Annotated
from uuid import UUID

from pydantic import ValidationError
import typer

from paperread import __version__
from paperread.book_match import confirm_book
from paperread.db import Database
from paperread.errors import AppError
from paperread.models import Book, EntryInput
from paperread.safety import SafetyError, SafetyPolicy
from paperread.upstream import check_upstreams

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False, add_completion=False)


@dataclass
class Settings:
    root: Path
    db_path: Path | None
    account: str
    mock: bool
    read_only: bool

    def database(self):
        SafetyPolicy.load(self.root / "config.toml")
        return Database(self.db_path or self.root / "data" / ("mock.sqlite" if self.mock else "ledger.sqlite"), mock=self.mock)

    def stats(self, db, *, allow_live=False):
        if self.mock:
            from paperread.mock_adapter import MockStats
            return MockStats(db, self.account)
        from paperread.official_stats import OfficialStats
        return OfficialStats(root=self.root, enabled=self.read_only or allow_live, account_id=self.account)


@contextmanager
def sync_service(settings, db, *, writer=False, allow_live=False, entry_id=None):
    from paperread.credentials import CredentialStore, app_directory
    from paperread.sync_service import SyncService
    from paperread.wereadgear_adapter import BoundedWriter, MockWriter
    stats = settings.stats(db, allow_live=allow_live)
    try:
        write_adapter = None
        if writer:
            if settings.mock:
                write_adapter = MockWriter(stats)
            else:
                if any(check["status"] != "PASS" for check in check_upstreams(settings.root)):
                    raise AppError("SOURCE_MISMATCH", "上游源码身份不一致，已阻断写入")
                book_id = db.entry(str(entry_id), settings.account)["book_id"] if entry_id else None
                profile = CredentialStore(settings.account, book_id=book_id).load()
                if profile.api_book_id is not None:
                    mapping = db.route(settings.account, book_id)
                    if (not mapping or mapping["web_book_id"] != profile.web_book_id or
                            mapping["identity_hash"] != profile.identity_hash):
                        raise AppError("CONTEXT_REQUIRED", "本书上下文未准备或登录后已失效，请重新准备")
                write_adapter = BoundedWriter(profile)
                from paperread.session_manager import SessionManager
                write_adapter.session_manager = SessionManager(settings.account, book_id=book_id)
        lock = db.path.parent / "mock-writer.lock" if settings.mock else app_directory() / "writer.lock"
        yield SyncService(db, stats, write_adapter, account_id=settings.account, lock_path=lock)
    finally:
        stats.close()


def checked(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except AppError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None
        except ValidationError:
            typer.echo("INPUT_INVALID: 输入无效；请检查非空书名、正整数时长、日期、时区和版本信息", err=True)
            raise typer.Exit(1) from None
        except (SafetyError, sqlite3.Error, OSError, ValueError):
            typer.echo("BLOCKED: 本地配置、数据或文件无效；未自动重发网络写入", err=True)
            raise typer.Exit(1) from None
        except (typer.Exit, typer.Abort):
            raise
        except Exception:
            typer.echo("INTERNAL_ERROR: 操作异常，已保留待核验信息；不会打印原始异常或自动重发", err=True)
            raise typer.Exit(1) from None
    return wrapper


def emit(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key not in {"input_json", "input_hash"}}
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@app.callback()
def main(ctx: typer.Context,
         root: Path = typer.Option(Path("."), help="项目根目录"),
         db: Path | None = typer.Option(None, help="数据库路径，Mock 与真实数据不能混用"),
         account: str = typer.Option("default", help="本地账户别名"),
         mock: bool = typer.Option(False, help="纯本地合成演示，不是真实核验"),
         read_only: bool = typer.Option(False, help="允许显式官方只读查询")) -> None:
    """PaperRead Sync: local ledger and independently verified statistics."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account) or (mock and read_only):
        typer.echo("INPUT_INVALID: 账户别名无效或模式冲突", err=True)
        raise typer.Exit(1)
    ctx.obj = Settings(root.resolve(), db, account, mock, read_only)


@app.command()
def doctor(
    ctx: typer.Context,
    root: Annotated[Path | None, typer.Option(help="Project root; defaults to global --root")] = None,
) -> None:
    """Check local safety configuration and pinned source; never access credentials."""
    root = root or ctx.obj.root
    try:
        SafetyPolicy.load(root / "config.toml")
        checks = check_upstreams(root)
    except (SafetyError, OSError, ValueError, KeyError):
        typer.echo("BLOCKED: local safety configuration or source lock is invalid", err=True)
        raise typer.Exit(1) from None
    result = {
        "phase": "integration", "software_version": __version__, "mode": "offline", "live_writes_enabled": False,
        "explicit_live_probe_available": True,
        "schedule_enabled": False, "daemon_enabled": False,
        "browser_fallback": False, "automatic_retry": False,
        "remote_verification": "NOT_RUN", "upstreams": checks,
    }
    typer.echo(json.dumps(result, ensure_ascii=True, indent=2))
    if any(check["status"] != "PASS" for check in checks):
        raise typer.Exit(1)


@app.command()
@checked
def search(ctx: typer.Context, query: str, max_idx: int = typer.Option(0, min=0)):
    """搜索候选；离线查本地，--read-only 查官方，--mock 查示例。"""
    if not query.strip():
        raise AppError("QUERY_EMPTY", "书名不能为空")
    settings = ctx.obj
    page_info = {}
    with settings.database() as db:
        if settings.mock or settings.read_only:
            stats = settings.stats(db)
            try:
                books = stats.search(query, max_idx=max_idx)
                page_info = getattr(stats, "page_info", {})
                db.cache_candidates(books)
            finally:
                stats.close()
        else:
            books = db.search_local(query)
        emit({"mode": "mock" if settings.mock else "read-only" if settings.read_only else "offline",
              "candidates": [book.model_dump() for book in books], "selection": "NONE",
              "page": page_info,
              "notice": "搜索为分页片段；不会自动选择第一项"})


@app.command()
@checked
def bind(ctx: typer.Context, book_id: str,
         title: str | None = None, author: str | None = None,
         translator: str = "", publisher: str = "", isbn: str = "",
         corresponding_edition: bool = False, confirm: bool = False):
    """显示版本并人工绑定；缺少精确版本时需 --corresponding-edition。"""
    settings = ctx.obj
    with settings.database() as db:
        if title is not None:
            if settings.mock:
                raise AppError("MOCK_CATALOG", "Mock 模式请从示例搜索候选中选择")
            book = Book(book_id=book_id, title=title, author=author or "", translator=translator,
                        publisher=publisher, isbn=isbn)
        elif settings.read_only:
            stats = settings.stats(db)
            try:
                book = stats.book_info(book_id)
            finally:
                stats.close()
        else:
            book = db.candidate(book_id)
        emit(book)
        accepted = confirm or typer.confirm("已核对作者、译者、出版社/ISBN 并确认此版本？", default=False)
        emit(db.bind(confirm_book(book, confirmed=accepted, corresponding_edition=corresponding_edition)))


@app.command()
@checked
def add(ctx: typer.Context, book_id: str = typer.Option(...),
        reading_date: str = typer.Option(..., "--date"), minutes: int = typer.Option(..., min=1, max=1440),
        source: str = "paper", timezone: str = "Asia/Shanghai", entry_id: UUID | None = None,
        session_start: str | None = None, session_end: str | None = None,
        note: str = "", new_session: bool = False):
    """保存真实纸质阅读事件；不会自动联网。"""
    values = {"account_id": ctx.obj.account, "book_id": book_id, "reading_date": reading_date,
              "duration_seconds": minutes * 60, "source": source, "timezone": timezone,
              "session_start": session_start, "session_end": session_end, "note": note}
    if entry_id:
        values["entry_id"] = entry_id
    entry = EntryInput.model_validate(values)
    with ctx.obj.database() as db:
        emit(db.add(entry, separate_session=new_session))


@app.command(name="list")
@checked
def list_entries(ctx: typer.Context):
    """列出当前本地账户的记录。"""
    with ctx.obj.database() as db:
        emit([{key: value for key, value in entry.items() if key not in {"input_json", "input_hash"}}
              for entry in db.entries(ctx.obj.account)])


@app.command()
@checked
def preview(ctx: typer.Context, entry_id: UUID):
    """展示书籍、已核验额度、剩余额度和当前同步日。"""
    from zoneinfo import ZoneInfo
    with ctx.obj.database() as db:
        entry = db.entry(str(entry_id), ctx.obj.account)
        emit({"entry": {k: v for k, v in entry.items() if k not in {"input_json", "input_hash"}},
              "book": db.book(entry["book_id"]).model_dump(),
              "remaining_seconds": entry["duration_seconds"] - entry["verified_seconds"],
              "sync_date_now": datetime.now(ZoneInfo(entry["timezone"])).date().isoformat(),
              "mode": "mock" if ctx.obj.mock else "offline",
              "notice": "纸质阅读日期保持原样；远端按当前会话时间计入，不保证补记历史日桶。"})


@app.command(name="import")
@checked
def import_entries(ctx: typer.Context, path: Path, new_session: bool = False):
    """原子导入 JSON 记录数组；沿用 UUID；始终仅本地保存。"""
    values = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(values, list) or len(values) > 10000:
        raise AppError("IMPORT_FORMAT", "导入文件应为至多 10000 条记录的 JSON 数组")
    entries = [EntryInput.model_validate(item) for item in values]
    if any(entry.account_id != ctx.obj.account for entry in entries):
        raise AppError("ACCOUNT_MISMATCH", "批量导入的账户别名必须与当前账户一致")
    with ctx.obj.database() as db:
        result = db.import_entries(entries, separate_session=new_session)
        emit({"count": len(result), "entry_ids": [row["entry_id"] for row in result], "network_writes": 0})


@app.command()
@checked
def export(ctx: typer.Context, format: str = "csv", output: Path | None = None):
    """导出记录；JSON 可重新导入，CSV 包括状态与已核验/剩余额度。"""
    with ctx.obj.database() as db:
        rows = db.entries(ctx.obj.account)
        if format == "json":
            content = json.dumps([json.loads(row["input_json"]) for row in rows], ensure_ascii=False, indent=2)
        elif format == "csv":
            fields = ["entry_id", "account_id", "book_id", "title", "author", "translator", "publisher", "isbn",
                      "reading_date", "timezone", "duration_seconds", "verified_seconds", "remaining_seconds",
                      "status", "sync_at", "note"]
            buffer = io.StringIO(newline="")
            writer = csv.DictWriter(buffer, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                book = db.book(row["book_id"])
                value = {**row, **book.model_dump(), "remaining_seconds": row["duration_seconds"] - row["verified_seconds"]}
                writer.writerow({key: ("'" + str(value[key]) if isinstance(value.get(key), str) and
                    value[key].startswith(("=", "+", "-", "@", "\t", "\r")) else value.get(key, "")) for key in fields})
            content = buffer.getvalue()
        else:
            raise AppError("EXPORT_FORMAT", "仅支持 csv 或 json")
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content, encoding="utf-8-sig" if format == "csv" else "utf-8", newline="")
            emit({"exported": len(rows), "format": format})
        else:
            typer.echo(content)


@app.command()
@checked
def baseline(ctx: typer.Context, entry_id: UUID):
    """官方只读单书 + 月度基线；需全局 --read-only 或 --mock。"""
    with ctx.obj.database() as db, sync_service(ctx.obj, db) as service:
        emit(service.baseline(str(entry_id)))


@app.command()
@checked
def diagnose(ctx: typer.Context, entry_id: UUID):
    """只读诊断上次尝试；不提交时长、不代替 App 核验、不解锁写入。"""
    with ctx.obj.database() as db, sync_service(ctx.obj, db) as service:
        emit(service.diagnose(str(entry_id)))


@app.command()
@checked
def attest(ctx: typer.Context, entry_id: UUID,
           app_book_seconds: int = typer.Option(..., min=0),
           app_period_seconds: int = typer.Option(..., min=0),
           same_account: bool = False, no_other_reading: bool = False):
    """人工核对基线字段口径、账号和其他活动；绝不发送阅读写入。"""
    with ctx.obj.database() as db, sync_service(ctx.obj, db, writer=True, entry_id=entry_id) as service:
        emit(service.attest(str(entry_id), app_book_seconds=app_book_seconds,
            app_period_seconds=app_period_seconds, same_account=same_account, no_other_reading=no_other_reading))


def approve_live(settings, entry, book, seconds, *, live, confirm):
    from paperread.wereadgear_adapter import require_live_flags
    require_live_flags(live=live, confirm=confirm)
    emit({"book": book.model_dump(), "paper_reading_date": entry["reading_date"],
          "paper_duration_seconds": entry["duration_seconds"], "approved_now_seconds": seconds,
          "verified_seconds": entry["verified_seconds"], "notice": "仅补记实际阅读；按当前服务器时间同步。请暂停其他阅读/听书及自动化。"})
    phrase = f"{book.book_id}:{seconds}"
    answer = typer.prompt(f"再次确认书籍/版本、实际阅读与额度，请输入 {phrase}", default="", show_default=False)
    if answer != phrase:
        raise AppError("USER_CANCELLED", "确认不匹配，未创建写意图或发送请求")
    SafetyPolicy.load(settings.root / "config.toml").require_write(
        live=live, confirm=confirm, interactive_confirmed=True)


@app.command()
@checked
def probe(ctx: typer.Context, entry_id: UUID, seconds: int = typer.Option(60, min=1, max=60),
          live: bool = False, confirm: bool = False, dry_run: bool = False, attempt_id: UUID | None = None):
    """单次小额试验；真实模式必须 --live --confirm 并交互输入精确确认。"""
    if ctx.obj.mock and live:
        raise AppError("MODE_CONFLICT", "Mock 不能与 --live 同用")
    if not ctx.obj.mock and not dry_run:
        from paperread.wereadgear_adapter import require_live_flags
        require_live_flags(live=live, confirm=confirm)
    with ctx.obj.database() as db:
        entry = db.entry(str(entry_id), ctx.obj.account)
        if dry_run:
            emit({"dry_run": True, "book": db.book(entry["book_id"]).model_dump(), "seconds": seconds,
                  "remaining_seconds": entry["duration_seconds"]-entry["verified_seconds"], "network_writes": 0})
            return
        if not ctx.obj.mock:
            approve_live(ctx.obj, entry, db.book(entry["book_id"]), seconds, live=live, confirm=confirm)
        with sync_service(ctx.obj, db, writer=True, allow_live=live and confirm, entry_id=entry_id) as service:
            emit(service.start(str(entry_id), seconds=seconds, attempt_id=attempt_id, live=live, confirm=confirm))


def verify_entry(ctx, entry_id, app_book_seconds, app_period_seconds, no_other_reading, same_day_confirmed, reconcile=False,
                 concurrent_reading_confirmed=False):
    with ctx.obj.database() as db, sync_service(ctx.obj, db) as service:
        method = service.reconcile if reconcile else service.verify
        result = method(str(entry_id), app_book_seconds=app_book_seconds, app_period_seconds=app_period_seconds,
                        no_other_reading=no_other_reading, same_day_confirmed=same_day_confirmed,
                        concurrent_reading_confirmed=concurrent_reading_confirmed)
        emit(result)
        if result["verification_status"] not in ("VERIFIED", "CANCELLED"):
            raise typer.Exit(2)


@app.command()
@checked
def verify(ctx: typer.Context, entry_id: UUID, app_book_seconds: int | None = typer.Option(None, min=0),
           app_period_seconds: int | None = typer.Option(None, min=0), no_other_reading: bool = False,
           same_day_confirmed: bool = False, concurrent_reading_confirmed: bool = False):
    """独立查询远端数据；首次试验还必须提供客户端复核读数。"""
    verify_entry(ctx, entry_id, app_book_seconds, app_period_seconds, no_other_reading, same_day_confirmed,
                 concurrent_reading_confirmed=concurrent_reading_confirmed)


@app.command()
@checked
def reconcile(ctx: typer.Context, entry_id: UUID, app_book_seconds: int | None = typer.Option(None, min=0),
              app_period_seconds: int | None = typer.Option(None, min=0), no_other_reading: bool = False,
              same_day_confirmed: bool = False, concurrent_reading_confirmed: bool = False):
    """对账并恢复未决状态；绝不重发阅读请求。"""
    verify_entry(ctx, entry_id, app_book_seconds, app_period_seconds, no_other_reading, same_day_confirmed, True,
                 concurrent_reading_confirmed=concurrent_reading_confirmed)


@app.command()
@checked
def submit(ctx: typer.Context, entry_id: UUID, remaining: bool = False, live: bool = False,
           confirm: bool = False, dry_run: bool = False,
           no_other_reading: bool = False, same_day_confirmed: bool = False):
    """按已确认的余额分批提交并自动核验；不要求先做 probe。"""
    if not remaining:
        raise AppError("REMAINING_REQUIRED", "请显式使用 --remaining；不会另行提交完整原时长")
    if ctx.obj.mock and live:
        raise AppError("MODE_CONFLICT", "Mock 不能与 --live 同用")
    if not ctx.obj.mock and not dry_run:
        from paperread.wereadgear_adapter import require_live_flags
        require_live_flags(live=live, confirm=confirm)
    with ctx.obj.database() as db:
        entry = db.entry(str(entry_id), ctx.obj.account)
        budget = entry["available_seconds"]
        if dry_run or budget == 0:
            emit({"dry_run": dry_run, "remaining_seconds": budget, "network_writes": 0, "status": entry["status"]})
            return
        if not no_other_reading or not same_day_confirmed:
            raise AppError("ACTIVITY_CONFIRMATION", "余额会话需显式确认 --no-other-reading --same-day-confirmed")
        with sync_service(ctx.obj, db, writer=True, allow_live=live and confirm, entry_id=entry_id) as service:
            service._grant(entry, "remaining")
            if not ctx.obj.mock:
                approve_live(ctx.obj, entry, db.book(entry["book_id"]), budget, live=live, confirm=confirm)
            result=service.run_remaining(str(entry_id),budget_seconds=budget,live=live,confirm=confirm,
                                         progress=typer.echo)
            emit(result)
            if result["batch_status"]!="COMPLETED":
                raise typer.Exit(2)


@app.command()
@checked
def audit(ctx: typer.Context, entry_id: UUID, output: Path | None = None):
    """仅从本地导出脱敏核验审计，不要求重新登录或联网。"""
    with ctx.obj.database() as db:
        entry = db.entry(str(entry_id), ctx.obj.account)
        attempts = [dict(row) for row in db.conn.execute(
            """SELECT attempt_id,attempt_no,requested_seconds,credited_seconds,started_at,finished_at,
            transport_status,verification_status,before_book_seconds,after_book_seconds,before_period_seconds,
            after_period_seconds,error_code FROM sync_attempts WHERE entry_id=? ORDER BY attempt_no""", (str(entry_id),))]
        value = {"entry": {k:v for k,v in entry.items() if k not in {"input_json","input_hash"}},
                 "book": db.book(entry["book_id"]).model_dump(), "attempts": attempts,
                 "source": "mock" if ctx.obj.mock else "official", "remote_exactly_once_guaranteed": False}
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
        else:
            emit(value)


@app.command()
@checked
def serve(ctx: typer.Context, port: int = typer.Option(8765, min=1024, max=65535)):
    """本地真实流程或 Mock 界面；仅 127.0.0.1，真实写入需逐次明确确认。"""
    try:
        import uvicorn
        from paperread.web import create_app
    except ImportError:
        raise AppError("WEB_DEPENDENCY", "请在本机安装 requirements.lock 中的 Web 可选依赖") from None
    app_instance=create_app(ctx.obj,origin=f"http://127.0.0.1:{port}")
    typer.echo(f"本地界面 http://127.0.0.1:{port} ；Ctrl+C 停止")
    uvicorn.run(app_instance,host="127.0.0.1",port=port,access_log=False,log_level="warning")


credentials_app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
app.add_typer(credentials_app, name="credentials")


@credentials_app.command(name="import")
@checked
def import_credentials(ctx: typer.Context, path: Path):
    """本机读取已授权 JSON，使用 Windows 当前用户 DPAPI 加密保存；不回显。"""
    from paperread.credentials import CredentialStore, import_upstream_file
    if ctx.obj.mock:
        raise AppError("MODE_CONFLICT", "Mock 模式不需要真实凭证")
    SafetyPolicy.load(ctx.obj.root / "config.toml")
    CredentialStore(ctx.obj.account).save(import_upstream_file(path))
    emit({"status": "STORED_ENCRYPTED", "notice": "仅验证本地格式；尚未证明服务器登录有效"})


@credentials_app.command(name="key")
@checked
def connect_read_key(ctx: typer.Context):
    """隐藏输入并加密保存官方只读 Key；不写入环境文件或命令参数。"""
    import getpass
    from paperread.credentials import ApiKeyStore, app_directory
    from paperread.file_lock import WriterLock
    if ctx.obj.mock:
        raise AppError("MODE_CONFLICT", "Mock 不需要真实 Key")
    with WriterLock(app_directory()/"writer.lock"), ctx.obj.database() as db:
        if db.conn.execute("SELECT 1 FROM write_guard").fetchone():
            raise AppError("RECONCILE_REQUIRED", "未决写入期间不能更换 Key")
        key = getpass.getpass("输入官方只读 Key（不回显）: ")
        ApiKeyStore(ctx.obj.account).save_key(key)
        key = ""
    emit({"status":"KEY_STORED_DPAPI"})


def wait_browser_operation(settings, action, entry_id=None):
    import time
    from paperread.web_operations import WebOperations
    if settings.mock:
        raise AppError("MODE_CONFLICT", "Mock 不需要真实浏览器授权")
    values = {}
    if entry_id:
        with settings.database() as db:
            values["book_id"] = db.entry(str(entry_id),settings.account)["book_id"]
    operations = WebOperations(settings)
    try:
        identifier = operations.handle(action,values)
        message = None
        while True:
            job = next(item for item in operations.jobs.snapshots() if item["id"]==identifier)
            if job["message"] != message:
                typer.echo(job["message"])
                message=job["message"]
            if job["state"] not in {"RUNNING","QUEUED"}:
                if job["state"] != "DONE":
                    raise AppError(job["error_code"] or "BROWSER_FAILED",job["message"])
                emit(job["result"])
                break
            time.sleep(0.5)
    finally:
        operations.jobs.shutdown()


@app.command()
@checked
def login(ctx: typer.Context):
    """打开独立本机浏览器扫码；阅读计时请求全部拦截。"""
    wait_browser_operation(ctx.obj,"login")


@app.command()
@checked
def prepare(ctx: typer.Context, entry_id: UUID):
    """准备本书官方 ID / 网页路由映射和章节上下文，不提交阅读时长。"""
    wait_browser_operation(ctx.obj,"context",entry_id)


def configure_console():
    # Windows console and captured PowerShell output use one explicit encoding.
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")


def run():
    configure_console()
    app()


if __name__ == "__main__":
    run()
