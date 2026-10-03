"""Données du graphique Lightweight Charts de /scalp v2 (gex/app.py::scalp_v2_chart_data)."""
from __future__ import annotations

import time

import pandas as pd
import pytest

from gex import app, store, tickcapture


def _ticks(n: int, start_price: float = 30000.0) -> pd.DataFrame:
    now = time.time()
    rows = [{"ts": now - (n - i) * 1.0, "price": start_price + i * 0.25,
             "side": "BUY" if i % 2 == 0 else "SELL", "volume": 1}
            for i in range(n)]
    return pd.DataFrame(rows)


_CTX = {"zg": 29900.0, "hvl": 29950.0, "keys": {"call_wall": 30200.0}, "walls": []}


def test_pas_de_ticks_renvoie_des_niveaux_sans_bougies(monkeypatch):
    monkeypatch.setattr(store, "load_ticks", lambda s, d: pd.DataFrame())
    monkeypatch.setattr(tickcapture, "_session_day", lambda ts: "2026-10-03")
    out = app.scalp_v2_chart_data("NQ", _CTX, 30040.0)
    assert out["candles"] == [] and out["markers"] == []
    assert len(out["levels"]) == 3  # zg, hvl, call_wall


def test_construit_des_bougies_depuis_des_ticks_recents(monkeypatch):
    monkeypatch.setattr(store, "load_ticks", lambda s, d: _ticks(300))
    monkeypatch.setattr(tickcapture, "_session_day", lambda ts: "2026-10-03")
    out = app.scalp_v2_chart_data("NQ", _CTX, 30040.0)
    assert len(out["candles"]) > 0
    c = out["candles"][0]
    assert set(c) == {"time", "open", "high", "low", "close"}
    assert out["levels"] and set(out["levels"][0]) == {"name", "price", "color"}


def test_ticks_trop_vieux_sont_exclus_du_lookback(monkeypatch):
    old = time.time() - 3600 * 5  # 5h dans le passé, hors lookback_min=90
    df = pd.DataFrame([{"ts": old, "price": 30000.0, "side": "BUY", "volume": 1}])
    monkeypatch.setattr(store, "load_ticks", lambda s, d: df)
    monkeypatch.setattr(tickcapture, "_session_day", lambda ts: "2026-10-03")
    out = app.scalp_v2_chart_data("NQ", _CTX, 30040.0)
    assert out["candles"] == [] and out["markers"] == []
