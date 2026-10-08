"""Lecture normalisée /scalp v2 et banc de mesure d'edge (gex/edge.py)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gex import edge

P = edge.EdgeParams()


def test_zone_de_regime_par_rapport_au_flip():
    assert edge.regime_zone(110.0, 100.0, 20.0, 1.0, 0.15)[0] == "frein"
    assert edge.regime_zone(90.0, 100.0, 20.0, -1.0, 0.15)[0] == "accelerateur"
    assert edge.regime_zone(101.0, 100.0, 20.0, 1.0, 0.15)[0] == "transition"
    # au-dessus du flip mais GEX 0DTE négatif : les mesures se contredisent
    assert edge.regime_zone(110.0, 100.0, 20.0, -1.0, 0.15)[0] == "transition"
    assert edge.regime_zone(100.0, None, 20.0, -1.0, 0.15) == ("accelerateur", None)


def test_excès_en_unites_de_mouvement_attendu():
    r = edge.reading(115.0, 100.0, 20.0, 90.0, 1.0, [], 0.0, None, None, P)
    assert r["ext_em"] == pytest.approx(0.75) and r["excess_dir"] == 1
    assert r["setup"] == "fade" and r["fade_dir"] == -1 and r["confirmations"] == ["frein"]
    # même écart en points, EM deux fois plus grand : plus un excès
    assert edge.reading(115.0, 100.0, 40.0, 90.0, 1.0, [], 0.0, None, None, P)["excess_dir"] == 0


def test_absorption_et_flux_confirment_l_epuisement():
    absorb = [{"ts": 900.0, "price": 115.5, "side": "BUY"}]       # acheteurs absorbés en haut
    r = edge.reading(115.0, 100.0, 20.0, None, None, absorb, 1000.0, -60.0, 100.0, P)
    assert set(r["confirmations"]) >= {"absorption", "flux"}
    # absorption de vendeurs (support) : ne confirme PAS l'épuisement d'une hausse
    r = edge.reading(115.0, 100.0, 20.0, None, None,
                     [{"ts": 900.0, "price": 115.5, "side": "SELL"}], 1000.0, None, None, P)
    assert "absorption" not in r["confirmations"]


def test_zone_d_acceleration_marque_le_setup_a_eviter():
    r = edge.reading(85.0, 100.0, 20.0, 95.0, -1.0, [], 0.0, None, None, P)
    assert r["excess_dir"] == -1 and r["setup"] == "avoid" and r["fade_dir"] == 0


def test_simulation_cible_stop_et_temps():
    p = edge.EdgeParams(target_em=0.5, stop_em=0.5, horizon_min=3)
    hi, lo, cl = np.array([101, 106]), np.array([99, 100]), np.array([100, 105])
    assert edge.simulate_trade(hi, lo, cl, 100.0, 1, 10.0, p)["exit"] == "target"
    # cible et stop dans la même barre : le stop compte
    assert edge.simulate_trade(np.array([106]), np.array([94]), np.array([100]),
                               100.0, 1, 10.0, p)["pnl_em"] == -0.5
    r = edge.simulate_trade(np.array([102, 102]), np.array([99, 99]), np.array([101, 102]),
                            100.0, 1, 10.0, p)
    assert r["exit"] == "time" and r["pnl_em"] == pytest.approx(0.2)


def _bars(prices, t0=1_760_000_000.0):
    p = np.asarray(prices, dtype=float)
    return pd.DataFrame({"ts": t0 + 60 * np.arange(len(p)), "open": p, "high": p + 0.5,
                         "low": p - 0.5, "close": p})


def test_un_marche_qui_revient_a_la_moyenne_donne_un_edge_au_setup():
    # pic rapide à +1,5 EM puis retour : le rejet entré au pic gagne
    path = [100] * 20 + list(np.linspace(100, 130, 4)) + list(np.linspace(130, 100, 60)) + [100] * 60
    ctx = pd.DataFrame({"ts": [0.0], "zg": [80.0], "gex0": [1.0]})
    trades = pd.DataFrame(edge.run_session(_bars(path), 100.0, 20.0, ctx, [], None, P))
    setup = trades[trades["kind"] == "setup"]
    assert len(setup) >= 2 and setup["pnl_em"].sum() > 0
    assert set(trades["kind"]) == {"setup", "naive", "random"}


def test_resume_par_famille_avec_intervalle():
    t = pd.DataFrame({"kind": ["setup"] * 4 + ["naive"] * 4, "day": ["a", "a", "b", "b"] * 2,
                      "pnl_em": [0.3, 0.2, 0.3, -0.1, 0.1, -0.2, 0.0, 0.1],
                      "mfe_em": 0.3, "mae_em": 0.1})
    s = edge.summarize(t, n_boot=200)
    assert s.loc["setup", "n"] == 4 and s.loc["setup", "expectancy_em"] == pytest.approx(0.175)
    assert s.loc["setup", "ci90_lo"] <= s.loc["setup", "expectancy_em"] <= s.loc["setup", "ci90_hi"]


def test_parametres_valides_lus_sinon_defauts(tmp_path):
    p, ok = edge.load_params(tmp_path / "absent.json")
    assert not ok and p == edge.EdgeParams()
    f = tmp_path / "p.json"
    f.write_text('{"version": "edge-v1", "excess_em": 0.9, "require": 2, "validated": true}')
    p, ok = edge.load_params(f)
    assert ok and p.excess_em == 0.9 and p.require == 2
