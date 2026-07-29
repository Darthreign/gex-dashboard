"""FEED_CONFIG peut arriver PLUSIEURS fois sur une même connexion dxLink —
le serveur le renvoie à chaque évolution de la configuration du feed, y
compris après notre propre souscription.

Régression du 2026-07-29 : en passant au format COMPACT, la souscription est
passée de CHANNEL_OPENED (unique) à FEED_CONFIG (répété), donc la salve
entière repartait à chaque renvoi — trois salves observées en 100 ms, et
dxFeed rejetait la collecte en "BAD_ACTION: Your subscription rate is too
high". Plus aucune chaîne native NQ/ES ne passait.

Ces tests rejouent une session complète contre un faux WebSocket : ce qui est
vérifié, c'est qu'un seul FEED_SUBSCRIPTION est émis quel que soit le nombre
de FEED_CONFIG reçus.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from gex import futopt, rtquote


class FakeWS:
    """WebSocket minimal rejouant un scénario dxLink, avec trois FEED_CONFIG.

    Enregistre tout ce que le client envoie dans `sent`, pour compter les
    FEED_SUBSCRIPTION.
    """

    def __init__(self, script: list[dict]) -> None:
        self._script = list(script)
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if not self._script:
            raise asyncio.TimeoutError
        return json.dumps(self._script.pop(0))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self._script:
            raise StopAsyncIteration
        return json.dumps(self._script.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def subscriptions(self) -> list[dict]:
        return [m for m in self.sent if m.get("type") == "FEED_SUBSCRIPTION"]


SCRIPT = [
    {"type": "AUTH_STATE", "channel": 0, "state": "UNAUTHORIZED"},
    {"type": "AUTH_STATE", "channel": 0, "state": "AUTHORIZED"},
    {"type": "CHANNEL_OPENED", "channel": 1},
    # les trois renvois qui déclenchaient la régression
    {"type": "FEED_CONFIG", "channel": 1},
    {"type": "FEED_CONFIG", "channel": 1},
    {"type": "FEED_CONFIG", "channel": 1},
]


def _patch_ws(monkeypatch, module, ws: FakeWS) -> None:
    """Remplace websockets.connect dans le module testé (les deux font un
    `import websockets` local, donc on patche la bibliothèque elle-même)."""
    import websockets

    monkeypatch.setattr(websockets, "connect", lambda *a, **k: ws)


def test_rtquote_ne_souscrit_quune_fois(monkeypatch):
    ws = FakeWS(SCRIPT)
    _patch_ws(monkeypatch, rtquote, ws)
    q = rtquote.RealtimeQuotes()
    monkeypatch.setattr(q, "_quote_token", lambda: ("tok", "wss://x", "acc"))
    monkeypatch.setattr(q, "_resolve_symbols", lambda access: {"NQ": "/NQU26:XCME"})

    asyncio.run(q._session())

    assert len(ws.subscriptions()) == 1


def test_futopt_ne_souscrit_quune_fois(monkeypatch):
    ws = FakeWS(SCRIPT)
    _patch_ws(monkeypatch, futopt, ws)
    monkeypatch.setattr(futopt, "quote_token", lambda: ("tok", "wss://x", "acc"))

    asyncio.run(futopt._collect_one(["./NQC28000:XCME"], ("Quote", "Greeks", "Summary"),
                                    timeout=0.1))

    assert len(ws.subscriptions()) == 1


def test_la_souscription_porte_bien_tous_les_symboles(monkeypatch):
    """Le verrou ne doit pas tronquer la salve : une seule souscription, mais
    complète (symboles x événements)."""
    ws = FakeWS(SCRIPT)
    _patch_ws(monkeypatch, futopt, ws)
    monkeypatch.setattr(futopt, "quote_token", lambda: ("tok", "wss://x", "acc"))

    symbols = ["./NQC28000:XCME", "./NQP28000:XCME"]
    events = ("Quote", "Greeks", "Summary")
    asyncio.run(futopt._collect_one(symbols, events, timeout=0.1))

    subs = ws.subscriptions()
    assert len(subs) == 1
    assert len(subs[0]["add"]) == len(symbols) * len(events)
