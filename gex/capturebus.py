"""Liaison entre le process CAPTURE et le dashboard (WebSocket local, poussé).

Pourquoi : quand tout vivait dans `run.py`, chaque redémarrage du dashboard
coupait la capture des ticks et de l'order flow. Désormais :

    process capture (gex.capture)  ──WebSocket 127.0.0.1──▶  dashboard
    flux dxFeed, ticks, tape, barres,                        miroir en mémoire
    écriture disque                                          (RemoteTape)

Le serveur (`serve`) pousse toutes les 250 ms ce qui est NOUVEAU depuis le
dernier envoi à CE client, et un instantané complet à la connexion. Le miroir
(`RemoteTape`) expose la même interface de lecture que `flowtape.FlowTape`
(`recent_prints`, `live_rows`, `live_points`, `status`) : le code des graphes ne
sait pas s'il lit un collecteur local ou distant.

Sans `GEX_CAPTURE_URL`, rien de tout cela n'est utilisé : le dashboard reste
autonome comme avant (promesse du README). Par défaut la liaison
n'écoute que sur 127.0.0.1 ; pour la joindre via Tailscale, GEX_CAPTURE_BIND y
ajoute l'IP Tailscale (jamais 0.0.0.0 : Wi-Fi et réseau local seraient exposés).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime

from .flowtape import PRINT_BUFFER, FlowTape
from .rtquote import BACKOFF_MAX, BACKOFF_START, _env

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PUSH_INTERVAL_S = 0.25
STALE_AFTER_S = 5.0          # sans message depuis ce délai, le miroir se dit hors ligne
MAX_MESSAGE = 2 ** 26        # l'instantané (prints + points live) dépasse le 1 Mo par défaut


def remote_url() -> str | None:
    """URL du process capture si le dashboard doit s'y brancher, sinon None
    (= mode autonome, collecteurs dans le même process)."""
    return _env("GEX_CAPTURE_URL") or None


# ---------------------------------------------------------------------------
# Côté capture : serveur
# ---------------------------------------------------------------------------

async def _handler(ws, tape: FlowTape) -> None:
    marks = None                                   # None => instantané au 1er envoi
    while True:
        payload, marks = tape.export_delta(marks)
        payload["type"] = "delta"
        payload["sent"] = time.time()
        await ws.send(json.dumps(payload))
        await asyncio.sleep(PUSH_INTERVAL_S)


def bind_hosts() -> list[str]:
    """Adresses d'écoute, depuis GEX_CAPTURE_BIND (séparées par des virgules) ;
    127.0.0.1 par défaut. Pour joindre la capture via Tailscale, y ajouter l'IP
    Tailscale du PC (ex. « 127.0.0.1,100.109.109.123 ») : ne PAS mettre 0.0.0.0,
    qui exposerait aussi le Wi-Fi et le réseau local (flux courtier, sans mot de
    passe, usage personnel)."""
    brut = _env("GEX_CAPTURE_BIND") or DEFAULT_HOST
    hosts = [h.strip() for h in brut.split(",") if h.strip()]
    return hosts or [DEFAULT_HOST]


async def _serve(tape: FlowTape, host, port: int) -> None:
    import websockets
    from contextlib import AsyncExitStack

    async def handler(ws):
        try:
            await _handler(ws, tape)
        except websockets.ConnectionClosed:
            pass

    hosts = [host] if isinstance(host, str) else list(host)
    async with AsyncExitStack() as stack:
        ecoutes = []
        for h in hosts:
            try:
                await stack.enter_async_context(websockets.serve(
                    handler, h, port, max_size=MAX_MESSAGE,
                    ping_interval=20, ping_timeout=20))
                ecoutes.append(h)
            except OSError as exc:
                # p. ex. Tailscale pas encore monté au démarrage du PC : on ne
                # renonce pas à la capture pour une adresse indisponible
                log.warning("Liaison capture : impossible d'écouter sur %s:%d (%s)",
                            h, port, exc)
        if not ecoutes:
            raise RuntimeError("liaison capture : aucune adresse d'écoute disponible")
        log.info("Liaison capture : écoute sur %s",
                 ", ".join(f"ws://{h}:{port}" for h in ecoutes))
        await asyncio.Future()                     # tourne indéfiniment


def serve(tape: FlowTape, host=DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Bloque : sert `tape` aux abonnés. À lancer dans le process capture.
    `host` : une adresse, ou une liste (chacune indisponible est ignorée)."""
    asyncio.run(_serve(tape, host, port))


def serve_in_thread(tape: FlowTape, host=DEFAULT_HOST,
                    port: int = DEFAULT_PORT) -> threading.Thread:
    th = threading.Thread(target=serve, args=(tape, host, port),
                          name="capturebus", daemon=True)
    th.start()
    return th


# ---------------------------------------------------------------------------
# Côté dashboard : miroir
# ---------------------------------------------------------------------------

class RemoteTape:
    """Miroir en lecture seule d'un `FlowTape` qui vit dans un autre process.

    Même interface de lecture que `FlowTape` pour ce qu'utilise le dashboard.
    Hors ligne (capture arrêtée, coupure), `status()` le dit et les lectures
    renvoient ce qu'on avait : le dashboard n'a jamais à planter pour ça."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.lock = threading.Lock()
        self._prints: dict[str, deque] = {}
        self._pts: dict[str, deque] = {}
        self._rows: dict[str, list[dict]] = {}
        self._status: tuple[str, int] = ("connecting", 0)
        self._last_msg = 0.0
        self._started = False

    # -- cycle de vie -----------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, name="remotetape", daemon=True).start()

    def _run(self) -> None:
        backoff = BACKOFF_START
        while True:
            try:
                asyncio.run(self._session())
                backoff = BACKOFF_START
            except Exception as exc:  # noqa: BLE001 — la liaison doit survivre à tout
                log.warning("Liaison capture interrompue (%s) — reprise dans %.0f s",
                            exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _session(self) -> None:
        import websockets
        async with websockets.connect(self.url, max_size=MAX_MESSAGE,
                                      ping_interval=20, ping_timeout=20) as ws:
            log.info("Liaison capture : connecté à %s", self.url)
            async for raw in ws:
                self.apply(json.loads(raw))
        raise ConnectionError("liaison fermée par la capture")

    # -- application d'un message (testable sans réseau) -----------------

    def apply(self, msg: dict, now: float | None = None) -> None:
        with self.lock:
            if msg.get("snapshot"):
                self._prints.clear()
                self._pts.clear()
            for s, recs in (msg.get("prints") or {}).items():
                buf = self._prints.setdefault(s, deque(maxlen=PRINT_BUFFER))
                buf.extend(recs)
            for s, pts in (msg.get("pts") or {}).items():
                q = self._pts.setdefault(s, deque())
                q.extend(tuple(p) for p in pts)
            cutoff = (time.time() if now is None else now) - FlowTape.SECONDS_KEPT
            for q in self._pts.values():
                while q and q[0][0] < cutoff:
                    q.popleft()
            rows = msg.get("rows")
            if rows is not None:
                self._rows = {
                    s: [{**r, "timestamp": datetime.fromisoformat(r["timestamp"])}
                        for r in lst] for s, lst in rows.items()}
            st = msg.get("status")
            if st:
                self._status = (st[0], int(st[1]))
            self._last_msg = time.time() if now is None else now

    # -- lecture (même interface que FlowTape) ----------------------------

    def _online(self) -> bool:
        return self._last_msg > 0 and time.time() - self._last_msg < STALE_AFTER_S

    def status(self) -> tuple[str, int]:
        with self.lock:
            state, n = self._status
        return (state, n) if self._online() else ("off", 0)

    def recent_prints(self, symbol: str, min_size: float = 0.0,
                      include_combos: bool = True, limit: int = 60) -> list[dict]:
        with self.lock:
            buf = list(self._prints.get(symbol, ()))
        out = []
        for rec in reversed(buf):
            if rec["size"] < min_size:
                continue
            if rec["combo"] and not include_combos:
                continue
            out.append(dict(rec))
            if len(out) >= limit:
                break
        return out

    def live_rows(self, symbol: str) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self._rows.get(symbol, ())]

    def live_points(self, symbol: str, window_s: int = 300,
                    now: float | None = None) -> list[tuple[float, float, int]]:
        now = time.time() if now is None else now
        with self.lock:
            return [p for p in self._pts.get(symbol, ()) if p[0] >= now - window_s]
