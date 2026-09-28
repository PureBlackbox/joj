"""Settings loaded from environment variables / a .env file.

No third-party dependency on purpose: one small file you can read in two minutes.
Real environment variables always win over values in the .env file.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

LIVE_URL = "https://api-capital.backend-capital.com"
DEMO_URL = "https://demo-api-capital.backend-capital.com"


class ConfigError(Exception):
    """Raised at startup when the configuration is missing or unsafe."""


def load_env_file(path: str | Path = ".env", environ: Optional[dict] = None) -> None:
    """Load KEY=VALUE lines into the environment (no inline comments, quotes optional)."""
    environ = os.environ if environ is None else environ
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        environ.setdefault(key, value)


def _bool(value: Optional[str], default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(env: Mapping[str, str], key: str, default: float, errors: list) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        errors.append(f"{key} must be a number (got {raw!r})")
        return default


def _int(env: Mapping[str, str], key: str, default: int, errors: list) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        errors.append(f"{key} must be a whole number (got {raw!r})")
        return default


def _csv(value: Optional[str]) -> frozenset:
    return frozenset(x.strip() for x in (value or "").split(",") if x.strip())


PLACEHOLDER_SECRETS = {"", "changeme", "change_me", "change-me", "your-secret", "secret"}


@dataclass(frozen=True)
class Settings:
    # --- Capital.com -------------------------------------------------------------
    capital_api_key: str
    capital_identifier: str        # your Capital.com login (email)
    capital_api_password: str      # the custom password chosen when the API key was created
    capital_account_id: str        # optional: the account to trade on
    capital_demo: bool
    capital_base_url: str
    # --- webhook / admin ---------------------------------------------------------------
    webhook_secret: str
    admin_token: str
    allowed_ips: frozenset
    allowed_epics: frozenset
    # --- safety ------------------------------------------------------------------------------
    dry_run: bool
    kill_switch: bool
    max_leverage: float
    leverage_tolerance: float
    max_qty: float
    max_price_deviation_pct: float
    max_signal_age_seconds: int
    max_orders_per_day: int
    max_consecutive_failures: int
    enable_reduce: bool
    # --- misc ------------------------------------------------------------------------------------
    data_dir: Path
    log_level: str
    telegram_bot_token: str
    telegram_chat_id: str

    @property
    def secrets(self) -> list:
        """Values that must never appear in logs."""
        vals = [self.capital_api_key, self.capital_api_password, self.webhook_secret,
                self.admin_token, self.telegram_bot_token]
        return [v for v in vals if v]

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        errors: list = []

        def need(key: str) -> str:
            value = (env.get(key) or "").strip()
            if not value:
                errors.append(f"{key} is required")
            return value

        api_key = need("CAPITAL_API_KEY")
        identifier = need("CAPITAL_IDENTIFIER")
        api_password = need("CAPITAL_API_PASSWORD")
        secret = need("WEBHOOK_SECRET")
        if secret:
            if secret.lower() in PLACEHOLDER_SECRETS or len(secret) < 16:
                errors.append("WEBHOOK_SECRET must be at least 16 characters and not a placeholder")
            if re.search(r'["\\\s]', secret):
                errors.append('WEBHOOK_SECRET must not contain quotes, backslashes or spaces (it is pasted into JSON)')

        demo = _bool(env.get("CAPITAL_DEMO"), True)
        base = (env.get("CAPITAL_BASE_URL") or "").strip().rstrip("/") or (DEMO_URL if demo else LIVE_URL)

        s = cls(
            capital_api_key=api_key,
            capital_identifier=identifier,
            capital_api_password=api_password,
            capital_account_id=(env.get("CAPITAL_ACCOUNT_ID") or "").strip(),
            capital_demo=demo,
            capital_base_url=base,
            webhook_secret=secret,
            admin_token=(env.get("ADMIN_TOKEN") or "").strip(),
            allowed_ips=_csv(env.get("ALLOWED_IPS")),
            allowed_epics=_csv(env.get("ALLOWED_EPICS")) or frozenset({"BTCUSD"}),
            dry_run=_bool(env.get("DRY_RUN"), True),
            kill_switch=_bool(env.get("KILL_SWITCH"), False),
            max_leverage=_float(env, "MAX_LEVERAGE", 22.0, errors),
            leverage_tolerance=_float(env, "LEVERAGE_TOLERANCE", 0.10, errors),
            max_qty=_float(env, "MAX_QTY", 0.01, errors),
            max_price_deviation_pct=_float(env, "MAX_PRICE_DEVIATION_PCT", 0.5, errors),
            max_signal_age_seconds=_int(env, "MAX_SIGNAL_AGE_SECONDS", 900, errors),
            max_orders_per_day=_int(env, "MAX_ORDERS_PER_DAY", 30, errors),
            max_consecutive_failures=_int(env, "MAX_CONSECUTIVE_FAILURES", 3, errors),
            enable_reduce=_bool(env.get("ENABLE_REDUCE"), False),
            data_dir=Path((env.get("DATA_DIR") or "data").strip()),
            log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
            telegram_bot_token=(env.get("TELEGRAM_BOT_TOKEN") or "").strip(),
            telegram_chat_id=(env.get("TELEGRAM_CHAT_ID") or "").strip(),
        )
        if s.max_leverage <= 0 or s.max_qty <= 0:
            errors.append("MAX_LEVERAGE and MAX_QTY must be greater than zero")
        if errors:
            raise ConfigError("Configuration problems:\n  - " + "\n  - ".join(errors))
        return s
