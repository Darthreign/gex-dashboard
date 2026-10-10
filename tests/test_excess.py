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

def test_script_bout_en_bout(tmp_path, monkeypatch):
    from gex import store
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    day = "2026-10-07"
    start = datetime(2026, 10, 7, 9, 30, tzinfo=ET).timestamp()
    # 9h30 -> 12h : +50, -160 (excès baissier), puis remontée
    path = np.concatenate([np.linspace(20000, 20050, 30), np.linspace(20050, 19890, 60),
                           np.linspace(19890, 19980, 90)])
    ticks = pd.DataFrame({"ts": start + 60 * np.arange(len(path)) + 1,
                          "price": path, "side": "BUY", "size": 1.0})
    (tmp_path / "ticks" / "NQ").mkdir(parents=True)
    ticks.to_parquet(tmp_path / "ticks" / "NQ" / f"{day}.parquet")
    # tape vérifié : dealers vendeurs pendant la baisse (soutien de l'excès)
    mins = pd.date_range("2026-10-07 09:30", periods=len(path), freq="1min")
    tape = pd.DataFrame({"timestamp": mins, "hedge_call_buy": 0.0,
                         "hedge_call_sell": -5e6, "hedge_put_buy": -5e6,
                         "hedge_put_sell": 0.0, "mult_source": "courtier"})
    (tmp_path / "tape" / "NQ").mkdir(parents=True)
    tape.to_parquet(tmp_path / "tape" / "NQ" / f"{day}.parquet")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import excess_report
    ev, _ = excess_report.run(excess_report.load_sessions("NQ"), "A", 100, 60, 120)
    assert len(ev) == 1
    e = ev.iloc[0]
    assert e["sens"] == "baissier" and e["couleur"] == "rouge" and e["segment"] == "cash"
    assert e["hit_10"]
    md = excess_report.report("NQ", 100, 60, 120)
    assert "Variante A" in md and "rouge" in md and "aucun EM disponible" in md
    # le 25/09 est exclu d'office
    assert "2026-09-25" in excess_report.EXCLUDED_DAYS
