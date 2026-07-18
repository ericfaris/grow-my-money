"""Structured logging with a secret-redaction filter.

Defense-in-depth: even if a credential value reaches a log call (e.g. echoed in
an SDK traceback), the redaction filter scrubs registered secret substrings from
every record before it is emitted.
"""
from __future__ import annotations

import logging
import sys

_REDACT: set[str] = set()


class RedactionFilter(logging.Filter):
    """Replace any registered secret substring in a log message with ****."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = msg
        for secret in _REDACT:
            if secret and secret in redacted:
                redacted = redacted.replace(secret, "****")
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def register_secret(value: str | None) -> None:
    """Register a value to be scrubbed from all future log records."""
    if value and len(str(value)) >= 4:
        _REDACT.add(str(value))


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if getattr(root, "_gmm_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler.addFilter(RedactionFilter())
    root.addHandler(handler)
    root.setLevel(level)
    root._gmm_configured = True  # type: ignore[attr-defined]
