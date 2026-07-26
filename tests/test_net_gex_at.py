"""GEX net recalculé à un spot arbitraire.

Cette fonction remplace, en séance, le GEX net figé au spot délayé de 15 min.
Si elle dérive, on lit un régime de marché qui n'est pas celui en cours.
"""
from __future__ import annotations

import numpy as np
import pytest
import pandas as pd

from gex import metrics


def _chain(spot: float = 7400.0) -> pd.DataFrame:
    """Chaîne d'indice réaliste : plus d'open interest sur les puts.

    Une chaîne strictement symétrique donnerait un GEX net nul partout — calls
    et puts de même strike ont le même gamma et se compensent exactement.
    """
    rows = []
    for k in np.arange(spot - 200, spot + 201, 50):
        for typ, oi in (("C", 800.0), ("P", 1200.0)):
            rows.append({"strike": float(k), "type": typ, "iv": 0.15,
                         "t_years": 0.02, "open_interest": oi,
                         "volume": 100.0})
    return pd.DataFrame(rows)


def test_coherent_avec_le_profil():
    """Le point isolé doit valoir ce que donne la grille au même endroit :
    c'est ce qui garantit que GEX net et Gamma Flip parlent du même profil."""
    df = _chain()
    grid, profile = metrics.gamma_profile(df, 7400.0)
    i = int(np.argmin(np.abs(grid - 7400.0)))
    assert metrics.net_gex_at(df, float(grid[i])) == pytest.approx(profile[i])


def test_varie_significativement_avec_le_spot():
    """C'est tout l'intérêt : le GEX net suit le spot même à chaîne figée.

    Sans cette dépendance, recalculer n'apporterait rien et le spot délayé de
    15 min suffirait. On vérifie que l'effet est matériel, pas résiduel.
    """
    df = _chain()
    ref = metrics.net_gex_at(df, 7400.0)
    ecarte = metrics.net_gex_at(df, 7300.0)
    assert abs(ecarte - ref) / abs(ref) > 0.05


def test_profil_maximal_au_coeur_des_strikes():
    """Le gamma agrégé culmine là où se concentrent les strikes : le profil
    forme une cloche, il n'est pas monotone. (Une version antérieure de ce
    test supposait l'inverse.)"""
    df = _chain()
    vals = [metrics.net_gex_at(df, s) for s in (7300, 7350, 7400, 7450, 7500)]
    assert vals[2] == min(vals)          # creux au centre (net négatif)
    assert vals[0] > vals[1] and vals[4] > vals[3]


def test_chaine_vide():
    assert metrics.net_gex_at(pd.DataFrame(
        {"strike": [], "type": [], "iv": [], "t_years": [],
         "open_interest": [], "volume": []}), 7400.0) is None


def test_iv_nulle_ignoree():
    """Une IV absente rendrait le gamma BS indéfini : ces lignes sont exclues,
    comme dans le calcul du Gamma Flip — les deux restent cohérents."""
    df = _chain()
    df.loc[df["type"] == "C", "iv"] = 0.0
    only_puts = metrics.net_gex_at(df, 7400.0)
    assert only_puts is not None and only_puts < 0   # puts seuls => négatif
