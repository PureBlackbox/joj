"""A small mock of the Capital.com REST API, written from the official docs, for tests.

It is NOT the real broker: it proves this relay behaves correctly against the documented
contract (tokens, 401 on expiry, dealReference + confirms, netting, rejections).
"""
from __future__ import annotations

import itertools
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeCapital:
    def __init__(self, api_key="KEY", identifier="me@example.com", password="pw"):
        self.api_key, self.identifier, self.password = api_key, identifier, password
        self.requests: list = []                # (method, path, body)
        self.positions: dict = {}               # dealId -> dict
        self.confirms: dict = {}
        self.logins = 0
        self.tokens = None
        self.hedging = False
        self.equity = 10.13
        self.current_account = "ACC1"
        self.market_status = "TRADEABLE"
        self.bid, self.offer = 79000.0, 79010.0
        self.min_size, self.step, self.lot_size = 0.0001, 0.0001, 1
        self.min_dist_pct = 0.1
        self.reject_next = 0                    # number of upcoming order requests to reject
        self.merge_keeps_old_levels = True      # worst case: a merged add does not take the new stop/target
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._server = None
        self.base_url = ""

    # ---- lifecycle ---------------------------------------------------------------------------
    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):      # silence
                pass

            def _do(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                body = json.loads(raw) if raw else None
                with fake._lock:
                    status, payload, headers = fake.route(method, self.path, dict(self.headers), body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self): self._do("GET")
            def do_POST(self): self._do("POST")
            def do_PUT(self): self._do("PUT")
            def do_DELETE(self): self._do("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=lambda: self._server.serve_forever(poll_interval=0.02), daemon=True).start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    # ---- helpers for tests -----------------------------------------------------------------------
    def expire_session(self):
        self.tokens = None

    def order_calls(self):
        return [r for r in self.requests if r[0] in ("POST", "PUT", "DELETE") and "/positions" in r[1]]

    def first_position(self):
        return next(iter(self.positions.values()), None)

    # ---- routing -----------------------------------------------------------------------------------
    def route(self, method, path, headers, body):
        h = {k.lower(): v for k, v in headers.items()}
        self.requests.append((method, path, body))
        if method == "POST" and path == "/api/v1/session":
            if h.get("x-cap-api-key") != self.api_key or not body \
                    or body.get("identifier") != self.identifier or body.get("password") != self.password:
                return 401, {"errorCode": "error.invalid.details"}, {}
            self.logins += 1
            self.tokens = (f"CST{self.logins}", f"SEC{self.logins}")
            return 200, {"currentAccountId": self.current_account, "accountInfo": {"balance": self.equity}}, \
                {"CST": self.tokens[0], "X-SECURITY-TOKEN": self.tokens[1]}
        if not self.tokens or h.get("cst") != self.tokens[0] or h.get("x-security-token") != self.tokens[1]:
            return 401, {"errorCode": "error.invalid.session.token"}, {}

        if path == "/api/v1/session" and method == "PUT":
            self.current_account = body["accountId"]
            return 200, {"dealingEnabled": True}, {}
        if path == "/api/v1/accounts" and method == "GET":
            return 200, {"accounts": [{"accountId": self.current_account, "preferred": True,
                                       "balance": {"balance": self.equity, "deposit": self.equity,
                                                   "profitLoss": 0.0, "available": self.equity}}]}, {}
        if path == "/api/v1/accounts/preferences" and method == "GET":
            return 200, {"hedgingMode": self.hedging,
                         "leverages": {"CRYPTOCURRENCIES": {"current": 2, "available": [1, 2]}}}, {}
        if path.startswith("/api/v1/markets/") and method == "GET":
            return 200, {"instrument": {"epic": "BTCUSD", "lotSize": self.lot_size},
                         "dealingRules": {"minDealSize": {"unit": "POINTS", "value": self.min_size},
                                          "maxDealSize": {"unit": "POINTS", "value": 100},
                                          "minSizeIncrement": {"unit": "POINTS", "value": self.step},
                                          "minStopOrProfitDistance": {"unit": "PERCENTAGE", "value": self.min_dist_pct}},
                         "snapshot": {"marketStatus": self.market_status, "bid": self.bid, "offer": self.offer}}, {}
        if path == "/api/v1/positions" and method == "GET":
            return 200, {"positions": [self._pos_json(p) for p in self.positions.values()]}, {}
        if path.startswith("/api/v1/positions/"):
            deal_id = path.rsplit("/", 1)[1]
            p = self.positions.get(deal_id)
            if method == "GET":
                return (200, self._pos_json(p), {}) if p else (404, {"errorCode": "error.not-found"}, {})
            if p is None:
                return 404, {"errorCode": "error.not-found"}, {}
            if method == "PUT":
                if "stopLevel" in body:
                    p["stopLevel"] = body["stopLevel"]
                if "profitLevel" in body:
                    p["profitLevel"] = body["profitLevel"]
                return 200, {"dealReference": self._confirm(deal_id, "AMENDED")}, {}
            if method == "DELETE":
                del self.positions[deal_id]
                return 200, {"dealReference": self._confirm(deal_id, "CLOSED")}, {}
        if path == "/api/v1/positions" and method == "POST":
            return self._open(body)
        if path.startswith("/api/v1/confirms/") and method == "GET":
            c = self.confirms.get(path.rsplit("/", 1)[1])
            return (200, c, {}) if c else (404, {"errorCode": "error.not-found"}, {})
        return 404, {"errorCode": "error.unknown-route"}, {}

    def _pos_json(self, p):
        return {"position": {k: v for k, v in p.items() if v is not None}, "market": {"epic": "BTCUSD"}}

    def _confirm(self, deal_id, status="OPENED", accepted=True, reason=None):
        ref = f"o_{next(self._ids)}"
        self.confirms[ref] = {"dealStatus": "ACCEPTED" if accepted else "REJECTED", "reason": reason,
                              "dealReference": ref, "status": "OPEN",
                              "affectedDeals": [{"dealId": deal_id, "status": status}]}
        return ref

    def _open(self, body):
        if self.reject_next > 0:
            self.reject_next -= 1
            return 200, {"dealReference": self._confirm("none", accepted=False, reason="INSUFFICIENT_FUNDS")}, {}
        direction, size = body["direction"], float(body["size"])
        existing = next(iter(self.positions.values()), None)
        if existing and not self.hedging:
            if existing["direction"] == direction:                 # same side: merge
                existing["size"] = round(existing["size"] + size, 8)
                if not self.merge_keeps_old_levels:
                    existing["stopLevel"] = body.get("stopLevel")
                    existing["profitLevel"] = body.get("profitLevel")
                return 200, {"dealReference": self._confirm(existing["dealId"])}, {}
            remaining = round(existing["size"] - size, 8)          # opposite side: net off
            if remaining > 0:
                existing["size"] = remaining
                return 200, {"dealReference": self._confirm(existing["dealId"], "PARTIALLY_CLOSED")}, {}
            del self.positions[existing["dealId"]]
            return 200, {"dealReference": self._confirm(existing["dealId"], "CLOSED")}, {}
        deal_id = f"DEAL{next(self._ids)}"
        self.positions[deal_id] = {"dealId": deal_id, "direction": direction, "size": size,
                                   "level": self.offer if direction == "BUY" else self.bid,
                                   "stopLevel": body.get("stopLevel"), "profitLevel": body.get("profitLevel"),
                                   "upl": 0.0, "epic": body["epic"]}
        return 200, {"dealReference": self._confirm(deal_id)}, {}
