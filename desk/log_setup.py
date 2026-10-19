"""Logging: console + size-rotated log files, with secrets masked out.

Every line (including error tracebacks) passes through RedactingFormatter,
which replaces any known secret value and anything that looks like an API
key or token with "***". Each process writes its own file (desk.log,
dashboard.log, watchdog.log) so rotation never fights over a locked file.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

MASK = "***"

# Things that look like secrets even if we weren't told about them.
_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),                    # Anthropic keys
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"),             # Telegram bot tokens
    re.compile(r"(?<=/bot)[^/\s]+(?=/)"),                        # token inside a Telegram API URL
    re.compile(r"(?i)(?<=auth_token=)[^&\s'\"]+"),               # CryptoPanic token in a URL
    re.compile(r"(?i)(?<=['\"]sign['\"]: ['\"])[0-9a-f]{32,}"),  # HMAC signature in a header dict
    re.compile(r"(?i)(?<=['\"]key['\"]: ['\"])[^'\"]{8,}"),      # API key in a header dict
]


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for s in sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True):
        text = text.replace(s, MASK)
    for pat in _PATTERNS:
        text = pat.sub(MASK, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formats a record normally, then masks secrets in the finished text."""

    def __init__(self, fmt: str, secrets: Iterable[str] = ()):
        super().__init__(fmt=fmt, datefmt="%Y-%m-%d %H:%M:%S")
        self._secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)


_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(
    log_dir: str | Path,
    *,
    process_name: str = "desk",
    level: str = "INFO",
    max_mb: float = 10,
    backups: int = 5,
    secrets: Iterable[str] = (),
    console: bool = True,
) -> logging.Logger:
    """Configure the root logger. Safe to call more than once."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    secrets = list(secrets)

    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_desk_handler", False):     # only replace handlers we created
            root.removeHandler(h)
            h.close()
    root.setLevel(level)

    file_handler = RotatingFileHandler(
        log_dir / f"{process_name}.log",
        maxBytes=int(max_mb * 1024 * 1024),
        backupCount=backups,
        encoding="utf-8",
    )
    file_handler.setFormatter(RedactingFormatter(_FORMAT, secrets))
    file_handler._desk_handler = True
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(RedactingFormatter(_FORMAT, secrets))
        stream._desk_handler = True
        root.addHandler(stream)

    # HTTP libraries log full request URLs at INFO (Telegram puts its token in
    # the URL), so keep them quiet. The formatter would mask it anyway.
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("desk")
