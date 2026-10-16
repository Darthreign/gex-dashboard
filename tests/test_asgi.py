"""Serveur ASGI (gex/asgi.py) : SSE asynchrones, Flask/Dash via a2wsgi."""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from types import SimpleNamespace

import pytest

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("starlette")
pytest.importorskip("a2wsgi")

from flask import Flask  # noqa: E402

from gex import api, broadcast  # noqa: E402
from gex.api import register_api  # noqa: E402
from gex.asgi import build  # noqa: E402


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def serveur(monkeypatch):
    broadcast._channels.pop(("price", "NQ"), None)
    prix = {"v": 30000.0}

    def last(symbol):
        prix["v"] += 0.25
        return prix["v"]
    monkeypatch.setattr(api, "_futures_last_price", last)
    flask_app = Flask(__name__)
    register_api(flask_app)
    port = _port()
    srv = uvicorn.Server(uvicorn.Config(build(SimpleNamespace(server=flask_app)),
                                        host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield port
    srv.should_exit = True
    th.join(5)


async def _lire_evenement(port: int, path: str) -> str:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"GET {path} HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n".encode())
    await w.drain()
    buf = b""
    while b"data:" not in buf:
        buf += await asyncio.wait_for(r.read(1024), 5)
    w.close()
    return buf.decode()


def test_flux_prix_asynchrone_et_desabonnement(serveur):
    txt = asyncio.run(_lire_evenement(serveur, "/api/v1/NQ/stream"))
    assert "text/event-stream" in txt and "data: 300" in txt
    ch = broadcast._channels[("price", "NQ")]
    for _ in range(50):                     # la déconnexion libère l'abonné
        if ch.subscribers == 0:
            break
        time.sleep(0.05)
    assert ch.subscribers == 0


def test_routes_flask_servies_via_a2wsgi(serveur):
    async def get():
        r, w = await asyncio.open_connection("127.0.0.1", serveur)
        w.write(b"GET /api/v1/symbols HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        await w.drain()
        data = await asyncio.wait_for(r.read(), 5)
        w.close()
        return data.decode()
    assert asyncio.run(get()).startswith("HTTP/1.1 200")


def test_deux_cents_flux_sans_deux_cents_threads(serveur):
    avant = threading.active_count()

    async def tous():
        conns = []
        for _ in range(200):
            r, w = await asyncio.open_connection("127.0.0.1", serveur)
            w.write(b"GET /api/v1/NQ/stream HTTP/1.1\r\nHost: x\r\n\r\n")
            conns.append((r, w))
        await asyncio.gather(*(w.drain() for _, w in conns))
        recus = 0
        for r, _ in conns:
            buf = b""
            while b"data:" not in buf:
                buf += await asyncio.wait_for(r.read(1024), 10)
            recus += 1
        pendant = threading.active_count()
        for _, w in conns:
            w.close()
        return recus, pendant
    recus, pendant = asyncio.run(tous())
    assert recus == 200
    assert pendant - avant < 10          # un producteur partagé, pas un thread par flux
