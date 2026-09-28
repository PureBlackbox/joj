"""Optional Telegram notifications. Never raises: a failed notification must not affect trading."""
from __future__ import annotations

import logging

from . import aio_http

log = logging.getLogger("relay.notify")


class Notifier:
    def __init__(self, bot_token: str, chat_id: str, dry_run: bool = False):
        self.token, self.chat_id, self.dry_run = bot_token, chat_id, dry_run
        self.enabled = bool(bot_token and chat_id)

    async def send(self, text: str) -> None:
        prefix = "[DRY RUN] " if self.dry_run else ""
        log.info("NOTIFY %s%s", prefix, text)
        if not self.enabled:
            return
        try:
            await aio_http.request(
                "POST", f"https://api.telegram.org/bot{self.token}/sendMessage",
                json_body={"chat_id": self.chat_id, "text": f"{prefix}{text}"[:3500]}, timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - notifications are best effort
            log.warning("telegram notification failed: %s", type(exc).__name__)
