"""Courbes de flux d'options de la séance pour un client distant (Squirrel X).

Trois mesures par sous-jacent, tirées des barres 1 min du tape (gex/flowtape.py) :

- `net_delta` : delta net créé par les transactions, en $ de sous-jacent, POINT DE
  VUE DEALER (positif = dealers longs delta : les preneurs ont acheté des puts ou
  vendu des calls). Même signe que la tuile « DEX net » du dashboard.
- `net_premium` : prime nette en $, point de vue dealer (positif = les preneurs
  ont payé plus de primes qu'ils n'en ont encaissé).
- `notional_calls` / `notional_puts` : notionnel BRUT traité, en $, sans signe :
  somme de taille × multiplicateur × spot, toutes transactions (combos et agresseur
  inconnu compris). Absent des barres antérieures à son ajout (22/11/2026).

Historique : `GET /api/v1/ticks/flow/<SYMBOLE>/<jour>.parquet`, valeurs PAR MINUTE
(le client cumule). Direct : canal `flow:<SYMBOLE>` de `/ticks/stream?flow=...`,
qui porte la minute en cours et le CUMUL de la journée.

Le jour est celui du fichier de tape : la date de New York de la minute. Périmètre
du tape : fenêtre de strikes et d'échéances souscrite, pas toute la chaîne.
"""
from __future__ import annotations

import io
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from . import broadcast, store

SYMBOLS = ("NQ", "ES", "QQQ", "NDX", "SPX", "SPY")
NY = ZoneInfo("America/New_York")
MEASURES = ["net_delta", "net_premium", "notional_calls", "notional_puts"]
COLUMNS = ["ts", *MEASURES]
FEED_INTERVAL_S = 2.0


def available_days(symbol: str) -> list[str]:
    return store.tape_days(symbol) if symbol in SYMBOLS else []


def session(symbol: str, day: str) -> pd.DataFrame:
    """Barres 1 min de `day` : `ts` (début de minute, secondes Unix) et les
    mesures PAR MINUTE. Une mesure absente du fichier reste vide (NaN), jamais
    remplacée par zéro."""
    from .app import tape_day
    bars = tape_day(symbol, day)
    if bars.empty:
        return pd.DataFrame(columns=COLUMNS, dtype=float)
    stamp = pd.to_datetime(bars["timestamp"]).dt.tz_localize(NY, ambiguous="NaT", nonexistent="NaT")
    keep = stamp.notna().to_numpy()
    out = pd.DataFrame({"ts": [float(t.timestamp()) for t in stamp[keep]]})
    for col in MEASURES:
        out[col] = (pd.to_numeric(bars[col], errors="coerce").to_numpy()[keep]
                    if col in bars else float("nan"))
    return out


def session_parquet(symbol: str, day: str) -> bytes | None:
    df = session(symbol, day)
    if df.empty:
        return None
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def _num(v) -> float | None:
    v = float(v)
    return round(v, 2) if math.isfinite(v) else None


def event(symbol: str, clock=None) -> dict | None:
    """Cumul de la journée en cours et valeurs de sa dernière minute."""
    day = (clock() if clock else datetime.now(NY)).strftime("%Y-%m-%d")
    df = session(symbol, day)
    if df.empty:
        return None
    last = df.iloc[-1]
    return {"symbol": symbol, "day": day, "t": int(last["ts"]),
            # min_count=1 : une mesure jamais renseignée reste null, pas 0
            "cum": {c: _num(df[c].sum(min_count=1)) for c in MEASURES},
            "bar": {c: _num(last[c]) for c in MEASURES}}


def producer(symbol: str, build=event):
    last = {"j": None}

    def produce() -> str | None:
        content = build(symbol)
        if content is None:
            return None
        j = json.dumps(content, allow_nan=False)
        if j == last["j"]:
            return None
        last["j"] = j
        return f"data: {j}\n\n"
    return produce


def feed_channels(flow: str | None) -> dict[str, broadcast.Channel]:
    """Canaux `flow:<SYMBOLE>` demandés par `flow=NQ,QQQ` ; un symbole inconnu est ignoré."""
    return {f"flow:{s}": broadcast.channel(("flow-feed", s), lambda s=s: producer(s), FEED_INTERVAL_S)
            for s in (flow or "").split(",") if s in SYMBOLS}
