import io
import logging

import pytest

from paperread.redaction import REDACTED, RedactingFormatter, redact


@pytest.mark.parametrize("name", ["wr_skey", "wr_vid", "WEREAD_API_KEY", "Cookie", "Set-Cookie", "Authorization", "captured_payload", "headers", "ps", "pc", "s", "sg", "user_info", "sign_key"])
def test_sensitive_values_and_containers_are_dropped(name):
    assert redact({"nested": [{name: "synthetic-never-log-this"}]}) == {
        "nested": [{name: REDACTED}],
    }


def test_unknown_objects_never_have_their_repr_or_str_called():
    class Secret:
        def __repr__(self):
            raise AssertionError("must not render secret objects")

        __str__ = __repr__

    assert redact(Secret()) == REDACTED


def test_log_formatter_covers_arguments_tracebacks_and_partial_tokens():
    stream = io.StringIO()
    logger = logging.Logger("redaction-test")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.warning("Cookie=%s", "synthetic-never-log-this")
    try:
        raise ValueError("WEREAD_API_KEY=synthetic-never-log-this")
    except ValueError:
        logger.exception("request failed")
    logger.info("wr_skey prefix: %s***", "synthet")
    assert "synthetic-never-log-this" not in stream.getvalue()
    assert "synthet" not in stream.getvalue()
    assert stream.getvalue().count(REDACTED) == 3


def test_non_sensitive_evidence_is_preserved():
    value = {"book_id": "test-A", "before_seconds": 12, "status": "NOT_RUN", "captured_at":"2026-10-23T00:00:00Z"}
    assert redact(value) == value
