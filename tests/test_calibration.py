"""Forward implicite par parité et IV inversée du mid (metrics.calibrate_chain)."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from gex import greeks, metrics

R = 0.04


def _chain(spot=100.0, q=0.02, t=30 / 365, smile=lambda k: 0.20 + 0.4 * (1 - k / 100.0),
           feed_iv=0.55, half_spread=0.01):
    rows = []
    for k in np.arange(90.0, 110.5, 1.0):
        sig = smile(k)
        for typ in ("C", "P"):
            f = greeks.call_price if typ == "C" else greeks.put_price
            px = float(f(spot, k, t, R, sig, q))
            rows.append({"expiry": date(2026, 11, 20), "type": typ, "strike": k,
                         "t_years": t, "bid": max(px - half_spread, 0.0),
                         "ask": px + half_spread, "iv": feed_iv, "volume": 10.0,
                         "_true": sig})
    return pd.DataFrame(rows)


def test_forward_par_parite_retrouve_le_dividende():
    spot, q, t = 100.0, 0.02, 30 / 365
    df = _chain(spot, q, t)
    fwd = metrics.implied_forwards(df, spot, R)
    assert fwd[date(2026, 11, 20)] == pytest.approx(spot * np.exp((R - q) * t), abs=0.02)
    cal = metrics.calibrate_chain(df, spot, R)
    assert cal["carry_q"].iloc[0] == pytest.approx(q, abs=0.003)


def test_iv_inversee_du_cote_otm_et_appliquee_aux_deux():
    df = _chain()
    cal = metrics.calibrate_chain(df, 100.0, R)
    near = cal[(cal["strike"] >= 92) & (cal["strike"] <= 108)]
    assert (near["iv_source"] == "otm").all()
    assert near["iv"].to_numpy() == pytest.approx(near["_true"].to_numpy(), abs=0.005)
    # même IV pour le call et le put d'un strike
    piv = near.pivot_table(index="strike", columns="type", values="iv")
    assert piv["C"].to_numpy() == pytest.approx(piv["P"].to_numpy())
    # l'IV du flux est conservée pour comparaison, pas utilisée
    assert (cal["iv_feed"] == 0.55).all()


def test_option_sur_future_portage_egal_au_taux():
    """Forward = sous-jacent (le future lui-même) : q = r, soit du Black-76."""
    df = _chain(spot=100.0, q=R)
    cal = metrics.calibrate_chain(df, 100.0, R)
    assert cal["carry_q"].iloc[0] == pytest.approx(R, abs=0.003)


def test_sans_cotation_repli_sur_iv_du_flux():
    df = _chain().assign(bid=0.0, ask=0.0)
    cal = metrics.calibrate_chain(df, 100.0, R)
    assert (cal["iv_source"] == "feed").all() and (cal["iv"] == 0.55).all()
    assert (cal["carry_q"] == 0.0).all()


def test_fourchette_trop_large_ignoree():
    df = _chain(half_spread=5.0)
    cal = metrics.calibrate_chain(df, 100.0, R)
    otm_loin = cal[(cal["strike"] == 90.0) & (cal["type"] == "P")]
    assert otm_loin["iv_source"].iloc[0] == "feed"
