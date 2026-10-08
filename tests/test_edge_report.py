"""Banc d'edge de bout en bout sur des séances synthétiques (gex/edge_report.py) :
il doit trouver l'edge là où il existe (zone frein qui revient à la moyenne)
et le refuser là où il n'existe pas (zone d'accélération en tendance)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from gex import edge, edge_report, store
from gex.config import SETTINGS
from gex.metrics import ET


def _session(day, rng, frein, drift_sign):
    t0 = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET).timestamp()
    n = 390 * 12                                    # un tick toutes les 5 s
    steps = rng.normal(0, 2.2, n)
    p = np.empty(n)
    p[0] = 20000.0
    for i in range(1, n):
        dev = p[i - 1] - p[0]
        pull = -0.1 * dev if frein and abs(dev) > 40 else 0.0
        drift = 0.0 if frein else 0.036 * drift_sign
        p[i] = p[i - 1] + steps[i] + pull + drift
    p = np.round(p * 4) / 4
    return pd.DataFrame({"ts": t0 + 5 * np.arange(n), "price": p, "volume": 1.0,
                         "side": np.where(np.diff(np.r_[p[0], p]) >= 0, "BUY", "SELL")})


@pytest.fixture()
def donnees(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    store._HISTORY_CACHE = None
    rng = np.random.default_rng(5)
    d, k, hist = date(2026, 6, 1), 0, []
    while k < 24:
        if d.weekday() < 5:
            frein = k % 2 == 0
            store.append_ticks("NQ", _session(d, rng, frein, 1 if k % 4 == 1 else -1)
                               .to_dict("records"), datetime(d.year, d.month, d.day))
            hist.append({"timestamp": datetime(d.year, d.month, d.day, 9, 31), "symbol": "NQ",
                         "zero_gamma": 19800.0 if frein else 20300.0,
                         "net_gex_0dte": 1e9 if frein else -1e9, "net_gex": 0.0, "spot": 20000.0})
            k += 1
        d += timedelta(days=1)
    (tmp_path / "history").mkdir()
    pd.DataFrame(hist).to_parquet(tmp_path / "history" / "metrics.parquet")
    monkeypatch.setattr(edge, "GRID", {"excess_em": (0.5,), "require": (1,),
                                       "target_em": (0.3,), "stop_em": (0.3,)})
    monkeypatch.setattr(edge_report, "MIN_TRAIN_TRADES", 5)
    yield tmp_path
    store._HISTORY_CACHE = None


def test_le_filtre_de_zone_separe_l_edge_du_piege(donnees):
    r = edge_report.report("NQ")
    assert r["sessions"] >= 20
    zones = r["by_zone"]["mean"]
    assert zones["frein"] > 0 > zones["accelerateur"]
    assert r["test"].loc["setup", "expectancy_em"] > r["test"].loc["naive", "expectancy_em"]
    saved = json.loads(edge_report.params_path("NQ").read_text())
    assert saved["excess_em"] == 0.5 and "validated" in saved
    assert "Hors échantillon" in edge_report.to_markdown(r)


def test_trop_peu_de_seances(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    assert "error" in edge_report.report("ES")
