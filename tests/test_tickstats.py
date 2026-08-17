"""Capture tick d'ouverture : collecteur (gex/tickcapture), stats à la demande
(gex/tickstats) et endpoint /tick_context.

Le cœur : `stop_swept` — la réponse objective à « un stop aurait-il été
balayé ? », qu'une bougie 1 min ne peut pas donner.
"""
from __future__ import annotations

from datetime import datetime, time

import pandas as pd

from gex import tickcapture, tickstats
from gex.metrics import ET


# Univers de test : streamer -> libellé, comme le construit _build_universe.
_UNIV = {"/NQU26:XCME": "NQ", "/ESU26:XCME": "ES"}


def _print(stream, price, **kw):
    return {"eventType": "TimeAndSale", "eventSymbol": stream, "price": price, **kw}


def test_record_mapping_et_schema():
    c = tickcapture.TickCapture()
    c.record(_UNIV, _print("/NQU26:XCME", 100.0, size=3, bidPrice=99.75,
                           askPrice=100.25, aggressorSide="BUY",
                           time=1_700_000_000_000), now=42.0)
    c.record(_UNIV, _print("/ZZZ:XCME", 50.0), now=42.0)      # non suivi -> ignoré
    c.record(_UNIV, _print("/NQU26:XCME", float("nan")), now=42.0)  # NaN -> ignoré
    buf = c._buf
    assert list(buf) == ["NQ"] and len(buf["NQ"]) == 1
    row = buf["NQ"][0]
    # socle ticks_full + surensemble bid/ask/side : TOUT le brut conservé
    assert set(row) == {"ts", "price", "volume", "bid", "ask", "side", "source"}
    assert row["price"] == 100.0 and row["volume"] == 3 and row["source"] == "dxfeed"
    assert isinstance(row["volume"], int)
    assert row["bid"] == 99.75 and row["ask"] == 100.25 and row["side"] == "BUY"
    # horodatage d'ÉCHANGE (ms) prioritaire sur la réception locale
    assert row["ts"] == 1_700_000_000.0


def test_record_repli_temps_local_et_defauts():
    c = tickcapture.TickCapture()
    c.record(_UNIV, _print("/ESU26:XCME", 5000.0), now=99.5)   # ni time, size, bid/ask, side
    row = c._buf["ES"][0]
    assert row["ts"] == 99.5 and row["volume"] == 0            # garde-fou int, pas de NaN
    assert row["bid"] is None and row["ask"] is None and row["side"] is None


def test_start_sans_identifiants_reste_inerte(monkeypatch):
    monkeypatch.setattr(tickcapture, "credentials_present", lambda: False)
    c = tickcapture.TickCapture()
    c.start()
    assert c._started is False                          # aucune session ouverte


def test_drain_vide_le_buffer():
    c = tickcapture.TickCapture()
    c.record(_UNIV, _print("/NQU26:XCME", 100.0, size=1), now=1.0)
    c.record(_UNIV, _print("/NQU26:XCME", 101.0, size=2), now=2.0)
    out = c.drain()
    assert [r["price"] for r in out["NQ"]] == [100.0, 101.0]
    assert c._buf == {} and c.drain() == {}            # vidé, re-drain vide


def test_append_ticks_parquet_journalier(tmp_path, monkeypatch):
    """append_ticks écrit UN parquet par jour, schéma ts/price/volume/source,
    et concatène les flushes successifs du même jour."""
    from datetime import datetime

    from gex import store
    from gex.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    ts = datetime(2026, 8, 17, 10, 0)
    store.append_ticks("NQ", [{"ts": 1.0, "price": 100.0, "volume": 3, "source": "dxfeed"}], ts)
    store.append_ticks("NQ", [{"ts": 2.0, "price": 101.0, "volume": 1, "source": "dxfeed"}], ts)
    df = store.load_ticks("NQ", "2026-08-17")
    assert list(df.columns) == ["ts", "price", "volume", "source"]
    assert list(df["price"]) == [100.0, 101.0] and list(df["volume"]) == [3, 1]
    # un seul fichier journalier
    assert (tmp_path / "ticks" / "NQ" / "2026-08-17.parquet").exists()


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
