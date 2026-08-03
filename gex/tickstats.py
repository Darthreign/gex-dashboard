"""Statistiques de la fenêtre de clôture, calculées À LA DEMANDE depuis les
ticks bruts (cf. gex/tickrec, gex/store.load_ticks).

Rien n'est stocké ici : on rejoue le brut. La fonction clé est `stop_swept` —
la réponse objective à « un stop aurait-il été balayé ? », impossible à obtenir
d'une bougie 1 min. Fonctions pures, testables.
"""
from __future__ import annotations

import pandas as pd


def _r2(x) -> float:
    return round(float(x), 2)


def window_metrics(df: pd.DataFrame, split_ts: float | None = None) -> dict:
    """Caractérise la fenêtre : O/H/L/C, range, excursions. Si `split_ts` (epoch
    de la clôture 16h ET) est fourni, sépare AVANT / APRÈS la clôture — c'est le
    « casino d'après 22h » chiffré, à la résolution du tick."""
    if df is None or df.empty:
        return {"available": False, "n_ticks": 0}
    d = df.sort_values("ts")
    p = d["price"]
    out = {
        "available": True, "n_ticks": int(len(d)),
        "open": _r2(p.iloc[0]), "close": _r2(p.iloc[-1]),
        "high": _r2(p.max()), "low": _r2(p.min()),
    }
    out["range"] = _r2(out["high"] - out["low"])
    out["max_up"] = _r2(out["high"] - out["open"])
    out["max_down"] = _r2(out["open"] - out["low"])

    if split_ts is not None:
        pre = d[d["ts"] < split_ts]["price"]
        post = d[d["ts"] >= split_ts]["price"]
        if len(pre):
            out["pre_range"] = _r2(pre.max() - pre.min())
        if len(post):
            # prix de clôture = dernier avant 16h, sinon premier après
            c = float(pre.iloc[-1]) if len(pre) else float(post.iloc[0])
            out["close_2200"] = _r2(c)
            out["post_high"] = _r2(post.max())
            out["post_low"] = _r2(post.min())
            out["post_range"] = _r2(post.max() - post.min())
            out["post_max_up"] = _r2(post.max() - c)     # excursion haussière post-clôture
            out["post_max_down"] = _r2(c - post.min())   # ... baissière
            if out.get("pre_range"):
                out["post_expansion"] = _r2(out["post_range"] / out["pre_range"])
    return out


def stop_swept(df: pd.DataFrame, entry_price: float, stop_pts: float,
               direction: int, after_ts: float | None = None) -> dict:
    """Un stop aurait-il été touché ? Rejoue les ticks (optionnellement à partir
    de `after_ts`) pour une entrée fictive.

    `direction` : +1 long (stop sous l'entrée) / -1 short (stop au-dessus).
    Renvoie `swept` (booléen) et `max_adverse_excursion` — la pire avancée
    contre la position, la mesure directe du risque de balayage.
    """
    d = df.sort_values("ts")
    if after_ts is not None:
        d = d[d["ts"] >= after_ts]
    if d.empty:
        return {"available": False}
    p = d["price"]
    if direction >= 0:                     # long : stop en dessous
        stop = entry_price - stop_pts
        mae = _r2(entry_price - p.min())
        swept = bool(p.min() <= stop)
    else:                                  # short : stop au-dessus
        stop = entry_price + stop_pts
        mae = _r2(p.max() - entry_price)
        swept = bool(p.max() >= stop)
    return {"available": True, "direction": "long" if direction >= 0 else "short",
            "entry": entry_price, "stop": _r2(stop), "stop_pts": stop_pts,
            "swept": swept, "max_adverse_excursion": mae}
