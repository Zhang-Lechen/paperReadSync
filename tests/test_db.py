from datetime import date, timedelta
import sqlite3
from uuid import uuid4

from pydantic import ValidationError
import pytest

from paperread.book_match import confirm_book
from paperread.db import Database
from paperread.errors import AppError
from paperread.models import Book, EntryInput


@pytest.fixture
def ledger(tmp_path):
    with Database(tmp_path / "ledger.sqlite") as db:
        db.bind(confirm_book(Book(book_id="A", title="安娜·卡列尼娜", author="托尔斯泰", translator="译者 A"), confirmed=True))
        yield db


def entry(**updates):
    return EntryInput(book_id="A", reading_date="2026-10-22", duration_seconds=3600, **updates)


def test_date_seconds_and_idempotency(ledger):
    value = entry()
    first = ledger.add(value)
    second = ledger.add(value)
    assert first["entry_id"] == second["entry_id"]
    assert first["duration_seconds"] == 3600
    assert first["reading_date"] == "2026-10-22"
    assert len(ledger.entries("default")) == 1


def test_same_attributes_need_separate_session_confirmation(ledger):
    ledger.add(entry())
    with pytest.raises(AppError, match="POSSIBLE_DUPLICATE"):
        ledger.add(entry())
    ledger.add(entry(), separate_session=True)
    assert len(ledger.entries("default")) == 2


def test_same_id_different_content_is_not_silently_overwritten(ledger):
    value = entry()
    ledger.add(value)
    with pytest.raises(AppError, match="ID_CONFLICT"):
        ledger.add(value.model_copy(update={"note": "different"}))
    assert ledger.entry(str(value.entry_id))["note"] == ""


def test_batch_is_atomic_when_a_later_entry_is_invalid(ledger):
    first = entry()
    second = EntryInput(book_id="unbound", reading_date="2026-10-22", duration_seconds=60)
    with pytest.raises(AppError, match="BOOK_UNBOUND"):
        ledger.import_entries([first, second])
    assert ledger.entries("default") == []


def test_sqlite_failure_rolls_back_entry_and_audit(ledger):
    ledger.conn.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT,'failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.add(entry())
    assert ledger.entries("default") == []


@pytest.mark.parametrize("seconds", [0, -1, 1.5, True, "60", 86401])
def test_duration_is_strict_positive_integer(seconds):
    with pytest.raises(ValidationError):
        EntryInput(book_id="A", reading_date="2026-10-22", duration_seconds=seconds)


def test_future_date_and_invalid_timezone_are_rejected():
    for values in ({"reading_date": date.today() + timedelta(days=2)}, {"timezone": "invalid/place"}):
        with pytest.raises(ValidationError):
            EntryInput.model_validate({"book_id": "A", "reading_date": "2026-10-22", "duration_seconds": 60, **values})


def test_book_version_requires_explicit_confirmation():
    value = Book(book_id="A", title="安娜", author="作者")
    with pytest.raises(AppError, match="BOOK_UNCONFIRMED"):
        confirm_book(value, confirmed=False)
    with pytest.raises(AppError, match="EDITION_UNKNOWN"):
        confirm_book(value, confirmed=True)
    assert confirm_book(value, confirmed=True, corresponding_edition=True).corresponding_edition
    with pytest.raises(ValidationError):
        Book(book_id="A", title="  ", author="作者")


def test_account_scoped_duplicates_and_access(ledger):
    first = ledger.add(entry(account_id="one"))
    ledger.add(entry(account_id="two"))
    with pytest.raises(AppError, match="ENTRY_NOT_FOUND"):
        ledger.entry(first["entry_id"], "two")


def test_mock_database_cannot_be_opened_as_real(tmp_path):
    path = tmp_path / "test.sqlite"
    Database(path, mock=True).close()
    with pytest.raises(AppError, match="BACKEND_MISMATCH"):
        Database(path, mock=False)


def test_session_bounds_and_timezone():
    value = EntryInput(book_id="A", reading_date="2026-10-22", duration_seconds=3600,
                       session_start="2026-10-22T10:00:00+08:00", session_end="2026-10-22T11:00:00+08:00")
    assert value.duration_seconds == 3600
    with pytest.raises(ValidationError):
        EntryInput.model_validate({**value.model_dump(), "session_end": "2026-10-22T10:30:00+08:00"})


def test_database_rejects_budget_violation(ledger):
    value = ledger.add(entry())
    with pytest.raises(sqlite3.IntegrityError):
        ledger.conn.execute("UPDATE reading_entries SET verified_seconds=3601 WHERE entry_id=?", (value["entry_id"],))


def test_version_one_migration_keeps_reading_entries(tmp_path):
    path=tmp_path/"v1.sqlite"
    with Database(path) as db:
        db.bind(confirm_book(Book(book_id="A",title="合成书",author="作者",publisher="出版社"),confirmed=True))
        item=db.add(entry())
        db.conn.execute("DROP TABLE book_routes")
        db.conn.execute("PRAGMA user_version=1")
    with Database(path) as db:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0]==3
        assert db.entry(item["entry_id"])["duration_seconds"]==3600
        db.save_route("default","A","route-A","https://weread.qq.com/web/reader/route-A","synthetic-hash")
        assert db.route("default","A")["web_book_id"]=="route-A"
