"""SQLite ledger. All mutation decisions and intents use BEGIN IMMEDIATE."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from paperread.errors import AppError
from paperread.models import Book, EntryInput, State, StatsPair, utc_now

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS books(
 book_id TEXT PRIMARY KEY, document TEXT NOT NULL,
 confirmed_by_user INTEGER NOT NULL CHECK(confirmed_by_user IN (0,1)));
CREATE TABLE IF NOT EXISTS candidates(book_id TEXT PRIMARY KEY, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reading_entries(
 entry_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, book_id TEXT NOT NULL REFERENCES books,
 reading_date TEXT NOT NULL, timezone TEXT NOT NULL,
 duration_seconds INTEGER NOT NULL CHECK(typeof(duration_seconds)='integer' AND duration_seconds>0),
 verified_seconds INTEGER NOT NULL DEFAULT 0 CHECK(typeof(verified_seconds)='integer' AND verified_seconds>=0 AND verified_seconds<=duration_seconds),
 source TEXT NOT NULL CHECK(source='paper'), session_start TEXT, session_end TEXT, note TEXT NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, sync_at TEXT,
 input_json TEXT NOT NULL, input_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS near_duplicate ON reading_entries(account_id,book_id,reading_date,duration_seconds);
CREATE TABLE IF NOT EXISTS remote_snapshots(
 snapshot_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES reading_entries,
 book_id TEXT NOT NULL, period TEXT NOT NULL, source TEXT NOT NULL,
 value_seconds INTEGER NOT NULL, captured_at TEXT NOT NULL,
 attempt_id TEXT, role TEXT NOT NULL, raw_response_redacted TEXT NOT NULL,
 pair_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attestations(
 entry_id TEXT PRIMARY KEY REFERENCES reading_entries, snapshot_id TEXT NOT NULL,
 identity_hash TEXT NOT NULL, writer_identity_hash TEXT NOT NULL, confirmed_at TEXT NOT NULL,
 document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_attempts(
 attempt_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES reading_entries,
 attempt_no INTEGER NOT NULL, requested_seconds INTEGER NOT NULL CHECK(requested_seconds>0),
 before_book_seconds INTEGER NOT NULL, after_book_seconds INTEGER,
 before_period_seconds INTEGER NOT NULL, after_period_seconds INTEGER,
 started_at TEXT NOT NULL, finished_at TEXT, transport_status TEXT NOT NULL,
 verification_status TEXT NOT NULL, request_hash TEXT NOT NULL,
 error_code TEXT, error_message_redacted TEXT,
 before_json TEXT NOT NULL, after_json TEXT, result_json TEXT,
 purpose TEXT NOT NULL CHECK(purpose IN ('probe','remaining')),
 credited_seconds INTEGER NOT NULL DEFAULT 0,
 UNIQUE(entry_id,attempt_no));
CREATE TABLE IF NOT EXISTS write_guard(slot INTEGER PRIMARY KEY CHECK(slot=1), attempt_id TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS audit_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT, attempt_id TEXT,
 event TEXT NOT NULL, captured_at TEXT NOT NULL, details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS book_routes(
 account_id TEXT NOT NULL, book_id TEXT NOT NULL REFERENCES books,
 web_book_id TEXT NOT NULL, official_reader_link TEXT NOT NULL,
 captured_at TEXT NOT NULL, identity_hash TEXT NOT NULL,
 PRIMARY KEY(account_id,book_id));
CREATE TABLE IF NOT EXISTS removed_books(
 account_id TEXT NOT NULL, book_id TEXT NOT NULL REFERENCES books,
 removed_at TEXT NOT NULL, PRIMARY KEY(account_id,book_id));
PRAGMA user_version=3;
COMMIT;
"""

MIGRATION_2 = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS book_routes(
 account_id TEXT NOT NULL, book_id TEXT NOT NULL REFERENCES books,
 web_book_id TEXT NOT NULL, official_reader_link TEXT NOT NULL,
 captured_at TEXT NOT NULL, identity_hash TEXT NOT NULL,
 PRIMARY KEY(account_id,book_id));
PRAGMA user_version=2;
COMMIT;
"""

MIGRATION_3 = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS removed_books(
 account_id TEXT NOT NULL, book_id TEXT NOT NULL REFERENCES books,
 removed_at TEXT NOT NULL, PRIMARY KEY(account_id,book_id));
PRAGMA user_version=3;
COMMIT;
"""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Database:
    def __init__(self, path: Path, *, mock: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > 3:
            self.close()
            raise AppError("DB_VERSION", "数据库版本较新，请升级程序")
        if version == 0:
            try:
                self.conn.executescript(SCHEMA)
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.rollback()
                self.close()
                raise
        elif version == 1:
            try:
                self.conn.executescript(MIGRATION_2)
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.rollback()
                self.close()
                raise
        if version in (1, 2):
            try:
                self.conn.executescript(MIGRATION_3)
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.rollback()
                self.close()
                raise
        mode = "mock" if mock else "real"
        try:
            with self.transaction():
                self.conn.execute("INSERT OR IGNORE INTO metadata VALUES('backend',?)", (mode,))
                actual = self.conn.execute("SELECT value FROM metadata WHERE key='backend'").fetchone()[0]
                if actual != mode:
                    raise AppError("BACKEND_MISMATCH", "Mock 与真实数据库必须分开")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def event(self, event: str, entry_id=None, attempt_id=None, details=None):
        self.conn.execute(
            "INSERT INTO audit_events(entry_id,attempt_id,event,captured_at,details) VALUES(?,?,?,?,?)",
            (entry_id, attempt_id, event, utc_now(), json.dumps(details or {}, ensure_ascii=True)),
        )

    def cache_candidates(self, books: list[Book]):
        with self.transaction():
            for book in books:
                self.conn.execute("INSERT OR REPLACE INTO candidates VALUES(?,?)", (book.book_id, book.model_dump_json()))

    def candidate(self, book_id: str) -> Book:
        row = self.conn.execute("SELECT document FROM candidates WHERE book_id=?", (book_id,)).fetchone()
        if not row:
            raise AppError("BOOK_NOT_FOUND", "没有该候选书，请先搜索或明确录入版本信息")
        return Book.model_validate_json(row[0])

    def search_local(self, query: str) -> list[Book]:
        rows = self.conn.execute("SELECT document FROM candidates UNION SELECT document FROM books").fetchall()
        books = {book.book_id: book for row in rows if
                 query.casefold() in (book := Book.model_validate_json(row[0])).title.casefold()}
        return list(books.values())

    def bind(self, book: Book):
        if not book.confirmed_by_user or not book.confirmed_at:
            raise AppError("BOOK_UNCONFIRMED", "版本需由用户明确确认")
        if not (book.translator or book.publisher or book.isbn or book.corresponding_edition):
            raise AppError("EDITION_UNKNOWN", "缺少版本信息，请明确标记为对应电子版")
        with self.transaction():
            existing = self.conn.execute("SELECT document FROM books WHERE book_id=?", (book.book_id,)).fetchone()
            if existing:
                current = Book.model_validate_json(existing[0])
                if current.model_dump(exclude={"confirmed_at", "reader_link"}) != book.model_dump(exclude={"confirmed_at", "reader_link"}):
                    raise AppError("BOOK_LOCKED", "已绑定书籍不能静默更改版本信息")
                if book.reader_link and book.origin == "official":
                    current = current.model_copy(update={"reader_link": book.reader_link})
                    self.conn.execute("UPDATE books SET document=? WHERE book_id=?", (current.model_dump_json(), book.book_id))
                return current
            self.conn.execute("INSERT INTO books VALUES(?,?,1)", (book.book_id, book.model_dump_json()))
            self.event("BOOK_BOUND", details={"book_id": book.book_id, "origin": book.origin})
        return book

    def book(self, book_id: str) -> Book:
        row = self.conn.execute("SELECT document FROM books WHERE book_id=?", (book_id,)).fetchone()
        if not row:
            raise AppError("BOOK_UNBOUND", "请先确认并绑定书籍版本")
        return Book.model_validate_json(row[0])

    def managed_books(self, account_id: str):
        return [{"book": Book.model_validate_json(row[0]), "removed": row[1] is not None}
                for row in self.conn.execute("""SELECT b.document,r.removed_at FROM books b
                    LEFT JOIN removed_books r ON r.book_id=b.book_id AND r.account_id=?
                    ORDER BY r.removed_at IS NOT NULL,b.book_id""", (account_id,))]

    def set_book_removed(self, account_id: str, book_id: str, *, removed: bool):
        # Remove from this account's picker, retaining ledger and verification evidence.
        with self.transaction():
            self.book(book_id)
            if removed:
                self.conn.execute("INSERT OR IGNORE INTO removed_books VALUES(?,?,?)",
                                  (account_id, book_id, utc_now()))
            else:
                self.conn.execute("DELETE FROM removed_books WHERE account_id=? AND book_id=?",
                                  (account_id, book_id))
            self.event("BOOK_REMOVED" if removed else "BOOK_RESTORED",
                       details={"account_id": account_id, "book_id": book_id})

    def _add(self, entry: EntryInput, separate_session: bool):
        value = entry.model_dump(mode="json")
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=True)
        existing = self.conn.execute("SELECT * FROM reading_entries WHERE entry_id=?", (str(entry.entry_id),)).fetchone()
        if existing:
            if existing["input_hash"] != digest(encoded):
                raise AppError("ID_CONFLICT", "相同记录 ID 的内容不同，已保留原记录")
            return dict(existing)
        book = self.book(entry.book_id)
        if self.conn.execute("SELECT 1 FROM removed_books WHERE account_id=? AND book_id=?",
                             (entry.account_id, entry.book_id)).fetchone():
            raise AppError("BOOK_REMOVED", "这本书已从选择列表移除，请先在书籍管理中恢复")
        backend = self.conn.execute("SELECT value FROM metadata WHERE key='backend'").fetchone()[0]
        if (book.origin == "mock") != (backend == "mock"):
            raise AppError("BACKEND_MISMATCH", "示例书籍不能用于真实记录")
        duplicate = self.conn.execute(
            "SELECT entry_id FROM reading_entries WHERE account_id=? AND book_id=? AND reading_date=? AND duration_seconds=?",
            (entry.account_id, entry.book_id, str(entry.reading_date), entry.duration_seconds),
        ).fetchone()
        if duplicate and not separate_session:
            raise AppError("POSSIBLE_DUPLICATE", "同账户同书同日同样时长已有记录；确认独立阅读后用 --new-session")
        now = utc_now()
        self.conn.execute(
            """INSERT INTO reading_entries(entry_id,account_id,book_id,reading_date,timezone,duration_seconds,
            source,session_start,session_end,note,status,created_at,updated_at,input_json,input_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(entry.entry_id), entry.account_id, entry.book_id, str(entry.reading_date), entry.timezone,
             entry.duration_seconds, entry.source, value["session_start"], value["session_end"], entry.note,
             State.LOCAL_ONLY, now, now, encoded, digest(encoded)),
        )
        self.event("ENTRY_ADDED", str(entry.entry_id), details={"separate_session_confirmed": separate_session})
        return self.entry(str(entry.entry_id))

    def add(self, entry: EntryInput, *, separate_session: bool = False):
        with self.transaction():
            return self._add(entry, separate_session)

    def import_entries(self, entries: list[EntryInput], *, separate_session=False):
        with self.transaction():
            return [self._add(entry, separate_session) for entry in entries]

    def entry(self, entry_id: str, account_id: str | None = None):
        row = self.conn.execute("SELECT * FROM reading_entries WHERE entry_id=?", (entry_id,)).fetchone()
        if not row or (account_id is not None and row["account_id"] != account_id):
            raise AppError("ENTRY_NOT_FOUND", "未找到当前账户的阅读记录")
        return self._with_reservation(dict(row))

    def _with_reservation(self, value):
        reserved = sum(row["reserved_seconds"] for row in self.reserved_attempts(value["entry_id"]))
        return {**value, "reserved_seconds":reserved,
                "available_seconds":max(0,value["duration_seconds"]-value["verified_seconds"]-reserved)}

    def reserved_attempts(self, entry_id):
        attempts = []
        for row in self.conn.execute("SELECT attempt_id,requested_seconds,result_json FROM sync_attempts WHERE entry_id=? AND verification_status='WITHDRAWN_UNVERIFIED' ORDER BY attempt_no",
                                     (entry_id,)):
            document = json.loads(row["result_json"] or "{}")
            amount = document.get("reserved_seconds", row["requested_seconds"])
            if type(amount) is not int or amount < row["requested_seconds"]:
                raise AppError("RECOVERY_REQUIRED", "旧试验保留额度异常；未释放任何额度")
            if document.get("reservation_released_for_retry") is True:
                continue
            attempts.append({**dict(row), "reserved_seconds":amount})
        return attempts

    def entries(self, account_id: str):
        return [self._with_reservation(dict(row)) for row in self.conn.execute(
            "SELECT * FROM reading_entries WHERE account_id=? ORDER BY created_at,entry_id", (account_id,),
        )]

    def set_state(self, entry_id: str, state: State):
        self.conn.execute("UPDATE reading_entries SET status=?,updated_at=? WHERE entry_id=?", (state, utc_now(), entry_id))

    def save_snapshot(self, entry_id: str, pair: StatsPair, role: str, attempt_id=None):
        snapshot_id = str(uuid4())
        self.conn.execute(
            """INSERT INTO remote_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (snapshot_id, entry_id, pair.book_id, pair.period_key, pair.source, pair.book_seconds,
             pair.captured_at, attempt_id, role, json.dumps(pair.evidence, ensure_ascii=True), pair.model_dump_json()),
        )
        return snapshot_id

    def latest_snapshot(self, entry_id: str, role: str = "baseline"):
        row = self.conn.execute(
            "SELECT snapshot_id,pair_json FROM remote_snapshots WHERE entry_id=? AND role=? ORDER BY rowid DESC LIMIT 1",
            (entry_id, role),
        ).fetchone()
        if not row:
            raise AppError("BASELINE_REQUIRED", "请先采集独立远端统计基线")
        return row[0], StatsPair.model_validate_json(row[1])

    def validate_route_update(self, account_id, book_id, web_book_id, official_reader_link, identity_hash):
        """Protect pending book/account evidence while allowing other-book preparation."""
        active = self.conn.execute("""SELECT e.account_id,e.book_id,a.writer_identity_hash
            FROM write_guard w JOIN sync_attempts s ON s.attempt_id=w.attempt_id
            JOIN reading_entries e ON e.entry_id=s.entry_id
            JOIN attestations a ON a.entry_id=e.entry_id""").fetchone()
        if active is None:
            if self.conn.execute("SELECT 1 FROM write_guard").fetchone():
                raise AppError("RECOVERY_REQUIRED", "未决写入缺少原账号信息，请先检查原记录")
            return False
        if active["account_id"]!=account_id or active["writer_identity_hash"]!=identity_hash:
            raise AppError("RECOVERY_ACCOUNT_MISMATCH", "不能用其他账号替换未决写入的授权")
        if active["book_id"]==book_id:
            original = self.route(account_id,book_id)
            if not original or any(original[key]!=value for key,value in (
                    ("web_book_id",web_book_id),("official_reader_link",official_reader_link),("identity_hash",identity_hash))):
                raise AppError("RECOVERY_CONTEXT_MISMATCH", "未决写入原书的路由不能更改，原记录仍保留")
            return True
        return False

    def save_route(self, account_id, book_id, web_book_id, official_reader_link, identity_hash):
        with self.transaction():
            self.book(book_id)
            preserve = self.validate_route_update(account_id,book_id,web_book_id,official_reader_link,identity_hash)
            if preserve:
                return  # Same-book session refresh retains the original route verbatim.
            self.conn.execute("INSERT OR REPLACE INTO book_routes VALUES(?,?,?,?,?,?)",
                (account_id, book_id, web_book_id, official_reader_link, utc_now(), identity_hash))
            self.event("BOOK_ROUTE_CONFIRMED", details={"account_id": account_id,
                "book_id": book_id, "web_book_id": web_book_id, "source": "official_reader_link"})

    def route(self, account_id, book_id):
        row = self.conn.execute("SELECT * FROM book_routes WHERE account_id=? AND book_id=?", (account_id, book_id)).fetchone()
        return dict(row) if row else None
