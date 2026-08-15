"""Capture TICK-PAR-TICK des futures NQ et ES sur l'HEURE D'OUVERTURE US
(9h30-10h30 ET = 15h30-16h30 Paris).

Pourquoi une session dxLink DÉDIÉE, et non un tap sur le flux du dashboard :
le flux temps réel (`rtquote.QUOTES`) s'abonne à `Quote`/`Trade`, deux
événements CONFLATÉS — dxFeed n'y livre qu'un échantillon (~1 print toutes
les quelques secondes), suffisant pour un spot d'affichage mais pas pour
rejouer une séquence à la seconde. `TimeAndSale`, lui, livre CHAQUE
transaction. On ouvre donc notre propre connexion, on s'abonne à
`TimeAndSale` sur les deux contrats front, et on écoute — sans jamais toucher
`QUOTES`, pour que le dashboard reste en direct quoi qu'il arrive à cette
capture.

Ce qu'on garde : le BRUT intégral de la fenêtre (prix, taille, bid/ask, côté
agresseur, horodatage d'échange). C'est la seule donnée non reconstituable —
ni CBOE ni le feed courtier ne rejouent un historique tick-par-tick (le
courtier n'expose l'historique qu'en bougies `Candle`). Un tick non capté est
perdu pour toujours : d'où « brut conservé, jamais recalculé ». La fenêtre
(~60 min/jour) pèse quelques dizaines de Mo par contrat, écrits une seule fois
à la fermeture — jamais dans le chemin d'ingestion.

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

# Borne DURE de session : même si le job de vidange (flush) ne se déclenchait
# pas (crash du scheduler, horloge de travers), l'écoute ne peut pas déborder
# au-delà de cette durée. La fenêtre visée fait 60 min ; on laisse une marge
# pour couvrir un léger décalage de planification sans jamais tourner sans fin.
MAX_SESSION_S = 75 * 60

# Cadence de re-vérification du drapeau `active` quand le flux est silencieux :
# `recv` est enveloppé dans un wait_for pour que la fermeture soit prise en
# compte en ~1 s même si aucun print n'arrive (rare à l'ouverture, mais on ne
# veut pas dépendre du trafic pour s'arrêter proprement).
RECV_TIMEOUT_S = 1.0


class TickCapture:
    """Session dxLink dédiée qui bufferise chaque `TimeAndSale` de NQ/ES
    pendant la fenêtre d'ouverture. Armée/vidée par le scheduler ; totalement
    inerte hors fenêtre (aucun thread, aucune connexion)."""

    def __init__(self, symbols: tuple[str, ...] = TRACKED_FUTURES) -> None:
        self.symbols = tuple(symbols)
        self.active = False
        self._buf: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = "off"

    # -- cycle de vie (appelé par le scheduler) ---------------------------

    def arm(self) -> None:
        """Ouvre la fenêtre : connecte une session dédiée et bufferise les
        prints. Sans identifiants courtier, ne fait rien — le repli public est
        délayé ~15 min, sans valeur pour du tick-par-tick."""
        if not credentials_present():
            log.info("Capture tick d'ouverture désactivée (identifiants absents)")
            return
        with self._lock:
            if self.active:
                return
            self.active = True
            self._buf = {}
        self._state = "connecting"
        self._thread = threading.Thread(
            target=self._run, name="tickcapture", daemon=True)
        self._thread.start()
        log.info("Capture tick d'ouverture ARMÉE "
                 "(9h30-10h30 ET / 15h30-16h30 Paris)")

    def stop_and_flush(self) -> int:
        """Ferme la fenêtre, attend la fin de la session, écrit le brut sur
        disque. Renvoie le nombre de ticks écrits."""
        with self._lock:
            self.active = False
        t = self._thread
        if t is not None:
            t.join(timeout=15)  # la boucle relit `active` toutes les ~1 s
        self._thread = None
        with self._lock:
            buf, self._buf = self._buf, {}
        if not buf:
            return 0

        from . import store
        from .metrics import ET
        from datetime import datetime

        now = datetime.now(ET)
        total = 0
        for symbol, rows in buf.items():
            if rows:
                try:
                    store.append_ticks(symbol, rows, now)
                    total += len(rows)
                except Exception:  # noqa: BLE001 — une écriture ratée n'en condamne pas six
                    log.exception("Capture tick : échec écriture %s", symbol)
        if total:
            log.info("Capture tick d'ouverture : %d ticks écrits (%s)",
                     total, now.date())
        return total

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
            "size": _num(item.get("size")),
            "bid": _num(item.get("bidPrice")),
            "ask": _num(item.get("askPrice")),
            "side": item.get("aggressorSide") or None,
            "source": "dxfeed",
        }
        with self._lock:
            self._buf.setdefault(symbol, []).append(row)

    def _build_universe(self, access: str) -> dict[str, str]:
        """streamer -> libellé (NQ/ES), pour les seuls futures suivis.

        Réutilise `resolve_symbols`, qui lit le contrat actif via l'API
        authentifiée (`/NQU26:XCME`) — un future NON résolu est OMIS, jamais
        rabattu sur le ticker action homonyme (cf. rtquote.resolve_symbols)."""
        syms = resolve_symbols(access)
        out: dict[str, str] = {}
        for label in self.symbols:
            s = syms.get(label)
            if s:
                out[s] = label
            else:
                log.warning("Capture tick : %s non résolu — exclu de la fenêtre",
                            label)
        return out

    def _run(self) -> None:
        backoff = BACKOFF_START
        while self.active:
            try:
                asyncio.run(self._session())
                backoff = BACKOFF_START
            except Exception as exc:  # noqa: BLE001 — la capture doit survivre à tout
                if not self.active:
                    break
                self._state = "disconnected"
                log.warning("Capture tick interrompue (%s) — reprise dans %.0f s",
                            exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
        self._state = "off"

    async def _session(self) -> None:
        """Une session : résout l'univers, s'abonne à TimeAndSale, écoute
        jusqu'à la fermeture (`active=False`) ou la borne dure de session.

        Modelée sur `flowtape._session`, mais : un SEUL type d'événement
        (TimeAndSale, pas de Greeks), pas de recentrage (le contrat front ne
        roule pas en une heure), et `recv` borné par un timeout pour relire le
        drapeau `active` même quand le flux se tait."""
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
            deadline = time.monotonic() + MAX_SESSION_S

            while self.active and time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
                except asyncio.TimeoutError:
                    continue  # rien reçu : on reboucle pour relire `active`
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
                    log.info("Capture tick active — %s",
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


def _num(v) -> float | None:
    """float propre, ou None (NaN et non-numérique compris) — pour ne jamais
    écrire un NaN déguisé en mesure dans le parquet."""
    return float(v) if isinstance(v, (int, float)) and v == v else None


# Singleton partagé : le scheduler arme (9h30 ET) et vide (10h30 ET).
CAPTURE = TickCapture()
