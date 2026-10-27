"""Replay de la heatmap (gex/replay.py, app.heatmap_replay)."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from gex import app, replay
from gex.metrics import ET


def _lv(spot=30000.0, zg=29900.0, prof=((29950.0, 0.5, 0.1), (30050.0, -0.3, 0.0))):
    return {"spot": spot, "lo": spot * 0.96, "hi": spot * 1.04, "profile": list(prof),
            "zero_gamma": zg, "hvl": None, "call_wall": 30500.0, "put_support": 29500.0,
            "d1_min": None, "d1_max": None, "walls": [(30500.0, "GEX1")]}


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    replay._LAST_PROFILE.clear()
    return tmp_path


def test_profil_ecrit_seulement_quand_il_change(data_dir):
    t0 = datetime(2026, 10, 15, 10, 0, tzinfo=ET).timestamp()
    p = replay.record("NQ", _lv(), t0)
    replay.record("NQ", _lv(zg=29910.0), t0 + 60)                      # même profil
    replay.record("NQ", _lv(prof=((29950.0, 0.7, 0.1),)), t0 + 120)    # profil changé
    assert p.name == "2026-10-15.jsonl"
    lines = [json.loads(x) for x in p.read_text().splitlines()]
    assert ["profile" in x for x in lines] == [True, False, True]
    frames = replay.load("NQ", "2026-10-15")
    assert [f["zero_gamma"] for f in frames] == [29900.0, 29910.0, 29900.0]
    assert frames[1]["profile"] == frames[0]["profile"]                # reporté
    assert frames[2]["profile"] == [[29950.0, 0.7, 0.1]]


def test_seance_globex_depuis_18h_la_veille(data_dir):
    soir = datetime(2026, 10, 14, 18, 30, tzinfo=ET).timestamp()
    assert replay.record("ES", _lv(), soir).name == "2026-10-15.jsonl"


def test_ligne_illisible_ignoree(data_dir):
    t0 = datetime(2026, 10, 15, 10, 0, tzinfo=ET).timestamp()
    p = replay.record("NQ", _lv(), t0)
    with p.open("a") as fh:
        fh.write('{"t": 12')                                           # écriture coupée
    assert len(replay.load("NQ", "2026-10-15")) == 1


def test_record_all_un_symbole_absent_nempeche_pas_les_autres(data_dir, monkeypatch):
    import pandas as pd

    class St:
        def __init__(self, ok):
            self.enriched = pd.DataFrame({"strike": [1.0]}) if ok else None
            self.snapshot = type("S", (), {"spot": 30000.0})() if ok else None

    monkeypatch.setattr(app, "chain_state", lambda s: St(s == "NQ"))
    monkeypatch.setattr(app, "heatmap_levels", lambda s, df, spot, w: _lv(spot))
    now = datetime(2026, 10, 15, 10, 0, tzinfo=ET).timestamp()
    replay.record_all(now)
    assert replay.load("NQ", "2026-10-15") and not replay.load("ES", "2026-10-15")


def test_heatmap_replay_met_a_lechelle_et_filtre(data_dir, monkeypatch):
    t0 = datetime(2026, 10, 15, 10, 0, tzinfo=ET).timestamp()
    replay.record("NQ", _lv(), t0)
    replay.record("NQ", _lv(zg=29950.0), t0 + 60)
    monkeypatch.setattr(app, "heatmap_spec", lambda *a, **k: {"series": [], "lines": []})
    monkeypatch.setattr(app, "_transform_for", lambda s, u: ((lambda v: v * 2), 2.0, "ratio"))
    r = app.heatmap_replay("NQ", "fr", unit="X", levels_shown=["zero_gamma"], now=t0 + 90)
    assert r["session"] == "2026-10-15" and len(r["frames"]) == 2
    f = r["frames"][1]
    titles = {ln["title"]: ln["price"] for ln in f["lines"]}
    assert titles["Gamma Flip"] == 59900.0 and "Call Wall" not in titles   # ×2, filtré
    assert f["profile"][0] == {"price": 59900.0, "oi": 0.5, "vol": 0.1}
    vide = app.heatmap_replay("ES", "fr", now=t0)
    assert vide["frames"] == [] and "Pas encore" in vide["message"]


def test_heatmap_levels_identique_a_laffichage(monkeypatch):
    """Le replay enregistre exactement ce que calcule la heatmap en direct."""
    # pas de lecture de l'historique réel (son cache fausserait d'autres tests)
    monkeypatch.setattr(app, "ref_spot", lambda symbol, spot: spot)
    import numpy as np
    import pandas as pd
    from gex import greeks
    from gex.metrics import YEAR_SECONDS
    spot, t = 20000.0, 3600 / YEAR_SECONDS
    rows = []
    for k in np.arange(19500, 20501, 50.0):
        for typ in ("C", "P"):
            rows.append({"strike": k, "type": typ, "expiry": "2026-10-15", "open_interest": 500.0,
                         "volume": 50.0, "t_years": t, "iv": 0.2, "multiplier": 20.0,
                         "spot": spot})
    df = pd.DataFrame(rows)
    df["gamma_bs"] = greeks.gamma(spot, df["strike"], t, 0.0, 0.2)
    df["gex"] = df["gamma_bs"] * df["open_interest"] * 20 * spot ** 2 * 0.01 * \
        np.where(df["type"] == "C", 1, -1)
    df["delta_bs"] = np.where(df["type"] == "C", greeks.call_delta(spot, df["strike"], t, 0.0, 0.2),
                              greeks.put_delta(spot, df["strike"], t, 0.0, 0.2))
    df["dex"] = -df["delta_bs"] * df["open_interest"] * 20 * spot
    full = app.heatmap_levels("NQ", df, spot)
    part = app.heatmap_levels("NQ", df, spot, shown=["call_wall"])
    assert full["profile"] == part["profile"] and full["call_wall"] == part["call_wall"]
    assert part["zero_gamma"] is None and part["walls"] == []


def _snap(spot):
    return pd.DataFrame({"strike": [spot], "spot": [spot]})


def test_reconstruction_depuis_les_chaines_sauvegardees(data_dir, monkeypatch):
    from gex import store
    jours = {
        "2026-10-14": [(datetime(2026, 10, 14, 17, 30), _snap(1.0)),     # avant Globex : exclu
                       (datetime(2026, 10, 14, 18, 30), _snap(30000.0))],
        "2026-10-15": [(datetime(2026, 10, 15, 10, 0), _snap(30100.0)),
                       (datetime(2026, 10, 15, 17, 30), _snap(2.0))],   # après 17h : exclu
    }
    monkeypatch.setattr(store, "load_day_snapshots", lambda key, day: jours.get(day, []))
    monkeypatch.setattr(store, "previous_close_spot", lambda s, day=None: 29900.0)
    vus = []

    def niveaux(sym, df, spot, w, ref=None):
        vus.append((spot, ref))
        return _lv(spot, zg=spot - 50)
    monkeypatch.setattr(app, "heatmap_levels", niveaux)
    replay._SNAP_CACHE.clear()
    frames = replay.from_snapshots("NQ", "2026-10-15")
    assert [f["spot"] for f in frames] == [30000.0, 30100.0]
    assert all(ref == 29900.0 for _, ref in vus)              # clôture de la veille de CETTE séance
    assert frames[0]["t"] == datetime(2026, 10, 14, 18, 30, tzinfo=ET).timestamp()
    assert all(f["profile"] for f in frames)


import pandas as pd  # noqa: E402


def test_replay_du_jour_choisi(data_dir, monkeypatch):
    from gex import store
    monkeypatch.setattr(app, "heatmap_spec", lambda *a, **k: {"series": [], "lines": [], "day": a[2]})
    monkeypatch.setattr(app, "_transform_for", lambda s, u: ((lambda v: v), 1.0, "native"))
    monkeypatch.setattr(store, "load_day_snapshots",
                        lambda key, day: [(datetime(2026, 10, 15, 9, 0), _snap(30000.0))]
                        if day == "2026-10-15" else [])
    monkeypatch.setattr(store, "previous_close_spot", lambda s, day=None: 29900.0)
    monkeypatch.setattr(app, "heatmap_levels", lambda sym, df, spot, w, ref=None: _lv(spot))
    replay._SNAP_CACHE.clear()
    # une image enregistrée à 10h00 : la chaîne de 9h00 la précède et s'ajoute devant
    replay.record("NQ", _lv(30050.0), datetime(2026, 10, 15, 10, 0, tzinfo=ET).timestamp())
    now = datetime(2026, 10, 23, 12, 0, tzinfo=ET).timestamp()
    r = app.heatmap_replay("NQ", "fr", now=now, day="2026-10-15")
    assert r["session"] == "2026-10-15" and r["base"]["day"] == "2026-10-15"
    assert [f["t"] for f in r["frames"]] == [
        datetime(2026, 10, 15, 9, 0, tzinfo=ET).timestamp(),
        datetime(2026, 10, 15, 10, 0, tzinfo=ET).timestamp()]
    courant = app.heatmap_replay("NQ", "fr", now=now)
    assert courant["session"] == "2026-10-23" and courant["base"]["day"] is None
