from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from gex import greeks, metrics
from gex.metrics import ET, TRADING_DAYS


def _units(a: datetime, b: datetime) -> float:
    return float(metrics.variance_time_years(a, pd.Series([pd.Timestamp(b)]))[0]) * TRADING_DAYS


def test_une_seance_vaut_une_unite():
    assert _units(datetime(2026, 10, 5, 16, tzinfo=ET),
                  datetime(2026, 10, 6, 16, tzinfo=ET)) == pytest.approx(1.0, abs=1e-3)


def test_week_end_ajoute_peu():
    w = _units(datetime(2026, 10, 9, 16, tzinfo=ET), datetime(2026, 10, 12, 16, tzinfo=ET))
    assert 1.05 < w < 1.25


def test_derniere_heure_pese_plus_qu_une_heure_de_nuit():
    fin = _units(datetime(2026, 10, 6, 15, tzinfo=ET), datetime(2026, 10, 6, 16, tzinfo=ET))
    nuit = _units(datetime(2026, 10, 6, 22, tzinfo=ET), datetime(2026, 10, 6, 23, tzinfo=ET))
    assert fin > 5 * nuit


def test_reparametrisation_preserve_prix_et_gamma():
    s, k, t, r, sig, q = 100.0, 102.0, 3 / 365, 0.04, 0.2, 0.01
    tv = 2.2 / 252
    ratio = t / tv
    args = (s, k, tv, r * ratio, sig * np.sqrt(ratio), q * ratio)
    assert greeks.call_price(*args) == pytest.approx(greeks.call_price(s, k, t, r, sig, q))
    assert greeks.gamma(*args) == pytest.approx(greeks.gamma(s, k, t, r, sig, q))


def test_charm_par_seance_dans_add_second_order():
    exp = (datetime.now(ET) + pd.Timedelta(days=10)).date()
    from gex.ingest import ChainSnapshot
    opts = pd.DataFrame([{"contract": "X", "expiry": exp, "type": "C", "strike": 105.0,
                          "bid": 0.0, "ask": 0.0, "iv": 0.2, "open_interest": 100.0,
                          "volume": 0.0, "delta_cboe": 0.0, "gamma_cboe": 0.0,
                          "last_trade_price": 0.0}])
    now = datetime.now(ET)
    df = metrics.enrich(ChainSnapshot("TST", 100.0, now.replace(tzinfo=None),
                                      now.replace(tzinfo=None), opts))
    assert "t_var" in df and df["t_var"].iloc[0] > 0
    sec = metrics.add_second_order(df, 100.0)
    assert np.isfinite(sec["charm"]).all() and sec["charm"].iloc[0] < 0  # call OTM perd du delta
