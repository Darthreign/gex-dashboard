"""Meilleur bid / ask et dernière transaction des futures, en direct, pour Squirrel X.

La capture reçoit `Quote` et `TimeAndSale` de dxFeed sans regroupement et relaie
l'état du contrat ACTIF de chaque produit toutes les 250 ms (cf. capturebus) ; ce
module le pousse dans le flux SSE des ticks sous l'étiquette `quote:<PRODUIT>`,
seulement quand il change. Profondeur limitée au premier niveau : dxFeed ne nous
ouvre pas le service DOM (essai du 15/11/2026).

Données courtier : personnelles, non redistribuables.
"""
from __future__ import annotations

import json

from . import broadcast
from .tickcapture import TICK_ONLY_FUTURES, TRACKED_FUTURES

PRODUCTS = TRACKED_FUTURES + TICK_ONLY_FUTURES
FEED_INTERVAL_S = 0.25      # rythme de la liaison capture -> dashboard (capturebus.PUSH_INTERVAL_S)
FIELDS = ("contract", "bid", "ask", "bid_size", "ask_size", "last", "last_size", "last_t")


def read(symbol: str) -> dict | None:
    """État courant de `symbol` : miroir de la capture en mode séparé, capture
    locale sinon (même règle que api._futures_last_price)."""
    from . import flowtape
    from .capturebus import remote_url
    remote = getattr(flowtape.TAPE, "live_quote", None)
    if remote_url() and remote is not None:
        return remote(symbol)
    from .tickcapture import CAPTURE
    return CAPTURE.live_quote(symbol)


def _clean(v):
    """Nombre fini ou None : un NaN de dxFeed ne doit pas casser le JSON du flux."""
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    return v


def quote_producer(symbol: str, read=read):
    last = {"j": None}

    def produce():
        q = read(symbol)
        if not q:
            return None
        j = json.dumps({"symbol": symbol, **{k: _clean(q.get(k)) for k in FIELDS}}, allow_nan=False)
        if j == last["j"]:
            return None
        last["j"] = j
        return f"data: {j}\n\n"
    return produce


def feed_channels(quotes: str | None) -> dict[str, broadcast.Channel]:
    """Canaux `quote:<PRODUIT>` demandés par `quotes=NQ,ES` ; un produit inconnu est ignoré."""
    return {f"quote:{s}": broadcast.channel(("live-quote", s), lambda s=s: quote_producer(s), FEED_INTERVAL_S)
            for s in (quotes or "").split(",") if s in PRODUCTS}
