"""Logging to the console and to a rotating file, with secrets scrubbed from every line."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Iterable


class SecretFilter(logging.Filter):
    """Replace any known secret value with *** before a record is written."""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            msg = record.getMessage()
            for s in self._secrets:
                if s in msg:
                    msg = msg.replace(s, "***")
            record.msg, record.args = msg, ()
        return True


def setup_logging(level: str, data_dir: Path, secrets: Iterable[str]) -> None:
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    flt = SecretFilter(secrets)
    console = logging.StreamHandler()
    rotating = logging.handlers.RotatingFileHandler(log_dir / "relay.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    for h in (console, rotating):
        h.setFormatter(fmt)
        h.addFilter(flt)
        root.addHandler(h)
