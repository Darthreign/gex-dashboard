"""Chaînes OPRA en continu : réévaluation au spot live (gex/livechain.py,
futopt.reprice_native), volume compté sur les prints, flux multiplexé."""
import asyncio
import json
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from gex import broadcast, greeks, livechain, metrics
from gex.ingest import ChainSnapshot
from gex.metrics import ET
from gex.rtquote import QUOTES, Tick
from gex.scheduler import UnderlyingState, build_native_summary
from gex import rates
from gex.futopt import apply_greeks, reprice_native


def _native_chain(spot=30000.0):
    now = datetime.now(ET)
    rows = []
    for days in (1, 5):
        exp = (now + timedelta(days=days)).date()
        for k in np.arange(29000, 31001, 100.0):
            for cp in "CP":
                f = greeks.call_price if cp == "C" else greeks.put_price
                px = float(f(spot, k, days / 365, 0.045, 0.2, 0.045))
                rows.append({"contract": f"./Q{exp:%y%m%d}{cp}{int(k)}", "expiry": exp,
                             "type": cp, "strike": k, "bid": max(px - 1, 0), "ask": px + 1,
                             "iv": 0.2, "open_interest": 1000.0, "volume": 10.0,
                             "delta_cboe": 0.0, "gamma_cboe": 0.0, "last_trade_price": px})
    snap = ChainSnapshot("NQ", spot, now.replace(tzinfo=None), now.replace(tzinfo=None),
                         pd.DataFrame(rows))
    df = metrics.enrich(snap)
    df["streamer_symbol"] = df["contract"]
    df["multiplier"] = 20.0
    apply_greeks(df, spot, rates.current_rate())     # GEX au multiplicateur NQ
    snap, summary = build_native_summary("NQ", df, now)
    st = UnderlyingState(snap, df, summary, snap.feed_timestamp)
    return st


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    livechain.reset()
    monkeypatch.setattr(QUOTES, "ticks", {})
    yield
    livechain.reset()


def _quote(price, age=0.0):
    QUOTES.ticks["NQ"] = Tick(bid=price - 0.25, ask=price + 0.25, last=price,
                              ts=time.time() - age)


def test_reprice_suit_le_spot_et_garde_oi_iv():
    st = _native_chain()
    live = reprice_native(st.enriched, 30150.0)
    assert (live["spot"] == 30150.0).all()
    assert (live["open_interest"] == st.enriched["open_interest"]).all()
    assert np.allclose(live["iv"], st.enriched["iv"])
    assert not np.allclose(live["gex"], st.enriched["gex"])
    # même spot : mêmes grecs que la salve (aux secondes écoulées près)
    same = reprice_native(st.enriched, 30000.0)
    assert np.allclose(same["gex"], st.enriched["gex"], rtol=1e-3)


def test_reprice_volume_garde_le_plus_grand():
    st = _native_chain()
    s0 = st.enriched["streamer_symbol"].iloc[0]
    s1 = st.enriched["streamer_symbol"].iloc[1]
    live = reprice_native(st.enriched, 30000.0, volume={s0: 500.0, s1: 3.0})
    assert live.loc[live["streamer_symbol"] == s0, "volume"].iloc[0] == 500.0
    assert live.loc[live["streamer_symbol"] == s1, "volume"].iloc[0] == 10.0


def test_vue_live_au_spot_temps_reel(monkeypatch):
    st = _native_chain()
    lock = __import__("threading").Lock()
    _quote(30100.0)
    v = livechain.view("NQ", "NQ", st, lock)
    assert isinstance(v, livechain.LiveView)
    assert v.summary.spot == pytest.approx(30100.0)
    assert v.summary.net_gex != st.summary.net_gex
    assert v.last_feed_ts == st.last_feed_ts      # âge de la salve inchangé
    # dans l'intervalle minimal : même vue, sans recalcul
    _quote(30200.0)
    assert livechain.view("NQ", "NQ", st, lock) is v
    # au-delà : recalcul au nouveau spot
    monkeypatch.setattr(livechain, "MIN_INTERVAL_S", 0.0)
    assert livechain.view("NQ", "NQ", st, lock).summary.spot == pytest.approx(30200.0)


@pytest.mark.parametrize("price,age", [(None, 0), (30100.0, 1e4), (33000.0, 0)])
def test_vue_live_repli_sur_la_salve(price, age):
    st = _native_chain()
    if price is not None:
        _quote(price, age)          # absent, périmé, ou saut aberrant (> 5 %)
    assert livechain.view("NQ", "NQ", st, __import__("threading").Lock()) is st


def test_cboe_jamais_reevaluee():
    st = _native_chain()
    st.summary = metrics.SummaryMetrics(**{**st.summary.__dict__, "source": "cboe"})
    _quote(30100.0)
    assert livechain.view("NQ", "NQ", st, __import__("threading").Lock()) is st


def test_volume_par_contrat_compte_et_relaye():
    from gex.capturebus import RemoteTape
    from gex.flowtape import FlowTape
    tape = FlowTape()
    tape._by_stream = {".SPXW261009C6700": "SPX"}
    now = time.time()
    for size in (3, 4):
        tape.ingest_print({"eventSymbol": ".SPXW261009C6700", "size": size, "price": 1.0,
                           "aggressorSide": "BUY", "spreadLeg": True}, now)
    assert tape.contract_volumes() == {".SPXW261009C6700": 7.0}
    payload, marks = tape.export_delta(None)
    remote = RemoteTape("ws://x")
    remote.apply(payload)
    assert remote.contract_volumes() == {".SPXW261009C6700": 7.0}
    tape.ingest_print({"eventSymbol": ".SPXW261009C6700", "size": 1, "price": 1.0,
                       "aggressorSide": "SELL"}, now)
    delta, _ = tape.export_delta(marks)
    assert delta["vol"] == {".SPXW261009C6700": 8.0}
    remote.apply(delta)
    assert remote.contract_volumes()[".SPXW261009C6700"] == 8.0


def test_flux_multiplexe_etiquette_chaque_canal():
    a = broadcast.Channel(("t", "a"), lambda: None, 60)
    b = broadcast.Channel(("t", "b"), lambda: None, 60)
    a.publish('data: {"x": 1}\n\n')
    b.publish('data: {"y": 2}\n\n')

    async def first():
        gen = broadcast.multi_events_async({"A": a, "B": b}, keepalive=1)
        out = await gen.__anext__()
        await gen.aclose()
        return out
    msgs = [json.loads(line[6:]) for line in asyncio.run(first()).split("\n\n") if line]
    assert {m["id"]: m["d"] for m in msgs} == {"A": {"x": 1}, "B": {"y": 2}}


def test_lw_multi_filtre_les_entrees():
    from gex.app import lw_multi_channels
    q = json.dumps([{"id": "flow", "name": "flow", "args": {"symbol": "SPX"}},
                    {"id": "x", "name": "nope", "args": {"symbol": "SPX"}},
                    {"id": "y", "name": "flow", "args": {"symbol": "XXX"}}])
    assert list(lw_multi_channels(q)) == ["flow"]
    assert lw_multi_channels("pas du json") == {}
