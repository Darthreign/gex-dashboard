from datetime import date

import pandas as pd
import pytest

from gex import backfill, greeks
from gex.config import RISK_FREE_RATE


def test_dex_backfill_meme_convention_que_le_live(monkeypatch):
    """Dealers courts calls ET puts : un call (δ>0) donne un DEX négatif,
    un put (δ<0) un DEX positif — comme metrics.enrich."""
    snapshots = []
    monkeypatch.setattr(backfill.store, "save_snapshot",
                        lambda sym, d, ts: snapshots.append(d))
    day, expiry, spot, k = date(2026, 9, 1), date(2026, 9, 18), 5000.0, 5000.0
    t = backfill._t_years(pd.Series([expiry]), day)[0]
    chain = pd.DataFrame({
        "instrument_id": [1, 2],
        "type": ["C", "P"],
        "strike": [k, k],
        "expiry": [expiry, expiry],
        "close": [greeks.call_price(spot, k, t, RISK_FREE_RATE, 0.2),
                  greeks.put_price(spot, k, t, RISK_FREE_RATE, 0.2)],
        "open_interest": [100.0, 100.0],
        "volume": [10.0, 10.0],
    })
    assert backfill.build_day(chain, "SPX", day, spot=spot, persist_chain=True) is not None
    d = snapshots[0]
    assert d.loc[d["type"] == "C", "dex"].iloc[0] < 0
    assert d.loc[d["type"] == "P", "dex"].iloc[0] > 0
    assert d["dex"].iloc[0] == pytest.approx(-d["delta_bs"].iloc[0] * 100 * 100 * spot)
