"""Cadre d'amplitude de l'open US et mouvement attendu du jour — portage de
l'indicateur TradingView « Cadre d'amplitude moyen – Open US » (Pine de
l'utilisateur, 2026-10-18) pour le graphique /scalp.

Le serveur fournit les INGRÉDIENTS, le navigateur construit les lignes
selon les réglages (mode centré / asymétrique, extensions, multiplicateur σ,
ajustement par la vol implicite), comme les entrées du script Pine :

- cadre : amplitude moyenne, montée et baisse moyennes depuis l'open, sur la
  fenêtre mesurée (9h30-10h30 NY par défaut) des `n` séances précédentes,
  open du jour (premier prix de la fenêtre) ;
- vol implicite : VXN (NQ) ou VIX (ES), clôture de la veille, sa moyenne sur
  les `n` séances, et la dernière valeur connue au point de départ — pour le
  VXN, clôtures officielles CBOE et VXN recalculé en séance (gex/vxn.py) ;
- mouvement attendu : point de départ (clôture de la veille 16h00 NY par
  défaut, ouverture Globex 18h00, minuit, open 9h30), son prix, et deux
  amplitudes à 1σ : vol implicite (prix × IV / 100 / √252) et straddle de
  l'échéance proche (même mouvement attendu que le bandeau /scalp).

Heures en New York (naïves dans les fichiers de bougies), sorties en secondes
Unix. Bougies 1 min : store.load_prices.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

import pandas as pd

from . import store
from .metrics import ET

IV_SYMBOL = {"NQ": "VXN", "ES": "VIX"}
ANCHORS = ("close", "globex", "midnight", "open")
RTH_OPEN, RTH_CLOSE = time(9, 30), time(16, 0)


def parse_window(raw: str | None) -> tuple[time, time]:
    """« 0930-1030 » -> (9h30, 10h30) ; défaut si invalide ou vide."""
    try:
        a, b = (raw or "").split("-")
        t0 = time(int(a[:2]), int(a[2:4]))
        t1 = time(int(b[:2]), int(b[2:4]))
        if t0 < t1:
            return t0, t1
    except (ValueError, IndexError):
        pass
    return RTH_OPEN, time(10, 30)


def _bars(symbol: str, day: str) -> pd.DataFrame:
    b = store.load_prices(symbol, day)
    if b.empty:
        return b
    b = b.assign(timestamp=pd.to_datetime(b["timestamp"])).sort_values("timestamp")
    return b.reset_index(drop=True)


def _window(b: pd.DataFrame, day: str, t0: time, t1: time) -> pd.DataFrame:
    if b.empty:
        return b
    d = date.fromisoformat(day)
    lo, hi = datetime.combine(d, t0), datetime.combine(d, t1)
    return b[(b["timestamp"] >= lo) & (b["timestamp"] < hi)]


_CACHE: dict = {}


def _cached(key, fn):
    """Moyennes des séances PRÉCÉDENTES : fixes dans la journée (clé avec la date)."""
    if key not in _CACHE:
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = fn()
    return _CACHE[key]


def window_stats(symbol: str, today: str, t0: time, t1: time, n: int,
                 min_cover: float = 0.8) -> dict:
    """Moyennes de la fenêtre [t0, t1) sur les `n` séances PRÉCÉDENT `today`
    (fenêtre couverte à au moins `min_cover` en minutes, sinon écartée)."""
    expected = (datetime.combine(date.min, t1) - datetime.combine(date.min, t0)).seconds / 60
    ranges, ups, dns = [], [], []
    for day in reversed([d for d in store.price_days(symbol) if d < today]):
        w = _window(_bars(symbol, day), day, t0, t1)
        if len(w) < min_cover * expected:
            continue
        op, hi, lo = float(w["open"].iloc[0]), float(w["high"].max()), float(w["low"].min())
        ranges.append(hi - lo)
        ups.append(hi - op)
        dns.append(op - lo)
        if len(ranges) >= n:
            break
    k = len(ranges)
    return {"count": k, "avg_range": sum(ranges) / k if k else None,
            "avg_up": sum(ups) / k if k else None, "avg_dn": sum(dns) / k if k else None}


def _epoch(naive_et: datetime) -> float:
    return naive_et.replace(tzinfo=ET).timestamp()


def _last_at_or_before(symbol: str, when: datetime, lookback_days: int = 5) -> float | None:
    """Dernière clôture 1 min connue à `when` (heure NY naïve)."""
    for k in range(lookback_days + 1):
        day = (when.date() - timedelta(days=k)).isoformat()
        b = _bars(symbol, day)
        if b.empty:
            continue
        b = b[b["timestamp"] + pd.Timedelta(minutes=1) <= when]
        if not b.empty:
            return float(b["close"].iloc[-1])
    return None


# VXN : le flux du courtier ne le sert pas (cf. gex/vxn.py). En séance, VXN
# recalculé maison toutes les 15 min (bougies `VXN_CALC`) ; clôtures
# quotidiennes OFFICIELLES CBOE (history/vxn_daily.parquet, depuis 2009).
IV_INTRADAY = {"VXN": "VXN_CALC"}


def _official_daily(iv_symbol: str) -> pd.DataFrame:
    """Clôtures officielles quotidiennes (date, close) ; vide si absentes."""
    if iv_symbol != "VXN":
        return pd.DataFrame()
    from .config import SETTINGS
    from .vxn import DAILY_KEY
    path = SETTINGS.data_dir / "history" / f"{DAILY_KEY}.parquet"
    if not path.exists():
        return pd.DataFrame()
    d = pd.read_parquet(path)[["date", "close"]]
    d["date"] = pd.to_datetime(d["date"]).dt.date
    return d.sort_values("date")


def iv_at(iv_symbol: str, when: datetime) -> float | None:
    """Vol implicite connue à `when` (heure NY naïve). VXN : point maison du
    même jour s'il existe (en séance), sinon dernière clôture officielle
    publiée ; à la clôture de 16h00, la clôture OFFICIELLE du jour si elle
    est connue (définition du mouvement attendu « clôture à clôture »)."""
    intraday = IV_INTRADAY.get(iv_symbol, iv_symbol)
    official = _official_daily(iv_symbol)
    if not official.empty:
        known = official[(official["date"] < when.date())
                         | ((official["date"] == when.date()) & (when.time() >= time(16, 0)))]
        off = known.iloc[-1] if not known.empty else None
        if off is not None and off["date"] == when.date() and when.time() == time(16, 0):
            return float(off["close"])
        same_day = _last_at_or_before(intraday, when, lookback_days=0)
        if same_day is not None:
            return same_day
        return float(off["close"]) if off is not None else None
    return _last_at_or_before(intraday, when)


def iv_context(iv_symbol: str, today: str, n: int) -> dict:
    """Clôture de la veille de l'indice de vol et sa moyenne sur `n` séances
    (clôtures quotidiennes avant `today` : officielles pour le VXN, dernière
    bougie du jour sinon)."""
    official = _official_daily(iv_symbol)
    if not official.empty:
        closes = [float(c) for c in official.loc[
            official["date"] < date.fromisoformat(today), "close"].tail(n)][::-1]
    else:
        closes = []
        key = IV_INTRADAY.get(iv_symbol, iv_symbol)
        for day in reversed([d for d in store.price_days(key) if d < today]):
            b = _bars(key, day)
            if not b.empty:
                closes.append(float(b["close"].iloc[-1]))
            if len(closes) >= n:
                break
    return {"iv_prev": closes[0] if closes else None,
            "iv_avg": sum(closes) / len(closes) if closes else None,
            "iv_days": len(closes)}


def anchor_time(kind: str, now: datetime) -> tuple[datetime, datetime] | None:
    """(départ, fin d'affichage) du mouvement attendu en cours, heures NY
    naïves — comme le Pine : allumé au point de départ, éteint à la clôture
    cash suivante ; pour « close », le départ EST la clôture (16h00), donc
    affiché jusqu'à la clôture du lendemain. None entre la clôture et le
    point de départ suivant (autres choix)."""
    def close_of(d: date) -> datetime:
        return datetime.combine(d, RTH_CLOSE)
    if kind == "close":
        d = now.date()
        start = close_of(d) if d.weekday() < 5 and now >= close_of(d) \
            else close_of(_prev_weekday(d))
        return start, close_of(_next_weekday(start.date()))
    t = {"globex": time(18, 0), "midnight": time(0, 0), "open": RTH_OPEN}.get(kind)
    if t is None:
        return None
    start = datetime.combine(now.date(), t)
    if start > now:
        start -= timedelta(days=1)
    end = close_of(start.date() if t < RTH_CLOSE else _next_weekday(start.date()))
    return (start, end) if start <= now < end else None


def _prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _anchor_price(symbol: str, kind: str, start: datetime) -> float | None:
    if kind == "close":
        return _last_at_or_before(symbol, start)
    b = _bars(symbol, start.date().isoformat())
    if not b.empty:
        after = b[(b["timestamp"] >= start) & (b["timestamp"] < start + timedelta(minutes=5))]
        if not after.empty:
            return float(after["open"].iloc[0])
    return None


def compute(symbol: str, window: str | None = None, n: int = 60, anchor: str = "close",
            straddle_em: float | None = None, now: datetime | None = None) -> dict:
    """Ingrédients du cadre et du mouvement attendu pour le graphique /scalp."""
    now = now or datetime.now(ET).replace(tzinfo=None)
    today = now.date().isoformat()
    n = max(1, min(int(n or 60), 500))
    t0, t1 = parse_window(window)
    out: dict = {"symbol": symbol, "window": f"{t0:%H%M}-{t1:%H%M}", "n": n,
                 "iv_symbol": IV_SYMBOL.get(symbol), "frame": None, "em": None}
    stats = _cached(("stats", symbol, today, t0, t1, n),
                    lambda: window_stats(symbol, today, t0, t1, n))
    w = _window(_bars(symbol, today), today, t0, t1)
    if not w.empty:
        w = w[w["timestamp"] <= now]          # jamais de bougie « future » (replay, tests)
    iv = (_cached(("iv", out["iv_symbol"], today, n),
                  lambda: iv_context(out["iv_symbol"], today, n))
          if out["iv_symbol"] else {})
    if not w.empty and stats["count"]:
        out["frame"] = {**stats, "open": float(w["open"].iloc[0]),
                        "start": _epoch(datetime.combine(now.date(), t0)),
                        "end": _epoch(datetime.combine(now.date(), t1)),
                        "rth_end": _epoch(datetime.combine(now.date(), RTH_CLOSE)),
                        "iv_prev": iv.get("iv_prev"), "iv_avg": iv.get("iv_avg")}
    if anchor not in ANCHORS:
        anchor = "close"
    span = anchor_time(anchor, now)
    if span is not None:
        start, end = span
        px = _anchor_price(symbol, anchor, start)
        iv_now = iv_at(out["iv_symbol"], start) if out["iv_symbol"] else None
        if px is not None:
            out["em"] = {"anchor": anchor, "start": _epoch(start), "end": _epoch(end),
                         "base": px, "iv": iv_now,
                         "iv_em": px * iv_now / 100 / math.sqrt(252) if iv_now else None,
                         "straddle_em": straddle_em}
    out["stats_count"] = stats["count"]
    out["iv_days"] = iv.get("iv_days", 0)
    return out
