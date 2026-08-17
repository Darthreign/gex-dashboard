"""Capture TICK-PAR-TICK CONTINUE des futures NQ et ES — la totale, en
permanence sur toute la session (dimanche 18h ET → vendredi 17h ET).

Pourquoi une session dxLink DÉDIÉE, et non un tap sur le flux du dashboard :
le flux temps réel (`rtquote.QUOTES`) s'abonne à `Quote`/`Trade`, deux
événements CONFLATÉS — dxFeed n'y livre qu'un échantillon (~1 print toutes
les quelques secondes), suffisant pour un spot d'affichage mais pas pour
rejouer une séquence à la seconde. `TimeAndSale`, lui, livre CHAQUE
transaction, avec sa taille. On ouvre donc notre propre connexion, on
s'abonne à `TimeAndSale` sur les deux contrats front, et on écoute en
continu — sans jamais toucher `QUOTES`, pour que le dashboard reste en direct
quoi qu'il arrive ici.

Ce qu'on garde, aligné sur le jeu de référence `ticks_full` (Databento) pour
que la capture live soit DIRECTEMENT exploitable par le backtest : `ts` (epoch
s, heure d'échange), `price`, `volume`, `source`. C'est la seule donnée non
reconstituable — ni CBOE ni le feed courtier ne rejouent un historique
tick-par-tick (le courtier n'expose l'historique qu'en bougies `Candle`). Un
tick non capté est perdu pour toujours : d'où « brut conservé, jamais recalculé ».

Volume : la session tourne ~23h/j, 5j/7, à quelques dizaines à centaines de
prints/s par contrat en séance. On agrège donc en mémoire et on vide toutes
les ~30 s (cf. scheduler.flush_ticks) vers des CHUNKS PARTITIONNÉS
(`data/ticks/NQ/<jour>/part-*.parquet`) — jamais un fichier journalier réécrit
en boucle (O(n²) sur des millions de lignes). Le buffer mémoire ne porte donc
jamais plus de ~30 s de flux.

La session se RECYCLE périodiquement (reconnexion) pour reconstruire l'univers :
le contrat front roule chaque trimestre, et une reconnexion propre vaut mieux
qu'un canal ouvert depuis des jours.

⚠️ Licence : données courtier, usage personnel, non redistribuables. Écrit
avec `source="dxfeed"`, ce qui exclut ces fichiers de l'export (cf.
gex/export.py). Sans identifiants, ce module ne démarre pas.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from .rtquote import (
    BACKOFF_MAX,
    BACKOFF_START,
    credentials_present,
    quote_token,
    resolve_symbols,
)

log = logging.getLogger(__name__)

# Les deux futures suivis. La valeur EST le libellé de stockage (data/ticks/NQ),
# la clé de résolution dans resolve_symbols -> symbole streamer (/NQU26:XCME).
TRACKED_FUTURES: tuple[str, ...] = ("NQ", "ES")

# La session se recycle (reconnexion + reconstruction d'univers) à ce rythme :
# suffisant pour rattraper un roll de contrat le jour dit et repartir sur une
# connexion fraîche, sans reconnecter pour rien en pleine séance.
UNIVERSE_REFRESH_S = 30 * 60


class TickCapture:
    """Collecteur continu : une session dxLink dédiée qui bufferise chaque
    `TimeAndSale` de NQ/ES. Démarré une fois au boot ; le scheduler vide le
    buffer sur disque toutes les ~30 s (cf. flush_ticks). Sans identifiants,
    `start()` est sans effet."""

    def __init__(self, symbols: tuple[str, ...] = TRACKED_FUTURES) -> None:
        self.symbols = tuple(symbols)
        self._buf: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._started = False
        self._state = "off"

    # -- cycle de vie -----------------------------------------------------

    def start(self) -> None:
        """Lance le collecteur en tâche de fond (idempotent). Sans identifiants
        courtier, ne fait rien — le repli public délayé ~15 min n'a aucune
        valeur pour du tick-par-tick."""
        if self._started:
            return
        if not credentials_present():
            log.info("Capture tick continue désactivée (identifiants absents)")
            self._state = "off"
            return
        self._started = True
        self._state = "connecting"
        threading.Thread(target=self._run, name="tickcapture", daemon=True).start()

    def drain(self) -> dict[str, list[dict]]:
        """Récupère et vide le buffer (appelé par le scheduler pour écrire sur
        disque). Un swap sous verrou : la capture continue d'alimenter un
        buffer neuf pendant l'écriture."""
        with self._lock:
            out, self._buf = self._buf, {}
        return out

    # -- capture (chemin réseau) ------------------------------------------

    def record(self, universe: dict[str, str], item: dict, now: float) -> None:
        """Range un print TimeAndSale dans le buffer. Public : c'est le point
        testable du module (mapping, filtrage, forme de ligne), sans réseau.

        `now` (réception locale) ne sert que de repli : on préfère l'heure
        d'ÉCHANGE (`time`, en ms) quand elle est présente — c'est elle qui fait
        foi pour rejouer une séquence."""
        symbol = universe.get(item.get("eventSymbol"))
        if symbol is None:
            return
        price = item.get("price")
        if not isinstance(price, (int, float)) or price != price:
            return  # pas de prix exploitable (NaN inclus) -> ignoré
        exch = item.get("time")
        ts = exch / 1000.0 if isinstance(exch, (int, float)) and exch == exch else now
        row = {
            "ts": float(ts),
            "price": float(price),
            "volume": _vol(item.get("size")),
            "source": "dxfeed",
        }
        with self._lock:
            self._buf.setdefault(symbol, []).append(row)

    def _build_universe(self, access: str) -> dict[str, str]:
        """streamer -> libellé (NQ/ES), pour les seuls futures suivis.

        Réutilise `resolve_symbols`, qui lit le contrat actif via l'API
        authentifiée (`/NQU26:XCME`) — un future NON résolu est OMIS, jamais
        rabattu sur le ticker action homonyme (cf. rtquote.resolve_symbols).

        Le cache de contrat front est PURGÉ pour nos futures avant résolution :
        sans ça, une session recyclée pendant des semaines resterait collée à
        l'ancien contrat après un roll. Purge partagée (le flux live la
        rebâtira au besoin) — sans danger."""
        from . import rtquote
        for label in self.symbols:
            rtquote._FUTURE_STREAM_CACHE.pop(label, None)
        syms = resolve_symbols(access)
        out: dict[str, str] = {}
        for label in self.symbols:
            s = syms.get(label)
            if s:
                out[s] = label
            else:
                log.warning("Capture tick : %s non résolu — exclu", label)
        return out

    def _run(self) -> None:
        backoff = BACKOFF_START
        while True:
            try:
                asyncio.run(self._session())
                backoff = BACKOFF_START
            except Exception as exc:  # noqa: BLE001 — la capture doit survivre à tout
                self._state = "disconnected"
                log.warning("Capture tick interrompue (%s) — reprise dans %.0f s",
                            exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _session(self) -> None:
        """Une session : résout l'univers, s'abonne à TimeAndSale, écoute
        jusqu'au recyclage périodique (reconnexion pour reconstruire l'univers).

        Modelée sur `flowtape._session`, mais un SEUL type d'événement
        (TimeAndSale, pas de Greeks) et pas de recentrage (le sous-jacent est le
        future lui-même, pas une fenêtre de strikes)."""
        import websockets

        token, url, access = quote_token()
        universe = self._build_universe(access)
        if not universe:
            self._state = "degraded"
            raise RuntimeError("aucun future à suivre")

        async with websockets.connect(url, max_size=2 ** 24,
                                      ping_interval=None) as ws:
            async def send(m):
                await ws.send(json.dumps(m))

            await send({"type": "SETUP", "channel": 0, "version": "0.1-ticks",
                        "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})
            auth_sent = False
            subscribed = False
            deadline = time.monotonic() + UNIVERSE_REFRESH_S

            async for raw in ws:
                m = json.loads(raw)
                typ = m.get("type")
                if typ == "AUTH_STATE":
                    state = m.get("state")
                    if state == "UNAUTHORIZED" and not auth_sent:
                        auth_sent = True
                        await send({"type": "AUTH", "channel": 0, "token": token})
                    elif state == "UNAUTHORIZED":
                        raise RuntimeError("jeton dxFeed refusé")
                    elif state == "AUTHORIZED":
                        await send({"type": "CHANNEL_REQUEST", "channel": 1,
                                    "service": "FEED",
                                    "parameters": {"contract": "AUTO"}})
                elif typ == "CHANNEL_OPENED":
                    # aggregationPeriod 0 : CHAQUE transaction, pas un échantillon
                    await send({"type": "FEED_SETUP", "channel": 1,
                                "acceptAggregationPeriod": 0.0,
                                "acceptDataFormat": "FULL"})
                elif typ == "FEED_CONFIG" and not subscribed:
                    subscribed = True
                    await send({"type": "FEED_SUBSCRIPTION", "channel": 1,
                                "add": [{"type": "TimeAndSale", "symbol": s}
                                        for s in universe]})
                    self._state = "connected"
                    log.info("Capture tick continue active — %s",
                             ", ".join(f"{v}={k}" for k, v in universe.items()))
                elif typ == "KEEPALIVE":
                    await send({"type": "KEEPALIVE", "channel": 0})
                elif typ == "ERROR":
                    log.warning("dxFeed ERROR (capture tick) : %s", str(m)[:200])
                elif typ == "FEED_DATA":
                    now = time.time()
                    for item in m.get("data") or []:
                        if (isinstance(item, dict)
                                and item.get("eventType") == "TimeAndSale"):
                            self.record(universe, item, now)

                # Recyclage périodique : reconnexion + univers reconstruit (roll).
                if time.monotonic() > deadline:
                    log.info("Capture tick : renouvellement périodique de la session")
                    return


def _vol(v) -> int:
    """Taille du print en entier (contrats), ou 0 si absente/invalide — la
    colonne `volume` reste int64 (comme le jeu de référence), sans NaN. Un
    future porte toujours une taille ; le 0 ne sert que de garde-fou."""
    return int(v) if isinstance(v, (int, float)) and v == v and v >= 0 else 0


# Singleton partagé : démarré au boot (run.py / tt_web.py), vidé par le scheduler.
CAPTURE = TickCapture()
