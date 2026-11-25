"""VXN recalculé EN CONTINU (bougies 1 min), dans le process capture.

Le calcul est celui de gex/vxn.py (méthode CBOE). Ce qui change, c'est la
collecte : au lieu de la chaîne NDX complète toutes les 15 min (~18 000
contrats, 3-4 min de collecte), un abonnement PERMANENT aux cotations des
seules échéances qui encadrent 30 jours (~500 contrats pour la paire, mesuré le
25/11/2026), recalculé toutes les `COMPUTE_S` secondes.

Fenêtre de l'abonnement : les échéances entre `WINDOW_MIN_DAYS` et
`WINDOW_MAX_DAYS`, toutes strikes, les deux racines (NDX, NDXP) : la paire
retenue par `vxn.compute` en fait toujours partie, même quand elle glisse d'un
jour dans la séance. L'univers est reconstruit toutes les `UNIVERSE_REFRESH_S`.

Séance : les options NDX ne cotent que de 9h30 à 16h15 New York ; hors de cette
plage, la connexion est fermée et rien n'est calculé ni écrit.

Sorties :
- bougies 1 min dans data/prices/VXN_CALC/<jour>.parquet (même clé que le point
  de 15 min, qui ne tourne plus que si ce flux est muet) ;
- bougie en cours publiée dans data/live/vxn_bar.json pour le dashboard
  (canal `bar:VXN` de Squirrel X, cf. gex/cashbars.py).

Ce n'est PAS l'indice officiel CBOE : un VXN recalculé, à présenter comme tel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime

import pandas as pd

from . import vxn
from .metrics import ET
from .rtquote import (
    BACKOFF_MAX,
    BACKOFF_START,
    Bar,
    credentials_present,
    decode_compact_feed_data,
    feed_setup_message,
    quote_token,
)

log = logging.getLogger(__name__)

PRICES_KEY = vxn.PRICES_KEY
LIVE_NAME = "vxn_bar"
WINDOW_MIN_DAYS, WINDOW_MAX_DAYS = 20, 40
COMPUTE_S = 5.0
AGGREGATION_S = 1.0          # cotations regroupées par dxFeed : une par seconde au plus
UNIVERSE_REFRESH_S = 3600
OFF_SESSION_SLEEP_S = 30.0
FRESH_S = 120.0              # au-delà, le flux est considéré muet (repli 15 min)


def in_session(now_et: datetime) -> bool:
    from .scheduler import MARKET_CLOSE, MARKET_OPEN
    return now_et.weekday() < 5 and MARKET_OPEN <= now_et.time() <= MARKET_CLOSE


def select_universe(chain: pd.DataFrame, now_et: datetime) -> pd.DataFrame:
    """Contrats des échéances entre WINDOW_MIN_DAYS et WINDOW_MAX_DAYS."""
    if chain.empty:
        return chain
    days = pd.Series([vxn.minutes_to_settlement(e, bool(am), now_et) / vxn.MIN_PER_DAY
                      for e, am in zip(chain["expiry"], chain["settle_am"])], index=chain.index)
    return chain[(days > WINDOW_MIN_DAYS) & (days < WINDOW_MAX_DAYS)].reset_index(drop=True)


class VxnLive:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._state = "off"
        self._started = False
        self._universe = pd.DataFrame()
        self._quotes: dict[str, tuple[float, float]] = {}
        self._bar: Bar | None = None
        self._closed: Bar | None = None
        self._last: dict | None = None
        self._last_ok = 0.0

    # ------------------------------------------------------------ calcul
    def ingest_quote(self, item: dict) -> None:
        sym = item.get("eventSymbol")
        bid, ask = item.get("bidPrice"), item.get("askPrice")
        if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)):
            return
        if bid != bid or ask != ask:             # NaN : contrat sans cotation
            bid, ask = 0.0, 0.0
        with self.lock:
            self._quotes[sym] = (float(bid), float(ask))

    def chain(self) -> pd.DataFrame:
        with self.lock:
            uni, quotes = self._universe, dict(self._quotes)
        if uni.empty:
            return uni
        q = uni["streamer_symbol"].map(quotes)
        out = uni.copy()
        out["bid"] = [v[0] if isinstance(v, tuple) else float("nan") for v in q]
        out["ask"] = [v[1] if isinstance(v, tuple) else float("nan") for v in q]
        return out

    def tick(self, now_et: datetime, rate: float, write=None) -> dict | None:
        """Un recalcul : met à jour la bougie en cours ; à la minute suivante,
        écrit la bougie close. Renvoie le résultat de `vxn.compute` (ou None)."""
        res = vxn.compute(self.chain(), now_et, rate)
        if res is None:
            return None
        v = float(res["vxn"])
        minute = int(now_et.timestamp() // 60) * 60
        done = None
        with self.lock:
            if self._bar is not None and self._bar.minute < minute:
                done = self._closed = self._bar
                self._bar = None
            if self._bar is None:
                self._bar = Bar(minute, v, v, v, v)
            else:
                self._bar.update(v)
            self._last, self._last_ok = res, time.time()
        if done is not None:
            (write or self._write)(done, res)
        return res

    def _write(self, bar: Bar, res: dict) -> None:
        from . import store
        ts = datetime.fromtimestamp(bar.minute, ET).replace(tzinfo=None)
        store.append_prices(PRICES_KEY, [{
            "timestamp": ts, "open": bar.open, "high": bar.high, "low": bar.low,
            "close": bar.close, "near_expiry": str(res["near_expiry"]),
            "next_expiry": str(res["next_expiry"]), "near_strikes": res["near_strikes"],
            "next_strikes": res["next_strikes"], "n_contracts": len(self._universe),
            "source": "live_dxfeed"}], ts)

    def flush(self) -> None:
        """Fin de séance : la bougie en cours est écrite telle quelle."""
        with self.lock:
            bar, res = self._bar, self._last
            self._bar = None
            if bar is not None:
                self._closed = bar
        if bar is not None and res is not None:
            self._write(bar, res)

    def publish(self) -> None:
        from . import livestate
        with self.lock:
            cur, done = self._bar, self._closed
        as_dict = lambda b: None if b is None else {  # noqa: E731
            "minute": b.minute, "open": b.open, "high": b.high, "low": b.low, "close": b.close}
        livestate.publish_json(LIVE_NAME, {"cur": as_dict(cur), "done": as_dict(done)})

    def fresh(self, max_age: float = FRESH_S) -> bool:
        """Un VXN a-t-il été calculé il y a moins de `max_age` secondes ?"""
        return time.time() - self._last_ok < max_age

    def status(self) -> tuple[str, int]:
        return self._state, len(self._universe)

    # ------------------------------------------------------------ flux
    def start(self) -> None:
        if self._started:
            return
        if not credentials_present():
            log.info("VXN continu désactivé (identifiants absents)")
            return
        self._started = True
        threading.Thread(target=self._run, name="vxnlive", daemon=True).start()

    def _run(self) -> None:
        backoff = BACKOFF_START
        while True:
            if not in_session(datetime.now(ET)):
                self._state = "hors séance"
                time.sleep(OFF_SESSION_SLEEP_S)
                continue
            try:
                asyncio.run(self._session())
                backoff = BACKOFF_START
            except Exception as exc:  # noqa: BLE001 — le flux doit survivre à tout
                self._state = "disconnected"
                log.warning("VXN continu interrompu (%s) — reprise dans %.0f s", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    def _build_universe(self) -> pd.DataFrame:
        from . import idxopt
        _, _, access = quote_token()
        uni = select_universe(idxopt.fetch_chain_instruments("NDX", access), datetime.now(ET))
        with self.lock:
            self._universe = uni
            keep = set(uni["streamer_symbol"]) if not uni.empty else set()
            self._quotes = {k: v for k, v in self._quotes.items() if k in keep}
        return uni

    async def _session(self) -> None:
        """Univers, souscription unique (cf. futopt._collect_one : un ajout
        tardif n'est pas fiable sur dxLink), écoute et recalcul périodique ;
        se termine pour reconstruire l'univers, ou en fin de séance."""
        import websockets

        from . import rates
        token, url, _ = quote_token()
        uni = self._build_universe()
        if uni.empty:
            raise RuntimeError("aucune échéance NDX entre "
                               f"{WINDOW_MIN_DAYS} et {WINDOW_MAX_DAYS} jours")
        symbols = uni["streamer_symbol"].tolist()
        async with websockets.connect(url, max_size=2 ** 24,
                                      ping_interval=20, ping_timeout=20) as ws:
            async def send(m):
                await ws.send(json.dumps(m))

            await send({"type": "SETUP", "channel": 0, "version": "0.1-vxn",
                        "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})
            auth_sent = subscribed = False
            deadline = time.monotonic() + UNIVERSE_REFRESH_S
            next_compute = time.monotonic() + COMPUTE_S
            rate = rates.current_rate()
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    raw = None
                if raw is not None:
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
                                        "service": "FEED", "parameters": {"contract": "AUTO"}})
                    elif typ == "CHANNEL_OPENED":
                        await send(feed_setup_message(1, AGGREGATION_S))
                    elif typ == "FEED_CONFIG" and not subscribed:
                        # FEED_CONFIG revient à chaque évolution de la config :
                        # ne souscrire qu'une fois (cf. rtquote._session)
                        subscribed = True
                        await send({"type": "FEED_SUBSCRIPTION", "channel": 1,
                                    "add": [{"type": "Quote", "symbol": s} for s in symbols]})
                        self._state = "connected"
                        log.info("VXN continu actif — %d contrats NDX (%d échéances)",
                                 len(symbols), uni["expiry"].nunique())
                    elif typ == "KEEPALIVE":
                        await send({"type": "KEEPALIVE", "channel": 0})
                    elif typ == "ERROR":
                        log.warning("dxFeed ERROR (VXN continu) : %s", str(m)[:200])
                    elif typ == "FEED_DATA":
                        for item in decode_compact_feed_data(m.get("data") or []):
                            if isinstance(item, dict) and item.get("eventType") == "Quote":
                                self.ingest_quote(item)
                now_mono = time.monotonic()
                if subscribed and now_mono >= next_compute:
                    next_compute = now_mono + COMPUTE_S
                    now_et = datetime.now(ET)
                    if not in_session(now_et):
                        self.flush()
                        self.publish()
                        log.info("VXN continu : fin de séance")
                        return
                    try:
                        self.tick(now_et, rate)
                        self.publish()
                    except Exception:  # noqa: BLE001 — un cycle raté n'arrête pas le flux
                        log.exception("VXN continu : recalcul en échec")
                if now_mono > deadline:
                    log.info("VXN continu : renouvellement périodique de l'univers")
                    return


LIVE = VxnLive()


class LiveBars:
    """Côté dashboard : la bougie VXN publiée par la capture, présentée comme
    `rtquote.QUOTES.bar_state` pour réutiliser `cashbars.bar_producer`."""

    def __init__(self, max_age: float = FRESH_S) -> None:
        self._max_age = max_age
        self._cache: tuple[int, tuple] | None = None

    def bar_state(self, key: str) -> tuple[Bar | None, Bar | None]:
        from . import livestate
        path = livestate.live_dir() / f"{LIVE_NAME}.json"
        try:
            mtime = path.stat().st_mtime_ns
            if self._cache is None or self._cache[0] != mtime:
                doc = json.loads(path.read_text(encoding="utf-8"))
                data = doc.get("data") or {}
                to_bar = lambda d: None if not d else Bar(  # noqa: E731
                    int(d["minute"]), d["open"], d["high"], d["low"], d["close"])
                self._cache = (mtime, (doc.get("published", 0.0),
                                       to_bar(data.get("cur")), to_bar(data.get("done"))))
        except (OSError, ValueError, KeyError, TypeError):
            return None, None
        published, cur, done = self._cache[1]
        if time.time() - published > self._max_age:
            return None, done
        return cur, done


LIVE_BARS = LiveBars()
