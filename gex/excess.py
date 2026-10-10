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
- Second test (l'entrée réelle de l'utilisateur) : après l'excès, première
  réaction d'au moins 15 pts, puis retour à moins de 10 pts de l'extrême ;
  voyant lu à ce moment-là, puis continuation au-delà de l'extrême et suivi
  d'un ordre placé à 3 pts de l'extrême.
- Le gris est découpé à titre DESCRIPTIF (`gris_nuance`) ; le seuil du voyant
  n'est pas modifié.

Rien ici n'est un signal validé : c'est l'outil de mesure.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

COLORS = ("vert", "orange", "rouge", "gris", "sans flux")
TARGETS = (10.0, 15.0, 20.0)
MAE_MARKS = (25.0, 50.0, 100.0)


def _end(n: int, i: int, minutes: float, ts: np.ndarray | None) -> int:
    """Index de fin (exclu) d'un horizon de `minutes` après le point `i` :
    en bougies 1 min (`ts` absent) ou en temps réel sur des ticks."""
    if ts is None:
        return min(n, i + 1 + int(minutes))
    return int(np.searchsorted(ts, ts[i] + minutes * 60, side="right"))


def tick_path(ts: np.ndarray, price: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Chemin de prix tick par tick, sans les ticks qui répètent le prix
    précédent (même chemin, moins de points). Entrées triées par heure."""
    if not len(price):
        return ts, price
    keep = np.concatenate([[True], price[1:] != price[:-1]])
    return ts[keep], price[keep]


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
               weaken: float = 0.5, grp: np.ndarray | None = None) -> tuple[str, dict]:
    """Couleur du voyant à l'instant `t` (epoch s, DÉBUT d'une minute : seules
    les barres de tape déjà closes sont lues). Fenêtres : [t - recent, t) et
    [t - recent - prev, t - recent). Une minute sans barre = aucun print =
    flux nul ; une barre non exploitable (`ok` faux) dans les fenêtres =
    « sans flux ». `grp` (facultatif) : échelle de chaque barre ; des
    échelles différentes dans les fenêtres = « sans flux » (les rapports
    net / brut ne sont justes qu'à échelle commune)."""
    r0, p0 = t - recent_min * 60, t - (recent_min + prev_min) * 60
    in_r = (minute_ts >= r0) & (minute_ts < t)
    in_p = (minute_ts >= p0) & (minute_ts < r0)
    win = in_r | in_p
    if not win.any() or (~ok[win]).any():
        return "sans flux", {}
    if grp is not None and len(set(grp[win])) > 1:
        return "sans flux", {}
    net_r, gross_r = float(net[in_r].sum()), float(np.abs(gross[in_r]).sum())
    net_p, gross_p = float(net[in_p].sum()), float(np.abs(gross[in_p]).sum())
    s_r, s_p = net_r * excess_dir, net_p * excess_dir     # > 0 : soutient l'excès
    detail = {"net_recent": net_r, "gross_recent": gross_r,
              "net_prev": net_p, "gross_prev": gross_p,
              # > 0 : soutient l'excès ; |.| >= ratio : flux « franc »
              "support_ratio": s_r / gross_r if gross_r > 0 else np.nan}
    franc_r = gross_r > 0 and abs(net_r) / gross_r >= ratio
    push_p = gross_p > 0 and s_p / gross_p >= ratio
    if franc_r and s_r < 0:
        return "vert", detail
    if push_p and s_r / recent_min <= weaken * s_p / prev_min:
        return "orange", detail
    if franc_r and s_r > 0:
        return "rouge", detail
    return "gris", detail


def scale_classes(implied: np.ndarray, usable: np.ndarray, threshold: float,
                  window: int = 31) -> np.ndarray:
    """Échelle des barres de tape NON vérifiables (avant le correctif du
    multiplicateur, sans prints bruts) : delta moyen implicite = brut /
    (contrats × prix du NQ × valeur réelle du point). Juste, il reste sous 1
    (souvent 0,2-0,5) ; valorisé ×5, il est 5 fois plus grand. Médiane
    glissante sur `window` barres exploitables, comparée à `threshold` (calé
    sur les barres vérifiées) : "x1", "x5", ou "" (aucune barre exploitable).
    Sert seulement à savoir si une fenêtre a une échelle COMMUNE."""
    s = pd.Series(np.where(usable, implied, np.nan), dtype=float)
    med = s.rolling(window, center=True, min_periods=5).median().ffill().bfill()
    out = np.where(med < threshold, "x1", "x5").astype(object)
    out[med.isna().to_numpy()] = ""
    return out


def gris_nuance(support_ratio: float, ratio: float = 0.35, weak: float = 0.20) -> str:
    """Découpage DESCRIPTIF du gris (le seuil du voyant n'est pas modifié) :
    soutien ou opposition entre `weak` et `ratio`, ou flux quasi neutre."""
    if support_ratio is None or np.isnan(support_ratio):
        return "gris (aucun flux)"
    if support_ratio >= weak:
        return f"gris (soutien {weak:g}-{ratio:g})"
    if support_ratio <= -weak:
        return f"gris (contre {weak:g}-{ratio:g})"
    return f"gris (< {weak:g})"


def find_retest(high: np.ndarray, low: np.ndarray, i: int, excess_dir: int,
                reaction_pts: float = 15.0, retest_pts: float = 10.0,
                max_wait: int = 120, ts: np.ndarray | None = None) -> dict | None:
    """Second test après l'excès franchi en bougie `i` : l'extrême E suit
    l'excès jusqu'à une première réaction d'au moins `reaction_pts` contre
    lui, puis le prix revient à moins de `retest_pts` de E. Retour :
    {t, extreme, reaction_i} (t = bougie du retour) ou None dans `max_wait`
    minutes. Dans une même bougie, la réaction n'est retenue qu'avec un
    extrême déjà fixé (prudent : l'ordre intra-bougie est inconnu). Avec
    `ts`, les points sont des ticks (high = low = prix) et l'attente est en
    temps réel."""
    end = _end(len(high), i, max_wait, ts)
    ext = low[i] if excess_dir < 0 else high[i]
    reacted = None
    for k in range(i + 1, end):
        h, lo = float(high[k]), float(low[k])
        if reacted is None:
            back = (h - ext) if excess_dir < 0 else (ext - lo)
            if back >= reaction_pts:
                reacted = k
                continue
            ext = min(ext, lo) if excess_dir < 0 else max(ext, h)
        else:
            near = (lo <= ext + retest_pts) if excess_dir < 0 else (h >= ext - retest_pts)
            if near:
                return {"t": k, "extreme": float(ext), "reaction_i": reacted}
    return None


def retest_outcome(high: np.ndarray, low: np.ndarray, t: int, extreme: float,
                   fade_dir: int, entry_offset: float = 3.0, horizon: int = 120,
                   targets=TARGETS, ts: np.ndarray | None = None) -> dict:
    """Au second test (bougie `t`) : CONTINUATION au-delà de l'extrême
    précédent (le prix va-t-il plus loin ?) et, si l'ordre à `entry_offset`
    points de l'extrême est touché, le même suivi que `outcome` depuis ce
    prix d'entrée."""
    end = min(len(high), t + horizon) if ts is None else _end(len(high), t, horizon, ts)
    hi, lo = high[t:end], low[t:end]
    beyond = (extreme - lo) if fade_dir > 0 else (hi - extreme)
    res = {"continuation": float(max(0.0, beyond.max())) if len(hi) else 0.0}
    entry = extreme + fade_dir * entry_offset
    touched = (lo <= entry) if fade_dir > 0 else (hi >= entry)
    idx = np.flatnonzero(touched)
    res["rempli"] = bool(len(idx))
    if len(idx):
        f = t + int(idx[0])
        left = max(1, end - f - 1) if ts is None else (ts[t] + horizon * 60 - ts[f]) / 60
        res.update(outcome(high, low, f, entry, fade_dir, left, targets, ts))
    return res


def summarize_retests(events: pd.DataFrame, key: str = "couleur", order=COLORS,
                      targets=TARGETS, marks=MAE_MARKS) -> pd.DataFrame:
    """Par couleur au second test : continuation au-delà de l'extrême
    précédent, part des ordres remplis et, parmi eux, retours et MAE."""
    rows = []
    labels = [c for c in order if c in set(events.get(key, []))] if not events.empty else []
    labels += sorted(set(events[key]) - set(labels)) if not events.empty else []
    for label in labels:
        g = events[events[key] == label]
        n = len(g)
        row = {key: label, "n": n, "seances": g["day"].nunique(),
               "cont_med": g["continuation"].median(),
               "cont_p75": g["continuation"].quantile(0.75),
               "cont_p90": g["continuation"].quantile(0.90),
               "cont_max": g["continuation"].max()}
        for m in marks:
            row[f"cont>={m:g}_%"] = 100 * (g["continuation"] >= m).mean()
        k_lo, k_hi = wilson(int((g["continuation"] >= marks[1]).sum()), n)
        row[f"cont>={marks[1]:g}_ic95"] = f"{100 * k_lo:.0f}-{100 * k_hi:.0f}"
        f = g[g["rempli"]]
        row["rempli_%"] = 100 * len(f) / n
        for k in targets:
            row[f"retour_{k:g}_%"] = 100 * f[f"hit_{k:g}"].mean() if len(f) else np.nan
        row["mae_med"] = f["mae"].median() if len(f) else np.nan
        row["mae_p90"] = f["mae"].quantile(0.90) if len(f) else np.nan
        rows.append(row)
    return pd.DataFrame(rows).set_index(key) if rows else pd.DataFrame()


def outcome(high: np.ndarray, low: np.ndarray, i: int, level: float, fade_dir: int,
            horizon: float, targets=TARGETS, ts: np.ndarray | None = None) -> dict:
    """Après le franchissement au prix `level` dans la bougie `i` : excursion
    adverse (pire prix contre le fade, bougie `i` comprise — le franchissement
    précède forcément l'extrême de la bougie) et retours favorables (à partir
    de la bougie suivante seulement : dans la bougie `i`, l'ordre est
    inconnu). Avec `ts` (ticks), l'horizon et les délais sont en minutes
    réelles et l'ordre des prix est exact."""
    end = _end(len(high), i, horizon, ts)
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
            res[f"min_to_{k:g}"] = (j + 1 if ts is None
                                    else (ts[i + 1 + j] - ts[i]) / 60)
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


def summarize(events: pd.DataFrame, targets=TARGETS, marks=MAE_MARKS,
              key: str = "couleur", order=COLORS) -> pd.DataFrame:
    """Par couleur : nombre, séances, taux de retour (avec intervalle à 95 %
    pour le premier objectif), excursion adverse (médiane, p75, p90, max) et
    part des excès qui ont continué d'au moins 25 / 50 / 100 points."""
    rows = []
    labels = [c for c in order if not events.empty and c in set(events[key])]
    labels += sorted(set(events[key]) - set(labels)) if not events.empty else []
    for color in labels:
        g = events[events[key] == color]
        n = len(g)
        row = {key: color, "n": n, "seances": g["day"].nunique()}
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
    return pd.DataFrame(rows).set_index(key) if rows else pd.DataFrame()
