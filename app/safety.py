"""Pure safety checks (no network) and the kill switch.

Every function returns a short machine-readable reason code so the audit log and the
Telegram messages say exactly why something was refused.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Optional, Tuple


# ---- sizing ---------------------------------------------------------------------------------
def normalize_size(qty: float, min_size: Optional[float], step: Optional[float],
                   max_size: Optional[float]) -> Tuple[Optional[float], str]:
    """Round DOWN to the broker's size increment and check broker min/max."""
    if qty is None or qty <= 0:
        return None, "ZERO_QUANTITY"
    size = Decimal(str(qty))
    if step and step > 0:
        s = Decimal(str(step))
        size = (size / s).to_integral_value(rounding=ROUND_DOWN) * s
    if size <= 0:
        return None, "SIZE_TOO_SMALL"
    if min_size and size < Decimal(str(min_size)):
        return None, "SIZE_TOO_SMALL"
    if max_size and size > Decimal(str(max_size)):
        return None, "SIZE_ABOVE_BROKER_MAX"
    return float(size), "OK"


# ---- leverage -------------------------------------------------------------------------------
def leverage_check(new_notional: float, existing_notional: float, equity: float,
                   received_leverage: Optional[float], hard_cap: float, tolerance: float,
                   enforce_received: bool = True) -> Tuple[bool, str, float]:
    """Return (ok, reason, resulting_leverage).

    The resulting leverage is (existing + new exposure) / equity. It may never exceed the
    hard cap, and (when enforce_received) never the leverage the strategy asked for
    plus a small tolerance for price movement between signal and execution.
    """
    if equity is None or equity <= 0:
        return False, "NO_EQUITY", 0.0
    lev = (existing_notional + new_notional) / equity
    if lev > hard_cap + 1e-9:
        return False, "LEVERAGE_ABOVE_HARD_CAP", lev
    if enforce_received:
        if received_leverage is None or received_leverage <= 0:
            return False, "MISSING_LEVERAGE", lev
        if lev > received_leverage * (1.0 + tolerance) + 1e-9:
            return False, "LEVERAGE_ABOVE_SIGNAL", lev
    return True, "OK", lev


# ---- stop / target levels -------------------------------------------------------------------------
def check_levels(side: str, ref_price: float, stop: Optional[float], target: Optional[float],
                 min_distance_pct: Optional[float] = None, require_stop: bool = True) -> Tuple[bool, str]:
    """Stop and target must sit on the correct side of the price, and not too close."""
    if stop is None:
        return (False, "MISSING_STOP") if require_stop else (True, "OK")
    if ref_price is None or ref_price <= 0:
        return False, "NO_REFERENCE_PRICE"
    if side == "LONG":
        if stop >= ref_price:
            return False, "STOP_ON_WRONG_SIDE"
        if target is not None and target <= ref_price:
            return False, "TARGET_ON_WRONG_SIDE"
    else:
        if stop <= ref_price:
            return False, "STOP_ON_WRONG_SIDE"
        if target is not None and target >= ref_price:
            return False, "TARGET_ON_WRONG_SIDE"
    if min_distance_pct:
        if abs(ref_price - stop) / ref_price * 100.0 < min_distance_pct:
            return False, "STOP_TOO_CLOSE"
        if target is not None and abs(target - ref_price) / ref_price * 100.0 < min_distance_pct:
            return False, "TARGET_TOO_CLOSE"
    return True, "OK"


def stop_widens(side: str, current_stop: Optional[float], new_stop: float, eps: float = 1e-9) -> bool:
    """True if moving to new_stop would increase risk (the strategy only ever tightens)."""
    if current_stop is None:
        return False
    return new_stop < current_stop - eps if side == "LONG" else new_stop > current_stop + eps


def price_deviation_ok(live: Optional[float], signal_price: Optional[float], max_pct: float) -> Tuple[bool, float]:
    if not live or not signal_price:
        return True, 0.0          # nothing to compare against
    dev = abs(live - signal_price) / signal_price * 100.0
    return dev <= max_pct, dev


def is_stale(bar_time_ms: Optional[int], now_ms: int, max_age_seconds: int) -> bool:
    if bar_time_ms is None or max_age_seconds <= 0:
        return False
    return (now_ms - bar_time_ms) / 1000.0 > max_age_seconds


# ---- kill switch --------------------------------------------------------------------------------------
class KillSwitch:
    """New positions are blocked when EITHER is true:
      * KILL_SWITCH=true in the environment, or
      * the flag file data/KILL_SWITCH exists  ->  `touch data/KILL_SWITCH` works instantly, no restart.
    CLOSE / UPDATE stay allowed so an open position can always be managed and exited.
    """

    def __init__(self, env_flag: bool, flag_path: Path):
        self.env_flag = env_flag
        self.flag_path = Path(flag_path)

    def is_active(self) -> bool:
        return self.env_flag or self.flag_path.exists()

    def reason(self) -> str:
        if self.env_flag:
            return "KILL_SWITCH=true in environment"
        if self.flag_path.exists():
            try:
                return self.flag_path.read_text(encoding="utf-8").strip() or "flag file present"
            except OSError:
                return "flag file present"
        return ""

    def activate(self, reason: str) -> None:
        self.flag_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        self.flag_path.write_text(f"{stamp} {reason}\n", encoding="utf-8")

    def deactivate(self) -> bool:
        """Remove the flag file. Returns False if the environment flag still blocks trading."""
        if self.flag_path.exists():
            self.flag_path.unlink()
        return not self.env_flag
