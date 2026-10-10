"""Excès depuis le dernier swing et voyant de soutien des market makers
(gex/excess.py, scripts/excess_report.py)."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gex import excess
from gex.metrics import ET


def _path(points):
    """Chemin de prix linéaire par morceaux, une bougie par point (high = low)."""
    p = np.concatenate([np.linspace(a, b, int(abs(b - a)) + 1)[:-1]
                        for a, b in zip(points[:-1], points[1:])] + [[points[-1]]])
    return p, p


# --- swings et excès -------------------------------------------------------------

def test_exces_baissier_depuis_le_sommet_confirme():
    hi, lo = _path([1000, 1050, 940, 1000])
    ev = excess.find_excesses(hi, lo, 100, 60)
    assert len(ev) == 1
    e = ev[0]
    assert e["dir"] == -1 and e["swing"] == 1050 and e["level"] == 950
    assert lo[e["i"]] <= 950 and lo[e["i"] - 1] > 950


def test_un_seul_exces_par_jambe_puis_exces_inverse():
    # baisse de 150 depuis 1050, rebond de 70 (creux confirmé), puis +110
    hi, lo = _path([1000, 1050, 900, 970, 1010])
    ev = excess.find_excesses(hi, lo, 100, 60)
    assert [e["dir"] for e in ev] == [-1, 1]
    assert ev[1]["swing"] == 900 and ev[1]["level"] == 1000


def test_retracement_trop_court_ne_confirme_pas_de_swing():
    # +80, -50 (pas un swing à 60), +80 : pas d'excès baissier, un seul haussier
    hi, lo = _path([1000, 1080, 1030, 1110])
    ev = excess.find_excesses(hi, lo, 100, 60)
    assert all(e["dir"] == 1 for e in ev)


# --- voyant ----------------------------------------------------------------------

def _flow(minutes_net, gross=None, ok=True, t0=0.0):
    n = len(minutes_net)
    ts = t0 + 60 * np.arange(n, dtype=float)
    net = np.asarray(minutes_net, float)
    g = np.abs(net) if gross is None else np.asarray(gross, float)
    return ts, net, g, np.full(n, ok)


@pytest.mark.parametrize("prev,recent,dir_,attendu", [
    ([0] * 10, [-10] * 5, 1, "vert"),        # excès haussier, dealers vendeurs
    ([10] * 10, [10] * 5, 1, "rouge"),       # ils poussent, sans faiblir
    ([10] * 10, [2] * 5, 1, "orange"),       # ils poussaient, soutien / 5
    ([0] * 10, [0] * 5, 1, "gris"),
    ([-10] * 10, [-10] * 5, -1, "rouge"),    # excès baissier, dealers vendeurs
    ([0] * 10, [10] * 5, -1, "vert"),
])
def test_couleurs(prev, recent, dir_, attendu):
    ts, net, g, ok = _flow(prev + recent)
    color, _ = excess.flow_color(ts, net, g, ok, t=60 * 15, excess_dir=dir_)
    assert color == attendu


def test_flux_non_franc_gris():
    ts, net, g, ok = _flow([0] * 10 + [1] * 5, gross=[0] * 10 + [10] * 5)
    assert excess.flow_color(ts, net, g, ok, 60 * 15, 1)[0] == "gris"


def test_barre_non_verifiee_sans_flux():
    ts, net, g, ok = _flow([10] * 15)
    ok[12] = False
    assert excess.flow_color(ts, net, g, ok, 60 * 15, 1)[0] == "sans flux"
    assert excess.flow_color(ts[:0], net[:0], g[:0], ok[:0], 60 * 15, 1)[0] == "sans flux"


def test_le_voyant_ne_lit_pas_la_minute_de_lexces():
    ts, net, g, ok = _flow([0] * 15 + [-100])    # minute 15 = bougie de l'excès
    assert excess.flow_color(ts, net, g, ok, 60 * 15, 1)[0] == "gris"


# --- après l'excès ---------------------------------------------------------------

def test_outcome_mae_et_retours():
    # fade long à 950 : le prix descend à 930 puis remonte à 975
    hi = np.array([960, 945, 935, 950, 965, 975.0])
    lo = np.array([945, 935, 930, 940, 955, 965.0])
    r = excess.outcome(hi, lo, 0, 950.0, 1, horizon=10)
    assert r["mae"] == 20.0
    assert r["hit_10"] and r["min_to_10"] == 4 and r["mae_before_10"] == 20.0
    assert r["hit_15"] and r["hit_20"] and r["min_to_20"] == 5
    # la bougie de l'excès compte pour l'adverse, pas pour le favorable
    r0 = excess.outcome(np.array([990.0, 949]), np.array([940.0, 945]), 0, 950.0, 1, 10)
    assert r0["mae"] == 10.0 and not r0["hit_10"]


def test_outcome_short_et_horizon():
    hi = np.array([1005, 1010, 1020, 995.0])
    lo = np.array([998, 1000, 1005, 985.0])
    r = excess.outcome(hi, lo, 0, 1000.0, -1, horizon=2)
    assert r["bars_after"] == 2 and r["mae"] == 20.0 and not r["hit_10"]


def test_resume_par_couleur():
    ev = pd.DataFrame({"day": ["d1", "d1", "d2"], "couleur": ["vert", "rouge", "vert"],
                       "mae": [5.0, 120.0, 30.0],
                       **{f"hit_{k:g}": [True, False, True] for k in excess.TARGETS},
                       **{f"min_to_{k:g}": [3, np.nan, 8] for k in excess.TARGETS},
                       **{f"mae_before_{k:g}": [5.0, 120.0, 30.0] for k in excess.TARGETS}})
    s = excess.summarize(ev)
    assert s.loc["vert", "n"] == 2 and s.loc["vert", "retour_10_%"] == 100
    assert s.loc["rouge", "mae>=100_%"] == 100 and "orange" not in s.index


def test_wilson():
    lo, hi = excess.wilson(0, 13)
    assert lo == pytest.approx(0, abs=1e-9) and 0.2 < hi < 0.26


# --- script ----------------------------------------------------------------------

def _ecrire_seance(tmp_path, day, labeled, factor=1.0):
    """Séance synthétique : ticks toutes les 10 s le long d'un chemin (excès
    baissier, réaction, second test, remontée) et tape où les dealers vendent
    (soutien de l'excès). `labeled` : barres vérifiées ; sinon format
    ancien, montants × `factor`."""
    start = datetime.fromisoformat(day + "T09:30").replace(tzinfo=ET).timestamp()
    nodes = [(0, 20000), (30, 20050), (90, 19890), (100, 19910), (110, 19892), (210, 19980)]
    t = np.arange(0, 210 * 60, 10.0)
    price = np.round(np.interp(t, [m * 60 for m, _ in nodes], [p for _, p in nodes]) * 4) / 4
    ticks = pd.DataFrame({"ts": start + t, "price": price, "side": "BUY", "size": 1.0})
    (tmp_path / "ticks" / "NQ").mkdir(parents=True, exist_ok=True)
    ticks.to_parquet(tmp_path / "ticks" / "NQ" / f"{day}.parquet")
    mins = pd.date_range(day + " 09:30", periods=210, freq="1min")
    tape = pd.DataFrame({"timestamp": mins, "hedge_call_buy": 0.0,
                         "hedge_call_sell": -5e6 * factor, "hedge_put_buy": -5e6 * factor,
                         "hedge_put_sell": 0.0, "buy_contracts": 60.0, "sell_contracts": 40.0,
                         "delta_prints": 10.0, "no_delta_prints": 0.0})
    if labeled:
        tape["mult_source"] = "courtier"
    (tmp_path / "tape" / "NQ").mkdir(parents=True, exist_ok=True)
    tape.to_parquet(tmp_path / "tape" / "NQ" / f"{day}.parquet")


def _script():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import excess_report
    return excess_report


def test_script_bout_en_bout(tmp_path, monkeypatch):
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    _ecrire_seance(tmp_path, "2026-10-07", labeled=True)
    _ecrire_seance(tmp_path, "2026-10-06", labeled=False, factor=5.0)   # ancien, ×5
    er = _script()
    sessions, seuil = er.load_sessions("NQ")
    assert [s["day"] for s in sessions] == ["2026-10-06", "2026-10-07"]
    # delta moyen des barres vérifiées : 1e7 / (100 × ~19 950 × 20) ≈ 0,25
    assert seuil == pytest.approx(0.25 * np.sqrt(5), rel=0.02)
    assert set(sessions[0]["flow"]["grp"]) == {"x5"}
    ev, _ = er.run(sessions, "A", 100, 60, 120)
    assert len(ev) == 2
    by = ev.set_index("day")
    assert by.loc["2026-10-07", "flux"] == "vérifié"
    assert by.loc["2026-10-06", "flux"] == "delta moyen"
    assert (ev["couleur"] == "rouge").all() and (ev["segment"] == "cash").all()
    assert ev["hit_10"].all()
    # ticks : délai en minutes réelles, pas en bougies
    rt = er.retests(ev)
    assert len(rt) == 2 and (rt["couleur_exces"] == "rouge").all()
    assert rt["rempli"].all() and (rt["continuation"] == 0.0).all()
    md = er.report("NQ", 100, 60, 120)
    assert "Contrôle d'échelle" in md and "Flux vérifié" in md and "anciennes séances" in md
    assert "Second test" in md and "aucun EM disponible" in md
    # sans les anciennes barres : une seule séance
    only, _ = er.load_sessions("NQ", use_old=False)
    assert [s["day"] for s in only] == ["2026-10-07"]
    assert "2026-09-25" in er.EXCLUDED_DAYS


def test_ancienne_seance_a_echelle_changeante(tmp_path, monkeypatch):
    """Un changement d'échelle en cours de séance (×5 puis juste) : les
    fenêtres qui chevauchent la bascule sont « sans flux »."""
    f = pd.DataFrame({"ts": 60.0 * np.arange(120), "net": -1.0, "gross": 1.0,
                      "labeled": False, "usable": True,
                      "implied": np.r_[np.full(60, 1.25), np.full(60, 0.25)]})
    out = _script().apply_scale(f, threshold=0.56, use_old=True)
    assert set(out["grp"][:50]) == {"x5"} and set(out["grp"][70:]) == {"x1"}
    g = out["grp"].to_numpy(object)
    args = (out["ts"].to_numpy(), out["net"].to_numpy(), out["gross"].to_numpy(),
            out["ok"].to_numpy(bool))
    assert excess.flow_color(*args, t=60 * 66, excess_dir=-1, grp=g)[0] == "sans flux"
    assert excess.flow_color(*args, t=60 * 100, excess_dir=-1, grp=g)[0] == "rouge"
    # sans les anciennes barres : rien n'est exploitable
    assert not _script().apply_scale(f, 0.56, use_old=False)["ok"].any()


def test_tick_path_retire_les_repetitions():
    ts, p = excess.tick_path(np.arange(6.0), np.array([1, 1, 2, 2, 2, 1.0]))
    assert ts.tolist() == [0, 2, 5] and p.tolist() == [1, 2, 1]


def test_outcome_en_ticks_delais_en_minutes():
    ts = np.array([0.0, 30, 90, 200])
    p = np.array([950.0, 945, 962, 940])
    r = excess.outcome(p, p, 0, 950.0, 1, horizon=2, ts=ts)
    assert r["hit_10"] and r["min_to_10"] == pytest.approx(1.5)
    assert r["mae"] == 5.0                    # 940 est au-delà de l'horizon de 2 min


# --- second test et gris détaillé ---------------------------------------------------

def test_second_test_apres_reaction_de_15():
    # excès baissier franchi en 0 ; extrême 900, réaction à 920, retour à 908
    hi, lo = _path([950, 900, 920, 908, 880, 930])
    i = 0
    rt = excess.find_retest(hi, lo, i, -1, reaction_pts=15, retest_pts=10)
    assert rt is not None and rt["extreme"] == 900
    assert lo[rt["t"]] <= 910 and lo[rt["t"] - 1] > 910
    r = excess.retest_outcome(hi, lo, rt["t"], rt["extreme"], 1, entry_offset=3, horizon=200)
    assert r["continuation"] == 20.0                    # nouveau creux à 880
    assert r["rempli"] and r["mae"] == pytest.approx(23.0)   # entrée 903
    assert r["hit_20"]


def test_pas_de_second_test_sans_reaction():
    hi, lo = _path([950, 900, 910, 895])                # rebond de 10 seulement
    assert excess.find_retest(hi, lo, 0, -1, 15, 10) is None


def test_second_test_ordre_non_rempli():
    hi, lo = _path([1000, 1050, 1030, 1042, 1010])      # excès haussier, retest à 1042
    rt = excess.find_retest(hi, lo, 0, 1, 15, 10)
    r = excess.retest_outcome(hi, lo, rt["t"], rt["extreme"], -1, 3, 100)
    assert not r["rempli"] and r["continuation"] == 0.0 and "hit_10" not in r


def test_resume_second_test():
    rt = pd.DataFrame({"day": ["d1", "d2"], "couleur": ["rouge", "rouge"],
                       "continuation": [120.0, 5.0], "rempli": [True, False],
                       "mae": [125.0, np.nan],
                       **{f"hit_{k:g}": [False, np.nan] for k in excess.TARGETS}})
    s = excess.summarize_retests(rt)
    assert s.loc["rouge", "n"] == 2 and s.loc["rouge", "cont>=100_%"] == 50
    assert s.loc["rouge", "rempli_%"] == 50 and s.loc["rouge", "retour_10_%"] == 0


@pytest.mark.parametrize("r,attendu", [(0.3, "gris (soutien 0.2-0.35)"),
                                       (-0.25, "gris (contre 0.2-0.35)"),
                                       (0.05, "gris (< 0.2)"), (np.nan, "gris (aucun flux)")])
def test_nuances_du_gris(r, attendu):
    assert excess.gris_nuance(r) == attendu


def test_detail_porte_la_force_du_soutien():
    ts, net, g, ok = _flow([0] * 10 + [3] * 5, gross=[0] * 10 + [10] * 5)
    color, det = excess.flow_color(ts, net, g, ok, 60 * 15, 1)
    assert color == "gris" and det["support_ratio"] == pytest.approx(0.3)
