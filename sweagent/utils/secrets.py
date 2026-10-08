"""Redact credentials before formatting logs or accepting model output."""
from __future__ import annotations

import logging
import os
import re
import traceback

_SECRETS: set[str] = set()
_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}|\b(?:ghp_|github_pat_)[A-Za-z0-9_]+")
_AUTH = re.compile(r"(?i)(authorization\s*[:=]\s*)([^\r\n]+)")


def register_secret(value: str) -> None:
    if value:
        _SECRETS.add(value)


def credential_free_environment() -> dict[str, str]:
    """Docker Compose interpolation must not inherit model credentials."""
    return {name: value for name, value in os.environ.items()
            if not any(word in name.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))}


def redact(text: str) -> str:
    for name, value in os.environ.items():
        if value and any(word in name.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            text = text.replace(value, "[REDACTED]")
    for value in _SECRETS:
        text = text.replace(value, "[REDACTED]")
    text = _TOKEN.sub("[REDACTED]", text)
    return _AUTH.sub(r"\1[REDACTED]", text)


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + "".join(traceback.format_exception(*record.exc_info))
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(text)
        record.args = ()
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True
