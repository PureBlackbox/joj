"""Turns a validated Signal into broker actions, behind every safety gate.

Rules of the road
  * BUY / SELL / ADD are strict: kill switch, size, stop, leverage, price, duplicates.
  * CLOSE / UPDATE / REDUCE are risk-reducing and stay allowed while the kill switch is on.
  * CLOSE / UPDATE / ADD / REDUCE must refer to the trade that is currently open. A late
    message from an older trade can never touch a newer position.
  * The broker is the source of truth for "is there a position?" - not this program's memory.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional

from .capital import CapitalError, Position
from .config import Settings
from .models import Signal
from .notify import Notifier
from .safety import (KillSwitch, check_levels, is_stale, leverage_check, normalize_size,
                     price_deviation_ok, stop_widens)
from .store import Store

log = logging.getLogger("relay.executor")

OPEN_TRADE_KEY = "open_trade_id"


class Reject(Exception):
    """A safety gate refused the signal (this is normal, not an error)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code} {detail}".strip())
        self.code = code


class Ignore(Exception):
    """The signal is valid but there is nothing to do (stale id, no position, ...)."""


class Executor:
    def __init__(self, settings: Settings, broker, store: Store, kill: KillSwitch,
                 notifier: Notifier, clock: Callable[[], float] = time.time):
        self.s, self.broker, self.store, self.kill, self.notifier = settings, broker, store, kill, notifier
        self._clock = clock
        self._failures = 0
        self._lock = asyncio.Lock()

    # ---- public ---------------------------------------------------------------------------------------
    async def handle(self, sig: Signal, key: str) -> None:
        """Process one signal. Never raises; the outcome is written to the audit table."""
        async with self._lock:
            failed = False
            try:
                status, detail = await self._dispatch(sig)
                self._failures = 0
            except Ignore as exc:
                status, detail = "IGNORED", str(exc)
            except Reject as exc:
                status, detail = "REJECTED", str(exc)
            except CapitalError as exc:
                status, detail, failed = "FAILED", str(exc), True
            except Exception as exc:  # noqa: BLE001 - keep the worker alive whatever happens
                log.exception("unexpected error while handling %s", key)
                status, detail, failed = "FAILED", f"{type(exc).__name__}: {exc}", True
            if failed:
                await self._register_failure(detail)
        self.store.set_status(key, status, detail)
        log.info("SIGNAL RESULT key=%s status=%s detail=%s", key, status, detail)
        await self._notify(sig, status, detail)

    async def preflight(self) -> None:
        """Best-effort startup self-test. Logs loudly, never stops the server."""
        epic = sorted(self.s.allowed_epics)[0]
        mode = "DRY RUN" if self.s.dry_run else "LIVE ORDERS"
        env = "demo" if self.s.capital_demo else "REAL account"
        try:
            info = await self.broker.check_account(epic)
            log.info("preflight OK: %s", info)
            warn = ""
            if info.get("hedging_mode"):
                warn = " WARNING: hedging mode is ON; the relay will refuse to open trades until it is switched off."
            await self.notifier.send(f"AMF relay started ({mode}, {env}). Equity {info['equity']}, "
                                     f"{epic} {info['market_status']}.{warn}")
        except Exception as exc:  # noqa: BLE001
            log.critical("preflight FAILED: %s", exc)
            await self.notifier.send(f"AMF relay started ({mode}, {env}) but the broker self-test FAILED: {exc}")

    async def flatten(self) -> List[str]:
        """Emergency: close every open position on the allowed markets."""
        async with self._lock:
            closed: List[str] = []
            for epic in sorted(self.s.allowed_epics):
                for pos in await self.broker.get_positions(epic):
                    await self.broker.close_position(pos.deal_id)
                    closed.append(pos.deal_id)
            self.store.set_state(OPEN_TRADE_KEY, None)
            return closed

    # ---- dispatch --------------------------------------------------------------------------------------
    async def _dispatch(self, sig: Signal):
        if sig.symbol not in self.s.allowed_epics:
            raise Reject("SYMBOL_NOT_ALLOWED", sig.symbol)
        if sig.event in ("BUY", "SELL"):
            return await self._open(sig)
        if sig.event == "ADD":
            return await self._add(sig)
        if sig.event == "REDUCE":
            return await self._reduce(sig)
        if sig.event == "CLOSE":
            return await self._close(sig)
        return await self._update(sig)

    # ---- shared checks -----------------------------------------------------------------------------------
    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _day_start_iso(self) -> str:
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _require_open_trade(self, sig: Signal, lenient_if_unknown: bool = False) -> None:
        current = self.store.get_state(OPEN_TRADE_KEY)
        if current is None and lenient_if_unknown:
            log.warning("no open trade recorded; allowing %s for %s because it only reduces risk", sig.event, sig.trade_id)
            return
        if current != sig.trade_id:
            raise Ignore(f"STALE_TRADE_ID (signal {sig.trade_id}, open trade {current})")

    def _pre_open_gates(self, sig: Signal) -> None:
        s = self.s
        if self.kill.is_active():
            raise Reject("KILL_SWITCH", self.kill.reason())
        if not sig.qty or sig.qty <= 0:
            raise Reject("ZERO_QUANTITY")
        if is_stale(sig.bar_time, self._now_ms(), s.max_signal_age_seconds):
            raise Reject("STALE_SIGNAL", f"older than {s.max_signal_age_seconds}s")
        if self.store.count_opens_since(self._day_start_iso()) >= s.max_orders_per_day:
            raise Reject("DAILY_ORDER_LIMIT", f"{s.max_orders_per_day} orders today")

    async def _market_gates(self, sig: Signal, side: str):
        if await self.broker.hedging_mode():
            raise Reject("HEDGING_MODE_ON", "switch hedging off in the Capital.com account preferences")
        market = await self.broker.get_market(sig.symbol)
        if market.status not in (None, "TRADEABLE"):
            raise Reject("MARKET_NOT_TRADEABLE", str(market.status))
        if market.lot_size not in (None, 1.0):
            raise Reject("UNSUPPORTED_LOT_SIZE", f"lotSize={market.lot_size}; size units unverified")
        live = (market.offer if side == "LONG" else market.bid) or market.mid() or sig.price
        if not live:
            raise Reject("NO_LIVE_PRICE")
        ok, dev = price_deviation_ok(live, sig.price, self.s.max_price_deviation_pct)
        if not ok:
            raise Reject("PRICE_DEVIATION", f"live {live} vs signal {sig.price} ({dev:.2f}%)")
        return market, live

    # ---- BUY / SELL ------------------------------------------------------------------------------------------
    async def _open(self, sig: Signal):
        s, side = self.s, sig.side or "LONG"
        self._pre_open_gates(sig)
        if sig.qty > s.max_qty:
            raise Reject("QTY_ABOVE_MAX_QTY", f"{sig.qty} > {s.max_qty}")
        if sig.stop is None:
            raise Reject("MISSING_STOP", "the relay never opens a position without a stop-loss")
        if await self.broker.get_positions(sig.symbol):
            raise Reject("POSITION_ALREADY_OPEN", "the strategy only opens from flat")
        market, live = await self._market_gates(sig, side)
        size, why = normalize_size(sig.qty, market.min_size, market.step, market.max_size)
        if size is None:
            raise Reject(why, f"requested {sig.qty}")
        ok, why = check_levels(side, live, sig.stop, sig.target, market.min_distance_pct(live))
        if not ok:
            raise Reject(why, f"stop {sig.stop} target {sig.target} price {live}")
        equity = await self.broker.get_equity()
        ok, why, lev = leverage_check(size * live, 0.0, equity, sig.leverage, s.max_leverage, s.leverage_tolerance)
        if not ok:
            raise Reject(why, f"would be {lev:.1f}x on equity {equity:.2f}; signal leverage {sig.leverage}; cap {s.max_leverage}")
        if side == "LONG":
            res = await self.broker.open_long(sig.symbol, size, sig.stop, sig.target)
        else:
            res = await self.broker.open_short(sig.symbol, size, sig.stop, sig.target)
        self.store.set_state(OPEN_TRADE_KEY, sig.trade_id)
        return ("DRY_RUN" if res.dry_run else "DONE",
                f"{side} {size} {sig.symbol} ~{live} stop {sig.stop} target {sig.target} lev {lev:.1f}x deal {res.deal_id}")

    # ---- ADD (pyramiding) ------------------------------------------------------------------------------------------
    async def _add(self, sig: Signal):
        s = self.s
        self._require_open_trade(sig)
        self._pre_open_gates(sig)
        positions = await self.broker.get_positions(sig.symbol)
        if not positions:
            self.store.set_state(OPEN_TRADE_KEY, None)
            raise Ignore("NO_POSITION (nothing to add to)")
        side = positions[0].side
        if any(p.side != side for p in positions) or (sig.side and sig.side != side):
            raise Reject("SIDE_MISMATCH")
        existing = sum(p.size for p in positions)
        market, live = await self._market_gates(sig, side)
        size, why = normalize_size(sig.qty, market.min_size, market.step, market.max_size)
        if size is None:
            raise Reject(why, f"requested {sig.qty}")
        if existing + size > s.max_qty:
            raise Reject("QTY_ABOVE_MAX_QTY", f"position would be {existing + size:.6f} > {s.max_qty}")
        stop = sig.stop if sig.stop is not None else positions[0].stop
        ok, why = check_levels(side, live, stop, sig.target, market.min_distance_pct(live))
        if not ok:
            raise Reject(why)
        equity = await self.broker.get_equity()
        # The v1.1 ADD alert reports leverage BEFORE the add, so only the hard cap applies here.
        ok, why, lev = leverage_check(size * live, existing * live, equity, None, s.max_leverage,
                                      s.leverage_tolerance, enforce_received=False)
        if not ok:
            raise Reject(why, f"would be {lev:.1f}x; cap {s.max_leverage}")
        if side == "LONG":
            res = await self.broker.open_long(sig.symbol, size, stop, sig.target)
        else:
            res = await self.broker.open_short(sig.symbol, size, stop, sig.target)
        # A merged position may not inherit the new levels: set them explicitly on what is open now.
        for pos in await self.broker.get_positions(sig.symbol):
            new_stop = None if stop_widens(pos.side, pos.stop, stop) else stop
            await self.broker.modify_levels(pos.deal_id, new_stop, sig.target)
        return ("DRY_RUN" if res.dry_run else "DONE", f"ADD {side} {size} ~{live} total lev {lev:.1f}x deal {res.deal_id}")

    # ---- REDUCE (partial close) -------------------------------------------------------------------------------------
    async def _reduce(self, sig: Signal):
        if not self.s.enable_reduce:
            raise Ignore("REDUCE_DISABLED (set ENABLE_REDUCE=true after testing on demo)")
        self._require_open_trade(sig)
        positions = await self.broker.get_positions(sig.symbol)
        if not positions:
            raise Ignore("NO_POSITION")
        if len(positions) != 1:
            raise Reject("UNEXPECTED_POSITIONS", f"{len(positions)} open positions; refusing to guess")
        pos = positions[0]
        if await self.broker.hedging_mode():
            raise Reject("HEDGING_MODE_ON", "an opposite order would open a new position instead of reducing")
        market = await self.broker.get_market(sig.symbol)
        size, why = normalize_size(sig.qty or 0, market.min_size, market.step, market.max_size)
        if size is None:
            raise Reject(why)
        remaining = pos.size - size
        if remaining < (market.min_size or 0) or remaining <= 0:
            raise Reject("REDUCE_LEAVES_TOO_LITTLE", f"use CLOSE; position {pos.size}, reduce {size}")
        closing_direction = "SELL" if pos.direction == "BUY" else "BUY"
        res = await self.broker.reduce_position(sig.symbol, closing_direction, size)
        if not res.dry_run:
            after = sum(p.size for p in await self.broker.get_positions(sig.symbol))
            if abs(after - remaining) > 1e-9:
                self.kill.activate("auto: reduce produced an unexpected position size")
                raise CapitalError(f"REDUCE gave position {after}, expected {remaining}; kill switch engaged, check the platform")
        return ("DRY_RUN" if res.dry_run else "DONE", f"REDUCE {size}, remaining {remaining}")

    # ---- CLOSE -----------------------------------------------------------------------------------------------------------
    async def _close(self, sig: Signal):
        self._require_open_trade(sig, lenient_if_unknown=True)
        positions = await self.broker.get_positions(sig.symbol)
        if not positions:
            self.store.set_state(OPEN_TRADE_KEY, None)
            raise Ignore("NO_POSITION (already closed, e.g. by the broker-side stop or target)")
        results = [await self.broker.close_position(p.deal_id) for p in positions]
        self.store.set_state(OPEN_TRADE_KEY, None)
        dry = all(r.dry_run for r in results)
        return ("DRY_RUN" if dry else "DONE", f"closed {len(results)} position(s) [{sig.reason or 'signal'}]")

    # ---- UPDATE (stop / target) -----------------------------------------------------------------------------------------
    async def _update(self, sig: Signal):
        self._require_open_trade(sig)
        if sig.stop is None and sig.target is None:
            raise Ignore("NOTHING_TO_UPDATE")
        positions = await self.broker.get_positions(sig.symbol)
        if not positions:
            self.store.set_state(OPEN_TRADE_KEY, None)
            raise Ignore("NO_POSITION")
        market = await self.broker.get_market(sig.symbol)
        live = market.mid() or sig.price
        done: List[str] = []
        blocked: List[str] = []
        for pos in positions:
            new_stop, new_target = sig.stop, sig.target
            if new_stop is not None:
                if stop_widens(pos.side, pos.stop, new_stop):
                    blocked.append("stop widening blocked")
                    new_stop = None
                elif pos.stop is not None and abs(new_stop - pos.stop) < 0.005:
                    new_stop = None
            if new_target is not None and pos.target is not None and abs(new_target - pos.target) < 0.005:
                new_target = None
            if new_stop is None and new_target is None:
                continue
            ok, why = check_levels(pos.side, live, new_stop if new_stop is not None else pos.stop, new_target,
                                   market.min_distance_pct(live) if live else None, require_stop=False)
            if not ok:
                raise Reject(why, f"stop {new_stop} target {new_target} price {live}")
            res = await self.broker.modify_levels(pos.deal_id, new_stop, new_target)
            done.append(f"{pos.deal_id}: stop {new_stop} target {new_target}{' (dry)' if res.dry_run else ''}")
        if not done:
            raise Ignore("NO_CHANGE " + "; ".join(blocked))
        return "DONE", "; ".join(done + blocked)

    # ---- failures + notifications ------------------------------------------------------------------------------------------
    async def _register_failure(self, detail: str) -> None:
        self._failures += 1
        log.error("broker failure %d/%d: %s", self._failures, self.s.max_consecutive_failures, detail)
        if self._failures >= self.s.max_consecutive_failures and not self.kill.is_active():
            self.kill.activate(f"auto: {self._failures} consecutive broker failures")
            await self.notifier.send(f"KILL SWITCH ENGAGED after {self._failures} consecutive broker failures. "
                                     f"New trades are blocked. Last error: {detail}")

    async def _notify(self, sig: Signal, status: str, detail: str) -> None:
        if status in ("DONE", "DRY_RUN"):
            await self.notifier.send(f"{sig.event} {sig.trade_id} leg {sig.leg}: {detail}")
        elif status in ("REJECTED", "FAILED"):
            await self.notifier.send(f"{status}: {sig.event} {sig.trade_id} leg {sig.leg} -> {detail}")
