"""Moteur de backtest de niveaux.

Les parcours sont construits à la main : on connaît la réponse attendue, ce
qui permet de vérifier les définitions elles-mêmes (tenu, cassé, clôturé
au-delà) plutôt que de constater qu'un chiffre sort.
"""
from __future__ import annotations

import numpy as np
import pytest
import pandas as pd

from gex import backtest as bt


def test_niveau_jamais_approche_nest_pas_teste():
    """Un niveau lointain ne doit pas compter comme ayant « tenu » : sinon les
    niveaux les plus inutiles afficheraient les meilleurs scores."""
    path = np.array([100.0, 101.0, 100.5, 101.5])
    o = bt.evaluate_level("Call Wall", 120.0, path, open_px=100.0)
    assert o.side == "resistance"
    assert not o.tested and not o.broke
    assert o.excursion_pct == 0.0


def test_resistance_touchee_mais_tenue():
    path = np.array([100.0, 104.0, 105.0, 103.0, 102.0])
    o = bt.evaluate_level("Call Wall", 105.0, path, open_px=100.0)
    assert o.tested            # le prix est venu au contact exact
    assert not o.broke         # sans marge franchie
    assert not o.closed_beyond


def test_touche_dun_tick_nest_pas_une_cassure():
    """Sans marge, le bruit de cotation transformerait chaque contact en
    rupture et le taux de tenue s'effondrerait artificiellement."""
    path = np.array([100.0, 105.0, 105.01, 104.0])
    o = bt.evaluate_level("Call Wall", 105.0, path, open_px=100.0)
    assert o.tested and not o.broke


def test_resistance_cassee_et_cloturee_au_dela():
    path = np.array([100.0, 104.0, 106.0, 107.0, 106.5])
    o = bt.evaluate_level("Call Wall", 105.0, path, open_px=100.0)
    assert o.tested and o.broke and o.closed_beyond
    assert o.excursion_pct == (107.0 - 105.0) / 105.0


def test_cassure_puis_retour_sous_le_niveau():
    """Cassé en séance mais clôturé en dessous : les deux informations sont
    distinctes et doivent le rester."""
    path = np.array([100.0, 106.0, 107.0, 103.0])
    o = bt.evaluate_level("Call Wall", 105.0, path, open_px=100.0)
    assert o.broke and not o.closed_beyond


def test_support_symetrique():
    path = np.array([100.0, 96.0, 94.0, 95.0])
    o = bt.evaluate_level("Put Support", 95.0, path, open_px=100.0)
    assert o.side == "support"
    assert o.tested and o.broke
    assert not o.closed_beyond          # clôture pile sur le niveau
    assert o.excursion_pct == (95.0 - 94.0) / 95.0


def test_move_apres_cassure_part_de_la_cassure():
    """Le parcours mesuré ne doit pas inclure ce qui précède la rupture."""
    path = np.array([100.0, 104.0, 106.0, 110.0, 108.0])
    o = bt.evaluate_level("Call Wall", 105.0, path, open_px=100.0)
    assert o.move_after_break_pct == (110.0 - 105.0) / 105.0


def test_sans_cassure_pas_de_move():
    path = np.array([100.0, 104.0, 104.5])
    o = bt.evaluate_level("Call Wall", 105.0, path, open_px=100.0)
    assert o.move_after_break_pct is None


def test_synthese_taux_conditionnels_au_test():
    """hold_rate se calcule sur les séances testées, pas sur toutes."""
    outcomes = [
        # testé et tenu
        bt.evaluate_level("CW", 105.0, np.array([100.0, 105.0, 102.0]), 100.0, day="j1"),
        # testé et cassé
        bt.evaluate_level("CW", 105.0, np.array([100.0, 108.0, 108.0]), 100.0, day="j2"),
        # jamais approché
        bt.evaluate_level("CW", 105.0, np.array([100.0, 101.0, 100.5]), 100.0, day="j3"),
    ]
    s = bt.summarize(outcomes).iloc[0]
    assert s["n_sessions"] == 3
    assert s["n_tested"] == 2
    assert s["test_rate"] == 2 / 3
    assert s["hold_rate"] == 0.5      # 1 tenue sur 2 tests, pas sur 3 séances
    assert s["break_rate"] == 0.5


def test_synthese_vide():
    out = bt.summarize([])
    assert out.empty and "hold_rate" in out.columns


def test_session_ignore_les_niveaux_absents():
    path = np.array([100.0, 101.0])
    res = bt.evaluate_session({"a": 105.0, "b": None, "c": float("nan")}, path)
    assert [o.name for o in res] == ["a"]


def test_parcours_trop_court_ignore():
    assert bt.evaluate_session({"a": 105.0}, np.array([100.0])) == []


# --- comparaison des définitions de murs (10/10/2026) ---------------------------

def test_comparaison_des_definitions_de_murs(tmp_path, monkeypatch):
    """Séance construite : la concentration de calls est à 105 (compensée par
    des puts : GEX net négatif), l'ancienne définition (net) désigne 110. Le
    prix monte toucher 105 puis recule : nouvelle définition testée et tenue,
    ancienne jamais approchée."""
    from datetime import datetime, timedelta
    import sys
    from pathlib import Path
    from gex import metrics, store
    from gex.config import SETTINGS
    from tests.test_metrics import make_chain
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    exp = (datetime(2026, 10, 7) + timedelta(days=30)).date()
    rows = [{"expiry": exp, "type": t, "strike": float(k), "open_interest": float(oi)}
            for t, k, oi in (("C", 105, 5000), ("P", 105, 6000), ("C", 110, 800),
                             ("P", 95, 300))]
    df = metrics.enrich(make_chain(100.0, rows))
    df["spot"] = 100.0
    store.save_snapshot("NQ", df, datetime(2026, 10, 7, 8, 0))
    up = list(np.linspace(100.0, 105.0, 100)) + list(np.linspace(105.0, 101.0, 100))
    bars = [{"timestamp": datetime(2026, 10, 7, 9, 30) + timedelta(minutes=i),
             "open": p, "high": p, "low": p, "close": p, "source": "dxfeed"}
            for i, p in enumerate(up)]
    store.append_prices("NQ", bars, datetime(2026, 10, 7))
    res = bt.compare_walls("NQ", days=["2026-10-07"], bucket="Tout")
    by = res.set_index("name")
    assert by.loc["Call Wall (concentration)", "level"] == 105.0
    assert by.loc["Call Wall (net, ancien)", "level"] == 110.0
    assert bool(by.loc["Call Wall (concentration)", "tested"])
    assert not bool(by.loc["Call Wall (concentration)", "broke"])
    assert not bool(by.loc["Call Wall (net, ancien)", "tested"])
    assert bool(by.loc["Call Wall (concentration)", "definitions_differ"])
    s = bt.summarize_walls(res).set_index("name")
    assert s.loc["Call Wall (concentration)", "hold_rate"] == 1.0
    assert s.loc["Call Wall (concentration)", "median_distance_pct"] == pytest.approx(0.05)
    # rapport du script : lisible de bout en bout, sans écrire de données
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import backtest_walls
    md = backtest_walls.report("NQ", ["Tout"])
    assert "Call Wall (concentration)" in md and "diffèrent" in md


def test_comparaison_ignore_week_end_et_jours_sans_bougies(tmp_path, monkeypatch):
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    assert bt.compare_walls("NQ", days=["2026-10-10", "2026-10-07"]).empty
