import json
from pathlib import Path
from uuid import uuid4

import pytest

from paperread.book_match import confirm_book
from paperread.db import Database
from paperread.errors import AppError
from paperread.mock_adapter import MockStats
from paperread.models import Book,EntryInput
from paperread.sync_service import SyncService
from paperread.wereadgear_adapter import MockWriter


def setup(db_path,lock_path,seconds=90):
    db=Database(db_path,mock=True)
    db.bind(confirm_book(Book(book_id="mock-A",title="合成书",author="作者",publisher="示例",origin="mock"),confirmed=True))
    value=db.add(EntryInput(book_id="mock-A",reading_date="2026-10-22",duration_seconds=seconds))
    stats=MockStats(db)
    writer=MockWriter(stats)
    service=SyncService(db,stats,writer,lock_path=lock_path)
    service.baseline(value["entry_id"])
    service.attest(value["entry_id"],app_book_seconds=0,app_period_seconds=0,same_account=True,no_other_reading=True)
    return db,service,value["entry_id"],writer


def test_budget_after_probe_cannot_exceed_actual_remainder(tmp_path):
    db,service,entry,writer=setup(tmp_path/"db.sqlite",tmp_path/"writer.lock")
    try:
        service.start(entry,seconds=60)
        service.verify(entry,app_book_seconds=60,app_period_seconds=60,no_other_reading=True,same_day_confirmed=True)
        with pytest.raises(AppError,match="BUDGET_EXCEEDED"):
            service.start(entry,seconds=60,purpose="remaining")
        assert writer.calls==[("mock-A",60)]
    finally: db.close()


def test_sql_intent_failure_makes_zero_writer_calls(tmp_path):
    db,service,entry,writer=setup(tmp_path/"db.sqlite",tmp_path/"writer.lock")
    try:
        db.conn.execute("CREATE TRIGGER fail_intent BEFORE INSERT ON sync_attempts BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError): service.start(entry,seconds=60)
        assert writer.calls==[]
        assert not service.pending_path.exists()
        assert not service._active()
    finally: db.close()


def test_pending_intent_in_another_database_blocks_same_machine(tmp_path):
    lock=tmp_path/"writer.lock"
    db,service,entry,writer=setup(tmp_path/"one.sqlite",lock)
    second,other,entry_two,writer_two=setup(tmp_path/"two.sqlite",lock)
    try:
        service.start(entry,seconds=60)
        with pytest.raises(AppError,match="RECONCILE_REQUIRED"):
            other.start(entry_two,seconds=60)
        assert writer_two.calls==[]
    finally:
        db.close();second.close()


def test_completed_attempt_can_clean_marker_after_crash_between_commit_and_unlink(tmp_path):
    db,service,entry,writer=setup(tmp_path/"db.sqlite",tmp_path/"writer.lock")
    try:
        result=service.start(entry,seconds=60)
        service.verify(entry,app_book_seconds=60,app_period_seconds=60,no_other_reading=True,same_day_confirmed=True)
        service.pending_path.write_text(json.dumps({"attempt_id":result["attempt_id"],"database":str(db.path.resolve())}),encoding="utf-8")
        service.reconcile(entry)
        assert not service.pending_path.exists()
        assert len(writer.calls)==1
    finally: db.close()
