"""Sérialisation des niveaux pour l'indicateur TradingView.

Le format est imposé par l'indicateur tiers (« GEX Levels — Dealer Gamma
Exposure ») : ``prix,libellé,type;...``. Un écart silencieux sur le séparateur
ou les codes de type casserait le collage sans erreur visible, d'où ces tests.
"""
from __future__ import annotations

import pandas as pd

from gex.app import tv_levels_string

KEYS = {"call_wall": 21100.0, "put_support": 20800.0,
        "d1_min": 20870.0, "d1_max": 21030.0}


def _levels() -> pd.DataFrame:
    return pd.DataFrame({
        "strike": [21100.0, 20800.0],
        "gex": [3.2e9, -2.1e9],
        "rank": [1, 2],
        "expiry": [pd.Timestamp("2026-07-27")] * 2,
    })


def test_format_et_codes_de_type():
    s = tv_levels_string(_levels(), hvl=20960.4, zg=20950.5, keys=KEYS)
    entries = [e.split(",") for e in s.split(";")]
    assert all(len(e) == 3 for e in entries), "chaque entrée = prix,libellé,type"
    kinds = {e[1]: e[2] for e in entries}
    assert kinds["Gamma Flip"] == "flip"
    assert kinds["Call Wall"] == "res"
    assert kinds["Put Support"] == "sup"
    assert kinds["1D Max"] == "emh"
    assert kinds["1D Min"] == "eml"
    # murs GEX : le signe du gamma décide du code, pas le rang
    assert kinds["GEX1"] == "gpos"
    assert kinds["GEX2"] == "gneg"


def test_hvl_est_une_bascule():
    # HVL n'a pas de code dédié côté indicateur : c'est bien un flip, pondéré
    # par le volume du jour au lieu de l'open interest.
    s = tv_levels_string(None, hvl=20960.4, zg=None, keys=None)
    assert s == "20960.40,HVL,flip"


def test_transposition_appliquee():
    """La chaîne sort dans l'échelle affichée : coller des niveaux d'indice sur
    un graphique ES les placerait décalés du basis."""
    s = tv_levels_string(None, hvl=None, zg=20950.5, keys=None,
                         xf=lambda v: v + 35.25)
    assert s.startswith("20985.75,")


def test_valeurs_absentes_ignorees():
    s = tv_levels_string(None, hvl=None, zg=20950.5, keys={"call_wall": None})
    assert s == "20950.50,Gamma Flip,flip"
    assert tv_levels_string(None, None, None, None) == ""


def test_libelles_sans_separateur():
    """Une virgule ou un point-virgule dans un libellé décalerait tout le
    parsing côté indicateur."""
    s = tv_levels_string(_levels(), hvl=20960.4, zg=20950.5, keys=KEYS)
    for entry in s.split(";"):
        assert entry.count(",") == 2
