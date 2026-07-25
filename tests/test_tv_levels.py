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


def test_signe_du_gamma_donne_le_code_du_mur():
    # sans niveaux nommés, les murs sortent tels quels : le signe du gamma
    # décide de gpos/gneg, pas le rang
    s = tv_levels_string(_levels(), hvl=None, zg=None, keys=None)
    kinds = {e.split(",")[1]: e.split(",")[2] for e in s.split(";")}
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


def test_mur_deja_nomme_non_redouble():
    """Put Support et GEX2 peuvent désigner le même strike : le tracer deux
    fois superpose deux lignes et rend les étiquettes illisibles."""
    s = tv_levels_string(_levels(), hvl=None, zg=None, keys=KEYS)
    prices = [e.split(",")[0] for e in s.split(";")]
    assert len(prices) == len(set(prices)), "aucun prix en double"
    labels = [e.split(",")[1] for e in s.split(";")]
    # 21100 et 20800 sont déjà pris par Call Wall / Put Support
    assert "GEX1" not in labels and "GEX2" not in labels
    assert "Call Wall" in labels and "Put Support" in labels


def test_mur_collant_au_flip_absorbe():
    """Le flip tombe souvent à moins d'un point d'un mur : deux lignes y sont
    indiscernables, seule l'étiquette la plus parlante est gardée."""
    lv = pd.DataFrame({"strike": [7450.0], "gex": [-0.7e9], "rank": [1],
                       "expiry": [pd.Timestamp("2026-07-27")]})
    # flip à 7450.80, mur à 7450.00 : 0,8 pt d'écart sur ~7450
    s = tv_levels_string(lv, hvl=None, zg=7450.80, keys=None)
    assert s == "7450.80,Gamma Flip,flip"


def test_strikes_voisins_jamais_fusionnes():
    """La tolérance doit rester très en dessous de l'écart entre strikes,
    sinon on effacerait des murs bien réels."""
    lv = pd.DataFrame({"strike": [7450.0, 7425.0], "gex": [-0.7e9, -0.3e9],
                       "rank": [1, 2],
                       "expiry": [pd.Timestamp("2026-07-27")] * 2})
    s = tv_levels_string(lv, hvl=None, zg=None, keys=None)
    assert "7450.00,GEX1,gneg" in s and "7425.00,GEX2,gneg" in s


def test_murs_distincts_conserves():
    """Le dédoublonnage ne doit pas avaler les murs qui n'ont pas d'équivalent
    nommé — ce sont eux qui apportent l'information supplémentaire."""
    lv = _levels()
    lv.loc[len(lv)] = {"strike": 20900.0, "gex": 1.1e9, "rank": 3,
                       "expiry": pd.Timestamp("2026-07-27")}
    s = tv_levels_string(lv, hvl=None, zg=None, keys=KEYS)
    assert "20900.00,GEX3,gpos" in s


def test_libelles_sans_separateur():
    """Une virgule ou un point-virgule dans un libellé décalerait tout le
    parsing côté indicateur."""
    s = tv_levels_string(_levels(), hvl=20960.4, zg=20950.5, keys=KEYS)
    for entry in s.split(";"):
        assert entry.count(",") == 2
