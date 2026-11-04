"""Réaction du prix aux niveaux GEX selon leur signe (gex/levelreact.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gex import levelreact as lr


def _path(prices, step=1.0):
    return np.arange(len(prices)) * step, np.asarray(prices, float)


def test_reaction_traversee_indecis():
    # niveau 100, arrivée par le bas : touche à 100 puis revient à 90 -> réaction
    o = lr.touch_outcome(*_path([95, 99, 100, 97, 90]), level=100.0, r=10.0, window_s=100)
    assert o["issue"] == "réaction" and o["cote"] == "par le bas"
    # touche puis passe à 110 -> traversée
    assert lr.touch_outcome(*_path([95, 100, 104, 110]), 100.0, 10.0, 100)["issue"] == "traversée"
    # touche puis reste entre 91 et 109 -> indécis
    assert lr.touch_outcome(*_path([95, 100, 105, 95, 102]), 100.0, 10.0, 100)["issue"] == "indécis"
    # arrivée par le haut : symétrie
    o = lr.touch_outcome(*_path([105, 100, 104, 110]), 100.0, 10.0, 100)
    assert o["cote"] == "par le haut" and o["issue"] == "réaction"        # remonte de 10 : rebond
    assert lr.touch_outcome(*_path([105, 100, 95, 90]), 100.0, 10.0, 100)["issue"] == "traversée"


def test_cas_sans_contact():
    assert lr.touch_outcome(*_path([90, 95, 99]), 100.0, 10.0, 100) is None       # jamais atteint
    assert lr.touch_outcome(*_path([100, 95, 105]), 100.0, 10.0, 100) is None     # démarre dessus
    # le délai coupe l'observation : le rebond arrive trop tard
    ts = np.array([0.0, 1.0, 2.0, 500.0])
    assert lr.touch_outcome(ts, np.array([95.0, 100.0, 105.0, 90.0]), 100.0, 10.0, 120)["issue"] == "indécis"
    # saut au-dessus du niveau : le premier tick au-delà compte comme contact
    assert lr.touch_outcome(*_path([95, 112]), 100.0, 10.0, 100)["issue"] == "traversée"


def test_marche_aleatoire_donne_50_50():
    """Seuils symétriques : sans effet, la part de réactions vaut ~50 %
    (par symétrie de la marche autour du niveau au moment du contact)."""
    rng = np.random.default_rng(0)
    out = []
    for _ in range(6000):
        px = 100 + np.cumsum(rng.choice([-1.0, 1.0], 600)) - 6
        o = lr.touch_outcome(np.arange(600.0), px, 100.0, 10.0, 599)
        if o and o["issue"] != "indécis":
            out.append(o["issue"] == "réaction")
    assert len(out) > 2500
    assert np.mean(out) == pytest.approx(0.5, abs=3 * 0.5 / np.sqrt(len(out)))   # 3 écarts types


def test_niveaux_temoins_ecartes_des_vrais_niveaux():
    real = np.array([30000.0, 30020.0])
    p = lr.placebo_levels(real)
    assert all(np.abs(real - q).min() >= lr.MIN_PLACEBO_GAP for q in p) and len(p) > 0
    assert 30030.0 not in p and 29990.0 not in p        # 30000+30 et 30020-30 : trop près de l'autre
    assert 30060.0 in p or 30050.0 in p


def _chain():
    exp = pd.Timestamp("2026-11-06 20:00", tz="UTC")
    rows = [(30000.0, "C", 4000.0), (30000.0, "P", 500.0),      # strike à calls dominants
            (29800.0, "P", 5000.0), (29800.0, "C", 300.0),      # strike à puts dominants
            (30500.0, "C", 40.0)]                               # petit
    return pd.DataFrame({"strike": [r[0] for r in rows], "type": [r[1] for r in rows],
                         "expiration": [exp] * len(rows), "oi": [r[2] for r in rows],
                         "iv": 0.2, "fwd": 1.0})


def test_niveaux_de_la_seance_signes_et_classes():
    t = pd.Timestamp("2026-11-04 14:30", tz="UTC").timestamp()
    lv = lr.session_levels(_chain(), t, 30000.0, 0.04, "tdb", 5)
    assert list(lv["signe"][:2].sort_values()) == ["négatif", "positif"]
    top = lv.iloc[0]
    assert top["strike"] in (30000.0, 29800.0) and top["rang"] == 1
    assert (lv["gex"].abs().diff().dropna() <= 1e-9).all()            # classé par |GEX net| décroissant
    by = dict(zip(lv["strike"], lv["signe"]))
    assert by[30000.0] == "positif" and by[29800.0] == "négatif"
    assert lr.session_levels(pd.DataFrame(), t, 30000.0, 0.04).empty


def _evenements(p_pos, p_neg, p_temoin, n_days=120, seed=0):
    """Événements simulés : par séance, un effet COMMUN (dépendance
    intra-séance) autour de la probabilité de réaction du groupe."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        day = f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}"
        shift = rng.normal(0, 0.08)
        for groupe, p, n in (("positif", p_pos, 3), ("négatif", p_neg, 2), ("témoin", p_temoin, 8)):
            for _ in range(n):
                issue = "réaction" if rng.random() < np.clip(p + shift, 0, 1) else "traversée"
                rows.append({"day": day, "groupe": groupe, "strike": 0.0, "rang": 1, "gex": 1.0,
                             "echelle": 0, "touche": True,
                             "cote": rng.choice(["par le bas", "par le haut"]),
                             "issue": issue, "delai_s": 10.0})
    return pd.DataFrame(rows)


def test_analyse_retrouve_un_effet_plante_et_pas_de_faux_positif():
    vrai = lr.analyse(_evenements(0.70, 0.35, 0.50), n_boot=400)
    t = vrai["tests"].set_index("comparaison")
    assert t.loc["positif − négatif", "verdict"] == "tient"
    assert t.loc["positif − négatif", "écart_pts_%"] == pytest.approx(35, abs=12)
    assert t.loc["positif − témoin", "écart_pts_%"] > 10
    table = vrai["tables"][0]
    assert table.loc["positif", "réaction_%"] > 60 and table.loc["négatif", "réaction_%"] < 45
    nul = lr.analyse(_evenements(0.5, 0.5, 0.5, seed=3), n_boot=400)
    assert not (nul["tests"]["verdict"] == "tient").any()


def test_evenements_d_une_seance_simulee():
    """Chemin construit à la main : le niveau positif rebondit, le négatif est traversé."""
    ts = np.arange(0.0, 600.0)
    px = np.concatenate([np.linspace(30000, 30100, 100), np.linspace(30100, 30085, 50),
                         np.linspace(30085, 29900, 450)])
    levels = pd.DataFrame({"strike": [30100.0, 29950.0], "gex": [3e9, -2e9], "gross": [4e9, 3e9],
                           "rang": [1, 2], "signe": ["positif", "négatif"]})
    ev = pd.DataFrame(lr.session_events("2026-11-04", ts, px, levels))
    micro = ev[(ev["echelle"] == 0) & (ev["groupe"] != "témoin")].set_index("groupe")
    assert micro.loc["positif", "issue"] == "réaction" and micro.loc["négatif", "issue"] == "traversée"
    assert micro.loc["positif", "cote"] == "par le bas" and micro.loc["négatif", "cote"] == "par le haut"
    assert (ev["groupe"] == "témoin").any() and set(ev["echelle"]) == {0, 1, 2}


def test_le_rapport_s_assemble():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import level_reaction_report as rep
    md = rep.report(_evenements(0.7, 0.4, 0.5), "NQ", "tdb", "cash", 200)
    assert "## Échelle micro" in md and "tient" in md and "positif − témoin" in md
    assert "aucune séance" in rep.report(pd.DataFrame(), "NQ", "tdb", "cash", 10)


def test_script_de_bout_en_bout_sur_donnees_simulees(monkeypatch, tmp_path):
    """Toute la chaîne de lecture (fenêtre cash, clôture de la veille, chaîne,
    niveaux, contacts, rapport écrit) sur 40 séances simulées."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import excess_report
    import level_reaction_report as rep
    from gex import dbgamma, rates
    from gex.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-09-01", periods=40)]
    monkeypatch.setattr(excess_report, "gamma_context",
                        lambda raw=None: (pd.DataFrame({"x": [1]}), pd.DataFrame({"x": [1]})))
    monkeypatch.setattr(excess_report, "_tick_days", lambda symbol: set(days))
    rng = np.random.default_rng(1)

    def path(symbol, day):
        w0, w1 = rep.window_bounds(day, "globex")                 # nuit comprise
        ts = np.arange(w0, w1, 5.0)
        px = 30000 + np.cumsum(rng.normal(0, 1.5, len(ts)))
        return ts, np.round(px * 4) / 4
    monkeypatch.setattr(excess_report, "session_path", path)
    monkeypatch.setattr(excess_report, "_prev_close", lambda s, d, a, c: (30000.0, 1.0))
    monkeypatch.setattr(dbgamma, "session_chain", lambda *a, **k: _chain())
    monkeypatch.setattr(rates, "current_rate", lambda: 0.04)
    # chaîne à la date fixe : l'échéance doit rester future par rapport à chaque séance
    monkeypatch.setattr(dbgamma, "select", lambda chain, t, spot, mode: chain)

    ev = rep.collect("NQ", None, None, "tdb", "cash", 5)
    assert not ev.empty and ev["day"].nunique() == 40
    assert {"positif", "négatif", "témoin"} <= set(ev["groupe"])
    assert set(ev["echelle"]) == {0, 1, 2}
    md = rep.report(ev, "NQ", "tdb", "cash", 150)
    assert "Échelle micro" in md and "séances" in md
    # la fenêtre cash exclut la nuit : aucun contact avant 9h30 ET (délais non négatifs)
    assert (ev["delai_s"].dropna() >= 0).all()
    w0, w1 = rep.window_bounds("2026-11-04", "cash")
    assert w1 - w0 == 6.5 * 3600
