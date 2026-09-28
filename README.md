# AMF webhook relay

Receives the JSON alerts from the **AMF-BTC Engine v1.1** TradingView strategy and turns them
into orders on **Capital.com**, behind a stack of safety checks.

```
TradingView alert --HTTPS POST--> Caddy (TLS) --> FastAPI /webhook --> queue --> worker
                                                       |                          |
                                             passphrase, JSON check,       safety gates, then
                                             duplicate check (SQLite)      Capital.com REST API
```

The web request only authenticates, validates, de-duplicates and queues the alert (a few
milliseconds; TradingView cancels requests that take longer than 3 seconds). A single worker then
executes alerts strictly one at a time, in the order they arrived.

## Project structure

```
amf-relay/
├── app/
│   ├── main.py          FastAPI routes: /webhook /health /status /admin/*   (thin glue)
│   ├── handler.py       auth, JSON validation, duplicate check, queue; worker loop
│   ├── executor.py      BUY / SELL / ADD / REDUCE / CLOSE / UPDATE logic + safety gates
│   ├── capital.py       Capital.com client: open_long, open_short, close_position,
│   │                    modify_stop_loss, modify_take_profit (+ modify_levels, reduce_position)
│   ├── safety.py        size, leverage, stop/target checks, staleness, KillSwitch
│   ├── models.py        alert JSON -> Signal (field names and aliases)
│   ├── store.py         SQLite: duplicate protection, audit log, "which trade is open"
│   ├── config.py        settings from .env (fails to start if unsafe)
│   ├── notify.py        optional Telegram messages
│   ├── logging_setup.py console + rotating file log, secrets scrubbed
│   └── aio_http.py      tiny HTTP helper (standard library)
├── tests/               75 tests incl. a mock Capital.com server:  python -m unittest discover -s tests -t .
├── deploy/              amf-relay.service (systemd), Caddyfile (HTTPS)
├── requirements.txt     only fastapi + uvicorn
└── .env.example
```

## Alert fields

The Pine v1.1 alert uses these names; the shorter names you listed work as aliases.

| You asked for | Pine v1.1 sends | Notes |
|---|---|---|
| id | `trade_id` (+ `leg`) | one `trade_id` per trade, shared by BUY/ADD/REDUCE/CLOSE/UPDATE |
| event | `event` | BUY SELL ADD REDUCE CLOSE UPDATE |
| side | `direction` | LONG / SHORT |
| price | `entry` | |
| stop / target | `stop_loss` / `take_profit` | may be `null` |
| confidence, mode | `confidence`, `mode` | logged only |
| risk | `risk_pct` | logged only |
| leverage | `leverage` | checked against the resulting leverage |
| qty | `size` | BTC |
| (secret) | `passphrase` | compared in constant time, never logged or stored |

## What the safety layer does

* **Auth:** wrong or missing passphrase -> 401; 10 failures from one IP in 10 minutes -> that IP is blocked for a while. Optional `ALLOWED_IPS`.
* **Duplicates:** every action gets a key (BUY/SELL once per `trade_id`, ADD/REDUCE once per leg, CLOSE once per trade, UPDATE once per bar). Stored in SQLite, so restarts do not forget.
* **Stale messages:** CLOSE / UPDATE / ADD / REDUCE are ignored unless they belong to the trade that is currently open.
* **Never opens** with quantity 0, without a stop-loss, with a stop/target on the wrong side of the price or closer than the broker minimum, above `MAX_QTY`, above the broker's min/max size (rounded *down* to the size step), when the market is not tradeable, when the live price is more than `MAX_PRICE_DEVIATION_PCT` away from the alert, when the alert is older than `MAX_SIGNAL_AGE_SECONDS`, when a position is already open, when hedging mode is on, or past `MAX_ORDERS_PER_DAY`.
* **Leverage:** resulting exposure / equity may not exceed `MAX_LEVERAGE` **or** the alert's own `leverage` (+`LEVERAGE_TOLERANCE`). A missing leverage value is refused. (ADD alerts carry the leverage from *before* the add, so they are checked against the hard cap and `MAX_QTY` only.)
* **Stops only tighten:** an UPDATE that would move the stop away from price is ignored.
* **Kill switch** (blocks BUY/SELL/ADD; CLOSE/UPDATE keep working): `KILL_SWITCH=true`, or `touch data/KILL_SWITCH` (instant, no restart), or `POST /admin/kill`. It also engages **automatically** after `MAX_CONSECUTIVE_FAILURES` broker failures in a row. `POST /admin/flatten` engages it and closes every position.
* **Dry run:** `DRY_RUN=true` (the default) logs every order it would send and sends none; logins, prices and balance are still read, so credentials are proven.
* **Every** received alert is logged, including rejected ones, and its outcome is stored (`DONE`, `DRY_RUN`, `REJECTED`, `IGNORED`, `FAILED`).

Rejection codes you will see: `ZERO_QUANTITY` `SIZE_TOO_SMALL` `SIZE_ABOVE_BROKER_MAX` `QTY_ABOVE_MAX_QTY` `MISSING_STOP` `STOP_ON_WRONG_SIDE` `STOP_TOO_CLOSE` `TARGET_ON_WRONG_SIDE` `LEVERAGE_ABOVE_SIGNAL` `LEVERAGE_ABOVE_HARD_CAP` `MISSING_LEVERAGE` `POSITION_ALREADY_OPEN` `MARKET_NOT_TRADEABLE` `PRICE_DEVIATION` `STALE_SIGNAL` `STALE_TRADE_ID` `HEDGING_MODE_ON` `KILL_SWITCH` `DAILY_ORDER_LIMIT` `SYMBOL_NOT_ALLOWED` `NO_POSITION`.

## 1. Prepare Capital.com

1. Create a **demo** account (or use your live one later) and switch on **2FA**.
2. Settings > API integrations > **Generate new key**. Pick a *custom password* for the key, copy the key (shown once).
3. Account preferences: **hedging mode OFF** (the relay assumes one net position). Note the crypto leverage your account allows; the relay cannot exceed it (a higher order is refused by the broker).
4. Confirm the market name: after the first start the log prints the epic's status, or search once with `GET /api/v1/markets?searchTerm=BTC`. The default epic is `BTCUSD` and must match the Pine input *Broker symbol*.

## 2. Run locally

Python 3.10+ is required.

```bash
cd amf-relay
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env       # then edit .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"    # use this for WEBHOOK_SECRET

python -m unittest discover -s tests -t .    # optional: 75 tests, no network needed
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Startup prints a self-test (login, account, equity, hedging mode, market status, minimum size).
Send a test alert (keep `DRY_RUN=true`):

```bash
curl -s -X POST http://127.0.0.1:8000/webhook -H "Content-Type: application/json" -d '{
 "trade_id":"TEST-0001","leg":"E0","passphrase":"YOUR_WEBHOOK_SECRET","event":"BUY","symbol":"BTCUSD",
 "direction":"LONG","entry":79000,"stop_loss":78800,"take_profit":79400,"confidence":75,
 "mode":"SCALP","risk_pct":1,"leverage":8,"size":0.001,"reason":"SIGNAL"}'
```

Use a price near the *current* BTC price (the relay refuses entries far from the live price) and a
new `trade_id` for every test. To receive real TradingView alerts on your own machine you need a
public HTTPS address (a tunnel such as ngrok or cloudflared); on a server, see below.

## 3. Deploy on a VPS (Ubuntu 22.04 / 24.04)

Any small VPS is enough (1 vCPU, 1 GB). You also need a domain or subdomain pointing at its IP.

```bash
# as root (or with sudo)
apt update && apt install -y python3-venv ufw caddy      # Caddy: see caddyserver.com/docs/install if not in your repo
adduser --system --group --home /opt/amf-relay amf
# copy the project to /opt/amf-relay (scp -r, git clone, ...), then:
chown -R amf:amf /opt/amf-relay
cd /opt/amf-relay
sudo -u amf python3 -m venv venv
sudo -u amf venv/bin/pip install -r requirements.txt
sudo -u amf cp .env.example .env && sudo -u amf chmod 600 .env && sudo -u amf nano .env
sudo -u amf mkdir -p data

cp deploy/amf-relay.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now amf-relay
journalctl -u amf-relay -f                     # watch the startup self-test

# HTTPS: edit deploy/Caddyfile (your domain), then
cp deploy/Caddyfile /etc/caddy/Caddyfile && systemctl reload caddy

ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
curl https://relay.example.com/health          # -> {"status":"ok"}
```

Port 8000 stays bound to `127.0.0.1`; Caddy is the only thing exposed, and it forwards only
`/webhook` and `/health`. Ports 80/443 are also the only ports TradingView will call.

## 4. Connect TradingView

1. Turn on **2FA** on your TradingView account; webhooks need it, and a paid plan.
2. Pine strategy inputs: *Webhook passphrase* = your `WEBHOOK_SECRET`, *Broker symbol* = `BTCUSD` (your epic).
3. Create **one** alert on the strategy:
   * Condition: **AMF-BTC Engine v1.1**, then choose **Order fills and alert() function calls**
   * Message: `{{strategy.order.alert_message}}`
   * Notifications: tick **Webhook URL** and enter `https://relay.example.com/webhook`
4. Leave `DRY_RUN=true` and watch `journalctl -u amf-relay -f` until the alerts you expect show up as `DRY_RUN`.

Do not create a second alert for the same strategy; duplicates are filtered, but there is no reason to test that.
Optionally set `ALLOWED_IPS` to TradingView's sender addresses (see `.env.example`).

## 5. Demo -> live checklist

1. Demo account, `DRY_RUN=true`: alerts arrive, log lines look right.
2. Demo account, `DRY_RUN=false`: check the first real demo trade in the Capital.com app: **size** equals the alert (0.001 -> 0.001), **stop and target are attached**, later UPDATEs move the stop, CLOSE closes it.
3. Try the kill switch (`touch data/KILL_SWITCH`) and `POST /admin/flatten` once, on purpose.
4. Only then `CAPITAL_DEMO=false` with a new *live* API key, and keep `MAX_QTY` tiny.

## 6. Operating it

```bash
journalctl -u amf-relay -f                 # live log (also data/logs/relay.log)
sudo -u amf touch /opt/amf-relay/data/KILL_SWITCH      # block new trades now
sudo -u amf rm    /opt/amf-relay/data/KILL_SWITCH      # allow them again
# admin routes work only from the server itself (Caddy does not expose them):
curl -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:8000/status
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:8000/admin/flatten
```

* Set an uptime monitor on `https://relay.example.com/health`, and enable Telegram messages.
* The stop-loss and target are held **by the broker** from the moment of entry, so an open trade stays protected if this server is down; but a missed UPDATE or CLOSE is not replayed.
* Capital.com API keys expire (one year by default): put the date in your calendar.
* Changing the secret: edit `.env`, `systemctl restart amf-relay`, and change the Pine input.

## Known limits (read before going live)

* **Only tested against a mock** of the Capital.com API written from the official documentation. Step 5.2 is the real test.
* **Partial closes (REDUCE)** are off by default: the API has no partial close, and the opposite-order netting this relies on is not described in the documentation I read. With it off, a TradingView partial does not shrink the broker position; the later CLOSE still exits everything.
* Position **size** is assumed to be in BTC (lot size 1); the relay refuses to trade if the market reports a different lot size.
* Equity is the account `balance`, which the docs' examples show as deposit + profit/loss.
* This is a relay, not a risk manager: the strategy decides what to trade; the relay only refuses what looks unsafe.
