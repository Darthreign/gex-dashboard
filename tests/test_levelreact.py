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


def _path(prices, step=1.0):
    return np.arange(len(prices)) * step, np.asarray(prices, float)


def _o(prices, level=100.0, zone=5.0, retrace=20.0, **kw):
    ts, px = _path(prices)
    return lr.touch_outcome(ts, px, level, zone, retrace, t_end=kw.pop("t_end", 1e9), **kw)


def test_traversee_expedition_et_retracement():
    # arrive par le bas : entre à 95, sort à 105, monte à 140, recule de 25 (à 115),
    # puis repart plus haut : l'expédition s'arrête au premier recul >= 20
    o = _o([90, 96, 105, 120, 140, 125, 115, 130, 150])
    assert o["issue"] == "traversée" and o["cote"] == "par le bas"
    assert o["exp"] == pytest.approx(140 - 105)         # depuis le bord de sortie (105)
    assert o["retr"] == pytest.approx(25) and o["retr_oui"] and not o["retour"]
    # retracement qui revient jusque dans la zone (bord de sortie 105)
    o = _o([90, 96, 105, 130, 100])
    assert o["issue"] == "traversée" and o["exp"] == pytest.approx(25) and o["retour"]
    # jamais de recul de 20 pts : expédition ouverte jusqu'à la fin, sans retracement
    o = _o([90, 96, 105, 130, 125, 150])
    assert o["exp"] == pytest.approx(45) and not o["retr_oui"] and np.isnan(o["retr"])


def test_la_profondeur_du_retracement_s_arrete_au_nouveau_sommet():
    # pic 140, recul à 115, nouveau sommet à 145 puis recul plus profond à 95 :
    # le retracement mesuré est celui du premier recul (25), pas du second
    o = _o([90, 96, 105, 140, 115, 145, 95])
    assert o["retr"] == pytest.approx(25)


def test_rejet_et_expedition_dans_l_autre_sens():
    # entre à 95, va à 99 (dans la zone), recule de 20 depuis 99 (à 79) : rejet ;
    # l'expédition part du bord d'entrée (95) vers le bas : jusqu'à 60, puis rebond de 22
    o = _o([90, 96, 99, 85, 79, 70, 60, 82])
    assert o["issue"] == "rejet"
    assert o["exp"] == pytest.approx(95 - 60) and o["retr"] == pytest.approx(22)
    assert o["retr_oui"] and not o["retour"]


def test_arrivee_par_le_haut_est_symetrique():
    o = _o([112, 104, 95, 70, 50], level=100.0)       # entre à 105, sort à 95 : traversée
    assert o["cote"] == "par le haut" and o["issue"] == "traversée"
    assert o["exp"] == pytest.approx(95 - 50)
    o = _o([112, 104, 101, 130])                      # recule de 29 depuis 101... : rejet
    assert o["cote"] == "par le haut" and o["issue"] == "rejet"


def test_cas_sans_contact_et_cas_particuliers():
    assert _o([90, 95.5, 99]) is not None and _o([80, 85, 90]) is None     # jamais dans la zone
    assert _o([102, 95, 90]) is None                                      # démarre dans la zone
    assert _o([90, 96, 99, 100, 99, 98])["issue"] == "non résolu"          # reste dans la zone
    # saut au-dessus de la zone : le premier tick au-delà compte comme contact et traversée
    assert _o([90, 112, 130])["issue"] == "traversée"


def test_contacts_tardifs_ecartes():
    ts = np.array([0.0, 100.0, 5000.0, 5100.0])
    px = np.array([90.0, 91.0, 96.0, 120.0])
    assert lr.touch_outcome(ts, px, 100.0, 5.0, 20.0, t_end=6000.0)["issue"] == "trop tard"
    assert lr.touch_outcome(ts, px, 100.0, 5.0, 20.0, t_end=9000.0)["issue"] == "traversée"


def test_marche_aleatoire_traverse_environ_61_pour_cent():
    """Repère affiché dans le rapport : exp(−largeur / recul). Pour une marche
    de ±1, la valeur exacte est (M / (M + 1)) ** largeur."""
    rng = np.random.default_rng(0)
    out = []
    for _ in range(4000):
        px = 100 + np.cumsum(rng.choice([-1.0, 1.0], 3000)) - 8
        o = lr.touch_outcome(np.arange(3000.0), px, 100.0, 5.0, 20.0, t_end=1e9)
        if o and o["issue"] in ("traversée", "rejet"):
            out.append(o["issue"] == "traversée")
    exact = (20 / 21) ** 10
    assert len(out) > 2000
    assert np.mean(out) == pytest.approx(exact, abs=3 * 0.5 / np.sqrt(len(out)))
    assert lr.baseline_crossing(5.0, 20.0) == pytest.approx(0.6065, abs=1e-3)


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




def _evenements(p_pos, p_neg, p_tem, exp_pos, exp_neg, exp_tem, n_days=140, seed=0):
    """Événements simulés : par séance, un effet COMMUN (dépendance intra-séance)
    autour de la probabilité de traversée et de l'expédition de chaque groupe."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        day = f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}"
        shift = rng.normal(0, 0.06)
        for groupe, p, scale, n in (("positif", p_pos, exp_pos, 3), ("négatif", p_neg, exp_neg, 2),
                                    ("témoin", p_tem, exp_tem, 8)):
            for _ in range(n):
                trav = rng.random() < np.clip(p + shift, 0, 1)
                exp = rng.exponential(scale) + 5
                retr_oui = rng.random() < 0.8
                rows.append({"day": day, "groupe": groupe, "strike": 0.0, "rang": 1, "gex": 1.0,
                             "zone": 5.0, "retrace": 20.0, "touche": True,
                             "cote": rng.choice(["par le bas", "par le haut"]),
                             "issue": "traversée" if trav else "rejet", "exp_pts": exp,
                             "retr_pts": 20 + rng.exponential(10) if retr_oui else np.nan,
                             "retr_oui": retr_oui, "retour_zone": bool(rng.random() < 0.3)})
    return pd.DataFrame(rows)


def test_analyse_retrouve_un_effet_plante_et_pas_de_faux_positif():
    # positifs moins traversés et expéditions courtes ; négatifs très traversés, expéditions longues
    vrai = lr.analyse(_evenements(0.45, 0.80, 0.60, 30, 90, 50), n_boot=300)
    t = vrai["tests"].set_index(["mesure", "comparaison"])
    taux = t.loc[("taux de traversée", "positif − négatif")]
    assert taux["verdict"] == "tient" and taux["écart"] == pytest.approx(-35, abs=12)
    exp = t.loc[("expédition médiane après traversée", "positif − négatif")]
    assert exp["verdict"] == "tient" and exp["écart"] < -20
    res = vrai["resume"]
    assert res.loc["positif", "traversée_%"] < 55 < res.loc["négatif", "traversée_%"]
    assert vrai["apres_traversee"].loc["négatif", "exp_méd"] > vrai["apres_traversee"].loc["positif", "exp_méd"]
    nul = lr.analyse(_evenements(0.6, 0.6, 0.6, 50, 50, 50, seed=3), n_boot=300)
    assert not (nul["tests"]["verdict"] == "tient").any()


def test_comparaisons_par_cote_d_arrivee():
    t = lr.analyse(_evenements(0.45, 0.80, 0.60, 30, 90, 50), n_boot=200)["tests"]
    assert {"positif − négatif, par le bas", "positif − négatif, par le haut"} <= set(t["comparaison"])
    assert t["p_holm"].notna().all() and len(t) >= 12


def test_evenements_d_une_seance_simulee():
    """Chemin construit à la main : le niveau positif rejette le prix, le négatif est traversé."""
    steps = np.concatenate([np.linspace(30000, 30099, 100),    # monte vers le niveau positif
                            np.linspace(30099, 30075, 24),     # recule de 24 : rejet
                            np.linspace(30075, 30040, 35),
                            np.linspace(30040, 30062, 22),     # rebond de 22
                            np.linspace(30062, 29900, 162),    # descend, traverse le niveau négatif
                            np.full(300, 29900.0)])
    ts = np.arange(len(steps)) * 60.0
    levels = pd.DataFrame({"strike": [30100.0, 29950.0], "gex": [3e9, -2e9], "gross": [4e9, 3e9],
                           "rang": [1, 2], "signe": ["positif", "négatif"]})
    ev = pd.DataFrame(lr.session_events("2026-11-04", ts, steps, levels))
    vrais = ev[ev["groupe"] != "témoin"].set_index("groupe")
    pos, neg = vrais.loc["positif"], vrais.loc["négatif"]
    assert pos["issue"] == "rejet" and pos["cote"] == "par le bas"
    assert pos["exp_pts"] == pytest.approx(30095 - 30040, abs=1.0) and pos["retr_pts"] == pytest.approx(22, abs=1.0)
    assert neg["issue"] == "traversée" and neg["cote"] == "par le haut"
    assert neg["exp_pts"] == pytest.approx(29945 - 29900, abs=1.0)
    assert not neg["retr_oui"]                                 # le prix s'arrête à 29900, sans recul de 20
    assert (ev["groupe"] == "témoin").any() and (ev["zone"] == 5.0).all()


def test_zones_temoins_ne_chevauchent_pas_les_vraies_zones():
    ts = np.arange(0.0, 300.0)
    px = 30000 + np.cumsum(np.random.default_rng(0).normal(0, 2, 300))
    levels = pd.DataFrame({"strike": [30000.0, 30030.0], "gex": [1e9, -1e9], "gross": [2e9, 2e9],
                           "rang": [1, 2], "signe": ["positif", "négatif"]})
    for zone in (5.0, 20.0):
        ev = pd.DataFrame(lr.session_events("2026-11-04", ts, px, levels, zone))
        real = ev[ev["groupe"] != "témoin"]["strike"].unique()
        temoins = ev[ev["groupe"] == "témoin"]["strike"].unique()
        assert (ev["zone"] == zone).all() and len(temoins) > 0
        assert all(np.abs(real - t).min() >= 2 * zone + 5 for t in temoins)


def test_le_rapport_s_assemble_avec_plusieurs_zones():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import level_reaction_report as rep
    base = _evenements(0.45, 0.80, 0.60, 30, 90, 50)
    ev = pd.concat([base.assign(zone=10.0), base.assign(zone=5.0)], ignore_index=True)
    md = rep.report(ev, "NQ", "tdb", "cash", 150)
    assert md.index("# Niveaux GEX") < md.index("# Sensibilité : zone de ± 5 pts")   # 1re = principale
    for titre in ("## Traversée ou rejet", "## Après une traversée", "## Après un rejet",
                  "## Comparaisons", "Repère", "tient"):
        assert titre in md
    assert "aucune séance" in rep.report(pd.DataFrame(), "NQ", "tdb", "cash", 10)


def test_script_de_bout_en_bout_sur_donnees_simulees(monkeypatch, tmp_path):
    """Toute la chaîne de lecture (fenêtre cash, clôture de la veille, chaîne,
    niveaux, contacts, rapport) sur 40 séances simulées."""
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
    monkeypatch.setattr(dbgamma, "select", lambda chain, t, spot, mode: chain)

    ev = rep.collect("NQ", None, None, "tdb", "cash", 5, (5.0, 10.0), 20.0)
    assert not ev.empty and ev["day"].nunique() == 40 and set(ev["zone"]) == {5.0, 10.0}
    assert {"positif", "négatif", "témoin"} <= set(ev["groupe"])
    assert set(ev["issue"]) <= {"traversée", "rejet", "non résolu", "non touché", "trop tard"}
    md = rep.report(ev, "NQ", "tdb", "cash", 120)
    assert "## Traversée ou rejet" in md and "# Sensibilité : zone de ± 10 pts" in md
    w0, w1 = rep.window_bounds("2026-11-04", "cash")
    assert w1 - w0 == 6.5 * 3600
