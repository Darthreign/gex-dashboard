"""Calcul UNIQUE, diffusion à tous les clients.

Deux mécanismes, un même principe : le coût d'un calcul ne doit pas croître
avec le nombre d'onglets ouverts.

- `Channel` : un flux poussé (SSE). UN SEUL thread producteur par clé
  (symbole, paramètres) calcule le contenu, le sérialise une fois et le publie
  avec un numéro de version. Chaque connexion se contente d'attendre la
  version suivante et d'écrire des octets déjà prêts. Le producteur démarre
  au premier abonné et s'arrête après `idle_stop` secondes sans abonné.
  Avant : une boucle de calcul PAR connexion (1 à 10 Hz chacune).

- `shared` : décorateur de callback Dash. Les onglets qui demandent la même
  chose (mêmes entrées, au compteur d'intervalle près) dans la fenêtre `ttl`
  reçoivent le MÊME résultat, calculé une fois ; les requêtes concurrentes
  attendent le premier calcul au lieu de le refaire (single-flight).
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

KEEPALIVE_S = 15.0


class Channel:
    def __init__(self, key: tuple, producer: Callable[[], str | None],
                 interval: float, idle_stop: float = 30.0):
        self.key = key
        self._producer = producer
        self._interval = interval
        self._idle_stop = idle_stop
        self._cond = threading.Condition()
        self._version = 0
        self._payload: str | None = None
        self._subscribers = 0
        self._last_unsub = time.monotonic()
        self._thread: threading.Thread | None = None
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    # -- producteur -----------------------------------------------------
    def publish(self, payload: str) -> None:
        with self._cond:
            self._version += 1
            self._payload = payload
            self._cond.notify_all()
            waiters = list(self._waiters)
        for loop, ev in waiters:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:  # boucle fermée : l'abonné est parti
                pass

    def _run(self) -> None:
        while True:
            t0 = time.monotonic()
            with self._cond:
                idle = (self._subscribers == 0
                        and t0 - self._last_unsub >= self._idle_stop)
                if idle:
                    self._thread = None
                    return
            try:
                payload = self._producer()
            except Exception:  # noqa: BLE001 — un cycle raté ne doit jamais arrêter le flux
                log.exception("Producteur %s en échec", self.key)
                payload = None
            if payload is not None:
                self.publish(payload)
            time.sleep(max(0.0, self._interval - (time.monotonic() - t0)))

    def _ensure_running(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name=f"chan-{'-'.join(map(str, self.key))}")
            self._thread.start()

    # -- abonnés --------------------------------------------------------
    def subscribe(self) -> int:
        """Renvoie la version déjà vue (0) : un nouvel abonné reçoit d'abord
        le dernier contenu publié, comme un instantané."""
        with self._cond:
            self._subscribers += 1
            self._ensure_running()
        return 0

    def unsubscribe(self) -> None:
        with self._cond:
            self._subscribers -= 1
            self._last_unsub = time.monotonic()

    def wait(self, seen: int, timeout: float) -> tuple[int, str | None]:
        """Bloque jusqu'à une version > `seen` ou `timeout`. (version, contenu)."""
        with self._cond:
            if self._version <= seen:
                self._cond.wait(timeout)
            if self._version > seen:
                return self._version, self._payload
            return seen, None

    async def wait_async(self, seen: int, timeout: float) -> tuple[int, str | None]:
        with self._cond:
            if self._version > seen:
                return self._version, self._payload
            loop = asyncio.get_running_loop()
            ev = asyncio.Event()
            self._waiters.add((loop, ev))
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            with self._cond:
                self._waiters.discard((loop, ev))
        with self._cond:
            if self._version > seen:
                return self._version, self._payload
        return seen, None

    @property
    def subscribers(self) -> int:
        return self._subscribers


_channels: dict[tuple, Channel] = {}
_registry_lock = threading.Lock()


def channel(key: tuple, factory: Callable[[], Callable[[], str | None]],
            interval: float) -> Channel:
    """Le canal de `key`, créé au premier appel. `factory()` fabrique le
    producteur (une fermeture qui garde son propre état de déduplication)."""
    with _registry_lock:
        ch = _channels.get(key)
        if ch is None:
            ch = _channels[key] = Channel(key, factory(), interval)
        return ch


def sse_events(ch: Channel, keepalive: float = KEEPALIVE_S):
    """Générateur SSE synchrone (serveur WSGI à threads)."""
    seen = ch.subscribe()
    try:
        while True:
            v, payload = ch.wait(seen, keepalive)
            if payload is not None and v > seen:
                seen = v
                yield payload
            else:
                yield ": keepalive\n\n"
    finally:
        ch.unsubscribe()


async def sse_events_async(ch: Channel, keepalive: float = KEEPALIVE_S):
    """Même flux, pour un serveur asyncio : aucune connexion ne tient de thread."""
    seen = ch.subscribe()
    try:
        while True:
            v, payload = await ch.wait_async(seen, keepalive)
            if payload is not None and v > seen:
                seen = v
                yield payload
            else:
                yield ": keepalive\n\n"
    finally:
        ch.unsubscribe()


def _retag(tag: str, payload: str) -> str:
    """« data: {json}\n\n » d'un canal -> « data: {"id": tag, "d": {json}} »."""
    body = payload[len("data: "):].strip() if payload.startswith("data: ") else payload.strip()
    return f'data: {{"id":{json.dumps(tag)},"d":{body}}}\n\n'


def _fresh(chs: dict[str, "Channel"], seen: dict[str, int]) -> list[str]:
    out = []
    for tag, ch in chs.items():
        with ch._cond:
            v, p = ch._version, ch._payload
        if v > seen[tag] and p is not None:
            seen[tag] = v
            out.append(_retag(tag, p))
    return out


def multi_events(chs: dict[str, "Channel"], keepalive: float = KEEPALIVE_S,
                 poll: float = 0.25):
    """Plusieurs canaux sur UNE connexion SSE (serveur WSGI à threads), chaque
    message étiqueté par sa cible. Un navigateur n'ouvre que 6 connexions
    HTTP/1.1 par serveur : un flux par graphique les épuisait, et les
    requêtes Dash restaient alors en attente indéfiniment."""
    seen = {tag: ch.subscribe() for tag, ch in chs.items()}
    try:
        idle = 0.0
        while True:
            out = _fresh(chs, seen)
            if out:
                idle = 0.0
                yield "".join(out)
                continue
            time.sleep(poll)
            idle += poll
            if idle >= keepalive:
                idle = 0.0
                yield ": keepalive\n\n"
    finally:
        for ch in chs.values():
            ch.unsubscribe()


async def multi_events_async(chs: dict[str, "Channel"], keepalive: float = KEEPALIVE_S):
    """Même multiplexage pour un serveur asyncio : un seul événement réveillé
    par n'importe lequel des canaux."""
    seen = {tag: ch.subscribe() for tag, ch in chs.items()}
    loop = asyncio.get_running_loop()
    ev = asyncio.Event()
    for ch in chs.values():
        with ch._cond:
            ch._waiters.add((loop, ev))
    try:
        while True:
            ev.clear()                   # avant la lecture : aucun réveil perdu
            out = _fresh(chs, seen)
            if out:
                yield "".join(out)
                continue
            try:
                await asyncio.wait_for(ev.wait(), keepalive)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        for ch in chs.values():
            with ch._cond:
                ch._waiters.discard((loop, ev))
            ch.unsubscribe()


# ---------------------------------------------------------------- callbacks

_memo: dict[tuple, tuple[float, object]] = {}
_memo_locks: dict[tuple, threading.Lock] = {}
_memo_guard = threading.Lock()
STATS = {"hits": 0, "misses": 0}


def _key_part(v) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def shared(ttl: float, skip: tuple[int, ...] = (0,)):
    """Partage le résultat d'un callback entre onglets pendant `ttl` secondes.

    `skip` : positions des arguments propres à chaque onglet (le compteur
    `n_intervals`) exclues de la clé. L'entrée déclenchante (`dash.ctx`) en
    fait partie, pour les callbacks qui en dépendent. Les exceptions (dont
    PreventUpdate) ne sont jamais mises en cache."""
    def deco(fn):
        name = fn.__qualname__

        @functools.wraps(fn)
        def wrapper(*args):
            try:
                from dash import ctx
                trig = ctx.triggered_id
            except Exception:  # noqa: BLE001 — hors requête Dash (tests)
                trig = None
            key = (name, _key_part(trig),
                   tuple(_key_part(a) for i, a in enumerate(args) if i not in skip))
            now = time.monotonic()
            hit = _memo.get(key)
            if hit and now - hit[0] < ttl:
                STATS["hits"] += 1
                return hit[1]
            with _memo_guard:
                lock = _memo_locks.setdefault(key, threading.Lock())
            with lock:
                hit = _memo.get(key)
                if hit and time.monotonic() - hit[0] < ttl:
                    STATS["hits"] += 1
                    return hit[1]
                STATS["misses"] += 1
                out = fn(*args)
                _memo[key] = (time.monotonic(), out)
                _prune(now)
                return out
        return wrapper
    return deco


def _prune(now: float, max_age: float = 600.0) -> None:
    if len(_memo) < 512:
        return
    for k in [k for k, (t, _) in list(_memo.items()) if now - t > max_age]:
        _memo.pop(k, None)
        _memo_locks.pop(k, None)
