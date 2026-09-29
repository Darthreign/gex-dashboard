"""Capture TICK-PAR-TICK CONTINUE des futures NQ et ES — la totale, en
permanence sur toute la session (dimanche 18h ET → vendredi 17h ET).

Pourquoi une session dxLink DÉDIÉE, et non un tap sur le flux du dashboard :
le flux temps réel (`rtquote.QUOTES`) s'abonne à `Quote`/`Trade`, deux
événements CONFLATÉS — dxFeed n'y livre qu'un échantillon (~1 print toutes
les quelques secondes), suffisant pour un spot d'affichage mais pas pour
rejouer une séquence à la seconde. `TimeAndSale`, lui, livre CHAQUE
transaction, avec sa taille. On ouvre donc notre propre connexion, on
s'abonne à `TimeAndSale` sur NQ et ES, et on écoute en continu — sans jamais
toucher `QUOTES`, pour que le dashboard reste en direct quoi qu'il arrive ici.

Pour chaque future, on suit le contrat ACTIF **et le SUIVANT** : la série
continue bascule AU VOLUME (comme le `NQ.v.0` de Databento), ce qui suppose de
pouvoir comparer les deux. Seul le contrat dominant est écrit sur disque ; le
choix de la séance est figé d'après le volume de la veille (cf. gex/roll).

Ce qu'on garde : TOUT le brut du print, sans rien jeter — `ts` (epoch s, heure
d'échange), `price`, `volume`, `bid`, `ask`, `side` (côté agresseur),
`ts_recv` (heure de réception locale, pour mesurer la latence), `source`, et
depuis le 2026-09-28 les TAILLES affichées au meilleur bid / ask
(`bid_size`, `ask_size`, plus l'état précédent `prev_*`), tirées de l'événement
`Quote` : un print plus gros que la taille affichée est un indice de quantité
cachée (iceberg). Absentes des fichiers antérieurs (NaN).
Le socle `ts/price/volume/source` est aligné sur le jeu de référence
`ticks_full` (Databento), donc la capture reste DIRECTEMENT exploitable par le
backtest ; `bid/ask/side` sont un SURENSEMBLE (colonnes en plus, ignorées par
qui n'en veut pas). On les garde parce qu'un moteur de test qui évolue pourrait
en avoir besoin un jour (ex. classer les prints par l'agresseur) : c'est la
seule donnée non reconstituable — ni CBOE ni le feed courtier ne rejouent un
historique tick-par-tick (le courtier n'expose l'historique qu'en bougies
`Candle`). Un tick non capté est perdu pour toujours : d'où « brut conservé,
jamais recalculé ».

Débit : la session tourne ~23h/j, 5j/7, à quelques centaines de prints/s en
séance. On agrège en mémoire et le scheduler vide toutes les 60 s vers le
parquet JOURNALIER de la séance (cf. scheduler.flush_ticks) : le buffer ne
porte jamais plus d'une minute de flux.

La session se RECYCLE périodiquement (reconnexion) pour reconstruire l'univers :
les contrats roulent chaque trimestre, et une reconnexion propre vaut mieux
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
from collections import deque

from .rtquote import (
    BACKOFF_MAX,
    BACKOFF_START,
    credentials_present,
    quote_token,
)

log = logging.getLogger(__name__)

# Fenêtre glissante gardée en mémoire pour la détection d'absorption en direct
# (cf. absorption_now / gex/iceberg.py) — distincte de `_buf`, qui lui est drainé
# et vidé toutes les 60 s vers le disque. Recalculer une salve sur 3 min de ticks
# coûte cher en pur Python (jusqu'à ~10k lignes sur ES) : on ne le refait donc
# qu'au plus toutes les ABSORPTION_RECOMPUTE_S, et on ne considère « fraîche »
# (digne d'allumer une pastille) qu'une salve achevée depuis moins de
# ABSORPTION_FRESH_S.
ICEBERG_WINDOW_S = 180.0
ABSORPTION_RECOMPUTE_S = 2.0
ABSORPTION_FRESH_S = 20.0

# Les deux futures suivis : le libellé sert de dossier de stockage
# (data/ticks/NQ) et de code produit pour résoudre les contrats (cf. gex/roll).
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
        self._buf: dict[str, dict[str, list[dict]]] = {}
        # ordre OFFICIEL des contrats par sous-jacent : [actif, suivant], tel que
        # le courtier les déclare. Sert de repli au choix de roll quand aucun
        # volume de la veille n'est connu (cf. roll.dominant) — un ordre déduit
        # du volume de la minute courante ne serait pas un repli fiable.
        self._order: dict[str, list[str]] = {}
        self._lock = threading.Lock()
        self._started = False
        self._state = "off"
        # Dernier état de cotation par contrat (événement Quote) : sert à ajouter
        # les TAILLES du meilleur bid / ask à chaque transaction (cf. quote/record).
        self._quotes: dict[str, dict] = {}
        # Dernier prix RÉELLEMENT échangé par sous-jacent (NQ/ES), à jour à chaque
        # print — c'est la seule source tick-accurate (TimeAndSale sans agrégation,
        # contrairement à rtquote.QUOTES qui est CONFLATÉ côté dxFeed). Exposé au
        # dashboard via capturebus pour le ticker de prix de la page /scalp.
        self._last: dict[str, float] = {}
        # Fenêtre glissante par sous-jacent (ICEBERG_WINDOW_S), pour la détection
        # d'absorption en direct — mêmes colonnes que les lignes écrites sur
        # disque (cf. record), directement consommables par gex.iceberg.
        self._recent: dict[str, deque] = {}
        self._absorb_cache: dict[str, tuple[float, list]] = {}

    def last_price(self, symbol: str) -> float | None:
        """Dernier prix échangé pour `symbol` ("NQ" ou "ES"), ou None."""
        with self._lock:
            return self._last.get(symbol)

    def recent_rows(self, symbol: str) -> list[dict]:
        """Copie des lignes des `ICEBERG_WINDOW_S` dernières secondes pour
        `symbol` — public : c'est le point testable de la détection d'absorption
        en direct, sans dépendre du minuteur de cache."""
        with self._lock:
            return list(self._recent.get(symbol, ()))

    def _absorption_flags(self, symbol: str, now: float) -> list:
        """Salves flaguées sur la fenêtre (cf. gex.iceberg), la plus ANCIENNE
        d'abord. Mise en cache `ABSORPTION_RECOMPUTE_S` — recalculer sur la
        fenêtre à chaque appel serait trop coûteux, cette méthode étant lue à
        chaque envoi capturebus, ~4x/s. Base commune à `absorption_now`
        (la plus fraîche, pour le clignotement) et `absorption_recent` (les
        derniers niveaux, affichés en permanence)."""
        with self._lock:
            cached = self._absorb_cache.get(symbol)
        if cached and now - cached[0] < ABSORPTION_RECOMPUTE_S:
            return cached[1]
        rows = self.recent_rows(symbol)
        flags = []
        if rows:
            import pandas as pd

            from . import iceberg as ib
            sw = ib.build_sweeps(pd.DataFrame(rows))
            flags = sorted(ib.flag_absorption(sw, symbol), key=lambda f: f.end_ts)
        with self._lock:
            self._absorb_cache[symbol] = (now, flags)
        return flags

    @staticmethod
    def _sweep_dict(f) -> dict:
        return {"side": f.side, "price": f.price,
               "ratio": round(f.ratio, 1) if f.ratio else None,
               "total": f.total_size, "n_prints": f.n_prints, "ts": f.end_ts}

    def absorption_now(self, symbol: str, now: float | None = None) -> dict | None:
        """Salve d'absorption la plus marquée, ACHEVÉE depuis moins de
        `ABSORPTION_FRESH_S` — ou None. C'est elle qui déclenche le clignotement."""
        now = time.time() if now is None else now
        fraiches = [f for f in self._absorption_flags(symbol, now)
                   if now - f.end_ts <= ABSORPTION_FRESH_S]
        if not fraiches:
            return None
        return self._sweep_dict(max(fraiches, key=lambda s: s.ratio or 0.0))

    def absorption_recent(self, symbol: str, limit: int = 3,
                          now: float | None = None) -> list[dict]:
        """Les `limit` dernières salves détectées sur la fenêtre (`ICEBERG_WINDOW_S`),
        la plus récente en premier — affichées en PERMANENCE (pas seulement tant
        que fraîches), pour garder une trace des derniers niveaux vus."""
        now = time.time() if now is None else now
        flags = self._absorption_flags(symbol, now)
        return [self._sweep_dict(f) for f in flags[-limit:][::-1]]

    def quote(self, item: dict) -> None:
        """Retient l'état de la cotation d'un contrat (bid/ask, tailles).

        On garde AUSSI l'état précédent des tailles : une transaction et la mise
        à jour de cotation qu'elle provoque arrivent dans un ordre non garanti.
        Si la cotation « après » précède le print, la taille affichée AVANT la
        transaction est dans `prev_*` ; sinon elle est dans les tailles courantes.
        C'est cette taille visible qui permet de juger un print trop gros pour ce
        qui était affiché (indice de quantité cachée, cf. détection d'iceberg)."""
        sym = item.get("eventSymbol")
        if not sym:
            return
        cur = {"bid": _num(item.get("bidPrice")), "bid_size": _num(item.get("bidSize")),
               "ask": _num(item.get("askPrice")), "ask_size": _num(item.get("askSize"))}
        with self._lock:
            old = self._quotes.get(sym)
            if old is None:
                cur.update(prev_bid_size=None, prev_ask_size=None)
            elif (old["bid"], old["bid_size"], old["ask"], old["ask_size"]) == (
                    cur["bid"], cur["bid_size"], cur["ask"], cur["ask_size"]):
                return                                    # doublon : rien ne change
            else:
                cur.update(prev_bid_size=old["bid_size"], prev_ask_size=old["ask_size"])
            self._quotes[sym] = cur

    def contract_order(self, symbol: str) -> list[str]:
        """[contrat actif, contrat suivant] pour `symbol`, ordre du courtier."""
        with self._lock:
            return list(self._order.get(symbol, []))

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

    def drain(self) -> dict[str, dict[str, list[dict]]]:
        """Récupère et vide le buffer, sous la forme {sous-jacent: {contrat:
        lignes}} (appelé par le scheduler pour écrire sur disque). Un swap sous
        verrou : la capture continue d'alimenter un buffer neuf pendant
        l'écriture."""
        with self._lock:
            out, self._buf = self._buf, {}
        return out

    # -- capture (chemin réseau) ------------------------------------------

    def record(self, universe: dict[str, tuple[str, str]], item: dict,
               now: float) -> None:
        """Range un print TimeAndSale dans le buffer, sous (sous-jacent,
        contrat). Public : c'est le point testable du module (mapping, filtrage,
        forme de ligne), sans réseau.

        `now` (réception locale) ne sert que de repli : on préfère l'heure
        d'ÉCHANGE (`time`, en ms) quand elle est présente — c'est elle qui fait
        foi pour rejouer une séquence."""
        entry = universe.get(item.get("eventSymbol"))
        if entry is None:
            return
        symbol, contract = entry
        price = item.get("price")
        if not isinstance(price, (int, float)) or price != price:
            return  # pas de prix exploitable (NaN inclus) -> ignoré
        exch = item.get("time")
        ts = exch / 1000.0 if isinstance(exch, (int, float)) and exch == exch else now
        row = {
            "ts": float(ts),
            "price": float(price),
            "volume": _vol(item.get("size")),
            "bid": _num(item.get("bidPrice")),
            "ask": _num(item.get("askPrice")),
            "side": item.get("aggressorSide") or None,
            # Heure de RECEPTION locale, a cote de l'heure d'echange. L'ecart
            # entre les deux EST la latence : en backtest le moteur agit a `ts`,
            # en live il ne voit le tick qu'a `ts_recv`. Sans cette colonne le
            # backtest suppose implicitement une latence nulle — biais
            # systematiquement optimiste, et non mesurable apres coup.
            "ts_recv": float(now),
            "source": "dxfeed",
        }
        with self._lock:
            q = self._quotes.get(item.get("eventSymbol")) or {}
            # tailles affichées au meilleur bid / ask : état courant de la cotation
            # et état précédent (cf. quote()). None tant qu'aucune cotation n'est
            # arrivée — jamais 0, qui voudrait dire « rien d'affiché ».
            row["bid_size"] = q.get("bid_size")
            row["ask_size"] = q.get("ask_size")
            row["prev_bid_size"] = q.get("prev_bid_size")
            row["prev_ask_size"] = q.get("prev_ask_size")
            self._buf.setdefault(symbol, {}).setdefault(contract, []).append(row)
            self._last[symbol] = float(price)
            # fenêtre glissante pour la détection d'absorption en direct (indépendante
            # du contrat : le dominant peut changer au roll, la fenêtre reste continue)
            recent = self._recent.setdefault(symbol, deque())
            recent.append(row)
            cutoff = row["ts"] - ICEBERG_WINDOW_S
            while recent and recent[0]["ts"] < cutoff:
                recent.popleft()

    def _build_universe(self, access: str) -> dict[str, tuple[str, str]]:
        """streamer -> (libellé NQ/ES, code contrat), pour le contrat ACTIF ET
        le SUIVANT de chaque future suivi.

        Les deux sont nécessaires : la série continue bascule au VOLUME (cf.
        gex/roll), ce qui suppose de pouvoir comparer les deux contrats. Seul le
        dominant est écrit sur disque, mais les volumes des deux sont mesurés.

        Un future non résolu est OMIS, jamais rabattu sur le ticker action
        homonyme (« NQ »/« ES » sont aussi des actions — cf.
        rtquote.resolve_symbols)."""
        from . import roll
        out: dict[str, tuple[str, str]] = {}
        for label in self.symbols:
            try:
                pair = roll.resolve_pair(label, access)
            except Exception:  # noqa: BLE001 — un référentiel muet n'en condamne pas deux
                log.exception("Capture tick : contrats %s non résolus — exclu", label)
                continue
            if not pair:
                log.warning("Capture tick : aucun contrat %s — exclu", label)
                continue
            for streamer, code in pair:
                out[streamer] = (label, code)
            with self._lock:
                self._order[label] = [code for _, code in pair]
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

        # ping_interval/ping_timeout : SANS eux, une coupure réseau sans trame
        # de fermeture propre (constatée le 2026-09-29 14h20 — rtquote.QUOTES,
        # sur une connexion différente avec ping actif, a détecté la coupure et
        # s'est reconnectée en quelques secondes ; cette connexion-ci, muette
        # sur ce point, est restée « zombie » 14 minutes sans lever d'exception,
        # donc sans jamais déclencher la boucle de reprise de _run) : le websocket
        # ne remarque jamais qu'il ne reçoit plus rien, la boucle de reprise
        # (cf. _run) n'est donc jamais déclenchée. Mêmes valeurs que les autres
        # connexions dxLink de ce projet (rtquote, flowtape, capturebus).
        async with websockets.connect(url, max_size=2 ** 24,
                                      ping_interval=20, ping_timeout=20) as ws:
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
                    # TimeAndSale = chaque transaction ; Quote = tailles du meilleur
                    # bid/ask, rattachées à chaque transaction (cf. quote/record)
                    await send({"type": "FEED_SUBSCRIPTION", "channel": 1,
                                "add": [{"type": t, "symbol": s}
                                        for s in universe for t in ("TimeAndSale", "Quote")]})
                    self._state = "connected"
                    log.info("Capture tick continue active — %s",
                             ", ".join(f"{lbl}:{code}={s}"
                                       for s, (lbl, code) in universe.items()))
                elif typ == "KEEPALIVE":
                    await send({"type": "KEEPALIVE", "channel": 0})
                elif typ == "ERROR":
                    log.warning("dxFeed ERROR (capture tick) : %s", str(m)[:200])
                elif typ == "FEED_DATA":
                    now = time.time()
                    for item in m.get("data") or []:
                        if not isinstance(item, dict):
                            continue
                        et = item.get("eventType")
                        if et == "TimeAndSale":
                            self.record(universe, item, now)
                        elif et == "Quote":
                            self.quote(item)

                # Recyclage périodique : reconnexion + univers reconstruit (roll).
                if time.monotonic() > deadline:
                    log.info("Capture tick : renouvellement périodique de la session")
                    return


def _vol(v) -> int:
    """Taille du print en entier (contrats), ou 0 si absente/invalide — la
    colonne `volume` reste int64 (comme le jeu de référence), sans NaN. Un
    future porte toujours une taille ; le 0 ne sert que de garde-fou."""
    return int(v) if isinstance(v, (int, float)) and v == v and v >= 0 else 0


def _num(v) -> float | None:
    """float propre, ou None (NaN et non-numérique compris) — pour bid/ask, où
    l'absence doit rester une absence, jamais un NaN déguisé en cotation."""
    return float(v) if isinstance(v, (int, float)) and v == v else None


# Singleton partagé : démarré au boot (run.py / tt_web.py), vidé par le scheduler.
CAPTURE = TickCapture()
