"""Le hasard peut-il expliquer les écarts du rapport d'excès ?

Les seconds tests d'une même séance ne sont pas indépendants (même régime,
même tendance, souvent plusieurs le même jour) : un test classique qui les
compte un par un surestime la significativité. Ici tout est fait par
RÉÉCHANTILLONNAGE DES SÉANCES (bootstrap par grappes) : on tire des séances
entières, avec remise, et on recalcule la statistique ; la dispersion obtenue
tient compte de la dépendance à l'intérieur d'une séance.

- `cluster_ci` : intervalle à 95 % d'une statistique (part de prolongations,
  quantile de la continuation…).
- `diff_test` : écart entre deux groupes (A − B), son intervalle à 95 %
  (quantiles des tirages) et une p-value bilatérale (écart rapporté à
  l'écart type des tirages, loi normale).
- `holm` : correction pour tests multiples (Holm-Bonferroni) — sur tous les
  critères essayés, un écart qui ne survit pas à la correction peut être dû
  au hasard d'avoir beaucoup cherché.

Les statistiques sont des fonctions d'un tableau numpy de valeurs
(`share_at_least`, `quantile_of`). Pur, testé dans tests/test_edgestats.py.
"""
from __future__ import annotations

from math import erfc, sqrt
from typing import Callable

import numpy as np
import pandas as pd

N_BOOT = 2000
Stat = Callable[[np.ndarray], float]


def share_at_least(mark: float) -> Stat:
    return lambda v: 100.0 * float((v >= mark).mean()) if len(v) else np.nan


def quantile_of(q: float) -> Stat:
    return lambda v: float(np.quantile(v, q)) if len(v) else np.nan


def _clusters(df: pd.DataFrame, cluster: str) -> list[np.ndarray]:
    codes = pd.factorize(df[cluster])[0]
    order = np.argsort(codes, kind="stable")
    cuts = np.flatnonzero(np.diff(codes[order])) + 1
    return np.split(order, cuts)


def cluster_boot(df: pd.DataFrame, col: str, stat: Stat, n_boot: int = N_BOOT,
                 seed: int = 0, cluster: str = "day") -> np.ndarray:
    """Valeurs de `stat` sur `n_boot` tirages de séances avec remise."""
    if df.empty or df[cluster].nunique() < 2:
        return np.array([])
    vals = df[col].to_numpy(dtype=float)
    groups, rng = _clusters(df, cluster), np.random.default_rng(seed)
    out = np.empty(n_boot)
    for k in range(n_boot):
        ix = np.concatenate([groups[i] for i in rng.integers(0, len(groups), len(groups))])
        out[k] = stat(vals[ix])
    return out[np.isfinite(out)]


def cluster_ci(df: pd.DataFrame, col: str, stat: Stat, n_boot: int = N_BOOT,
               seed: int = 0, cluster: str = "day") -> tuple[float, float]:
    b = cluster_boot(df, col, stat, n_boot, seed, cluster)
    if len(b) < 20:
        return (np.nan, np.nan)
    return float(np.quantile(b, 0.025)), float(np.quantile(b, 0.975))


def diff_test(df: pd.DataFrame, group, a, b, col: str, stat: Stat, n_boot: int = N_BOOT,
              seed: int = 0, cluster: str = "day") -> dict:
    """Écart stat(A) − stat(B), séances rééchantillonnées ENSEMBLE (une séance
    tirée apporte ses cas des deux groupes). `group` aligné sur `df`."""
    g = np.asarray(group)
    keep = (g == a) | (g == b)
    d = df.loc[keep].reset_index(drop=True)
    is_a = g[keep] == a
    na, nb = int(is_a.sum()), int((~is_a).sum())
    out = {"n_a": na, "n_b": nb, "seances": int(d[cluster].nunique()) if len(d) else 0,
           "diff": np.nan, "ic_lo": np.nan, "ic_hi": np.nan, "p": np.nan}
    if na == 0 or nb == 0:
        return out
    vals = d[col].to_numpy(dtype=float)
    out["diff"] = float(stat(vals[is_a]) - stat(vals[~is_a]))
    if d[cluster].nunique() < 2:
        return out
    groups, rng = _clusters(d, cluster), np.random.default_rng(seed)
    boots = []
    for _ in range(n_boot):
        ix = np.concatenate([groups[i] for i in rng.integers(0, len(groups), len(groups))])
        m = is_a[ix]
        if m.any() and (~m).any():
            boots.append(stat(vals[ix][m]) - stat(vals[ix][~m]))
    boots = np.asarray(boots)
    boots = boots[np.isfinite(boots)]
    if len(boots) < 20:
        return out
    out["ic_lo"], out["ic_hi"] = (float(np.quantile(boots, 0.025)),
                                  float(np.quantile(boots, 0.975)))
    # p-value par l'approximation normale de l'écart (écart type des tirages) :
    # la part des tirages de l'autre côté de zéro bute sur 1 / n_boot dès que
    # l'écart est net, ce qui fausse la correction pour tests multiples
    sd = float(boots.std(ddof=1))
    out["p"] = float(erfc(abs(out["diff"]) / sd / sqrt(2))) if sd > 0 else (
        0.0 if out["diff"] else 1.0)
    return out


def holm(pvals) -> np.ndarray:
    """p-values ajustées (Holm-Bonferroni), dans l'ordre d'entrée ; NaN conservés."""
    p = np.asarray(pvals, dtype=float)
    ok = np.flatnonzero(np.isfinite(p))
    adj = np.full(p.shape, np.nan)
    if not len(ok):
        return adj
    order = ok[np.argsort(p[ok], kind="stable")]
    m, run = len(order), 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * p[i]))
        adj[i] = run
    return adj
