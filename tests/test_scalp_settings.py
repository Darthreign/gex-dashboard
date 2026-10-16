"""Réglages fins du graphique /scalp (2026-10-16) : swings sur les mèches,
seuil de swing réglable, fusion des nœuds HVN/LVN en zones."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from gex import app
from gex.bars import zigzag


def _bars(rows):
    """rows : (open, high, low, close) ; ts_close = indice."""
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["ts_close"] = df.index.astype(float)
    df["ts_open"] = df["ts_close"]
    return df


def test_swing_sur_les_meches_et_non_les_corps():
    # sommet en MÈCHE à 120 (corps max 105), puis baisse ; creux en mèche à 80
    df = _bars([(100, 101, 99, 100), (100, 120, 99, 105), (105, 106, 95, 96),
                (96, 97, 80, 90), (90, 104, 89, 103), (103, 104, 100, 101)])
    corps = zigzag(df, min_move=10)
    meches = zigzag(df, min_move=10, wicks=True)
    assert corps.iloc[0]["price"] == 105                    # ancien comportement
    h = meches[meches["kind"] == "H"].iloc[0]
    lo = meches[meches["kind"] == "L"].iloc[-1]
    assert (h["price"], h["ts"]) == (120, 1.0)
    assert (lo["price"], lo["ts"]) == (80, 3.0)


def test_une_seule_barre_ne_fait_pas_sommet_et_retournement():
    # barre 1 : nouveau plus haut 130 ET plus bas 100 dans la même barre —
    # l'ordre haut/bas n'est pas connu : aucun pivot confirmé par elle seule
    rows = [(110, 111, 109, 110), (110, 130, 100, 105)]
    seule = zigzag(_bars(rows), min_move=20, wicks=True)
    assert seule["kind"].str.endswith("?").all()
    # la barre suivante (plus bas 104, 26 pts sous 130) confirme le sommet
    suite = zigzag(_bars(rows + [(105, 106, 104, 105)]), min_move=20, wicks=True)
    assert suite.iloc[0].to_dict() == {"ts": 1.0, "price": 130.0, "kind": "H"}


def test_mode_clotures_inchange():
    import numpy as np
    rng = np.random.default_rng(3)
    c = 100 + np.cumsum(rng.normal(0, 2, 400))
    df = pd.DataFrame({"close": c, "ts_close": np.arange(400.0)})
    a = zigzag(df, min_move=8)
    # même résultat qu'avec high = low = close en mode mèches
    b = zigzag(df.assign(high=c, low=c), min_move=8, wicks=True)
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True),
                                  check_dtype=False)


def test_swing_param_borne_et_arrondi():
    assert app.swing_param(None) is None and app.swing_param("") is None
    assert app.swing_param("0") is None and app.swing_param("-3") is None
    assert app.swing_param("12.3") == 12.25
    assert app.swing_param("5000") == 1000.0


def test_canal_distinct_par_seuil_de_swing():
    a = app.scalp_chart_channel("NQ", "t1", None)
    b = app.scalp_chart_channel("NQ", "t1", 20.0)
    assert a is not b and app.scalp_chart_channel("NQ", "t1", 20.0) is b


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_fusion_hvn_lvn_en_zones():
    src = (Path(app.__file__).parent / "assets" / "gex-orderflow-profile.js").read_text()
    fn = src[src.index("window.GexMergeUntested"):src.rindex("};") + 2]
    nodes = [{"price": 31050, "kind": "hvn"}, {"price": 31040, "kind": "hvn"},
             {"price": 31041, "kind": "lvn"}, {"price": 31042, "kind": "hvn"},
             {"price": 30900, "kind": "hvn"}, {"price": 31043, "kind": "lvn"}]
    js = ("global.window={};" + fn + f";const n={json.dumps(nodes)};"
          "console.log(JSON.stringify([window.GexMergeUntested(n,2,20),"
          "window.GexMergeUntested(n,0,20),window.GexMergeUntested(n,2,2)]));")
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True,
                                    check=True).stdout)
    fus, brut, limite = out
    hvn = [z for z in fus if z["kind"] == "hvn"]
    assert {(z["low"], z["high"], z["n"]) for z in hvn} == {(31050, 31050, 1),
                                                            (31040, 31042, 2),
                                                            (30900, 30900, 1)}
    lvn = [z for z in fus if z["kind"] == "lvn"]
    assert [(z["low"], z["high"]) for z in lvn] == [(31041, 31043)]   # jamais mêlé aux HVN
    assert len(brut) == 6                                   # 0 : aucune fusion
    assert [z["price"] for z in limite] == [31050, 31041]   # les plus récentes d'abord
