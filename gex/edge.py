"""Lecture normalisée de /scalp v2 et mesure de son edge — logique pure.

Hypothèse testée (scalping contrarien sur rejet) : un EXCÈS de prix, mesuré
en unités de mouvement attendu (EM) plutôt qu'en points fixes, se corrige
plus souvent qu'au hasard QUAND les teneurs de marché freinent (zone de gamma
positif, au-dessus du Gamma Flip) et qu'un signe d'épuisement le confirme
(absorption de sens opposé au mouvement, flux de couverture qui freine).
Dans la zone d'accélération (sous le flip), la politique est réglable
(`accel_policy`) : l'expérience de l'utilisateur (09/10) est que les freins y
restent actifs pour un LONG après une baisse, alors que shorter un excès
haussier y est risqué — d'où « longs seulement » par défaut, à confirmer par
le rapport (résultats par zone ET par sens). Un flux de couverture franc dans
le sens de l'excès (les dealers poussent le prix) met le setup « à éviter »
(`flow_veto`) : il peut emmener le prix bien au-delà de l'EM.

Le même code sert au direct (bandeau /scalp v2) et au backtest
(scripts/edge_report.py), qui mesure l'espérance en EM, la compare à un
rejet naïf et au hasard, et choisit les seuils sur une période avant de les
valider sur la suivante. Tant que ce rapport n'a pas tourné sur des séances
réelles, rien ici n'est un edge démontré.

Vocabulaire : `fade_dir` = sens du trade de rejet (+1 achat, -1 vente),
opposé au sens de l'excès.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

PARAMS_VERSION = "edge-v1"


@dataclass(frozen=True)
class EdgeParams:
    excess_em: float = 0.7          # |prix - ouverture| >= 0,7 EM : excès
    flip_band_em: float = 0.15      # zone neutre autour du Gamma Flip
    absorb_window_s: float = 600.0  # absorption opposée vue dans les 10 dernières min
    absorb_dist_em: float = 0.10    # ... à moins de 0,1 EM du prix
    flow_ratio: float = 0.35        # |net| / brut pour un flux de couverture « franc »
    require: int = 1                # confirmations minimales pour un setup
    target_em: float = 0.25
    stop_em: float = 0.25
    # cible / stop en POINTS (taille de trade de l'utilisateur : 5-10 pts sur
    # NQ) ; 0 = en EM (target_em / stop_em)
    target_pts: float = 0.0
    stop_pts: float = 0.0
    # zone d'accélération (gamma négatif) : "avoid" (aucun rejet), "long"
    # (rejet d'un excès BAISSIER seulement, donc achat), "both"
    accel_policy: str = "long"
    # flux de couverture franc DANS le sens de l'excès : setup à éviter
    flow_veto: bool = True
    horizon_min: int = 30
    cooldown_min: int = 15

    def to_json(self) -> dict:
        return {"version": PARAMS_VERSION, **asdict(self)}

    @classmethod
    def from_json(cls, d: dict) -> "EdgeParams":
        fields = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**fields)


def load_params(path: Path) -> tuple[EdgeParams, bool]:
    """Paramètres validés par le rapport s'ils existent (True), sinon défauts."""
    try:
        return EdgeParams.from_json(json.loads(path.read_text(encoding="utf-8"))), True
    except (OSError, ValueError, TypeError):
        return EdgeParams(), False


# ---------------------------------------------------------------- lecture

def regime_zone(spot: float, zg: float | None, em: float, gex0: float | None,
                band: float) -> tuple[str, float | None]:
    """(zone, distance au flip en EM). `frein` au-dessus du flip au-delà de
    la zone neutre, `accelerateur` en dessous, `transition` dedans ; sans
    flip, le signe du GEX 0DTE tranche. Un GEX 0DTE de signe contraire à la
    position par rapport au flip ramène en `transition` : les deux mesures
    ne s'accordent pas."""
    if zg is None or not em:
        if gex0 is None:
            return "inconnu", None
        return ("frein" if gex0 >= 0 else "accelerateur"), None
    d = (spot - zg) / em
    if abs(d) < band:
        return "transition", d
    zone = "frein" if d > 0 else "accelerateur"
    if gex0 is not None and (gex0 >= 0) != (zone == "frein"):
        return "transition", d
    return zone, d


def reading(spot: float, open_: float | None, em: float | None, zg: float | None,
            gex0: float | None, absorptions: list[dict], now_ts: float,
            flow_net: float | None, flow_gross: float | None,
            p: EdgeParams) -> dict:
    """Lecture à l'instant `now_ts`. `absorptions` : {ts, price, side} — side
    = sens AGRESSEUR absorbé (BUY : acheteurs absorbés à l'ask, résistance ;
    SELL : vendeurs absorbés au bid, support)."""
    out = {"ext_em": None, "zone": "inconnu", "dist_flip_em": None, "excess_dir": 0,
           "fade_dir": 0, "confirmations": [], "setup": "none", "em": em, "veto": None}
    if open_ is None or not em or em <= 0:
        return out
    ext = (spot - open_) / em
    zone, dflip = regime_zone(spot, zg, em, gex0, p.flip_band_em)
    excess_dir = 0 if abs(ext) < p.excess_em else (1 if ext > 0 else -1)
    out.update(ext_em=ext, zone=zone, dist_flip_em=dflip, excess_dir=excess_dir)
    if not excess_dir:
        return out
    conf = []
    if zone == "frein":
        conf.append("frein")
    # épuisement : l'agresseur DANS le sens de l'excès se fait absorber
    exhausted_side = "BUY" if excess_dir > 0 else "SELL"
    if any(a["side"] == exhausted_side and 0 <= now_ts - a["ts"] <= p.absorb_window_s
           and abs(a["price"] - spot) <= p.absorb_dist_em * em for a in absorptions):
        conf.append("absorption")
    pushing = False
    if flow_gross and flow_gross > 0 and flow_net is not None:
        if abs(flow_net) / flow_gross >= p.flow_ratio:
            if (flow_net > 0) != (excess_dir > 0):
                conf.append("flux")          # les dealers freinent l'excès
            else:
                pushing = True               # ils le poussent
    out["confirmations"] = conf
    fade_dir = -excess_dir
    accel_ok = (p.accel_policy == "both"
                or (p.accel_policy == "long" and fade_dir > 0))
    if pushing and p.flow_veto:
        out.update(setup="avoid", veto="flux")
    elif zone == "accelerateur" and not accel_ok:
        out.update(setup="avoid", veto="zone")
    elif len(conf) >= p.require:
        out.update(setup="fade", fade_dir=fade_dir)
    return out


# ---------------------------------------------------------------- simulation

def simulate_trade(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                   entry: float, direction: int, em: float, p: EdgeParams) -> dict:
    """Trade entré à `entry` au close de la barre de signal, suivi sur les
    barres SUIVANTES (au plus `horizon_min`). Cible et stop en EM ; si les
    deux sont touchés dans la même barre, le stop compte (hypothèse
    prudente). Résultat en EM, avec les excursions favorable / défavorable."""
    tgt = p.target_pts if p.target_pts > 0 else p.target_em * em
    stp = p.stop_pts if p.stop_pts > 0 else p.stop_em * em
    mfe = mae = 0.0
    for h, lo, c in zip(highs[:p.horizon_min], lows[:p.horizon_min], closes[:p.horizon_min]):
        fav = (h - entry) if direction > 0 else (entry - lo)
        adv = (entry - lo) if direction > 0 else (h - entry)
        mfe, mae = max(mfe, fav), max(mae, adv)
        if adv >= stp:
            return _result(-stp, "stop", mfe, mae, em)
        if fav >= tgt:
            return _result(tgt, "target", mfe, mae, em)
        last = c
    if not len(closes[:p.horizon_min]):
        return _result(0.0, "none", 0.0, 0.0, em)
    return _result((last - entry) * direction, "time", mfe, mae, em)


def _result(pts: float, exit_: str, mfe: float, mae: float, em: float) -> dict:
    """Résultat en EM (comparable d'un jour à l'autre) ET en points."""
    return {"pnl_em": pts / em, "pnl_pts": pts, "exit": exit_,
            "mfe_em": mfe / em, "mae_em": mae / em}


def minute_bars(ticks: pd.DataFrame) -> pd.DataFrame:
    """Barres 1 min (epoch de début, open/high/low/close) depuis des ticks."""
    t = ticks[ticks["side"].isin(("BUY", "SELL"))] if "side" in ticks else ticks
    if t.empty:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close"])
    m = (t["ts"].to_numpy(dtype=float) // 60) * 60
    g = pd.DataFrame({"m": m, "p": t["price"].to_numpy(dtype=float)}).groupby("m")["p"]
    return pd.DataFrame({"ts": g.first().index.to_numpy(), "open": g.first().to_numpy(),
                         "high": g.max().to_numpy(), "low": g.min().to_numpy(),
                         "close": g.last().to_numpy()})


def run_session(bars: pd.DataFrame, open_: float, em: float, ctx: pd.DataFrame | None,
                absorptions: list[dict], flow: pd.DataFrame | None, p: EdgeParams,
                start_idx: int = 15, end_cut: int = 30) -> list[dict]:
    """Rejoue une séance minute par minute. Trois familles de trades :
    `setup` (la lecture complète), `naive` (tout excès, sans zone ni
    confirmation), et `random` (même sens de rejet que l'excès courant, à des
    minutes tirées au hasard, même nombre que `naive`) — l'edge, c'est l'écart
    de `setup` à ces deux références. `ctx` : colonnes ts, zg, gex0 (asof).
    `flow` : colonnes ts, net, gross (fenêtre glissante déjà sommée)."""
    if bars.empty or not em:
        return []
    ts = bars["ts"].to_numpy(dtype=float)
    hi, lo, cl = (bars[c].to_numpy(dtype=float) for c in ("high", "low", "close"))
    ctx_ts = ctx["ts"].to_numpy(dtype=float) if ctx is not None and not ctx.empty else None
    flow_ts = flow["ts"].to_numpy(dtype=float) if flow is not None and not flow.empty else None
    trades, last_entry = [], {"setup": -1e18, "naive": -1e18}
    naive_idx = []
    for i in range(start_idx, max(start_idx, len(bars) - end_cut)):
        zg = gex0 = None
        if ctx_ts is not None:
            j = int(np.searchsorted(ctx_ts, ts[i], side="right")) - 1
            if j >= 0:
                zg = _num(ctx["zg"].iloc[j])
                gex0 = _num(ctx["gex0"].iloc[j])
        net = gross = None
        if flow_ts is not None:
            j = int(np.searchsorted(flow_ts, ts[i], side="right")) - 1
            if j >= 0:
                net, gross = float(flow["net"].iloc[j]), float(flow["gross"].iloc[j])
        r = reading(cl[i], open_, em, zg, gex0, absorptions, ts[i] + 60, net, gross, p)
        for kind, cond, d in (("setup", r["setup"] == "fade", r["fade_dir"]),
                              ("naive", r["excess_dir"] != 0, -r["excess_dir"])):
            if cond and ts[i] - last_entry[kind] >= p.cooldown_min * 60:
                last_entry[kind] = ts[i]
                res = simulate_trade(hi[i + 1:], lo[i + 1:], cl[i + 1:], cl[i], d, em, p)
                trades.append({"kind": kind, "ts": ts[i], "dir": d, "zone": r["zone"],
                               "ext_em": r["ext_em"], "conf": ",".join(r["confirmations"]),
                               **res})
                if kind == "naive":
                    naive_idx.append(i)
    rng = np.random.default_rng(int(ts[0]) % (2 ** 32))
    lo_i, hi_i = start_idx, max(start_idx + 1, len(bars) - end_cut)
    for _ in naive_idx:
        i = int(rng.integers(lo_i, hi_i))
        ext = cl[i] - open_
        d = -1 if ext > 0 else 1
        res = simulate_trade(hi[i + 1:], lo[i + 1:], cl[i + 1:], cl[i], d, em, p)
        trades.append({"kind": "random", "ts": ts[i], "dir": d, "zone": "",
                       "ext_em": ext / em, "conf": "", **res})
    return trades


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


# ---------------------------------------------------------------- statistiques

def summarize(trades: pd.DataFrame, n_boot: int = 2000, seed: int = 0,
              value: str = "pnl_em") -> pd.DataFrame:
    """Par famille : nombre, taux de gain, espérance en EM et son intervalle
    à 90 % par bootstrap PAR SÉANCE (les trades d'une même séance ne sont
    pas indépendants), MFE/MAE médians."""
    rows = []
    rng = np.random.default_rng(seed)
    for kind, g in trades.groupby("kind"):
        by_day = g.groupby("day")[value]
        sums, counts = by_day.sum().to_numpy(), by_day.count().to_numpy()
        boots = []
        if len(sums) >= 2:
            for _ in range(n_boot):
                k = rng.integers(0, len(sums), len(sums))
                boots.append(sums[k].sum() / max(counts[k].sum(), 1))
        lo, hi = (np.percentile(boots, [5, 95]) if boots else (np.nan, np.nan))
        unit = "pts" if value == "pnl_pts" else "em"
        rows.append({"kind": kind, "n": len(g), "days": len(sums),
                     "win_rate": float((g[value] > 0).mean()),
                     f"expectancy_{unit}": float(g[value].mean()),
                     "ci90_lo": float(lo), "ci90_hi": float(hi),
                     "mfe_med_em": float(g["mfe_em"].median()),
                     "mae_med_em": float(g["mae_em"].median())})
    return pd.DataFrame(rows).set_index("kind") if rows else pd.DataFrame()


GRID = {
    "excess_em": (0.5, 0.7, 0.9, 1.1),
    "require": (1, 2),
    "target_em": (0.2, 0.3, 0.4),
    "stop_em": (0.2, 0.3),
}


def param_grid(base: EdgeParams):
    import itertools
    keys = list(GRID)
    for values in itertools.product(*(GRID[k] for k in keys)):
        yield replace(base, **dict(zip(keys, values)))
