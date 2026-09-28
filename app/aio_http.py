"""Tiny async HTTP helper on the standard library (no third-party HTTP package needed).

Blocking urllib calls run in a worker thread so the event loop is never blocked.
HTTP error statuses are returned as normal Response objects; only network problems raise.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


class TransportError(Exception):
    """Network-level failure (DNS, connection, timeout, TLS)."""


@dataclass
class Response:
    status: int
    headers: dict          # header names lower-cased
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body)
        except ValueError:
            return {}

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def _sync_request(method: str, url: str, headers: dict, data: Optional[bytes], timeout: float) -> Response:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read() or b"")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise TransportError(str(e)) from e


async def request(method: str, url: str, *, headers: Optional[dict] = None,
                  json_body: Any = None, timeout: float = 10.0) -> Response:
    hdrs = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    hdrs.setdefault("Accept", "application/json")
    hdrs.setdefault("User-Agent", "amf-relay/1.0")
    return await asyncio.to_thread(_sync_request, method, url, hdrs, data, timeout)
