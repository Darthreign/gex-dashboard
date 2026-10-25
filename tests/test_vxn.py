"""VXN maison (gex/vxn.py) : méthode CBOE appliquée aux options NDX."""
from __future__ import annotations

import math
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from gex import vxn
from gex.metrics import ET

NOW = datetime(2026, 10, 26, 11, 0, tzinfo=ET)
R = 0.04


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs(s: float, k: float, t: float, sigma: float, cp: str) -> float:
    d1 = (math.log(s / k) + (R + sigma ** 2 / 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if cp == "C":
        return s * _ncdf(d1) - k * math.exp(-R * t) * _ncdf(d2)
    return k * math.exp(-R * t) * _ncdf(-d2) - s * _ncdf(-d1)


def _chain(sigma: float = 0.20, spot: float = 25000.0,
           expiries=((date(2026, 11, 20), True), (date(2026, 11, 27), False))) -> pd.DataFrame:
    rows = []
    for exp, am in expiries:
        t = vxn.minutes_to_settlement(exp, am, NOW) / vxn.N365
        for k in np.arange(spot * 0.5, spot * 1.5 + 1, 25.0):
            for cp in ("C", "P"):
                p = _bs(spot, float(k), t, sigma, cp)
                bid = round(p - 0.05, 2) if p > 0.30 else 0.0
                rows.append({"strike": float(k), "type": cp, "expiry": exp, "settle_am": am,
                             "underlying_symbol": "NDX" if am else "NDXP",
                             "bid": bid, "ask": round(p + 0.05, 2) if bid else 0.05})
    return pd.DataFrame(rows)


def test_volatilite_constante_retrouvee():
    res = vxn.compute(_chain(0.20), NOW, R)
    assert res is not None
    assert res["vxn"] == pytest.approx(20.0, abs=0.3)
    assert res["near_days"] < 30 < res["next_days"]


def test_suit_le_niveau_de_volatilite():
    lo = vxn.compute(_chain(0.15), NOW, R)["vxn"]
    hi = vxn.compute(_chain(0.30), NOW, R)["vxn"]
    assert lo == pytest.approx(15.0, abs=0.3) and hi == pytest.approx(30.0, abs=0.5)


def test_sans_echeance_apres_30_jours_rien():
    only_near = _chain(expiries=((date(2026, 11, 20), True),))
    assert vxn.compute(only_near, NOW, R) is None


def test_reglement_am_et_pm():
    am = vxn.minutes_to_settlement(date(2026, 10, 27), True, NOW)
    pm = vxn.minutes_to_settlement(date(2026, 10, 27), False, NOW)
    assert pm - am == pytest.approx(6.5 * 60)


def test_parse_cboe_daily():
    txt = "DATE,OPEN,HIGH,LOW,CLOSE\n10/09/2026,21.04,21.95,20.49,20.55\n10/08/2026,21.17,22.68,20.73,21.98\n"
    df = vxn.parse_cboe_daily(txt)
    assert df["date"].tolist() == [date(2026, 10, 8), date(2026, 10, 9)]
    assert df["close"].iloc[-1] == 20.55
