"""Chaînes OPRA « en continu » : réévaluées au spot temps réel entre deux salves.

Une chaîne dxFeed (OPRA pour SPX/NDX/SPY/QQQ, CME pour NQ/ES) arrive par
salves de quelques minutes (cf. scheduler.pull_native_index /
pull_native_options_fast). Entre deux salves, le prix bouge : le gamma de
chaque strike aussi, donc le profil GEX, le Gamma Flip, le GEX net. Ce module
fournit, à la lecture, une vue de la dernière salve réévaluée au spot live
(`futopt.reprice_native`) et complétée du volume compté sur les prints OPRA.

- Recalculée au plus toutes les `MIN_INTERVAL_S`, et seulement si le spot ou
  le volume a changé : un calcul partagé par tous les lecteurs (onglets,
  callbacks, flux), quel que soit leur nombre.
- L'open interest et l'IV restent ceux de la salve (l'IV en continu
  demanderait un abonnement permanent à toute la chaîne).
- Les chaînes CBOE (délayées de 15 min) ne passent jamais par ici : elles
  restent à leur rythme d'une minute.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from . import metrics
from .metrics import ET

log = logging.getLogger(__name__)

MIN_INTERVAL_S = 2.0
# un spot live plus vieux que ça (flux coupé) ne sert plus à réévaluer
SPOT_MAX_AGE_S = 120.0
# un écart de plus de 5 % entre spot live et spot de salve : donnée suspecte
# (mauvais contrat, cotation aberrante) — on garde la salve telle quelle
MAX_SPOT_JUMP = 0.05
REQUIRED = ("streamer_symbol", "carry_q", "multiplier", "iv", "t_years")


@dataclass
class LiveView:
    """Même interface de lecture que scheduler.UnderlyingState."""
    snapshot: object
    enriched: object
    summary: object
    last_feed_ts: datetime | None
    base_ts: datetime | None = None      # horodatage de la salve sous-jacente


_lock = threading.Lock()
_cache: dict[str, tuple] = {}            # clé -> (base_id, spot, vol_sig, t, vue)
_computing: dict[str, threading.Lock] = {}


def _live_spot(symbol: str) -> float | None:
    from .rtquote import QUOTES
    with QUOTES.lock:
        tick = QUOTES.ticks.get(symbol)
        if tick is None or time.time() - (tick.ts or 0) > SPOT_MAX_AGE_S:
            return None
        return tick.price


def _volumes() -> dict[str, float]:
    from .flowtape import TAPE
    fn = getattr(TAPE, "contract_volumes", None)
    try:
        return fn() if fn else {}
    except Exception:  # noqa: BLE001 — le volume live est un complément, jamais bloquant
        return {}


def eligible(state) -> bool:
    s, df = state.summary, state.enriched
    return (s is not None and df is not None and getattr(s, "source", None) == "dxfeed"
            and all(c in df.columns for c in REQUIRED))


def view(key: str, symbol: str, state, lock):
    """Vue live de `state` (sinon `state` lui-même si rien à réévaluer)."""
    with lock:
        snap, df, summary, ts = (state.snapshot, state.enriched, state.summary,
                                 state.last_feed_ts)
    if snap is None or df is None or summary is None:
        return state
    if not eligible(state):
        return state
    base_id = id(df)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        busy = _computing.setdefault(key, threading.Lock())
    if hit and hit[0] == base_id and now - hit[3] < MIN_INTERVAL_S:
        return hit[4]                    # cas courant : rien à faire
    spot = _live_spot(symbol)
    if not spot or not snap.spot or abs(spot / snap.spot - 1) > MAX_SPOT_JUMP:
        return state
    vols = _volumes()
    streams = df["streamer_symbol"]
    vol_sig = sum(vols.get(s, 0.0) for s in streams) if vols else 0.0
    if hit and hit[0] == base_id and hit[1] == spot and hit[2] == vol_sig:
        with _lock:                      # inchangé : on repousse l'échéance
            _cache[key] = (*hit[:3], now, hit[4])
        return hit[4]
    # un seul calcul à la fois par chaîne : les autres lecteurs prennent la
    # vue précédente (ou la salve) plutôt que d'attendre
    if not busy.acquire(blocking=False):
        return hit[4] if hit and hit[0] == base_id else state
    try:
        v = _compute(key, snap, df, summary, ts, spot, vols)
        with _lock:
            _cache[key] = (base_id, spot, vol_sig, now, v)
        return v
    except Exception:  # noqa: BLE001 — en cas d'échec, la salve reste affichée
        log.exception("Réévaluation live de %s en échec", key)
        return state
    finally:
        busy.release()


def _compute(key, snap, df, summary, ts, spot, vols) -> LiveView:
    from dataclasses import replace

    from .futopt import reprice_native
    from .scheduler import build_native_summary
    now = datetime.now(ET)
    live = reprice_native(df, spot, now, vols)
    # même chaîne à un autre spot : le Gamma Flip (IV figée) est celui de la salve
    metrics.register_live_alias(live, df, float(snap.spot))
    new_snap, new_summary = build_native_summary(summary.symbol, live, now)
    # le bandeau/tuiles affichent l'heure de la donnée : celle du spot live
    new_snap = replace(new_snap, symbol=snap.symbol)
    return LiveView(new_snap, live, new_summary, ts, base_ts=ts)


def reset() -> None:
    with _lock:
        _cache.clear()
