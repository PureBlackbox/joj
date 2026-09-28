"""Pure-logic tests: config, signal parsing, safety checks, store, kill switch."""
import json
import tempfile
import unittest
from pathlib import Path

from app.config import ConfigError, Settings, load_env_file
from app.models import SignalError, parse_signal
from app.safety import (KillSwitch, check_levels, is_stale, leverage_check, normalize_size,
                        price_deviation_ok, stop_widens)
from app.store import Store
from tests.helpers import BASE_ENV, alert


class ConfigTests(unittest.TestCase):
    def test_valid(self):
        s = Settings.from_env(dict(BASE_ENV))
        self.assertTrue(s.capital_demo)
        self.assertEqual(s.allowed_epics, frozenset({"BTCUSD"}))
        self.assertEqual(s.capital_base_url, "https://demo-api-capital.backend-capital.com")

    def test_live_url_when_demo_off(self):
        s = Settings.from_env(dict(BASE_ENV, CAPITAL_DEMO="false"))
        self.assertEqual(s.capital_base_url, "https://api-capital.backend-capital.com")

    def test_defaults_are_safe(self):
        env = {k: v for k, v in BASE_ENV.items() if k not in ("DRY_RUN", "CAPITAL_DEMO")}
        s = Settings.from_env(env)
        self.assertTrue(s.dry_run)
        self.assertTrue(s.capital_demo)
        self.assertFalse(s.enable_reduce)

    def test_missing_and_weak_secrets_refuse_to_start(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({})
        for bad in ("short", "change_me", 'has"quote-aaaaaaaaaaaa', "has space aaaaaaaaaaaa"):
            with self.assertRaises(ConfigError, msg=bad):
                Settings.from_env(dict(BASE_ENV, WEBHOOK_SECRET=bad))

    def test_env_file_loader_real_env_wins(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('# comment\nA=1\nB="two"\nC=\'three\'\n\nD=keep\n')
            env = {"D": "from-real-env"}
            load_env_file(p, env)
            self.assertEqual(env, {"A": "1", "B": "two", "C": "three", "D": "from-real-env"})


class ModelTests(unittest.TestCase):
    def test_pine_v11_names(self):
        s = parse_signal(alert("BUY"))
        self.assertEqual((s.event, s.side, s.qty, s.stop, s.target, s.leverage), ("BUY", "LONG", 0.001, 78800.0, 79400.0, 8.0))
        self.assertEqual(s.trade_id, "AMF-20260928-143522-L-001")
        self.assertFalse(hasattr(s, "passphrase"))

    def test_short_alias_names(self):
        s = parse_signal({"id": "trade-1", "event": "sell", "side": "short", "price": 100, "stop": 110,
                          "target": 90, "confidence": 70, "mode": "SCALP", "risk": 1, "leverage": 3, "qty": 0.5})
        self.assertEqual((s.trade_id, s.event, s.side, s.price, s.stop, s.target, s.qty), ("trade-1", "SELL", "SHORT", 100, 110, 90, 0.5))

    def test_null_levels_allowed(self):
        s = parse_signal(alert("CLOSE", stop_loss=None, take_profit=None))
        self.assertIsNone(s.stop)
        self.assertIsNone(s.target)

    def test_rejections(self):
        bad = [alert(event="HODL"), alert(trade_id="x"), alert(trade_id="bad id with spaces"),
               alert(size="abc"), alert(size=-1), alert(size=float("nan")), alert(entry=0),
               alert(event="BUY", direction="SHORT"), alert(event="SELL", direction="LONG"),
               alert(symbol="btc usd!"), alert(size=True)]
        for b in bad:
            with self.assertRaises(SignalError, msg=str(b)):
                parse_signal(b)
        with self.assertRaises(SignalError):
            parse_signal([1, 2])

    def test_zero_qty_parses_so_the_safety_layer_can_log_it(self):
        self.assertEqual(parse_signal(alert(size=0)).qty, 0.0)

    def test_dedupe_keys(self):
        self.assertEqual(parse_signal(alert("BUY")).dedupe_key(), parse_signal(alert("BUY", entry=1.0)).dedupe_key())
        self.assertNotEqual(parse_signal(alert("ADD", leg="E1")).dedupe_key(), parse_signal(alert("ADD", leg="E2")).dedupe_key())
        c1, c2 = parse_signal(alert("CLOSE", leg="E0")), parse_signal(alert("CLOSE", leg="E1", reason="EXIT_FILL"))
        self.assertEqual(c1.dedupe_key(), c2.dedupe_key())            # many exit fills -> one close
        u1, u2 = parse_signal(alert("UPDATE", bar_time=1)), parse_signal(alert("UPDATE", bar_time=2))
        self.assertNotEqual(u1.dedupe_key(), u2.dedupe_key())          # stop may move every bar


class SafetyTests(unittest.TestCase):
    def test_normalize_size(self):
        self.assertEqual(normalize_size(0.00057, 0.0001, 0.0001, 100), (0.0005, "OK"))     # rounds DOWN
        self.assertEqual(normalize_size(0.00009, 0.0001, 0.0001, 100)[1], "SIZE_TOO_SMALL")
        self.assertEqual(normalize_size(0, 0.0001, 0.0001, 100)[1], "ZERO_QUANTITY")
        self.assertEqual(normalize_size(1000, 0.0001, 0.0001, 100)[1], "SIZE_ABOVE_BROKER_MAX")
        self.assertEqual(normalize_size(0.0003, None, None, None), (0.0003, "OK"))

    def test_leverage(self):
        self.assertEqual(leverage_check(79.0, 0, 10.0, 8.0, 22, 0.1)[:2], (True, "OK"))            # 7.9x vs 8x
        self.assertEqual(leverage_check(90.0, 0, 10.0, 8.0, 22, 0.1)[1], "LEVERAGE_ABOVE_SIGNAL")  # 9x > 8.8x
        self.assertEqual(leverage_check(300.0, 0, 10.0, 100.0, 22, 0.1)[1], "LEVERAGE_ABOVE_HARD_CAP")
        self.assertEqual(leverage_check(10.0, 0, 10.0, None, 22, 0.1)[1], "MISSING_LEVERAGE")
        self.assertEqual(leverage_check(10.0, 0, 0.0, 5.0, 22, 0.1)[1], "NO_EQUITY")
        self.assertEqual(leverage_check(50.0, 100.0, 10.0, None, 22, 0.1, enforce_received=False)[:2], (True, "OK"))
        self.assertEqual(leverage_check(150.0, 100.0, 10.0, None, 22, 0.1, enforce_received=False)[1], "LEVERAGE_ABOVE_HARD_CAP")

    def test_levels(self):
        self.assertEqual(check_levels("LONG", 100, 99, 102)[0], True)
        self.assertEqual(check_levels("LONG", 100, 101, 102)[1], "STOP_ON_WRONG_SIDE")
        self.assertEqual(check_levels("LONG", 100, 99, 99.5)[1], "TARGET_ON_WRONG_SIDE")
        self.assertEqual(check_levels("SHORT", 100, 101, 98)[0], True)
        self.assertEqual(check_levels("SHORT", 100, 99, 98)[1], "STOP_ON_WRONG_SIDE")
        self.assertEqual(check_levels("LONG", 100, None, None)[1], "MISSING_STOP")
        self.assertEqual(check_levels("LONG", 100, 99.99, None, min_distance_pct=0.1)[1], "STOP_TOO_CLOSE")

    def test_stop_widening(self):
        self.assertTrue(stop_widens("LONG", 100, 99))
        self.assertFalse(stop_widens("LONG", 100, 101))
        self.assertTrue(stop_widens("SHORT", 100, 101))
        self.assertFalse(stop_widens("SHORT", 100, 99))
        self.assertFalse(stop_widens("LONG", None, 50))

    def test_deviation_and_staleness(self):
        self.assertTrue(price_deviation_ok(100.2, 100, 0.5)[0])
        self.assertFalse(price_deviation_ok(101, 100, 0.5)[0])
        self.assertTrue(price_deviation_ok(None, 100, 0.5)[0])
        self.assertTrue(is_stale(0, 10_000_000, 900))
        self.assertFalse(is_stale(9_500_000, 10_000_000, 900))
        self.assertFalse(is_stale(None, 10_000_000, 900))

    def test_kill_switch_file_and_env(self):
        with tempfile.TemporaryDirectory() as d:
            flag = Path(d) / "sub" / "KILL_SWITCH"
            k = KillSwitch(False, flag)
            self.assertFalse(k.is_active())
            k.activate("test")
            self.assertTrue(k.is_active())
            self.assertIn("test", k.reason())
            self.assertTrue(k.deactivate())
            self.assertFalse(k.is_active())
            env_k = KillSwitch(True, flag)
            self.assertTrue(env_k.is_active())
            self.assertFalse(env_k.deactivate())      # env flag cannot be lifted from the API
            self.assertTrue(env_k.is_active())


class StoreTests(unittest.TestCase):
    def test_duplicates_and_state(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "x.db")
            self.assertTrue(st.register("k1", "t", "BUY", "E0", {"a": 1}))
            self.assertFalse(st.register("k1", "t", "BUY", "E0", {"a": 1}))
            st.set_status("k1", "DONE", "ok")
            self.assertEqual(st.get_status("k1"), ("DONE", "ok"))
            self.assertEqual(st.count_opens_since("2000-01-01T00:00:00.000000Z"), 1)
            self.assertIsNone(st.get_state("x"))
            st.set_state("x", "1"); st.set_state("x", "2")
            self.assertEqual(st.get_state("x"), "2")
            st.set_state("x", None)
            self.assertIsNone(st.get_state("x"))
            self.assertEqual(len(st.recent()), 1)
            st.close()
            st2 = Store(Path(d) / "x.db")                 # survives a restart
            self.assertFalse(st2.register("k1", "t", "BUY", "E0", {}))
            st2.close()


if __name__ == "__main__":
    unittest.main()
