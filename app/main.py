"""FastAPI glue. All real logic lives in handler.py / executor.py / capital.py.

Run:  uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .capital import CapitalClient
from .config import Settings, load_env_file
from .executor import OPEN_TRADE_KEY, Executor
from .handler import WebhookHandler, worker
from .logging_setup import setup_logging
from .notify import Notifier
from .safety import KillSwitch
from .store import Store

log = logging.getLogger("relay")


def create_app(settings: Optional[Settings] = None, broker=None) -> FastAPI:
    if settings is None:
        load_env_file(".env")
        settings = Settings.from_env()          # exits with a clear message if the config is unsafe

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level, settings.data_dir, settings.secrets)
        store = Store(settings.data_dir / "relay.db")
        kill = KillSwitch(settings.kill_switch, settings.data_dir / "KILL_SWITCH")
        b = broker or CapitalClient(settings)
        notifier = Notifier(settings.telegram_bot_token, settings.telegram_chat_id, settings.dry_run)
        executor = Executor(settings, b, store, kill, notifier)
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        app.state.settings, app.state.store, app.state.kill = settings, store, kill
        app.state.executor, app.state.queue = executor, queue
        app.state.handler = WebhookHandler(settings, store, queue)
        task = asyncio.create_task(worker(queue, executor))
        log.info("relay starting: dry_run=%s demo=%s kill_switch=%s epics=%s", settings.dry_run,
                 settings.capital_demo, kill.is_active(), sorted(settings.allowed_epics))
        await executor.preflight()
        try:
            yield
        finally:
            task.cancel()
            await b.aclose()
            store.close()

    # interactive docs are switched off: nothing but /webhook and the admin routes exist
    app = FastAPI(title="AMF webhook relay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def admin_ok(request: Request) -> bool:
        token = settings.admin_token
        sent = request.headers.get("x-admin-token", "")
        return bool(token) and hmac.compare_digest(sent.encode(), token.encode())

    @app.post("/webhook")
    async def webhook(request: Request):
        raw = await request.body()
        ip = request.client.host if request.client else "unknown"
        code, body = app.state.handler.handle(raw, ip)
        return JSONResponse(body, status_code=code)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/status")
    async def status(request: Request):
        if not admin_ok(request):
            return JSONResponse({"status": "forbidden"}, status_code=403)
        st = app.state
        return {"dry_run": settings.dry_run, "demo": settings.capital_demo,
                "kill_switch": st.kill.is_active(), "kill_reason": st.kill.reason(),
                "open_trade_id": st.store.get_state(OPEN_TRADE_KEY), "queue": st.queue.qsize(),
                "recent": st.store.recent(15)}

    @app.post("/admin/kill")
    async def kill_on(request: Request):
        if not admin_ok(request):
            return JSONResponse({"status": "forbidden"}, status_code=403)
        app.state.kill.activate("manual via /admin/kill")
        log.warning("KILL SWITCH engaged manually")
        return {"kill_switch": True}

    @app.post("/admin/resume")
    async def kill_off(request: Request):
        if not admin_ok(request):
            return JSONResponse({"status": "forbidden"}, status_code=403)
        cleared = app.state.kill.deactivate()
        log.warning("kill switch released manually (env flag still blocking: %s)", not cleared)
        return {"kill_switch": app.state.kill.is_active()}

    @app.post("/admin/flatten")
    async def flatten(request: Request):
        if not admin_ok(request):
            return JSONResponse({"status": "forbidden"}, status_code=403)
        app.state.kill.activate("manual flatten via /admin/flatten")
        closed = await app.state.executor.flatten()
        log.warning("FLATTEN: closed %s", closed)
        return {"kill_switch": True, "closed_deals": closed}

    return app
