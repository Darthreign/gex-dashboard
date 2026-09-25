"""Reset du cumul d'order flow à l'open US (9h30 ET)."""
from __future__ import annotations

import pandas as pd

from gex import app, store


def _tape(day="2026-09-25"):
    ts = pd.date_range(f"{day} 09:00", periods=60, freq="min")     # 9h00 -> 9h59 ET
    return pd.DataFrame({"timestamp": ts, "net_delta": 1e6, "net_contracts": 1.0,
                         "net_calls": 1.0, "net_puts": 0.0, "hedge_call_buy": 1e6})


def test_hedge_frame_ne_remonte_pas_avant_lopen(monkeypatch):
    monkeypatch.setattr(store, "load_tape", lambda s, d: _tape(d))
    monkeypatch.setattr("gex.flowtape.TAPE.live_rows", lambda s: [])
    assert hedge_min(app.hedge_frame("SPX", "2026-09-25", 0)) == "09:30"
    assert hedge_min(app.hedge_frame("SPX", "2026-09-25", 45)) == "09:30"   # fenêtre coupée à l'open
    assert hedge_min(app.hedge_frame("SPX", "2026-09-25", 10)) == "09:49"   # fenêtre courte inchangée


def hedge_min(df):
    return f"{df['timestamp'].min():%H:%M}"


def test_hedge_frame_avant_lopen_garde_tout_le_premarche(monkeypatch):
    def tape(s, d):
        t = _tape(d)
        return t[t["timestamp"] < pd.Timestamp(f"{d} 09:30")]
    monkeypatch.setattr(store, "load_tape", tape)
    monkeypatch.setattr("gex.flowtape.TAPE.live_rows", lambda s: [])
    assert hedge_min(app.hedge_frame("SPX", "2026-09-25", 0)) == "09:00"


def test_tape_cumul_reparti_de_zero_a_lopen(monkeypatch):
    monkeypatch.setattr(store, "load_tape", lambda s, d: _tape(d))
    fig = app.tape_fig("SPX", "fr", "2026-09-25", ["net"])
    y = list(fig.data[0].y)
    assert y[29] == 30.0                # 9h00-9h29 : 30 M$ de pré-marché
    assert y[30] == 1.0                 # 9h30 : le cumul repart de zéro
    assert y[-1] == 30.0


def test_axe_live_a_la_seconde(monkeypatch):
    import time
    from gex.flowtape import TAPE
    n = time.time()
    monkeypatch.setattr(TAPE, "live_points", lambda s, w, now=None: [(n - 5, 1e6, 0), (n - 2, 2e6, 2)])
    assert app.hedge_fig("SPX", "fr", -1).layout.xaxis.tickformat == "%H:%M:%S"
    monkeypatch.setattr(store, "load_tape", lambda s, d: _tape(d))
    monkeypatch.setattr(TAPE, "live_rows", lambda s: [])
    assert app.hedge_fig("SPX", "fr", 15, "2026-09-25").layout.xaxis.tickformat == "%H:%M"
