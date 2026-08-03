"""Pinning de clôture — le prix se colle-t-il sur un strike / un mur GEX à 22h
(16h ET) ?

Hypothèse n°1 du labo : plus le gamma est concentré sur un strike, plus les
dealers, en se couvrant, tendent à « aimanter » le prix vers ce strike à
l'approche de la clôture. Pertinent surtout sur le cash et les ETF (SPX, NDX,
SPY, QQQ — enchère MOC + options qui expirent), les futures NQ/ES ne « ferment »
pas mais réagissent.

⚠️ Calcul **dérivé**, à la demande, depuis le brut déjà conservé (snapshot de
chaîne le plus proche de 16h + bougies) — conforme à la règle « recalculable →
pas stocké ». Aucun de ces chiffres n'est figé en base : ils se recalculent tant
que les sources sont là.

Fonctions pures et testables ; le chargement des données (snapshot, bougies)
est fait par l'appelant (l'endpoint API).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def strike_spacing(strikes) -> float | None:
    """Écartement typique entre strikes (médiane des écarts positifs)."""
    s = np.sort(np.unique(np.asarray(list(strikes), dtype=float)))
    if len(s) < 2:
        return None
    diffs = np.diff(s)
    diffs = diffs[diffs > 0]
    return float(np.median(diffs)) if len(diffs) else None


def top_walls(chain_df: pd.DataFrame, n: int = 2) -> list[tuple[float, float]]:
    """Les `n` strikes au plus gros gamma agrégé (en valeur absolue) :
    (strike, gex). Auto-suffisant, pour ne dépendre d'aucun filtrage externe."""
    by = chain_df.groupby("strike")["gex"].sum()
    order = by.abs().sort_values(ascending=False).index
    return [(float(k), float(by[k])) for k in order[:n]]


def _dist(a, b):
    return round(abs(a - b), 2) if a is not None and b is not None else None


def pin_metrics(chain_df: pd.DataFrame, close_price: float,
                window_closes: list[float] | None = None) -> dict:
    """Métriques de pinning pour une clôture donnée.

    - `chain_df` : chaîne enrichie (colonnes `strike`, `gex`) du snapshot ~16h ;
    - `close_price` : prix de clôture 16h ET du sous-jacent ;
    - `window_closes` : clôtures 1 min de la fenêtre pré-clôture (15h50-16h ET),
      pour compter les franchissements de strike (optionnel).

    `pin_ratio` : distance au strike le plus proche / (espacement / 2) →
    **0 = collé pile sur un strike, 1 = à mi-chemin entre deux**. C'est la mesure
    directe du pinning : sur des mois, sa distribution dit si le marché se colle.
    """
    strikes = sorted(float(s) for s in pd.unique(chain_df["strike"]))
    spacing = strike_spacing(strikes)
    nearest = min(strikes, key=lambda k: abs(k - close_price)) if strikes else None
    dist_ns = _dist(close_price, nearest)
    pin_ratio = (round(dist_ns / (spacing / 2), 3)
                 if dist_ns is not None and spacing else None)

    walls = top_walls(chain_df, 2)
    gex1 = walls[0][0] if len(walls) > 0 else None
    gex2 = walls[1][0] if len(walls) > 1 else None

    crossings = None
    if window_closes and spacing:
        idx = [int(round(c / spacing)) for c in window_closes]
        crossings = int(sum(abs(idx[i + 1] - idx[i]) for i in range(len(idx) - 1)))

    return {
        "close": round(float(close_price), 2),
        "nearest_strike": nearest,
        "dist_nearest_strike": dist_ns,
        "strike_spacing": spacing,
        "pin_ratio": pin_ratio,
        "closing_strike": nearest,          # le strike sur lequel ça a clôturé
        "gex1_strike": gex1, "dist_gex1": _dist(close_price, gex1),
        "gex2_strike": gex2, "dist_gex2": _dist(close_price, gex2),
        "strike_crossings_preclose": crossings,
    }
