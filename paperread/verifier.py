from dataclasses import dataclass
from datetime import datetime

from paperread.models import StatsPair

WEB_TOLERANCE_SECONDS = 5

@dataclass(frozen=True)
class Verdict:
    code: str
    attributable_seconds: int = 0
    complete: bool = False


def book_time_metric(pair):
    if pair.source == "mock":
        return "mock_seconds"
    field = pair.evidence.get("book_time_field")
    if field:
        return field
    book = pair.evidence.get("book", {})
    if isinstance(book,dict) and "recordReadingTime" in book and "readingTime" not in book:
        return "book.recordReadingTime"
    return "unspecified"


def compare(before: StatsPair, after: StatsPair, *, requested_seconds: int, elapsed_seconds: int,
            tolerance_seconds: int = 0) -> Verdict:
    if type(tolerance_seconds) is not int or not 0 <= tolerance_seconds <= WEB_TOLERANCE_SECONDS:
        raise ValueError("Invalid verification tolerance")
    for key in ("book_id", "account_id", "source", "identity_hash", "period_key"):
        if getattr(before, key) != getattr(after, key):
            return Verdict("IDENTITY_OR_PERIOD_MISMATCH")
    if book_time_metric(before) != book_time_metric(after):
        return Verdict("MEASUREMENT_CHANGED")
    try:
        if datetime.fromisoformat(after.captured_at) <= datetime.fromisoformat(before.captured_at):
            return Verdict("STALE_OBSERVATION")
    except (TypeError, ValueError):
        return Verdict("INVALID_CAPTURE_TIME")
    if (before.server_timestamp is not None and after.server_timestamp is not None
            and after.server_timestamp < before.server_timestamp):
        return Verdict("SERVER_TIME_REVERSED")
    book_delta = after.book_seconds - before.book_seconds
    period_delta = after.period_seconds - before.period_seconds
    if book_delta < 0 or period_delta < 0:
        return Verdict("COUNTER_DECREASED")
    if book_delta == 0 and period_delta == 0:
        return Verdict("NO_REMOTE_CHANGE")
    tolerance = min(tolerance_seconds, max(1, requested_seconds // 10))
    if (tolerance > 0 and elapsed_seconds >= requested_seconds and book_delta > 0 and period_delta > 0
            and abs(book_delta - requested_seconds) <= tolerance
            and abs(period_delta - requested_seconds) <= tolerance
            and abs(book_delta - period_delta) <= tolerance):
        code = "DUAL_DELTA_CONFIRMED" if book_delta == period_delta == requested_seconds else "DUAL_DELTA_WITH_TOLERANCE"
        return Verdict(code, requested_seconds, True)
    if book_delta == 0 or book_delta != period_delta:
        return Verdict("ATTRIBUTION_UNPROVEN")
    if book_delta > min(requested_seconds, elapsed_seconds):
        return Verdict("DELTA_EXCEEDS_BUDGET")
    return Verdict("DUAL_DELTA_CONFIRMED", book_delta, book_delta == requested_seconds)
