"""Zones de confluence (gex/confluence.py, versionné le 2026-10-26) et leur
regroupement réglable côté navigateur (assets/gex-confluence.js)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from gex import app, confluence


def test_regroupement_de_proche_en_proche():
    z = confluence.cluster_levels({"a": 100.0, "b": 140.0, "c": 185.0, "d": 300.0}, "NQ")
    assert [sorted(x.levels) for x in z] == [["a", "b", "c"], ["d"]]   # 40 puis 45 <= 50
    assert z[0].width == 85.0
    assert confluence.supported("ES") and not confluence.supported("SPX")


def _serveur(levels: dict[str, float], symbol: str) -> list[tuple]:
    """Même filtre d'affichage que app.scalp_confluence_zones."""
    zones = confluence.cluster_levels(levels, symbol)
    width = confluence.CLUSTER_POINTS[symbol] * 3
    kept = [((z.low + z.high) / 2, len(z)) for z in zones if len(z) >= 2 and z.width <= width]
    kept.sort(key=lambda z: -z[1])
    return [(round(p, 6), n) for p, n in kept[:10]]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_le_navigateur_retrouve_les_zones_du_serveur(seed):
    rng = np.random.default_rng(seed)
    fams = ["SPX", "NDX", "QQQ", "SPY", "NQ", "ES"]
    levels = {f"{rng.choice(fams)}:{k}": float(round(30000 + rng.uniform(-800, 800), 2))
              for k in range(60)}
    payload = [{"name": k, "family": k.split(":")[0], "price": v} for k, v in levels.items()]
    src = (Path(app.__file__).parent / "assets" / "gex-confluence.js").read_text()
    js = ("global.window={};" + src + f";const L={json.dumps(payload)};"
          "console.log(JSON.stringify(window.GexClusterConfluence(L,50,2,null,10)"
          ".map(z=>[z.price,z.n])));")
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True,
                                    check=True).stdout)
    assert [(round(p, 6), n) for p, n in out] == _serveur(levels, "NQ")
    # filtre par indice : sans SPX/SPY/ES, seuls les niveaux Nasdaq comptent
    js2 = js.replace("GexClusterConfluence(L,50,2,null,10)",
                     "GexClusterConfluence(L,50,2,['NDX','QQQ','NQ'],10)")
    out2 = json.loads(subprocess.run(["node", "-e", js2], capture_output=True, text=True,
                                     check=True).stdout)
    nasdaq = {k: v for k, v in levels.items() if k.split(":")[0] in ("NDX", "QQQ", "NQ")}
    assert [(round(p, 6), n) for p, n in out2] == _serveur(nasdaq, "NQ")


def test_niveaux_publies_avant_regroupement(monkeypatch):
    import pandas as pd
    app._CONFLUENCE_CACHE.clear()
    app._CONFLUENCE_LEVELS_CACHE.clear()

    class St:
        enriched = pd.DataFrame({"strike": [1.0]})
        snapshot = type("S", (), {"spot": 30000.0})()
    monkeypatch.setattr(app, "chain_state", lambda s: St())
    monkeypatch.setattr(confluence, "collect_levels",
                        lambda df, spot: {"call_wall": 30100.0, "zero_gamma": 29950.0})
    monkeypatch.setattr(app, "_transform_for", lambda src, sym: ((lambda v: v + 10), 1.0, "basis"))
    zones = app.scalp_confluence_zones("NQ")
    lv = app.scalp_confluence_levels("NQ")
    assert lv["gap"] == 50.0
    noms = {l["name"]: l["price"] for l in lv["levels"]}
    assert noms["NQ:call_wall"] == 30100.0 and noms["SPX:call_wall"] == 30110.0  # transposé
    assert {l["family"] for l in lv["levels"]} == {"SPX", "NDX", "QQQ", "SPY", "NQ", "ES"}
    assert zones and zones[0]["rank"] == 1
    assert "confluence_levels" in app._INDICATOR_CACHES                       # publié par le moteur
    app._CONFLUENCE_CACHE.clear()
    app._CONFLUENCE_LEVELS_CACHE.clear()
