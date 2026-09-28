"""Integration tests: the real CapitalClient + Executor against a mock Capital.com server."""
import unittest

from app.capital import CapitalClient, CapitalError
from tests.fake_capital import FakeCapital
from tests.helpers import Env, make_settings, sig


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = FakeCapital().start()
        self.addCleanup(self.fake.stop)

    def client(self, **kw):
        return CapitalClient(make_settings(self.fake.base_url, **kw))

    async def test_login_equity_and_market(self):
        c = self.client()
        self.assertAlmostEqual(await c.get_equity(), 10.13)
        m = await c.get_market("BTCUSD")
        self.assertEqual((m.status, m.min_size, m.step, m.lot_size), ("TRADEABLE", 0.0001, 0.0001, 1.0))
        self.assertEqual(self.fake.logins, 1)
        self.assertAlmostEqual(m.min_distance_pct(79000), 0.1)

    async def test_bad_credentials(self):
        c = CapitalClient(make_settings(self.fake.base_url, CAPITAL_API_PASSWORD="wrong"))
        with self.assertRaises(CapitalError) as cm:
            await c.get_equity()
        self.assertEqual(cm.exception.status, 401)

    async def test_open_long_sends_stop_and_target_and_confirms(self):
        c = self.client()
        res = await c.open_long("BTCUSD", 0.0005, 78800.0, 79400.0)
        self.assertTrue(res.ok and res.deal_id)
        body = self.fake.order_calls()[0][2]
        self.assertEqual(body, {"epic": "BTCUSD", "direction": "BUY", "size": 0.0005, "stopLevel": 78800.0, "profitLevel": 79400.0})
        pos = (await c.get_positions("BTCUSD"))[0]
        self.assertEqual((pos.side, pos.size, pos.stop, pos.target), ("LONG", 0.0005, 78800.0, 79400.0))

    async def test_open_short(self):
        c = self.client()
        await c.open_short("BTCUSD", 0.0003, 79300.0, 78600.0)
        self.assertEqual(self.fake.order_calls()[0][2]["direction"], "SELL")

    async def test_broker_rejection_raises_with_reason(self):
        self.fake.reject_next = 1
        with self.assertRaises(CapitalError) as cm:
            await self.client().open_long("BTCUSD", 0.0005, 78800.0)
        self.assertIn("INSUFFICIENT_FUNDS", str(cm.exception))
        self.assertEqual(self.fake.positions, {})

    async def test_session_expiry_is_recovered_with_one_relogin(self):
        c = self.client()
        await c.get_equity()
        self.fake.expire_session()                       # broker forgets the tokens -> next call gets 401
        self.assertAlmostEqual(await c.get_equity(), 10.13)
        self.assertEqual(self.fake.logins, 2)

    async def test_modify_stop_keeps_take_profit_and_vice_versa(self):
        c = self.client()
        await c.open_long("BTCUSD", 0.0005, 78800.0, 79400.0)
        pid = (await c.get_positions())[0].deal_id
        await c.modify_stop_loss(pid, 78900.0)
        p = self.fake.first_position()
        self.assertEqual((p["stopLevel"], p["profitLevel"]), (78900.0, 79400.0))
        await c.modify_take_profit(pid, 79500.0)
        self.assertEqual((p["stopLevel"], p["profitLevel"]), (78900.0, 79500.0))

    async def test_close_position(self):
        c = self.client()
        await c.open_long("BTCUSD", 0.0005, 78800.0)
        await c.close_position((await c.get_positions())[0].deal_id)
        self.assertEqual(await c.get_positions(), [])

    async def test_dry_run_reads_but_never_orders(self):
        c = self.client(DRY_RUN="true")
        res = await c.open_long("BTCUSD", 0.0005, 78800.0, 79400.0)
        self.assertTrue(res.dry_run)
        self.assertEqual(self.fake.order_calls(), [])
        self.assertAlmostEqual(await c.get_equity(), 10.13)      # read-only calls still work

    async def test_account_switch_when_configured(self):
        c = self.client(CAPITAL_ACCOUNT_ID="ACC2")
        await c.get_equity()
        self.assertEqual(self.fake.current_account, "ACC2")


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = FakeCapital().start()
        self.addCleanup(self.fake.stop)

    def env(self, **kw):
        e = Env(self.fake, **kw)
        self.addCleanup(e.close)
        return e

    # ---- BUY / SELL -----------------------------------------------------------------------------
    async def test_buy_opens_long_with_stop_and_target(self):
        e = self.env()
        status, detail = await e.run(sig("BUY"))
        self.assertEqual(status, "DONE", detail)
        p = self.fake.first_position()
        self.assertEqual((p["direction"], p["size"], p["stopLevel"], p["profitLevel"]), ("BUY", 0.001, 78800.0, 79400.0))
        self.assertEqual(e.store.get_state("open_trade_id"), "AMF-20260928-143522-L-001")

    async def test_sell_opens_short(self):
        e = self.env()
        status, _ = await e.run(sig("SELL", trade_id="T-SHORT-1", direction="SHORT", entry=79000.0,
                                    stop_loss=79300.0, take_profit=78500.0, leverage=8.0))
        self.assertEqual(status, "DONE")
        self.assertEqual(self.fake.first_position()["direction"], "SELL")

    async def test_zero_quantity_never_trades(self):
        e = self.env()
        status, detail = await e.run(sig("BUY", size=0))
        self.assertEqual(status, "REJECTED")
        self.assertIn("ZERO_QUANTITY", detail)
        self.assertEqual(self.fake.order_calls(), [])

    async def test_quantity_below_broker_minimum(self):
        e = self.env()
        status, detail = await e.run(sig("BUY", size=0.00005, leverage=1.0))
        self.assertEqual(status, "REJECTED")
        self.assertIn("SIZE_TOO_SMALL", detail)

    async def test_quantity_above_configured_max(self):
        e = self.env(MAX_QTY="0.0005")
        status, detail = await e.run(sig("BUY", size=0.001))
        self.assertIn("QTY_ABOVE_MAX_QTY", detail)
        self.assertEqual(self.fake.order_calls(), [])

    async def test_missing_stop_never_trades(self):
        e = self.env()
        status, detail = await e.run(sig("BUY", stop_loss=None))
        self.assertEqual(status, "REJECTED")
        self.assertIn("MISSING_STOP", detail)

    async def test_leverage_above_signal_is_rejected(self):
        e = self.env()          # 0.001 BTC ~ $79 on $10.13 equity = 7.8x
        status, detail = await e.run(sig("BUY", leverage=3.0))
        self.assertEqual(status, "REJECTED")
        self.assertIn("LEVERAGE_ABOVE_SIGNAL", detail)
        self.assertEqual(self.fake.order_calls(), [])

    async def test_leverage_above_hard_cap_is_rejected_even_if_signal_agrees(self):
        e = self.env(MAX_LEVERAGE="5")
        status, detail = await e.run(sig("BUY", leverage=8.0))
        self.assertIn("LEVERAGE_ABOVE_HARD_CAP", detail)

    async def test_missing_leverage_is_rejected(self):
        e = self.env()
        status, detail = await e.run(sig("BUY", leverage=None))
        self.assertIn("MISSING_LEVERAGE", detail)

    async def test_second_open_while_position_exists_is_rejected(self):
        e = self.env()
        await e.run(sig("BUY"))
        status, detail = await e.run(sig("BUY", trade_id="AMF-OTHER-TRADE-002"))
        self.assertEqual(status, "REJECTED")
        self.assertIn("POSITION_ALREADY_OPEN", detail)
        self.assertEqual(len(self.fake.positions), 1)

    async def test_market_closed(self):
        self.fake.market_status = "CLOSED"
        status, detail = await self.env().run(sig("BUY"))
        self.assertIn("MARKET_NOT_TRADEABLE", detail)

    async def test_price_deviation(self):
        self.fake.offer = 80500.0
        status, detail = await self.env().run(sig("BUY", entry=79010.0))
        self.assertIn("PRICE_DEVIATION", detail)

    async def test_stale_signal(self):
        status, detail = await self.env().run(sig("BUY", bar_time=1_000_000))
        self.assertIn("STALE_SIGNAL", detail)

    async def test_stop_on_wrong_side(self):
        status, detail = await self.env().run(sig("BUY", stop_loss=80000.0))
        self.assertIn("STOP_ON_WRONG_SIDE", detail)

    async def test_hedging_mode_blocks_opening(self):
        self.fake.hedging = True
        status, detail = await self.env().run(sig("BUY"))
        self.assertIn("HEDGING_MODE_ON", detail)

    async def test_daily_order_limit(self):
        e = self.env(MAX_ORDERS_PER_DAY="1")
        await e.run(sig("BUY"))
        await e.run(sig("CLOSE", reason="TEST"))
        status, detail = await e.run(sig("BUY", trade_id="AMF-SECOND-TRADE-002"))
        self.assertIn("DAILY_ORDER_LIMIT", detail)

    async def test_dry_run_reports_but_does_not_order(self):
        e = self.env(DRY_RUN="true")
        status, detail = await e.run(sig("BUY"))
        self.assertEqual(status, "DRY_RUN")
        self.assertEqual(self.fake.order_calls(), [])

    # ---- kill switch ------------------------------------------------------------------------------
    async def test_kill_switch_blocks_new_trades_but_allows_close(self):
        e = self.env()
        await e.run(sig("BUY"))
        e.kill.activate("test")
        status, detail = await e.run(sig("BUY", trade_id="AMF-BLOCKED-TRADE-003"))
        self.assertEqual(status, "REJECTED")
        self.assertIn("KILL_SWITCH", detail)
        status, _ = await e.run(sig("CLOSE"))
        self.assertEqual(status, "DONE")
        self.assertEqual(self.fake.positions, {})

    async def test_kill_switch_from_environment(self):
        status, detail = await self.env(KILL_SWITCH="true").run(sig("BUY"))
        self.assertIn("KILL_SWITCH", detail)

    async def test_three_consecutive_broker_failures_engage_the_kill_switch(self):
        e = self.env()
        self.fake.reject_next = 3
        for i in range(3):
            status, _ = await e.run(sig("BUY", trade_id=f"AMF-FAILING-TRADE-00{i}"))
            self.assertEqual(status, "FAILED")
        self.assertTrue(e.kill.is_active())
        status, detail = await e.run(sig("BUY", trade_id="AMF-AFTER-TRADE-009"))
        self.assertIn("KILL_SWITCH", detail)

    # ---- CLOSE --------------------------------------------------------------------------------------
    async def test_close_closes_and_clears_state(self):
        e = self.env()
        await e.run(sig("BUY"))
        status, _ = await e.run(sig("CLOSE", reason="TIME_STOP"))
        self.assertEqual(status, "DONE")
        self.assertEqual(self.fake.positions, {})
        self.assertIsNone(e.store.get_state("open_trade_id"))

    async def test_close_from_an_old_trade_never_touches_the_new_position(self):
        e = self.env()
        await e.run(sig("BUY", trade_id="AMF-NEW-TRADE-002"))
        status, detail = await e.run(sig("CLOSE", trade_id="AMF-OLD-TRADE-001"))
        self.assertEqual(status, "IGNORED")
        self.assertIn("STALE_TRADE_ID", detail)
        self.assertEqual(len(self.fake.positions), 1)

    async def test_close_when_broker_already_closed_it(self):
        e = self.env()
        await e.run(sig("BUY"))
        self.fake.positions.clear()                       # broker-side stop or target fired
        status, detail = await e.run(sig("CLOSE", reason="EXIT_FILL"))
        self.assertEqual(status, "IGNORED")
        self.assertIn("NO_POSITION", detail)
        self.assertIsNone(e.store.get_state("open_trade_id"))

    async def test_close_still_works_if_local_state_was_lost(self):
        e = self.env()
        await e.run(sig("BUY"))
        e.store.set_state("open_trade_id", None)
        status, _ = await e.run(sig("CLOSE"))
        self.assertEqual(status, "DONE")

    # ---- UPDATE ---------------------------------------------------------------------------------------
    async def test_update_tightens_stop_and_moves_target(self):
        e = self.env()
        await e.run(sig("BUY"))
        status, _ = await e.run(sig("UPDATE", bar_time=1, stop_loss=78900.0, take_profit=79500.0))
        self.assertEqual(status, "DONE")
        p = self.fake.first_position()
        self.assertEqual((p["stopLevel"], p["profitLevel"]), (78900.0, 79500.0))

    async def test_update_never_widens_the_stop(self):
        e = self.env()
        await e.run(sig("BUY"))
        await e.run(sig("UPDATE", bar_time=1, stop_loss=78900.0, take_profit=None))
        status, detail = await e.run(sig("UPDATE", bar_time=2, stop_loss=78500.0, take_profit=None))
        self.assertEqual(status, "IGNORED")
        self.assertEqual(self.fake.first_position()["stopLevel"], 78900.0)

    async def test_update_with_no_change_is_ignored(self):
        e = self.env()
        await e.run(sig("BUY"))
        status, detail = await e.run(sig("UPDATE", bar_time=1, stop_loss=78800.0, take_profit=79400.0))
        self.assertEqual(status, "IGNORED")

    async def test_update_for_stale_trade_is_ignored(self):
        e = self.env()
        await e.run(sig("BUY", trade_id="AMF-NEW-TRADE-002"))
        status, _ = await e.run(sig("UPDATE", trade_id="AMF-OLD-TRADE-001", bar_time=1, stop_loss=78900.0))
        self.assertEqual(status, "IGNORED")
        self.assertEqual(self.fake.first_position()["stopLevel"], 78800.0)

    async def test_update_stop_on_wrong_side_of_price_is_rejected(self):
        e = self.env()
        await e.run(sig("BUY"))
        status, detail = await e.run(sig("UPDATE", bar_time=1, stop_loss=79200.0, take_profit=None))
        self.assertEqual(status, "REJECTED")
        self.assertIn("STOP_ON_WRONG_SIDE", detail)

    # ---- ADD --------------------------------------------------------------------------------------------
    async def test_add_merges_and_reapplies_the_new_levels(self):
        e = self.env()
        await e.run(sig("BUY", size=0.0008, leverage=8.0))
        status, detail = await e.run(sig("ADD", leg="E1", size=0.0004, stop_loss=78850.0, take_profit=79600.0, leverage=6.0))
        self.assertEqual(status, "DONE", detail)
        p = self.fake.first_position()
        self.assertAlmostEqual(p["size"], 0.0012)
        self.assertEqual((p["stopLevel"], p["profitLevel"]), (78850.0, 79600.0))   # fake keeps old levels on merge; relay fixes them

    async def test_add_for_stale_trade_is_ignored(self):
        e = self.env()
        await e.run(sig("BUY", trade_id="AMF-NEW-TRADE-002"))
        status, _ = await e.run(sig("ADD", trade_id="AMF-OLD-TRADE-001", leg="E1", size=0.0004))
        self.assertEqual(status, "IGNORED")

    async def test_add_respects_max_total_quantity(self):
        e = self.env(MAX_QTY="0.001")
        await e.run(sig("BUY", size=0.0008, leverage=8.0))
        status, detail = await e.run(sig("ADD", leg="E1", size=0.0004))
        self.assertIn("QTY_ABOVE_MAX_QTY", detail)

    async def test_add_respects_hard_leverage_cap(self):
        e = self.env(MAX_LEVERAGE="9")
        await e.run(sig("BUY", size=0.0011, leverage=9.0))      # 8.6x
        status, detail = await e.run(sig("ADD", leg="E1", size=0.0004))
        self.assertIn("LEVERAGE_ABOVE_HARD_CAP", detail)

    # ---- REDUCE ----------------------------------------------------------------------------------------
    async def test_reduce_is_off_by_default(self):
        e = self.env()
        await e.run(sig("BUY"))
        status, detail = await e.run(sig("REDUCE", size=0.0004, reason="PARTIAL"))
        self.assertEqual(status, "IGNORED")
        self.assertIn("REDUCE_DISABLED", detail)
        self.assertEqual(self.fake.first_position()["size"], 0.001)

    async def test_reduce_when_enabled_shrinks_position(self):
        e = self.env(ENABLE_REDUCE="true")
        await e.run(sig("BUY"))
        status, detail = await e.run(sig("REDUCE", size=0.0004, reason="PARTIAL"))
        self.assertEqual(status, "DONE", detail)
        self.assertAlmostEqual(self.fake.first_position()["size"], 0.0006)
        self.assertEqual(self.fake.first_position()["direction"], "BUY")

    async def test_reduce_that_would_flip_or_flatten_is_rejected(self):
        e = self.env(ENABLE_REDUCE="true")
        await e.run(sig("BUY"))
        status, detail = await e.run(sig("REDUCE", size=0.001))
        self.assertEqual(status, "REJECTED")
        self.assertEqual(self.fake.first_position()["size"], 0.001)

    async def test_symbol_allowlist(self):
        status, detail = await self.env().run(sig("BUY", symbol="ETHUSD"))
        self.assertEqual(status, "REJECTED")
        self.assertIn("SYMBOL_NOT_ALLOWED", detail)


if __name__ == "__main__":
    unittest.main()
