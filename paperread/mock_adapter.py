"""Clearly synthetic, persistent demo server in a separate Mock database."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from paperread.models import Book, StatsPair


class MockStats:
    source = "mock"
    identity_hash = "mock-reader-identity"

    def __init__(self, db, account_id="default"):
        self.db = db
        self.account_id = account_id

    def _get(self, book_id):
        key = "mock-book:" + self.account_id + ":" + book_id
        row = self.db.conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        total = self.db.conn.execute("SELECT value FROM metadata WHERE key=?", (self._total_key(),)).fetchone()
        return {"book_seconds": int(row[0]) if row else 0, "period_seconds": int(total[0]) if total else 0}

    def _total_key(self):
        return "mock-total:" + self.account_id + ":" + datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")

    def advance(self, book_id, seconds):
        value = self._get(book_id)
        value["book_seconds"] += seconds
        value["period_seconds"] += seconds
        with self.db.transaction():
            self.db.conn.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)",
                ("mock-book:" + self.account_id + ":" + book_id, str(value["book_seconds"])))
            self.db.conn.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (self._total_key(), str(value["period_seconds"])))

    def search(self, query, **kwargs):
        return [Book(book_id="mock-anna-A", title="安娜·卡列尼娜（示例译本 A）", author="列夫·托尔斯泰",
                     translator="示例译者 A", publisher="示例出版社", origin="mock"),
                Book(book_id="mock-anna-B", title="安娜·卡列尼娜（示例译本 B）", author="列夫·托尔斯泰",
                     translator="示例译者 B", publisher="示例出版社", origin="mock")]

    def book_info(self, book_id):
        return self.db.candidate(book_id)

    def pair(self, book_id, timezone):
        now = datetime.now(ZoneInfo(timezone))
        base = int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        return StatsPair(book_id=book_id, account_id=self.account_id, **self._get(book_id),
                         period_key=f"monthly:{base}", source="mock", identity_hash=self.identity_hash,
                         evidence={"synthetic": True, "units": "seconds"})

    def close(self):
        pass
