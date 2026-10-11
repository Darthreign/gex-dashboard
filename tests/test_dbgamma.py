"""Piste 2 : positionnement gamma reconstruit depuis l'open interest Databento
(gex/dbgamma.py) et son branchement dans le rapport d'excès."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gex import dbgamma, greeks
from gex.metrics import YEAR_SECONDS

R = 0.04
SESSION = "2026-10-07"                                   # mercredi
START = dbgamma.session_start(SESSION)                   # mardi 18h00 ET
EXP = pd.Timestamp("2026-10-09 20:00", tz="UTC")         # vendredi 16h00 ET
PUB = START - pd.Timedelta(hours=10)                     # publié avant l'ouverture


def _defs():
    rows = [(1, "C", 20200.0), (2, "P", 19800.0), (3, "C", 20100.0), (4, "P", 19900.0),
            (5, "C", 20000.0)]
    return pd.DataFrame({"instrument_id": [r[0] for r in rows], "type": [r[1] for r in rows],
                         "strike": [r[2] for r in rows], "expiration": [EXP] * len(rows),
                         "underlying": ["NQZ6"] * len(rows)})


def _stats(oi, vol=None, settle=None, ts=PUB):
    rows = []
    for iid, v in oi.items():
        rows.append((iid, dbgamma.OPEN_INTEREST, v))
    for iid, v in (vol or {}).items():
        rows.append((iid, dbgamma.VOLATILITY, v))
    for iid, v in (settle or {}).items():
        rows.append((iid, dbgamma.SETTLEMENT_PRICE, v))
    return pd.DataFrame({"instrument_id": [r[0] for r in rows],
                         "stat_type": [r[1] for r in rows], "value": [r[2] for r in rows],
                         "ts_recv": [ts] * len(rows)})


def test_table_des_statistiques():
    raw = pd.DataFrame({"ts_recv": [PUB] * 4, "instrument_id": [1, 1, 1, 1],
                        "stat_type": [9, 3, 14, 6], "quantity": [500, 0, 0, 99],
                        "price": [np.nan, 12.5, 0.18, np.nan],
                        "update_action": [1, 1, 1, 1]}).set_index("ts_recv")
    t = dbgamma.stats_table(raw)
    assert t["stat_type"].tolist() == [9, 3, 14]
    assert t["value"].tolist() == [500.0, 12.5, 0.18]


def test_chaine_connue_a_louverture_seulement():
    oi = {i: 100.0 for i in range(1, 6)}
    vol = {i: 18.0 for i in range(1, 6)}                    # en %, ramené en décimal
    late = _stats({1: 99999.0}, ts=START + pd.Timedelta(hours=1))   # publié APRÈS
    stats = pd.concat([_stats(oi, vol), late], ignore_index=True)
    c = dbgamma.session_chain(stats, _defs(), SESSION, None, None, R)
    assert len(c) == 5 and (c["oi"] == 100).all()
    assert c["iv"].tolist() == pytest.approx([0.18] * 5) and set(c["iv_source"]) == {"publiée"}


def test_volatilite_depuis_le_reglement_puis_interpolee():
    prev_ts = dbgamma.settle_ts("2026-10-06")
    t = (EXP.timestamp() - prev_ts) / YEAR_SECONDS
    settle = {3: float(greeks.call_price(20000, 20100, t, R, 0.2, R)),
              5: float(greeks.call_price(20000, 20000, t, R, 0.22, R))}
    oi = {i: 100.0 for i in range(1, 6)}
    c = dbgamma.session_chain(_stats(oi, settle=settle), _defs(), SESSION, 20000.0, prev_ts, R)
    by = c.set_index("strike")
    assert by.loc[20100, "iv"] == pytest.approx(0.2, abs=1e-3)
    assert by.loc[20100, "iv_source"] == "règlement"
    assert by.loc[19900, "iv_source"] == "interpolée" and 0.2 <= by.loc[19900, "iv"] <= 0.22 + 1e-9


def test_contrat_et_echeance_filtres():
    d = _defs()
    d.loc[0, "underlying"] = "NQH7"                          # autre contrat
    d.loc[1, "expiration"] = START - pd.Timedelta(hours=1)    # déjà expirée
    c = dbgamma.session_chain(_stats({i: 10.0 for i in range(1, 6)},
                                     {i: 0.2 for i in range(1, 6)}), d, SESSION, None, None, R)
    assert sorted(c["strike"]) == [19900.0, 20000.0, 20100.0]


def _chain(rows):
    return pd.DataFrame({"strike": [r[0] for r in rows], "type": [r[1] for r in rows],
                         "expiration": [EXP] * len(rows), "oi": [r[2] for r in rows],
                         "iv": [0.2] * len(rows), "iv_source": ["publiée"] * len(rows)})


def test_regime_flip_et_mur():
    t = (START + pd.Timedelta(hours=16)).timestamp()
    # gros calls au-dessus (frein), gros puts en dessous (accélérateur)
    c = _chain([(20200, "C", 5000), (20100, "C", 1000), (19800, "P", 6000), (19900, "P", 1000)])
    up = dbgamma.view(c, t, 20150.0, extreme=20160.0, excess_dir=1, rate=R)
    assert up["regime"] == "frein" and up["mur"] == 20200
    assert up["dist_mur"] == pytest.approx(40.0) and 0 < up["part_mur"] < 1
    assert up["dist_flip"] > 0                                # au-dessus du flip
    down = dbgamma.view(c, t, 19850.0, extreme=19780.0, excess_dir=-1, rate=R)
    assert down["regime"] == "accélérateur" and down["mur"] == 19800
    assert down["dist_mur"] == pytest.approx(-20.0)           # extrême sous le mur
    assert down["dist_flip"] < 0
    assert dbgamma.view(c.iloc[0:0], t, 20000, 20000, 1, R)["regime"] == "inconnu"


def test_rapport_piste2(tmp_path, monkeypatch):
    """Le rapport lit la chaîne de chaque séance et classe les seconds tests."""
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from test_excess import _ecrire_seance
    _ecrire_seance(tmp_path, "2026-10-07", labeled=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import excess_report
    # chaîne autour de 19 900 : gros put à 19 850 (mur sous l'extrême baissier)
    defs = pd.DataFrame({"instrument_id": [1, 2], "type": ["P", "C"],
                         "strike": [19850.0, 20100.0], "expiration": [EXP, EXP],
                         "underlying": ["NQZ6", "NQZ6"]})
    stats = _stats({1: 5000.0, 2: 500.0}, {1: 0.2, 2: 0.2})
    monkeypatch.setattr(excess_report, "gamma_context", lambda raw=None: (defs, stats))
    sessions, _ = excess_report.load_sessions("NQ", gamma=True)
    assert len(sessions[0]["chain"]) == 2
    ev, _ = excess_report.run(sessions, "A", 100, 60, 120)
    rt = excess_report.retests(ev).iloc[0]
    assert rt["g_mur"] == 19850.0 and rt["g_dist_mur"] == pytest.approx(19890 - 19850, abs=1)
    md = excess_report.report("NQ", 100, 60, 120, gamma=True)
    assert "Piste 2" in md and "Chaînes reconstituées : 1 séances" in md
    assert rt["duree_reaction_s"] > 0
    # sans OI : arrêt explicite
    monkeypatch.setattr(excess_report, "gamma_context",
                        lambda raw=None: (pd.DataFrame(), pd.DataFrame()))
    with pytest.raises(SystemExit):
        excess_report.load_sessions("NQ", gamma=True)


def test_cache_des_fichiers_bruts(tmp_path, monkeypatch):
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import excess_report
    raw = tmp_path / "import" / "databento_raw"
    raw.mkdir(parents=True)
    (raw / "x.dbn.zst").write_bytes(b"")
    calls = []
    stats = _stats({1: 10.0})

    def fake(r):
        calls.append(r)
        return _defs(), stats
    monkeypatch.setattr(excess_report.dbgamma, "load_raw", fake)
    d1, s1 = excess_report.gamma_context()
    d2, s2 = excess_report.gamma_context()                 # relu depuis le cache
    assert len(calls) == 1 and len(d2) == len(d1) and len(s2) == len(s1)
    import os
    import time
    later = time.time() + 10
    os.utime(raw / "x.dbn.zst", (later, later))            # fichier brut plus récent
    excess_report.gamma_context()
    assert len(calls) == 2


def test_stabilite_et_duree_dans_lanalyse():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import excess_report
    from gex import excess
    rng = np.random.default_rng(1)
    days = [f"2026-{m:02d}-{d:02d}" for m in (1, 2, 4, 5) for d in range(1, 13)]
    rows = []
    for d in days:
        for _ in range(3):
            reg = rng.choice(["frein", "accélérateur"])
            rows.append({"day": d, "sens": rng.choice(["haussier", "baissier"]),
                         "g_regime": reg, "g_dist_mur": rng.uniform(-50, 600),
                         "g_part_mur": rng.uniform(0.01, 0.1),
                         "duree_reaction_s": rng.uniform(5, 900),
                         "continuation": rng.uniform(0, 150), "rempli": True, "mae": 10.0,
                         **{f"hit_{k:g}": True for k in excess.TARGETS}})
    rt = pd.DataFrame(rows)
    chain = _chain([(20000, "C", 1)])
    lines = "\n".join(excess_report.gamma_analysis(rt, "2026-04-01", [{"chain": chain}]))
    assert "stabilité par trimestre" in lines and "2026Q1" in lines and "2026Q2" in lines
    assert "accélérateur / réaction" in lines and "APPRENTISSAGE — régime" in lines
