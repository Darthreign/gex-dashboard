from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from gex import metrics, positioning, store
from gex.config import SETTINGS
from gex.ingest import ChainSnapshot
from gex.metrics import ET


def test_occ_vers_streamer():
    assert positioning.occ_to_streamer("SPXW260729C07400000") == ".SPXW260729C7400"
    assert positioning.occ_to_streamer("SPY260918P00562500") == ".SPY260918P562.5"


def _df():
    exp = (datetime.now(ET) + pd.Timedelta(days=5)).date()
    opts = pd.DataFrame([
        {"contract": f"SPXW{exp:%y%m%d}{cp}{int(k * 1000):08d}", "expiry": exp, "type": cp,
         "strike": k, "bid": 0.0, "ask": 0.0, "iv": 0.2, "open_interest": 100.0,
         "volume": 0.0, "delta_cboe": 0.0, "gamma_cboe": 0.0, "last_trade_price": 0.0}
        for k in (98.0, 100.0, 102.0) for cp in ("C", "P")])
    now = datetime.now(ET)
    snap = ChainSnapshot("SPX", 100.0, now.replace(tzinfo=None), now.replace(tzinfo=None), opts)
    return metrics.enrich(snap)


def test_sans_flux_le_book_retombe_sur_le_gex_naif():
    df = _df()
    b = positioning.book_summary(df, pd.Series(dtype=float), 100.0)
    assert b["net_gex_book"] == pytest.approx(b["net_gex_naive"])
    assert b["flow_coverage"] == 0.0


def test_achats_de_calls_par_les_preneurs_rendent_les_dealers_courts_gamma():
    df = _df()
    call = positioning.chain_keys(df)[df["type"] == "C"].iloc[1]
    flow = positioning.taker_flow(pd.DataFrame([
        {"contract": call, "side": "BUY", "size": 300.0, "spread": False, "ttype": "NEW"},
        {"contract": call, "side": "BUY", "size": 999.0, "spread": True, "ttype": "NEW"},   # combo : ignoré
        {"contract": call, "side": "SELL", "size": 50.0, "spread": False, "ttype": "CANCEL"},  # ignoré
    ]))
    assert flow[call] == 300.0
    book = positioning.dealer_book(df, flow, 100.0)
    row = book[positioning.chain_keys(book) == call].iloc[0]
    assert row["dealer_pos"] == pytest.approx(100.0 - 300.0)   # dealer court 200 calls
    assert row["gex_book"] < 0 and row["dex_book"] < 0
    b = positioning.book_summary(df, flow, 100.0)
    assert b["net_gex_book"] < b["net_gex_naive"] and 0 < b["flow_coverage"] < 1


def test_flux_du_jour_lu_sur_disque(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    positioning._cache.clear()
    store.append_optprints("SPX", [
        {"ts": 1.0, "contract": ".SPXW261009C100", "side": "SELL", "size": 7.0,
         "spread": False, "ttype": "NEW", "symbol": "SPX"}], datetime(2026, 10, 9, 10))
    flow = positioning.daily_taker_flow("SPX", date(2026, 10, 9))
    assert flow[".SPXW261009C100"] == -7.0
