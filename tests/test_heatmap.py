"""Profil de gamma par strike, pondéré open interest ou volume.

Les deux pondérations racontent des choses différentes : l'open interest décrit
le positionnement installé, le volume ce qui se traite en séance. Le profil les
superpose, d'où l'importance que les deux se calculent de la même façon.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from gex import metrics


def _chain() -> pd.DataFrame:
    return pd.DataFrame({
        "strike": [7400.0, 7400.0, 7450.0, 7450.0],
        "type": ["C", "P", "C", "P"],
        "gamma_bs": [2e-4, 2e-4, 1e-4, 1e-4],
        "open_interest": [1000.0, 3000.0, 500.0, 500.0],
        "volume": [100.0, 100.0, 900.0, 100.0],
    })


def test_calls_positifs_puts_negatifs():
    s = metrics.gex_by_strike_weighted(_chain(), 7400.0)
    # 7400 : 1000 calls contre 3000 puts -> net négatif
    assert s.loc[7400.0] < 0
    # 7450 : autant de calls que de puts, même gamma -> exactement nul
    assert s.loc[7450.0] == 0.0


def test_agregation_par_strike():
    s = metrics.gex_by_strike_weighted(_chain(), 7400.0)
    assert list(s.index) == [7400.0, 7450.0]


def test_ponderation_volume_differe_de_open_interest():
    """C'est tout l'intérêt de tracer les deux : sur 7450, le volume penche
    côté calls alors que l'open interest y est équilibré."""
    df = _chain()
    oi = metrics.gex_by_strike_weighted(df, 7400.0, "open_interest")
    vol = metrics.gex_by_strike_weighted(df, 7400.0, "volume")
    assert oi.loc[7450.0] == 0.0
    assert vol.loc[7450.0] > 0.0


def test_proportionnel_au_carre_du_spot():
    """GEX = γ × poids × 100 × spot² × 1 % : doubler le spot quadruple."""
    df = _chain()
    a = metrics.gex_by_strike_weighted(df, 1000.0).loc[7400.0]
    b = metrics.gex_by_strike_weighted(df, 2000.0).loc[7400.0]
    assert b == 4 * a


def test_chaine_vide():
    vide = pd.DataFrame({"strike": [], "type": [], "gamma_bs": [],
                         "open_interest": [], "volume": []})
    assert metrics.gex_by_strike_weighted(vide, 7400.0).empty


def test_colonne_de_ponderation_absente():
    """Un snapshot sans colonne volume ne doit pas faire tomber le graphique."""
    df = _chain().drop(columns=["volume"])
    assert metrics.gex_by_strike_weighted(df, 7400.0, "volume").empty
