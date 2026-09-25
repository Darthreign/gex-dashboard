"""Bilan de fin de séance (gex/eodreview) : détection des mouvements propres,
départ avant/après 16h15, lecture de la qualification d'un brief et notation.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from gex import eodreview as e


def _bars(closes, start="2026-09-23 09:30") -> pd.DataFrame:
    """Séance synthétique : 1 bougie/min à partir de 9h30 ET ; high/low = close +/- 1."""
    c = np.asarray(closes, dtype=float)
    ts = pd.date_range(start, periods=len(c), freq="min")
    return pd.DataFrame({"timestamp": ts, "open": c, "high": c + 1.0,
                         "low": c - 1.0, "close": c})


def _flat_then_ramp(n=390, ramp_start=0, ramp_pts=300.0, ramp_len=60, base=20000.0):
    c = np.full(n, base)
    c[ramp_start:ramp_start + ramp_len] = base + np.linspace(0, ramp_pts, ramp_len)
    c[ramp_start + ramp_len:] = base + ramp_pts
    return c


def test_mouvement_propre_detecte_avec_signe_et_indices():
    legs = e.clean_legs(_flat_then_ramp(ramp_pts=300.0))
    big = max(legs, key=lambda g: abs(g.pts))
    assert big.pts == 300.0 and big.start == 0


def test_baisse_propre_est_negative():
    legs = e.clean_legs(_flat_then_ramp(ramp_pts=-260.0))
    assert max(legs, key=lambda g: abs(g.pts)).pts == -260.0


def test_recul_a_linterieur_exclut_le_mouvement_complet():
    """0 -> 100, recul de 80, puis 20 -> 200 : le mouvement complet (0 -> 200) a un
    retracement interne de 40 % (> 30 %), il n'est pas « propre ». Reste le tronçon
    20 -> 200 (180 pts, sans recul). Un recul APRÈS le sommet, lui, ne compte pas."""
    c = np.concatenate([np.linspace(0, 100, 20), np.linspace(100, 20, 15),
                        np.linspace(20, 200, 30), np.full(325, 200.0)]) + 20000.0
    legs = e.clean_legs(c)
    assert max(abs(g.pts) for g in legs) == 180.0


def test_recul_apres_le_sommet_ne_compte_pas():
    c = np.concatenate([np.linspace(0, 200, 30), np.linspace(200, 100, 20),
                        np.full(340, 100.0)]) + 20000.0
    assert max(abs(g.pts) for g in e.clean_legs(c)) == 200.0


def test_sous_le_seuil_aucune_phase():
    f = e.summarize_session(_bars(_flat_then_ramp(ramp_pts=120.0)))
    assert f["dir_tier"] == "aucune" and f["dir_early"] == 0 and f["dir_pts"] == 0.0


def test_niveaux_phase_et_journee():
    phase = e.summarize_session(_bars(_flat_then_ramp(ramp_pts=180.0)))
    jour = e.summarize_session(_bars(_flat_then_ramp(ramp_pts=300.0)))
    assert phase["dir_tier"] == "phase" and jour["dir_tier"] == "journee"


def test_depart_precoce_avant_1615_paris():
    """Départ à 9h30 ET (15h30 Paris) : dans la fenêtre contrarienne."""
    f = e.summarize_session(_bars(_flat_then_ramp(ramp_start=0, ramp_pts=300.0)))
    assert f["dir_early"] == 1 and f["dir_debut_avant_1615"] == 1
    assert f["dir_debut"] == "09:30"


def test_depart_tardif_apres_1615_paris():
    """Départ à 12h26 ET (cas du 18/09) : le contrarien avait sa matinée."""
    start = 176                                    # 9h30 + 176 min = 12h26
    f = e.summarize_session(_bars(_flat_then_ramp(ramp_start=start, ramp_pts=267.0,
                                                   ramp_len=100)))
    assert f["dir_early"] == 0 and f["dir_debut_avant_1615"] == 0
    assert f["dir_debut"] == "12:26" and f["dir_tier"] == "journee"


def test_seance_lacunaire_non_evaluee():
    assert e.summarize_session(_bars(np.full(100, 20000.0))) is None


def test_hors_seance_us_ignore():
    """Les bougies de nuit (avant 9h30 ET) ne comptent pas dans la séance."""
    nuit = _bars(np.linspace(20000, 20400, 120), start="2026-09-23 03:00")
    seance = _bars(np.full(390, 20400.0))
    f = e.summarize_session(pd.concat([nuit, seance], ignore_index=True))
    assert f["dir_tier"] == "aucune"


BRIEF = """## ⚠️ PRÉCAUTION TRADING

🟠 **EXPANSION POSSIBLE — NON CONFIRMÉE**

**FACTEURS** ... la carte 🟢 n'est pas la conclusion.
"""


def test_qualification_lue_apres_la_precaution_trading():
    assert e.parse_qualification(BRIEF) == "EXPANSION_POSSIBLE"


def test_qualification_absente():
    assert e.parse_qualification("Pas de conclusion ici") is None
    assert e.parse_qualification(None) is None


def test_notation_des_briefs():
    dirs = {"dir_tier": "journee", "dir_early": 1}
    calme = {"dir_tier": "aucune", "dir_early": 0}
    tardif = {"dir_tier": "journee", "dir_early": 0}      # type 18/09
    assert e.score_brief("EXPANSION_CONFIRME", dirs) == 1
    assert e.score_brief("EXPANSION_POSSIBLE", calme) == 0
    assert e.score_brief("MEAN_REVERSION", calme) == 1
    assert e.score_brief("MEAN_REVERSION", dirs) == 0
    # mean-reversion annoncée, mouvement TARDIF : la fenêtre contrarienne était bonne
    assert e.score_brief("MEAN_REVERSION", tardif) == 1
    assert e.score_brief("MIXTE", dirs) is None
    assert e.score_brief("INSUFFISANT", dirs) is None
    assert e.score_brief(None, dirs) is None


def test_build_metrics_complet_et_bilan():
    bars = _bars(_flat_then_ramp(ramp_pts=300.0))
    m = e.build_metrics(bars, {"prebrief": BRIEF}, prev_atr=350.0)
    assert m["dir_tier"] == (None, "journee")
    assert m["brief_prebrief_qualif"] == (None, "EXPANSION_POSSIBLE")
    assert m["brief_prebrief_score"] == (1.0, None)
    assert m["range_over_prev_atr"][0] > 0
    assert m["eod_thresholds_version"] == (None, e.THRESHOLDS_VERSION)
    md = e.review_markdown("2026-09-23", m)
    assert "Journée directionnelle" in md and "prebrief" in md and "vérifié" in md


def test_build_metrics_seance_non_evaluable():
    assert e.build_metrics(_bars(np.full(50, 1.0))) is None
