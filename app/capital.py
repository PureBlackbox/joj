"""Capital.com REST client (https://open-api.capital.com/).

Facts this client is built around (from the official API docs):
  * POST /api/v1/session with header X-CAP-API-KEY and body {identifier, password}; the
    reply carries CST and X-SECURITY-TOKEN headers that must accompany every later call.
  * Both tokens die after 10 minutes without use -> we log in again when idle, and once
    more if a call is answered with 401.
  * At most 1 order request per 0.1 s, 1 session request per second.
  * POST /positions returns only a dealReference; GET /confirms/{ref} says whether the deal
    was really ACCEPTED, and lists the affected dealIds.
  * There is no partial close endpoint: DELETE /positions/{dealId} closes the whole position.

DRY_RUN=true makes every mutating call (open, amend, close) a logged no-op; read-only calls
(login, prices, positions, balance) still run so credentials and connectivity are proven.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from . import aio_http
from .config import Settings

log = logging.getLogger("relay.capital")

SESSION_IDLE_LIMIT = 9 * 60      # seconds; the broker expires sessions after 10 minutes
MIN_ORDER_SPACING = 0.12         # seconds; the broker allows one order request per 0.1 s
REQUEST_TIMEOUT = 10.0


class CapitalError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class MarketInfo:
    epic: str
    status: Optional[str]
    bid: Optional[float]
    offer: Optional[float]
    min_size: Optional[float]
    max_size: Optional[float]
    step: Optional[float]
    lot_size: Optional[float]
    min_dist_value: Optional[float]
    min_dist_unit: Optional[str]

    def mid(self) -> Optional[float]:
        if self.bid and self.offer:
            return (self.bid + self.offer) / 2.0
        return self.bid or self.offer

    def min_distance_pct(self, price: float) -> Optional[float]:
        """Minimum stop/target distance as a percentage of price (None if unknown)."""
        if self.min_dist_value is None or not price:
            return None
        if (self.min_dist_unit or "").upper() == "PERCENTAGE":
            return self.min_dist_value
        if (self.min_dist_unit or "").upper() == "POINTS":
            return self.min_dist_value / price * 100.0
        return None


@dataclass
class Position:
    deal_id: str
    epic: str
    direction: str          # "BUY" or "SELL"
    size: float
    level: float
    stop: Optional[float]
    target: Optional[float]
    upl: Optional[float]

    @property
    def side(self) -> str:
        return "LONG" if self.direction == "BUY" else "SHORT"


@dataclass
class DealResult:
    ok: bool
    deal_id: Optional[str]
    reference: Optional[str]
    status: str
    deal_ids: List[str] = field(default_factory=list)
    dry_run: bool = False
    raw: dict = field(default_factory=dict)


def _f(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


class CapitalClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.base = settings.capital_base_url
        self.dry_run = settings.dry_run
        self._cst: Optional[str] = None
        self._sec: Optional[str] = None
        self._account_id: str = ""
        self._last_used = 0.0
        self._last_order = 0.0
        self._login_lock = asyncio.Lock()

    # ---- session -----------------------------------------------------------------------------
    def _touch(self) -> None:
        self._last_used = time.monotonic()

    def _headers(self) -> dict:
        return {"CST": self._cst or "", "X-SECURITY-TOKEN": self._sec or ""}

    async def _ensure_session(self) -> None:
        idle = time.monotonic() - self._last_used
        if self._cst and self._sec and idle < SESSION_IDLE_LIMIT:
            return
        async with self._login_lock:
            idle = time.monotonic() - self._last_used
            if self._cst and self._sec and idle < SESSION_IDLE_LIMIT:
                return
            await self._login()

    async def _login(self) -> None:
        url = f"{self.base}/api/v1/session"
        body = {"identifier": self.s.capital_identifier, "password": self.s.capital_api_password,
                "encryptedPassword": False}
        resp = None
        for attempt in range(2):
            try:
                resp = await aio_http.request("POST", url, headers={"X-CAP-API-KEY": self.s.capital_api_key},
                                              json_body=body, timeout=REQUEST_TIMEOUT)
            except aio_http.TransportError as exc:
                raise CapitalError(f"login: network error: {exc}", code="TRANSPORT_ERROR") from exc
            if resp.status == 429 and attempt == 0:      # session endpoint: 1 request/second
                await asyncio.sleep(1.2)
                continue
            break
        assert resp is not None
        if resp.status != 200:
            raise CapitalError(f"login failed (HTTP {resp.status}): {resp.json().get('errorCode', '')}",
                               status=resp.status, code=resp.json().get("errorCode"))
        cst, sec = resp.headers.get("cst"), resp.headers.get("x-security-token")
        if not cst or not sec:
            raise CapitalError("login reply had no CST / X-SECURITY-TOKEN headers")
        self._cst, self._sec = cst, sec
        self._touch()
        self._account_id = str(resp.json().get("currentAccountId") or "")
        want = self.s.capital_account_id
        if want and want != self._account_id:
            r = await aio_http.request("PUT", f"{self.base}/api/v1/session", headers=self._headers(),
                                       json_body={"accountId": want}, timeout=REQUEST_TIMEOUT)
            if r.status != 200:
                raise CapitalError(f"could not switch to account {want} (HTTP {r.status})", status=r.status)
            self._account_id = want
        log.info("capital.com session opened (account %s, %s)", self._account_id or "?",
                 "demo" if self.s.capital_demo else "LIVE")

    # ---- low level calls -------------------------------------------------------------------------
    async def _call(self, method: str, path: str, body: Any = None, *, mutate: bool = False,
                    _retry_auth: bool = True) -> aio_http.Response:
        await self._ensure_session()
        if mutate:
            wait = MIN_ORDER_SPACING - (time.monotonic() - self._last_order)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_order = time.monotonic()
        attempts = 1 if mutate else 3       # never blindly repeat an order: it could double-fill
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                resp = await aio_http.request(method, self.base + path, headers=self._headers(),
                                              json_body=body, timeout=REQUEST_TIMEOUT)
                break
            except aio_http.TransportError as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
        else:
            msg = f"{method} {path}: network error: {last_exc}"
            if mutate:
                msg += " (order state UNKNOWN - check the platform before doing anything else)"
            raise CapitalError(msg, code="TRANSPORT_ERROR")
        if resp.status == 401 and _retry_auth:      # session expired: log in again, retry once
            log.info("session rejected (401); logging in again")
            self._cst = self._sec = None
            return await self._call(method, path, body, mutate=mutate, _retry_auth=False)
        self._touch()
        return resp

    @staticmethod
    def _raise_for(resp: aio_http.Response, what: str) -> None:
        if 200 <= resp.status < 300:
            return
        data = resp.json() if isinstance(resp.json(), dict) else {}
        code = data.get("errorCode")
        raise CapitalError(f"{what} failed (HTTP {resp.status}{', ' + str(code) if code else ''})",
                           status=resp.status, code=code)

    async def _json(self, method: str, path: str, what: str) -> dict:
        resp = await self._call(method, path)
        self._raise_for(resp, what)
        data = resp.json()
        return data if isinstance(data, dict) else {}

    # ---- read-only helpers ---------------------------------------------------------------------------
    async def get_equity(self) -> float:
        """Account equity. `balance` already includes open profit/loss (balance = deposit + profitLoss)."""
        data = await self._json("GET", "/api/v1/accounts", "get accounts")
        accounts = data.get("accounts", [])
        acct = next((a for a in accounts if str(a.get("accountId")) == self._account_id), None) \
            or next((a for a in accounts if a.get("preferred")), None)
        bal = _f(((acct or {}).get("balance") or {}).get("balance"))
        if bal is None:
            raise CapitalError("could not read the account balance")
        return bal

    async def get_market(self, epic: str) -> MarketInfo:
        data = await self._json("GET", f"/api/v1/markets/{epic}", f"get market {epic}")
        rules, snap, inst = data.get("dealingRules", {}), data.get("snapshot", {}), data.get("instrument", {})

        def rule(name: str) -> tuple:
            r = rules.get(name) or {}
            return _f(r.get("value")), r.get("unit")

        dist_v, dist_u = rule("minStopOrProfitDistance")
        return MarketInfo(epic=epic, status=snap.get("marketStatus"), bid=_f(snap.get("bid")),
                          offer=_f(snap.get("offer")), min_size=rule("minDealSize")[0],
                          max_size=rule("maxDealSize")[0], step=rule("minSizeIncrement")[0],
                          lot_size=_f(inst.get("lotSize")), min_dist_value=dist_v, min_dist_unit=dist_u)

    @staticmethod
    def _parse_position(item: dict) -> Position:
        p, m = item.get("position", {}), item.get("market", {})
        return Position(deal_id=str(p.get("dealId")), epic=str(m.get("epic") or p.get("epic") or ""),
                        direction=str(p.get("direction")), size=float(p.get("size", 0)),
                        level=float(p.get("level", 0) or 0), stop=_f(p.get("stopLevel")),
                        target=_f(p.get("profitLevel")), upl=_f(p.get("upl")))

    async def get_positions(self, epic: Optional[str] = None) -> List[Position]:
        data = await self._json("GET", "/api/v1/positions", "get positions")
        out = [self._parse_position(i) for i in data.get("positions", [])]
        return [p for p in out if not epic or p.epic == epic]

    async def get_position(self, deal_id: str) -> Optional[Position]:
        resp = await self._call("GET", f"/api/v1/positions/{deal_id}")
        if resp.status == 404:
            return None
        self._raise_for(resp, "get position")
        return self._parse_position(resp.json())

    async def get_preferences(self) -> dict:
        return await self._json("GET", "/api/v1/accounts/preferences", "get account preferences")

    async def hedging_mode(self) -> bool:
        return bool((await self.get_preferences()).get("hedgingMode"))

    async def check_account(self, epic: str) -> dict:
        """Startup self-test: proves login, account, balance and market data work."""
        await self._ensure_session()
        prefs = await self.get_preferences()
        market = await self.get_market(epic)
        return {
            "account_id": self._account_id, "equity": await self.get_equity(),
            "hedging_mode": bool(prefs.get("hedgingMode")),
            "crypto_leverage": (prefs.get("leverages", {}).get("CRYPTOCURRENCIES") or {}),
            "market_status": market.status, "min_size": market.min_size, "size_step": market.step,
            "lot_size": market.lot_size, "bid": market.bid, "offer": market.offer,
        }

    # ---- mutating calls ---------------------------------------------------------------------------------
    async def _confirm(self, reference: str) -> dict:
        for _ in range(8):
            resp = await self._call("GET", f"/api/v1/confirms/{reference}")
            if resp.status == 200 and resp.json().get("dealStatus"):
                return resp.json()
            if resp.status not in (200, 404):
                self._raise_for(resp, "confirm deal")
            await asyncio.sleep(0.35)
        raise CapitalError(f"no confirmation for {reference}; check the platform", code="CONFIRM_TIMEOUT")

    async def _submit(self, method: str, path: str, body: Optional[dict], what: str) -> DealResult:
        if self.dry_run:
            log.warning("DRY_RUN: would %s -> %s %s %s", what, method, path, body or "")
            return DealResult(True, "DRYRUN", "DRYRUN", "DRY_RUN", ["DRYRUN"], dry_run=True)
        resp = await self._call(method, path, body, mutate=True)
        self._raise_for(resp, what)
        ref = resp.json().get("dealReference")
        if not ref:
            raise CapitalError(f"{what}: reply had no dealReference")
        conf = await self._confirm(ref)
        status = str(conf.get("dealStatus"))
        ids = [str(d["dealId"]) for d in conf.get("affectedDeals", []) if d.get("dealId")]
        if not ids and conf.get("dealId"):
            ids = [str(conf["dealId"])]
        if status != "ACCEPTED":
            reason = conf.get("reason") or conf.get("rejectReason") or status
            raise CapitalError(f"{what} REJECTED by broker: {reason}", code="DEAL_REJECTED")
        log.info("%s accepted (ref %s, deals %s)", what, ref, ids)
        return DealResult(True, ids[0] if ids else None, ref, status, ids, raw=conf)

    async def _open(self, direction: str, epic: str, size: float, stop: Optional[float],
                    target: Optional[float]) -> DealResult:
        body: dict = {"epic": epic, "direction": direction, "size": size}
        if stop is not None:
            body["stopLevel"] = stop
        if target is not None:
            body["profitLevel"] = target
        return await self._submit("POST", "/api/v1/positions", body, f"open {direction} {size} {epic}")

    async def open_long(self, epic: str, size: float, stop: Optional[float], target: Optional[float] = None) -> DealResult:
        return await self._open("BUY", epic, size, stop, target)

    async def open_short(self, epic: str, size: float, stop: Optional[float], target: Optional[float] = None) -> DealResult:
        return await self._open("SELL", epic, size, stop, target)

    async def close_position(self, deal_id: str) -> DealResult:
        """Close one position completely (the API has no partial close)."""
        return await self._submit("DELETE", f"/api/v1/positions/{deal_id}", None, f"close position {deal_id}")

    async def reduce_position(self, epic: str, closing_direction: str, size: float) -> DealResult:
        """Partial close = a smaller order in the OPPOSITE direction. Only valid with hedging mode OFF."""
        return await self._submit("POST", "/api/v1/positions",
                                  {"epic": epic, "direction": closing_direction, "size": size},
                                  f"reduce {epic} by {size} ({closing_direction})")

    async def _amend(self, deal_id: str, stop: Optional[float], target: Optional[float]) -> DealResult:
        cur = await self.get_position(deal_id)       # keep whichever level we are not changing
        stop_final = stop if stop is not None else (cur.stop if cur else None)
        target_final = target if target is not None else (cur.target if cur else None)
        body: dict = {}
        if stop_final is not None:
            body["stopLevel"] = stop_final
        if target_final is not None:
            body["profitLevel"] = target_final
        if not body:
            raise CapitalError("nothing to amend")
        return await self._submit("PUT", f"/api/v1/positions/{deal_id}", body, f"amend position {deal_id} {body}")

    async def modify_stop_loss(self, deal_id: str, stop: float) -> DealResult:
        return await self._amend(deal_id, stop=stop, target=None)

    async def modify_take_profit(self, deal_id: str, target: float) -> DealResult:
        return await self._amend(deal_id, stop=None, target=target)

    async def modify_levels(self, deal_id: str, stop: Optional[float], target: Optional[float]) -> DealResult:
        return await self._amend(deal_id, stop=stop, target=target)

    async def aclose(self) -> None:
        return None
