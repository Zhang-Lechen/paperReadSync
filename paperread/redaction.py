"""Conservative redaction for local diagnostics, never a credential store."""

from collections.abc import Mapping
import logging
import re
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE = re.compile(
    r"cookie|authorization|api[_-]?key|token|secret|password|credential|"
    r"wr[_-]?(?:skey|vid)|sign[_-]?key|signature|captured_(?:headers|cookies|payload|appId)|headers|payload|"
    r"^(?:ps|pc|s|sg|appId|user_id|user_info|curl)$", re.I,
)
_MARKERS = re.compile(
    r"cookie|authorization|bearer|api[_-]?key|wr[_-]?(?:skey|vid)|"
    r"token|secret|password|credential|signature|captured_(?:headers|cookies|payload|appId)|payload|headers|"
    r"\b(?:ps|pc|sg|sign_key)\s*[:=]|\bwrk-", re.I,
)


def redact(value: Any) -> Any:
    """Drop entire sensitive containers and strings, including partial tokens."""
    if isinstance(value, Mapping):
        return {str(key): REDACTED if _SENSITIVE.search(str(key)) else redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return REDACTED if _MARKERS.search(value) else value
    if value is None or type(value) in (int, float, bool):
        return value
    # Unknown objects can have secret-bearing __str__/__repr__ implementations.
    return REDACTED


class RedactingFormatter(logging.Formatter):
    """Redact after formatting, so exception text and %-arguments are covered."""

    def format(self, record: logging.LogRecord) -> str:
        return str(redact(super().format(record)))
