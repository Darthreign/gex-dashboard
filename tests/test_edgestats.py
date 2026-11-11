"""Tests statistiques du rapport d'excès (gex/edgestats.py) et section
« Le hasard peut-il l'expliquer ? » (scripts/excess_report.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gex import edgestats as es


def test_holm_valeurs_connues():
    adj = es.holm([0.01, 0.04, 0.03, np.nan, 0.005])
    # tri : 0.005 (×4), 0.01 (×3), 0.03 (×2), 0.04 (×1), monotone
    assert np.allclose(adj[[4, 0, 2, 1]], [0.02, 0.03, 0.06, 0.06])
    assert np.isnan(adj[3])


def _seances(n_days, per_day, effet_jour, seed):
    """Prolongations par séance : un effet COMMUN à tous les cas d'un jour
    (dépendance intra-séance) + du bruit individuel."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        base = rng.normal(0, effet_jour)
        for _ in range(per_day):
            rows.append({"day": f"J{d:03d}", "continuation": max(0.0, 60 + base + rng.normal(0, 30))})
    return pd.DataFrame(rows)


def test_intervalle_par_seances_plus_large_quand_les_cas_dependent_du_jour():
    lie = _seances(40, 10, effet_jour=60, seed=1)
    lo, hi = es.cluster_ci(lie, "continuation", es.share_at_least(50))
    k, n = int((lie["continuation"] >= 50).sum()), len(lie)
    p = k / n
    naif = 2 * 1.96 * np.sqrt(p * (1 - p) / n) * 100
    assert (hi - lo) > 1.5 * naif           # le naïf est trop étroit
    assert lo < 100 * p < hi


def test_diff_test_detecte_un_vrai_ecart_et_pas_le_hasard():
    rng = np.random.default_rng(5)
    days = np.repeat([f"J{d:03d}" for d in range(60)], 8)
    g = rng.choice(["A", "B"], len(days))
    cont = np.where(g == "A", rng.exponential(120, len(days)), rng.exponential(40, len(days)))
    df = pd.DataFrame({"day": days, "continuation": cont})
    vrai = es.diff_test(df, g, "A", "B", "continuation", es.share_at_least(100), n_boot=500)
    assert vrai["diff"] > 20 and vrai["p"] < 0.01 and vrai["ic_lo"] > 0
    hasard = es.diff_test(df.assign(continuation=rng.exponential(60, len(days))), g, "A", "B",
                          "continuation", es.share_at_least(100), n_boot=500)
    assert hasard["ic_lo"] < 0 < hasard["ic_hi"]


def test_diff_test_groupe_vide():
    df = pd.DataFrame({"day": ["J1", "J2"], "continuation": [10.0, 20.0]})
    r = es.diff_test(df, ["A", "A"], "A", "B", "continuation", es.share_at_least(50))
    assert r["n_b"] == 0 and np.isnan(r["p"])


def test_section_du_rapport_separe_vrai_critere_et_bruit():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import excess_report
    rng = np.random.default_rng(11)
    rows = []
    for d in range(80):
        day = f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}"
        regime = rng.choice(["frein", "accélérateur"])         # régime du jour
        for _ in range(6):
            dur = rng.uniform(1, 300)
            scale = 140 if regime == "accélérateur" else 50
            rows.append({"day": day, "g_regime": regime, "g_regime_tdb2": regime,
                         "duree_reaction_s": dur, "couleur": rng.choice(["rouge", "vert"]),
                         "continuation": rng.exponential(scale)})
    rt = pd.DataFrame(rows)
    split = sorted(rt["day"].unique())[40]
    md = "\n".join(excess_report.robustness_analysis(rt, split, n_boot=400))
    # lignes « critère » du tableau (la 2e ligne d'un critère a l'index vide)
    lignes = [l for l in md.splitlines() if "pts" in l and ("tient" in l or "démontré" in l
                                                            or "fragile" in l)]
    regime = [l for l in lignes if l.startswith("régime (étude)")]
    assert regime and "tient" in regime[0]
    voyant = next(i for i, l in enumerate(lignes) if l.startswith("voyant"))
    assert all("tient" not in l for l in lignes[voyant:voyant + 2])   # bruit : jamais « tient »
    assert "régime × réaction, intervalles par séances" in md
