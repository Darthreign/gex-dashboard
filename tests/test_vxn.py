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


def _store_vxn(tmp_path, monkeypatch, calc_rows, daily_rows):
    from gex import store
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    vxn._LATEST_CACHE.clear()
    if calc_rows:
        store.append_prices(vxn.PRICES_KEY, calc_rows, calc_rows[0]["timestamp"])
    if daily_rows:
        p = tmp_path / "history" / f"{vxn.DAILY_KEY}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(daily_rows).to_parquet(p, index=False)


def test_latest_prefere_le_calcul_du_jour(tmp_path, monkeypatch):
    ts = datetime(2026, 10, 27, 16, 0)
    _store_vxn(tmp_path, monkeypatch,
               [{"timestamp": ts, "open": 25.3, "high": 25.3, "low": 25.3, "close": 25.3}],
               [{"date": date(2026, 10, 26), "open": 28.2, "high": 28.3, "low": 25.4, "close": 26.4}])
    out = vxn.latest(datetime(2026, 10, 27, 18, 0, tzinfo=ET))
    assert out["source"] == "calc" and out["value"] == 25.3
    assert out["variation_vs_cloture"] == pytest.approx(-1.1)
    assert out["officiel"]["date"] == "2026-10-26"


def test_latest_le_lendemain_matin_sert_la_cloture_officielle(tmp_path, monkeypatch):
    _store_vxn(tmp_path, monkeypatch,
               [{"timestamp": datetime(2026, 10, 27, 16, 0), "open": 25.3, "high": 25.3,
                 "low": 25.3, "close": 25.3}],
               [{"date": date(2026, 10, 27), "open": 26.0, "high": 26.5, "low": 25.0, "close": 25.4}])
    out = vxn.latest(datetime(2026, 10, 28, 8, 0, tzinfo=ET))
    assert out["source"] == "officiel" and out["value"] == 25.4 and out["asof"] == "2026-10-27"


def test_latest_sans_donnee(tmp_path, monkeypatch):
    _store_vxn(tmp_path, monkeypatch, [], [])
    assert vxn.latest(datetime(2026, 10, 27, 10, 0, tzinfo=ET))["available"] is False
