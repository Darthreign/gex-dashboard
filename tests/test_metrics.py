from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from gex import metrics
from gex.config import CONTRACT_MULTIPLIER
from gex.ingest import ChainSnapshot, parse_occ
from gex.metrics import ET


def make_chain(spot: float, rows: list[dict]) -> ChainSnapshot:
    defaults = {
        "bid": 1.0, "ask": 1.2, "iv": 0.20, "open_interest": 100.0,
        "volume": 0.0, "delta_cboe": 0.0, "gamma_cboe": 0.0,
        "last_trade_price": 0.0,
    }
    full = []
    for i, r in enumerate(rows):
        d = {**defaults, **r}
        d.setdefault("contract", f"TST{i:06d}")
        full.append(d)
    now = datetime.now(ET)
    return ChainSnapshot(
        symbol="TST", spot=spot, feed_timestamp=now.replace(tzinfo=None),
        fetched_at=now.replace(tzinfo=None), options=pd.DataFrame(full),
    )


def far_expiry() -> date:
    return (datetime.now(ET) + timedelta(days=30)).date()


def test_parse_occ():
    exp, cp, strike = parse_occ("NDX260821C04000000")
    assert exp == date(2026, 8, 21)
    assert cp == "C"
    assert strike == 4000.0
    # racine hebdo (SPXW)
    exp, cp, strike = parse_occ("SPXW260724P07400000")
    assert (exp, cp, strike) == (date(2026, 7, 24), "P", 7400.0)


def test_gex_sign_convention():
    """Calls → GEX positif, puts → négatif, magnitude = γ·OI·mult·S²·1%."""
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0, "open_interest": 10.0},
        {"expiry": exp, "type": "P", "strike": 100.0, "open_interest": 10.0},
    ])
    df = metrics.enrich(snap)
    call_gex = df.loc[df["type"] == "C", "gex"].iloc[0]
    put_gex = df.loc[df["type"] == "P", "gex"].iloc[0]
    assert call_gex > 0 and put_gex < 0
    # même strike/IV/expiry → gamma identique → magnitudes égales
    assert call_gex == pytest.approx(-put_gex, rel=1e-9)
    g = df.loc[df["type"] == "C", "gamma_bs"].iloc[0]
    assert call_gex == pytest.approx(g * 10 * CONTRACT_MULTIPLIER * 100.0**2 * 0.01, rel=1e-9)


def test_dex_signs():
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0},
        {"expiry": exp, "type": "P", "strike": 100.0},
    ])
    df = metrics.enrich(snap)
    assert df.loc[df["type"] == "C", "dex"].iloc[0] > 0
    assert df.loc[df["type"] == "P", "dex"].iloc[0] < 0


def test_zero_gamma_crossing():
    """Call OI sous le spot, put OI au-dessus → le GEX net passe de positif
    (spot bas, gamma du call domine) à négatif (spot haut) : crossing ~100."""
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 93.0, "open_interest": 100.0},
        {"expiry": exp, "type": "P", "strike": 107.0, "open_interest": 100.0},
    ])
    df = metrics.enrich(snap)
    zg = metrics.zero_gamma(df, 100.0)
    assert zg is not None
    assert 95.0 < zg < 105.0


def test_zero_gamma_none_when_all_positive():
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0, "open_interest": 100.0},
    ])
    df = metrics.enrich(snap)
    assert metrics.zero_gamma(df, 100.0) is None


def test_put_call_ratios():
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0, "open_interest": 200.0, "volume": 50.0},
        {"expiry": exp, "type": "P", "strike": 100.0, "open_interest": 100.0, "volume": 100.0},
    ])
    df = metrics.enrich(snap)
    r = metrics.put_call_ratios(df)
    assert r["pc_oi"] == pytest.approx(0.5)
    assert r["pc_volume"] == pytest.approx(2.0)


def test_flow_delta_increment():
    exp = far_expiry()
    prev = metrics.enrich(make_chain(100.0, [
        {"contract": "A", "expiry": exp, "type": "C", "strike": 100.0, "volume": 10.0},
        {"contract": "B", "expiry": exp, "type": "P", "strike": 100.0, "volume": 5.0},
    ]))
    cur = metrics.enrich(make_chain(100.0, [
        {"contract": "A", "expiry": exp, "type": "C", "strike": 100.0, "volume": 25.0},
        {"contract": "B", "expiry": exp, "type": "P", "strike": 100.0, "volume": 5.0},
    ]))
    flow = metrics.flow_delta(prev, cur, 100.0)
    assert flow["contracts_traded"] == pytest.approx(15.0)
    d = cur.loc[cur["contract"] == "A", "delta_bs"].iloc[0]
    assert flow["flow_total"] == pytest.approx(15.0 * d * CONTRACT_MULTIPLIER * 100.0)
    assert flow["flow_puts"] == pytest.approx(0.0)


def test_flow_delta_ignores_volume_reset():
    """Un volume qui baisse (reset overnight) ne doit pas produire de flux négatif."""
    exp = far_expiry()
    prev = metrics.enrich(make_chain(100.0, [
        {"contract": "A", "expiry": exp, "type": "C", "strike": 100.0, "volume": 500.0},
    ]))
    cur = metrics.enrich(make_chain(100.0, [
        {"contract": "A", "expiry": exp, "type": "C", "strike": 100.0, "volume": 0.0},
    ]))
    flow = metrics.flow_delta(prev, cur, 100.0)
    assert flow["flow_total"] == 0.0


def test_expired_contracts_dropped():
    past = (datetime.now(ET) - timedelta(days=3)).date()
    snap = make_chain(100.0, [
        {"expiry": past, "type": "C", "strike": 100.0},
        {"expiry": far_expiry(), "type": "C", "strike": 100.0},
    ])
    df = metrics.enrich(snap)
    assert len(df) == 1
