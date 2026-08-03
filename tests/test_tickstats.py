"""Capture tick de clôture : recorder (gex/tickrec), stats à la demande
(gex/tickstats) et endpoint /tick_context.

Le cœur : `stop_swept` — la réponse objective à « un stop aurait-il été
balayé ? », qu'une bougie 1 min ne peut pas donner.
"""
from __future__ import annotations

from datetime import datetime, time

import pandas as pd

from gex import tickrec, tickstats
from gex.metrics import ET


def test_recorder_inerte_hors_fenetre():
    r = tickrec.TickWindowRecorder(symbols=("NQ",))
    r.record("NQ", 1.0, 100.0)                 # inactif -> ignoré
    assert r.disarm_and_drain() == {}


def test_recorder_capture_et_filtre():
    r = tickrec.TickWindowRecorder(symbols=("NQ",))
    r.arm()
    assert r.active is True
    r.record("NQ", 1.0, 100.0, 99.9, 100.1)
    r.record("ES", 1.0, 50.0)                  # pas suivi -> ignoré
    r.record("NQ", 2.0, None)                  # prix None -> ignoré
    buf = r.disarm_and_drain()
    assert list(buf) == ["NQ"] and len(buf["NQ"]) == 1
    assert r.active is False                    # fenêtre refermée
    row = buf["NQ"][0]
    assert row["price"] == 100.0 and row["source"] == "dxfeed"


def _ticks():
    return pd.DataFrame({"ts": [990, 995, 1000, 1002, 1005],
                         "price": [100.0, 102.0, 101.0, 105.0, 99.0]})


def test_window_metrics_avant_apres_cloture():
    m = tickstats.window_metrics(_ticks(), split_ts=1000)
    assert m["open"] == 100 and m["close"] == 99
    assert m["high"] == 105 and m["low"] == 99 and m["range"] == 6
    assert m["max_up"] == 5 and m["max_down"] == 1
    # séparation à la clôture (ts=1000)
    assert m["pre_range"] == 2                   # [100,102]
    assert m["close_2200"] == 102                # dernier avant 16h
    assert m["post_max_up"] == 3 and m["post_max_down"] == 3   # [101,105,99] vs 102
    assert m["post_expansion"] == 3.0            # post_range 6 / pre_range 2


def test_window_metrics_vide():
    assert tickstats.window_metrics(pd.DataFrame()) == {"available": False, "n_ticks": 0}


def test_stop_swept_long_et_short():
    df = _ticks()
    # long, entrée 102, stop 2 pts (=100), à partir de la clôture : min post 99 <= 100
    long_hit = tickstats.stop_swept(df, entry_price=102, stop_pts=2, direction=1, after_ts=1000)
    assert long_hit["swept"] is True and long_hit["max_adverse_excursion"] == 3
    # long, stop large (5 pts = 97) : non touché
    long_ok = tickstats.stop_swept(df, entry_price=102, stop_pts=5, direction=1, after_ts=1000)
    assert long_ok["swept"] is False
    # short, entrée 102, stop 2 pts (=104) : max post 105 >= 104 -> touché
    short_hit = tickstats.stop_swept(df, entry_price=102, stop_pts=2, direction=-1, after_ts=1000)
    assert short_hit["swept"] is True and short_hit["direction"] == "short"


def test_tick_context_endpoint(monkeypatch):
    from gex import store
    from gex.api import _tick_context

    split = datetime.combine(datetime.fromisoformat("2026-08-03").date(),
                             time(16, 0), ET).timestamp()
    df = pd.DataFrame({"ts": [split - 10, split - 5, split, split + 2, split + 5],
                       "price": [100.0, 102.0, 101.0, 105.0, 99.0]})
    monkeypatch.setattr(store, "load_ticks", lambda s, d: df)

    ctx = _tick_context("NQ", "2026-08-03", entry=101, stop=1, direction=1)
    assert ctx["available"] is True and ctx["n_ticks"] == 5
    assert "post_max_down" in ctx                # séparation à 16h ET réussie
    assert ctx["stop_check"]["swept"] is True     # long stop 100, min post 99


def test_tick_context_sans_ticks(monkeypatch):
    from gex import store
    from gex.api import _tick_context
    monkeypatch.setattr(store, "load_ticks", lambda s, d: pd.DataFrame())
    ctx = _tick_context("NQ", "2026-08-03")
    assert ctx["available"] is False and "reason" in ctx
