"""Excès depuis le dernier swing et soutien des teneurs de marché — logique pure.

Question de l'utilisateur (scalp contrarien NQ, sans stop, renforcement sur le
frein suivant) : pendant un excès, les market makers le SOUTIENNENT-ils
(« ne rentre pas ») ou sont-ils CONTRE (« fade possible ») ?

- Swing : un sommet / creux est confirmé quand le prix repart d'au moins
  `swing_pts` dans l'autre sens (zigzag causal, bougies 1 min).
- Excès : le prix s'éloigne d'au moins `excess_pts` du dernier swing
  confirmé ; un seul excès par jambe (le premier franchissement), au niveau
  franchi.
- Voyant, d'après le flux de couverture des dealers (tape signé) des minutes
  PRÉCÉDANT la bougie de l'excès :
    vert   : flux net franc CONTRE l'excès ;
    orange : il soutenait l'excès et ce soutien a au moins diminué de moitié ;
    rouge  : flux net franc DANS le sens de l'excès, sans affaiblissement ;
    gris   : pas de flux net franc ;
    sans flux : barres de tape absentes ou non vérifiées.
  « Franc » = |net| / brut >= `ratio` (même seuil que le veto du bandeau,
  edge.EdgeParams.flow_ratio). Net > 0 = dealers acheteurs du sous-jacent
  (cf. flowtape : la couverture suit le delta pris par le preneur).
- Ensuite (mesure, pas un trade) : excursion adverse maximale, et retour de
  10 / 15 / 20 points en faveur du fade, dans un horizon donné.

Rien ici n'est un signal validé : c'est l'outil de mesure.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

COLORS = ("vert", "orange", "rouge", "gris", "sans flux")
TARGETS = (10.0, 15.0, 20.0)
MAE_MARKS = (25.0, 50.0, 100.0)


def find_excesses(high: np.ndarray, low: np.ndarray, excess_pts: float,
                  swing_pts: float) -> list[dict]:
    """Excès depuis le dernier swing confirmé. Retour : {i, dir, swing, level}
    — `i` index de la bougie du franchissement, `dir` sens de l'excès (+1
    haussier, -1 baissier), `level` = swing ± excess_pts (prix franchi)."""
    out = []
    leg = 0                                   # 0 : pas encore de swing confirmé
    ext_hi, ext_lo = high[0], low[0]
    swing, fired = None, False
    for i in range(len(high)):
        h, lo = float(high[i]), float(low[i])
        if leg >= 0:
            ext_hi = max(ext_hi, h)
        if leg <= 0:
            ext_lo = min(ext_lo, lo)
        if leg >= 0 and ext_hi - lo >= swing_pts:          # sommet confirmé
            swing, leg, fired, ext_lo = ext_hi, -1, False, lo
        elif leg <= 0 and h - ext_lo >= swing_pts:         # creux confirmé
            swing, leg, fired, ext_hi = ext_lo, 1, False, h
        if leg and not fired:
            reach = (h - swing) if leg > 0 else (swing - lo)
            if reach >= excess_pts:
                fired = True
                out.append({"i": i, "dir": leg, "swing": swing,
                            "level": swing + leg * excess_pts})
    return out


def flow_color(minute_ts: np.ndarray, net: np.ndarray, gross: np.ndarray,
               ok: np.ndarray, t: float, excess_dir: int, ratio: float = 0.35,
               recent_min: int = 5, prev_min: int = 10,
               weaken: float = 0.5) -> tuple[str, dict]:
    """Couleur du voyant à l'instant `t` (début de la bougie de l'excès, epoch
    s). Fenêtres : [t - recent, t) et [t - recent - prev, t - recent). Une
    minute sans barre = aucun print = flux nul ; une barre non vérifiée
    (`ok` faux) dans les fenêtres = « sans flux »."""
    r0, p0 = t - recent_min * 60, t - (recent_min + prev_min) * 60
    in_r = (minute_ts >= r0) & (minute_ts < t)
    in_p = (minute_ts >= p0) & (minute_ts < r0)
    if not (in_r | in_p).any() or (~ok[in_r | in_p]).any():
        return "sans flux", {}
    net_r, gross_r = float(net[in_r].sum()), float(np.abs(gross[in_r]).sum())
    net_p, gross_p = float(net[in_p].sum()), float(np.abs(gross[in_p]).sum())
    s_r, s_p = net_r * excess_dir, net_p * excess_dir     # > 0 : soutient l'excès
    detail = {"net_recent": net_r, "gross_recent": gross_r,
              "net_prev": net_p, "gross_prev": gross_p}
    franc_r = gross_r > 0 and abs(net_r) / gross_r >= ratio
    push_p = gross_p > 0 and s_p / gross_p >= ratio
    if franc_r and s_r < 0:
        return "vert", detail
    if push_p and s_r / recent_min <= weaken * s_p / prev_min:
        return "orange", detail
    if franc_r and s_r > 0:
        return "rouge", detail
    return "gris", detail


def outcome(high: np.ndarray, low: np.ndarray, i: int, level: float, fade_dir: int,
            horizon: int, targets=TARGETS) -> dict:
    """Après le franchissement au prix `level` dans la bougie `i` : excursion
    adverse (pire prix contre le fade, bougie `i` comprise — le franchissement
    précède forcément l'extrême de la bougie) et retours favorables (à partir
    de la bougie suivante seulement : dans la bougie `i`, l'ordre est
    inconnu)."""
    end = min(len(high), i + 1 + horizon)
    adv0 = (level - low[i]) if fade_dir > 0 else (high[i] - level)
    hi, lo = high[i + 1:end], low[i + 1:end]
    fav = (hi - level) if fade_dir > 0 else (level - lo)
    adv = (level - lo) if fade_dir > 0 else (hi - level)
    run_adv = np.maximum.accumulate(np.concatenate([[max(adv0, 0.0)], adv]))
    res = {"bars_after": len(hi), "mae": float(run_adv[-1])}
    for k in targets:
        hit = np.flatnonzero(fav >= k)
        if len(hit):
            j = int(hit[0])
            res[f"hit_{k:g}"] = True
            res[f"min_to_{k:g}"] = j + 1
            res[f"mae_before_{k:g}"] = float(run_adv[j + 1])   # pire avant le retour
        else:
            res[f"hit_{k:g}"] = False
            res[f"min_to_{k:g}"] = np.nan
            res[f"mae_before_{k:g}"] = float(run_adv[-1])
    return res


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalle de confiance à 95 % d'une proportion (petits échantillons)."""
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def summarize(events: pd.DataFrame, targets=TARGETS, marks=MAE_MARKS) -> pd.DataFrame:
    """Par couleur : nombre, séances, taux de retour (avec intervalle à 95 %
    pour le premier objectif), excursion adverse (médiane, p75, p90, max) et
    part des excès qui ont continué d'au moins 25 / 50 / 100 points."""
    rows = []
    for color in COLORS:
        g = events[events["couleur"] == color] if not events.empty else events
        if g.empty:
            continue
        n = len(g)
        row = {"couleur": color, "n": n, "seances": g["day"].nunique()}
        for k in targets:
            row[f"retour_{k:g}_%"] = 100 * g[f"hit_{k:g}"].mean()
        lo, hi = wilson(int(g[f"hit_{targets[0]:g}"].sum()), n)
        row[f"retour_{targets[0]:g}_ic95"] = f"{100 * lo:.0f}-{100 * hi:.0f}"
        row[f"min_med_{targets[0]:g}"] = g[f"min_to_{targets[0]:g}"].median()
        row[f"mae_avant_{targets[0]:g}_med"] = g[f"mae_before_{targets[0]:g}"].median()
        row["mae_med"] = g["mae"].median()
        row["mae_p75"] = g["mae"].quantile(0.75)
        row["mae_p90"] = g["mae"].quantile(0.90)
        row["mae_max"] = g["mae"].max()
        for m in marks:
            row[f"mae>={m:g}_%"] = 100 * (g["mae"] >= m).mean()
        rows.append(row)
    return pd.DataFrame(rows).set_index("couleur") if rows else pd.DataFrame()
