"""Explicitly enabled official Gateway reads; no local-history fallback."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from paperread.credentials import official_key
from paperread.errors import AppError
from paperread.models import Book, StatsPair, utc_now

GATEWAY = "https://i.weread.qq.com/api/agent/gateway"
READ_APIS = {"/store/search", "/shelf/sync", "/book/info", "/book/getprogress", "/readdata/detail"}


class OfficialStats:
    source = "official"

    def __init__(self, *, root: Path, enabled: bool, account_id: str,
                 client: httpx.Client | None = None, key: str | None = None):
        if not enabled:
            raise AppError("READ_ONLY_REQUIRED", "远端查询需显式使用 --read-only；默认仅访问本地")
        self.account_id = account_id
        self.key = key if key is not None else official_key(account_id)
        self.identity_hash = hashlib.sha256(self.key.encode()).hexdigest()
        try:
            lock = json.loads((root / "upstream.lock.json").read_text(encoding="utf-8"))
            # Parse the pinned document rather than trusting an out-of-date constant.
            text = (root / "vendor" / "WeChatReading" / "skills" / "SKILL.md").read_text(encoding="utf-8")
            import re
            found = re.search(r"^version:\s*([0-9.]+)\s*$", text, re.M)
            if not found or found[1] != lock["official_skill_version"]:
                raise ValueError()
            self.version = found[1]
        except (OSError, ValueError, KeyError):
            raise AppError("OFFICIAL_SCHEMA", "官方文档版本未锁定，请更新并重新审计") from None
        self._owned = client is None
        self.client = client or httpx.Client(timeout=15, follow_redirects=False, trust_env=False,
                                            transport=httpx.HTTPTransport(retries=0))

    def __repr__(self):
        return "OfficialStats(<private>)"

    def close(self):
        if self._owned:
            self.client.close()

    def request(self, api_name: str, **params) -> dict:
        if api_name not in READ_APIS or any(k in params for k in ("api_name", "skill_version", "params")):
            raise AppError("READ_API_DENIED", "仅允许审计过的官方只读接口")
        try:
            response = self.client.post(GATEWAY, headers={"Authorization": "Bearer " + self.key},
                                        json={"api_name": api_name, "skill_version": self.version, **params})
        except httpx.HTTPError:
            raise AppError("READ_NETWORK", "只读统计网络失败，未重试") from None
        if response.status_code in (401, 403):
            raise AppError("READ_AUTH", "只读授权失效或无权访问，请在本机重新授权")
        if response.status_code == 429:
            raise AppError("READ_RATE_LIMIT", "服务端限流，已停止且不自动重试")
        if response.status_code != 200:
            raise AppError("READ_HTTP", "只读接口返回非成功状态，已停止")
        try:
            value = response.json()
        except ValueError:
            raise AppError("READ_SCHEMA", "只读接口未返回有效 JSON") from None
        if not isinstance(value, dict):
            raise AppError("READ_SCHEMA", "只读接口结构不符合锁定文档")
        if "upgrade_info" in value:
            raise AppError("UPGRADE_REQUIRED", "服务端要求更新官方接口文档，已停止")
        if value.get("errcode", 0) != 0:
            raise AppError("READ_REMOTE_ERROR", "只读接口业务失败，请检查本地授权和接口版本")
        # The pinned documentation describes top-level fields; do not guess envelopes.
        return value

    def search(self, query: str, *, max_idx: int = 0) -> list[Book]:
        if not query.strip():
            raise AppError("QUERY_EMPTY", "搜索书名不能为空")
        value = self.request("/store/search", keyword=query.strip(), scope=10, maxIdx=max_idx)
        groups = value.get("results")
        if not isinstance(groups, list):
            raise AppError("READ_SCHEMA", "搜索返回缺少结果列表")
        found = {}
        indexes = []
        try:
            for group in groups:
                for result in group.get("books", []):
                    info = result.get("bookInfo", {})
                    if type(result.get("searchIdx")) is int:
                        indexes.append(result["searchIdx"])
                    if info.get("soldout") == 1:
                        continue
                    book = self._book(info)
                    found[book.book_id] = book
        except (AttributeError, TypeError, ValidationError):
            raise AppError("READ_SCHEMA", "搜索书目缺少必要字段") from None
        self.page_info = {"has_more": value.get("hasMore") == 1,
                          "next_max_idx": indexes[-1] if indexes else None}
        return list(found.values())

    @staticmethod
    def _book(value: dict) -> Book:
        return Book(book_id=value.get("bookId"), title=value.get("title"), author=value.get("author"),
                    translator=value.get("translator") or "", publisher=value.get("publisher") or "",
                    isbn=value.get("isbn") or "", origin="official", reader_link=value.get("readerLink") or "")

    def shelf(self) -> list[Book]:
        # Only books[] can participate in book-specific reading sessions.
        # The documented response has no reliable imported-book type flag.
        values = self.request("/shelf/sync").get("books")
        if not isinstance(values, list):
            raise AppError("READ_SCHEMA", "书架返回缺少书籍列表")
        found = {}
        try:
            for value in values:
                book = self._book({**value, "author": value.get("author") or "作者未提供"})
                found[book.book_id] = book.model_copy(update={"discovery_source": "shelf"})
        except (AttributeError, TypeError, ValidationError):
            raise AppError("READ_SCHEMA", "书架书籍缺少必要字段") from None
        return list(found.values())

    def shelf_book(self, book_id: str) -> Book:
        for book in self.shelf():
            if book.book_id == book_id:
                return book
        raise AppError("BOOK_NOT_FOUND", "当前账号书架中未找到这本书，请刷新书架后重新选择")

    def book_info(self, book_id: str) -> Book:
        try:
            book = self._book(self.request("/book/info", bookId=book_id))
        except ValidationError:
            raise AppError("READ_SCHEMA", "书籍详情缺少必要字段") from None
        if book.book_id != book_id:
            raise AppError("BOOK_MISMATCH", "返回书籍 ID 与所选书籍不一致")
        return book

    def pair(self, book_id: str, timezone: str) -> StatsPair:
        # Capture book + monthly total; timestamps must be close enough to compare.
        book = self.request("/book/getprogress", bookId=book_id)
        period = self.request("/readdata/detail", mode="monthly")
        try:
            if book["bookId"] != book_id:
                raise AppError("BOOK_MISMATCH", "单书统计返回了其他书籍，已停止")
            # The live reader displays progress.book.readingTime / 3600 and
            # / 60. recordReadingTime is a different counter and can stay zero.
            # The pinned book.md labels the latter ambiguously; do not use it
            # as a fallback for ordinary reading duration.
            seconds = book["book"]["readingTime"]
            total = period["totalReadTime"]
            base = period["baseTime"]
            if book.get("timestamp") is not None and type(book["timestamp"]) is not int:
                raise ValueError()
            if type(seconds) is not int or type(total) is not int or type(base) is not int:
                raise ValueError()
            current = datetime.now(ZoneInfo(timezone))
            start = datetime.fromtimestamp(base, ZoneInfo(timezone))
            if (start.year, start.month, start.day) != (current.year, current.month, 1):
                raise AppError("PERIOD_MISMATCH", "官方月度统计边界与当前时区不一致")
            return StatsPair(
                book_id=book_id, account_id=self.account_id, book_seconds=seconds, period_seconds=total,
                period_key=f"monthly:{base}", source="official", identity_hash=self.identity_hash,
                server_timestamp=book.get("timestamp"), captured_at=utc_now(),
                evidence={"book_time_field":"book.readingTime",
                          "book": {"bookId": book_id, "readingTime": seconds,
                                   "timestamp": book.get("timestamp")},
                          "period": {"baseTime": base, "totalReadTime": total},
                          "skill_version": self.version, "units": "seconds"},
            )
        except (KeyError, TypeError, ValueError, ValidationError, OverflowError):
            raise AppError("READ_SCHEMA", "统计缺少可信单书/周期整秒数或周期边界，不能建立基线") from None
