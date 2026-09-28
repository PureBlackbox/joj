import tempfile
import time
from pathlib import Path

from app.capital import CapitalClient
from app.config import Settings
from app.executor import Executor
from app.models import parse_signal
from app.notify import Notifier
from app.safety import KillSwitch
from app.store import Store

SECRET = "s3cret-passphrase-1234567890"

BASE_ENV = {
    "CAPITAL_API_KEY": "KEY", "CAPITAL_IDENTIFIER": "me@example.com", "CAPITAL_API_PASSWORD": "pw",
    "WEBHOOK_SECRET": SECRET, "DRY_RUN": "false", "CAPITAL_DEMO": "true",
}


def make_settings(base_url: str, **overrides) -> Settings:
    env = dict(BASE_ENV, CAPITAL_BASE_URL=base_url, **overrides)
    return Settings.from_env(env)


def alert(event="BUY", **kw) -> dict:
    """An alert exactly as the Pine v1.1 f_alert() builds it."""
    base = {"trade_id": "AMF-20260928-143522-L-001", "leg": "E0", "passphrase": SECRET, "event": event,
            "symbol": "BTCUSD", "tv_ticker": "BTCUSD", "direction": "LONG", "entry": 79010.0,
            "stop_loss": 78800.0, "take_profit": 79400.0, "confidence": 72.4, "mode": "SCALP",
            "risk_pct": 0.98, "leverage": 8.0, "size": 0.001, "reason": "SIGNAL",
            "bar_time": int(time.time() * 1000)}
    base.update(kw)
    return base


def sig(event="BUY", **kw):
    return parse_signal(alert(event, **kw))


class Env:
    """Executor + real CapitalClient wired to a FakeCapital server, in a temp directory."""

    def __init__(self, fake, **overrides):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = make_settings(fake.base_url, DATA_DIR=self.tmp.name, **overrides)
        self.broker = CapitalClient(self.s)
        self.store = Store(Path(self.tmp.name) / "relay.db")
        self.kill = KillSwitch(self.s.kill_switch, Path(self.tmp.name) / "KILL_SWITCH")
        self.executor = Executor(self.s, self.broker, self.store, self.kill, Notifier("", ""))

    async def run(self, signal):
        key = signal.dedupe_key()
        self.store.register(key, signal.trade_id, signal.event, signal.leg, {})
        await self.executor.handle(signal, key)
        return self.store.get_status(key)

    def close(self):
        self.store.close()
        self.tmp.cleanup()
