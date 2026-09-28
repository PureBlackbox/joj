"""Parsing and validation of the TradingView alert JSON.

The Pine Script v1.1 alert uses these field names:
  trade_id, leg, passphrase, event, symbol, tv_ticker, direction, entry, stop_loss,
  take_profit, confidence, mode, risk_pct, leverage, size, reason, bar_time
The shorter names (id, side, price, stop, target, risk, qty) are accepted as aliases.
The passphrase is NOT part of Signal, so it can never leak into logs or the database.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

EVENTS = ("BUY", "SELL", "ADD", "REDUCE", "CLOSE", "UPDATE")

_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{3,80}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9_.\-]{2,20}$")
_LEG_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")

_ALIASES = {
    "trade_id": ("trade_id", "id"),
    "leg": ("leg",),
    "event": ("event",),
    "symbol": ("symbol", "epic"),
    "side": ("direction", "side"),
    "price": ("entry", "price"),
    "stop": ("stop_loss", "stop"),
    "target": ("take_profit", "target"),
    "confidence": ("confidence",),
    "mode": ("mode",),
    "risk": ("risk_pct", "risk"),
    "leverage": ("leverage",),
    "qty": ("size", "qty"),
    "reason": ("reason",),
    "bar_time": ("bar_time",),
}


class SignalError(ValueError):
    """The alert is malformed."""


@dataclass(frozen=True)
class Signal:
    trade_id: str
    leg: str
    event: str
    symbol: str
    side: Optional[str]          # "LONG" / "SHORT" / None
    price: Optional[float]
    stop: Optional[float]
    target: Optional[float]
    confidence: Optional[float]
    mode: Optional[str]
    risk: Optional[float]
    leverage: Optional[float]
    qty: Optional[float]
    reason: Optional[str]
    bar_time: Optional[int]      # bar time in milliseconds (from Pine `time`)

    def dedupe_key(self) -> str:
        """One key per real-world action so retries and duplicates are ignored.

        BUY/SELL: once per trade id.  ADD/REDUCE: once per leg.  CLOSE: once per trade.
        UPDATE: once per bar (the stop legitimately moves many times per trade).
        """
        if self.event in ("BUY", "SELL"):
            return f"{self.trade_id}:OPEN"
        if self.event == "ADD":
            return f"{self.trade_id}:ADD:{self.leg}"
        if self.event == "REDUCE":
            return f"{self.trade_id}:REDUCE:{self.leg}:{self.reason or ''}"
        if self.event == "CLOSE":
            return f"{self.trade_id}:CLOSE"
        return f"{self.trade_id}:UPDATE:{self.bar_time if self.bar_time is not None else self.stop}:{self.target}"

    def as_dict(self) -> dict:
        return asdict(self)


def _pick(data: dict, field: str) -> Any:
    for name in _ALIASES[field]:
        if name in data:
            return data[name]
    return None


def _number(field: str, value: Any, *, positive: bool = False) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise SignalError(f"{field} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise SignalError(f"{field} must be a number") from None
    if not math.isfinite(number):
        raise SignalError(f"{field} must be a finite number")
    if number < 0 or (positive and number == 0):
        raise SignalError(f"{field} must be {'greater than' if positive else 'at least'} zero")
    return number


def _text(value: Any, limit: int = 40) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:limit]


def parse_signal(data: Any) -> Signal:
    """Validate a decoded JSON object and return a Signal, or raise SignalError."""
    if not isinstance(data, dict):
        raise SignalError("alert must be a JSON object")

    trade_id = _pick(data, "trade_id")
    if not isinstance(trade_id, str) or not _ID_RE.match(trade_id):
        raise SignalError("trade_id is missing or has invalid characters")

    event = str(_pick(data, "event") or "").strip().upper()
    if event not in EVENTS:
        raise SignalError(f"event must be one of {', '.join(EVENTS)}")

    symbol = str(_pick(data, "symbol") or "BTCUSD").strip().upper()
    if not _SYMBOL_RE.match(symbol):
        raise SignalError("symbol is invalid")

    leg = str(_pick(data, "leg") or "E0").strip()
    if not _LEG_RE.match(leg):
        raise SignalError("leg is invalid")

    raw_side = _pick(data, "side")
    side: Optional[str] = None
    if raw_side is not None:
        s = str(raw_side).strip().upper()
        if s in ("LONG", "BUY"):
            side = "LONG"
        elif s in ("SHORT", "SELL"):
            side = "SHORT"
        else:
            raise SignalError("side/direction must be LONG or SHORT")
    if event == "BUY":
        if side not in (None, "LONG"):
            raise SignalError("BUY event contradicts a SHORT direction")
        side = "LONG"
    elif event == "SELL":
        if side not in (None, "SHORT"):
            raise SignalError("SELL event contradicts a LONG direction")
        side = "SHORT"

    bar_time = _pick(data, "bar_time")
    if bar_time is not None:
        bar_time = int(_number("bar_time", bar_time) or 0)

    return Signal(
        trade_id=trade_id,
        leg=leg,
        event=event,
        symbol=symbol,
        side=side,
        price=_number("price", _pick(data, "price"), positive=True),
        stop=_number("stop", _pick(data, "stop"), positive=True),
        target=_number("target", _pick(data, "target"), positive=True),
        confidence=_number("confidence", _pick(data, "confidence")),
        mode=_text(_pick(data, "mode")),
        risk=_number("risk", _pick(data, "risk")),
        leverage=_number("leverage", _pick(data, "leverage")),
        qty=_number("qty", _pick(data, "qty")),
        reason=_text(_pick(data, "reason")),
        bar_time=bar_time,
    )
