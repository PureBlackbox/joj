"""Everything that happens between 'HTTP body arrives' and 'job is queued'.

Kept free of any web framework so it can be tested directly. It returns (status_code, body).
Nothing here talks to the broker: the alert is authenticated, validated, de-duplicated and
queued in a few milliseconds (TradingView gives up after 3 seconds), then the worker executes it.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections import defaultdict, deque
from typing import Callable, Tuple

from .config import Settings
from .executor import Executor
from .models import SignalError, parse_signal
from .store import Store

log = logging.getLogger("relay.webhook")

MAX_BODY_BYTES = 8192
AUTH_FAIL_LIMIT = 10          # this many bad passphrases from one IP ...
AUTH_FAIL_WINDOW = 600        # ... within 10 minutes -> temporarily blocked


class WebhookHandler:
    def __init__(self, settings: Settings, store: Store, queue: asyncio.Queue,
                 clock: Callable[[], float] = time.time):
        self.s, self.store, self.queue, self._clock = settings, store, queue, clock
        self._fails = defaultdict(deque)     # ip -> timestamps of failed authentications

    def _blocked(self, ip: str) -> bool:
        now, q = self._clock(), self._fails[ip]
        while q and now - q[0] > AUTH_FAIL_WINDOW:
            q.popleft()
        return len(q) >= AUTH_FAIL_LIMIT

    def handle(self, raw: bytes, client_ip: str) -> Tuple[int, dict]:
        s = self.s
        if s.allowed_ips and client_ip not in s.allowed_ips:
            log.warning("REJECTED webhook from %s: IP not in ALLOWED_IPS", client_ip)
            return 403, {"status": "forbidden"}
        if self._blocked(client_ip):
            log.warning("REJECTED webhook from %s: too many failed authentications", client_ip)
            return 429, {"status": "too_many_requests"}
        if len(raw) > MAX_BODY_BYTES:
            log.warning("REJECTED webhook from %s: body too large (%d bytes)", client_ip, len(raw))
            return 413, {"status": "too_large"}
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            log.warning("REJECTED webhook from %s: body is not JSON (%d bytes)", client_ip, len(raw))
            return 400, {"status": "not_json"}
        if not isinstance(data, dict):
            return 400, {"status": "not_an_object"}

        # authentication: constant-time compare of the passphrase inside the JSON body
        sent = data.get("passphrase")
        if not isinstance(sent, str) or not hmac.compare_digest(sent.encode(), s.webhook_secret.encode()):
            self._fails[client_ip].append(self._clock())
            log.warning("REJECTED webhook from %s: wrong or missing passphrase", client_ip)
            return 401, {"status": "unauthorized"}

        safe = {k: v for k, v in data.items() if k != "passphrase"}
        try:
            sig = parse_signal(data)
        except SignalError as exc:
            log.warning("REJECTED webhook from %s: invalid alert (%s) payload=%s", client_ip, exc, json.dumps(safe)[:500])
            return 422, {"status": "invalid", "error": str(exc)}

        key = sig.dedupe_key()
        log.info("SIGNAL RECEIVED key=%s event=%s trade=%s leg=%s side=%s qty=%s price=%s stop=%s target=%s "
                 "lev=%s conf=%s mode=%s risk=%s reason=%s ip=%s", key, sig.event, sig.trade_id, sig.leg, sig.side,
                 sig.qty, sig.price, sig.stop, sig.target, sig.leverage, sig.confidence, sig.mode, sig.risk,
                 sig.reason, client_ip)
        if not self.store.register(key, sig.trade_id, sig.event, sig.leg, safe):
            log.info("DUPLICATE ignored key=%s", key)
            return 200, {"status": "duplicate", "key": key}
        try:
            self.queue.put_nowait((sig, key))
        except asyncio.QueueFull:
            self.store.set_status(key, "REJECTED", "QUEUE_FULL")
            log.error("queue full, dropped %s", key)
            return 503, {"status": "busy"}
        return 200, {"status": "accepted", "key": key}


async def worker(queue: asyncio.Queue, executor: Executor) -> None:
    """Executes queued signals strictly one at a time, in arrival order."""
    while True:
        sig, key = await queue.get()
        try:
            await executor.handle(sig, key)
        except Exception:  # noqa: BLE001 - the worker must never die
            log.exception("worker error on %s", key)
        finally:
            queue.task_done()
