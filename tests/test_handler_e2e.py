"""Webhook handler tests and a full end-to-end run: raw Pine-style JSON -> handler -> queue -> worker -> broker."""
import asyncio
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path

from app.handler import WebhookHandler, worker
from app.store import Store
from tests.fake_capital import FakeCapital
from tests.helpers import SECRET, Env, alert, make_settings


def body(event="BUY", **kw) -> bytes:
    return json.dumps(alert(event, **kw)).encode()


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "h.db")
        self.addCleanup(self.store.close)
        self.queue = asyncio.Queue()
        self.now = [1_000_000.0]
        self.h = self._handler()

    def _handler(self, **env):
        s = make_settings("http://127.0.0.1:1", DATA_DIR=self.tmp.name, **env)
        return WebhookHandler(s, self.store, self.queue, clock=lambda: self.now[0])

    def test_valid_alert_is_accepted_and_queued(self):
        code, resp = self.h.handle(body(), "1.2.3.4")
        self.assertEqual((code, resp["status"]), (200, "accepted"))
        self.assertEqual(self.queue.qsize(), 1)

    def test_wrong_and_missing_passphrase_are_rejected(self):
        self.assertEqual(self.h.handle(body(passphrase="nope"), "1.2.3.4")[0], 401)
        no_pass = json.loads(body()); del no_pass["passphrase"]
        self.assertEqual(self.h.handle(json.dumps(no_pass).encode(), "1.2.3.4")[0], 401)
        self.assertEqual(self.h.handle(body(passphrase=12345), "1.2.3.4")[0], 401)
        self.assertEqual(self.queue.qsize(), 0)

    def test_duplicate_trade_id_is_not_queued_twice(self):
        self.assertEqual(self.h.handle(body(), "1.2.3.4")[1]["status"], "accepted")
        self.assertEqual(self.h.handle(body(), "1.2.3.4")[1]["status"], "duplicate")
        self.assertEqual(self.queue.qsize(), 1)

    def test_duplicates_survive_a_restart(self):
        self.h.handle(body(), "1.2.3.4")
        h2 = self._handler()                       # a new handler on the same database
        self.assertEqual(h2.handle(body(), "1.2.3.4")[1]["status"], "duplicate")

    def test_garbage_is_rejected(self):
        self.assertEqual(self.h.handle(b"not json", "1.2.3.4")[0], 400)
        self.assertEqual(self.h.handle(b"[1,2,3]", "1.2.3.4")[0], 400)
        self.assertEqual(self.h.handle(b"x" * 9000, "1.2.3.4")[0], 413)
        self.assertEqual(self.h.handle(body(event="HODL"), "1.2.3.4")[0], 422)
        self.assertEqual(self.h.handle(body(size="abc"), "1.2.3.4")[0], 422)

    def test_ip_allowlist(self):
        h = self._handler(ALLOWED_IPS="52.89.214.238, 34.212.75.30")
        self.assertEqual(h.handle(body(), "6.6.6.6")[0], 403)
        self.assertEqual(h.handle(body(), "34.212.75.30")[0], 200)

    def test_brute_force_lockout_then_recovery(self):
        for _ in range(10):
            self.assertEqual(self.h.handle(body(passphrase="guess"), "9.9.9.9")[0], 401)
        self.assertEqual(self.h.handle(body(), "9.9.9.9")[0], 429)          # even the right secret is blocked now
        self.assertEqual(self.h.handle(body(trade_id="AMF-OTHER-IP-001"), "8.8.8.8")[0], 200)
        self.now[0] += 601
        self.assertEqual(self.h.handle(body(trade_id="AMF-LATER-001"), "9.9.9.9")[0], 200)

    def test_secret_never_appears_in_logs(self):
        with self.assertLogs("relay.webhook", level="INFO") as cm:
            self.h.handle(body(), "1.2.3.4")
            self.h.handle(body(passphrase="wrong-guess-value"), "1.2.3.4")
            self.h.handle(body(event="HODL"), "1.2.3.4")
        self.assertNotIn(SECRET, "\n".join(cm.output))

    def test_queue_full(self):
        self.queue = asyncio.Queue(maxsize=1)
        h = self._handler(); h.queue = self.queue
        self.assertEqual(h.handle(body(trade_id="AMF-Q-001"), "1.1.1.1")[0], 200)
        self.assertEqual(h.handle(body(trade_id="AMF-Q-002"), "1.1.1.1")[0], 503)


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_trade_lifecycle_through_the_queue(self):
        fake = FakeCapital().start()
        self.addCleanup(fake.stop)
        env = Env(fake)
        self.addCleanup(env.close)
        queue = asyncio.Queue()
        handler = WebhookHandler(env.s, env.store, queue)
        task = asyncio.create_task(worker(queue, env.executor))
        self.addCleanup(task.cancel)
        tid = "AMF-20260928-143522-L-001"

        async def send(event, **kw):
            code, resp = handler.handle(body(event, trade_id=tid, **kw), "52.89.214.238")
            self.assertEqual(code, 200, resp)
            await queue.join()
            return resp

        await send("BUY")
        self.assertEqual(fake.first_position()["size"], 0.001)
        await send("UPDATE", bar_time=11, stop_loss=78900.0, take_profit=79400.0)
        self.assertEqual(fake.first_position()["stopLevel"], 78900.0)
        await send("UPDATE", bar_time=12, stop_loss=78850.0, take_profit=79400.0)      # widening: blocked
        self.assertEqual(fake.first_position()["stopLevel"], 78900.0)
        r = await send("BUY")                                                             # duplicate delivery
        self.assertEqual(r["status"], "duplicate")
        await send("CLOSE", leg="E0", reason="EXIT_FILL")
        self.assertEqual(fake.positions, {})
        r = await send("CLOSE", leg="E1", reason="EXIT_FILL")                             # second exit-fill alert
        self.assertEqual(r["status"], "duplicate")
        self.assertEqual(len(fake.order_calls()), 3)      # open, one amend, one close - nothing else
        recent = {row["event"]: row["status"] for row in env.store.recent(20)}
        self.assertEqual(recent["BUY"], "DONE")
        self.assertEqual(recent["CLOSE"], "DONE")


if __name__ == "__main__":
    unittest.main()
