from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from gex import greeks, moc
from gex.metrics import ET, YEAR_SECONDS

NOW = datetime(2026, 10, 7, 15, 30, tzinfo=ET)     # mercredi, 30 min avant la cloche
TODAY, LATER = "2026-10-07", "2026-11-20"


def _chain(rows, spot=100.0):
    d = pd.DataFrame(rows, columns=["strike", "type", "expiry", "dealer_pos"])
    secs = np.where(d["expiry"] == TODAY, 1800.0, 44 * 86400.0)
    d["t_years"] = secs / YEAR_SECONDS
    d["iv"] = 0.2
    d["multiplier"] = 100.0
    d["spot"] = spot
    is_call = (d["type"] == "C").to_numpy()
    c = greeks.call_delta(spot, d["strike"], d["t_years"], 0.0, 0.2)
    d["delta_bs"] = np.where(is_call, c, c - 1)
    d["gamma_bs"] = greeks.gamma(spot, d["strike"], d["t_years"], 0.0, 0.2)
    d["open_interest"] = d["dealer_pos"].abs()
    return d


@pytest.fixture(autouse=True)
def _rate(monkeypatch):
    monkeypatch.setattr(moc.rates, "current_rate", lambda: 0.0)


def test_cash_settled_itm_call_unwinds_hedge_by_buying():
    # dealer long 10 calls ITM : couvert court ~δ ; à l'échéance cash l'option
    # disparaît, il rachète TOUTE sa couverture = pos·δ·mult·S, décomposé en
    # convergence (vente du reliquat 1−δ, comme en physique) + débouclage cash
    b = _chain([(99.0, "C", TODAY, 10)])
    f = moc.hedge_flow(b, 100.0, NOW, cash_settled=True)
    d_now = float(b["delta_bs"].iloc[0])
    assert f["expiring"] + f["cash_unwind"] == pytest.approx(10 * d_now * 100 * 100.0, rel=1e-3)
    assert f["cash_unwind"] == pytest.approx(10 * 100 * 100.0)
    assert f["total"] == pytest.approx(f["expiring"])      # hors total
    assert f["charm"] == 0.0
    assert moc.hedge_flow(b, 100.0, NOW, cash_settled=False)["cash_unwind"] == 0.0


def test_physical_itm_call_sells_the_residual():
    # règlement physique : il reçoit le sous-jacent (δ=1), il revend le reliquat
    b = _chain([(99.0, "C", TODAY, 10)])
    f = moc.hedge_flow(b, 100.0, NOW, cash_settled=False)
    d_now = float(b["delta_bs"].iloc[0])
    assert f["expiring"] == pytest.approx(-10 * (1 - d_now) * 100 * 100.0, rel=1e-3)
    assert f["expiring"] < 0


def test_short_otm_put_expiring_worthless_dealer_buys_back_hedge():
    # dealer court un put OTM = long delta, couvert en VENDANT ; le put expire
    # sans valeur (δ -> 0) : il rachète sa vente -> achat
    b = _chain([(99.9, "P", TODAY, -10)])
    f = moc.hedge_flow(b, 100.0, NOW, cash_settled=False)
    assert f["expiring"] > 0


def test_am_settled_today_is_not_expiring():
    b = _chain([(99.0, "C", TODAY, 10)])
    b["settle_am"] = True
    _, _, exp = moc.close_deltas(b, 100.0, NOW)
    assert not exp.any()


def test_charm_on_long_otm_call_makes_dealers_buy():
    # le delta d'un call OTM fond avec le temps : le dealer long, couvert court,
    # a trop vendu -> il rachète
    b = _chain([(110.0, "C", LATER, 1000)])
    f = moc.hedge_flow(b, 100.0, NOW, cash_settled=False)
    assert f["charm"] > 0 and f["expiring"] == 0


def test_variance_clock_makes_last_half_hour_weigh_more():
    b = _chain([(103.0, "C", LATER, 1000)])
    cal = moc.hedge_flow(b, 100.0, NOW, False)["charm"]
    b["t_var"] = b["t_years"] * 365 / 252        # ordre de grandeur réaliste
    var = moc.hedge_flow(b, 100.0, NOW, False)["charm"]
    assert var > cal > 0


def test_after_close_nothing_left():
    b = _chain([(110.0, "C", LATER, 1000)])
    f = moc.hedge_flow(b, 100.0, datetime(2026, 10, 7, 16, 0, tzinfo=ET), False)
    assert f["charm"] == pytest.approx(0.0, abs=1e-6)


def test_letf_rebalance_follows_the_day_for_long_and_inverse():
    cfg = {"L3": (3.0, 1e9), "I3": (-3.0, 1e9), "I1": (-1.0, 1e9)}
    up = moc.letf_rebalance(0.01, cfg)
    assert up["by_etf"]["L3"] == pytest.approx(6e7)
    assert up["by_etf"]["I3"] == pytest.approx(12e7)
    assert up["by_etf"]["I1"] == pytest.approx(2e7)
    assert moc.letf_rebalance(-0.01, cfg)["total"] == pytest.approx(-up["total"])
    assert moc.letf_rebalance(None, cfg)["total"] is None


def test_letf_config_override(tmp_path, monkeypatch):
    monkeypatch.setattr(moc.SETTINGS, "data_dir", tmp_path, raising=False)
    cfg, custom = moc.letf_config("NQ")
    assert not custom and "TQQQ" in cfg
    (tmp_path / "moc_letf.json").write_text('{"NQ": {"TQQQ": [3, 1e10]}}')
    cfg, custom = moc.letf_config("NQ")
    assert custom and cfg == {"TQQQ": (3.0, 1e10)}
    (tmp_path / "moc_letf.json").write_text("{pas du json")
    assert not moc.letf_config("NQ")[1]


def test_estimate_converts_to_contracts_and_reports_missing():
    df = _chain([(99.0, "C", TODAY, 10), (110.0, "C", LATER, 1000)])
    df = df.drop(columns="dealer_pos")
    ch = [moc.ChainInput("NDX", df, 100.0)]
    r = moc.estimate("NQ", ch, {}, fut_price=100.0, day_return=0.01, now_et=NOW,
                     letf={"L3": (3.0, 1e6)})
    assert r["missing"] == ["QQQ", "NQ"]
    assert r["total"] == pytest.approx(r["options_total"] + 6e4)
    assert r["contracts"] == pytest.approx(r["total"] / (20 * 100.0))
    assert r["profile"] is not None and len(r["profile"]) == 41
    assert r["magnets"] and r["magnets"][0]["strike"] == 99.0


def test_profile_flips_sign_through_the_strike_on_physical_expiry():
    # dealer COURT un gros call du jour (physique) : sous le strike il expire
    # OTM (δ->0, rachat de la couverture longue = vente), au-dessus ITM
    # (δ->1 : il est assigné court, doit racheter le reliquat = achat)
    b = _chain([(100.0, "C", TODAY, -1000)])
    p = moc.flow_profile(b, 100.0, NOW, cash_settled=False, moves=[-0.005, 0.005])
    assert p["total"].iloc[0] < 0 < p["total"].iloc[1]


def test_session_phase():
    d = datetime(2026, 10, 7, tzinfo=ET)
    assert moc.session_phase(d.replace(hour=14)) == "avant"
    assert moc.session_phase(d.replace(hour=15, minute=10)) == "fenetre"
    assert moc.session_phase(d.replace(hour=15, minute=55)) == "noii"
    assert moc.session_phase(d.replace(hour=16, minute=1)) == "clos"
    assert moc.session_phase(datetime(2026, 10, 10, 15, 30, tzinfo=ET)) == "clos"


def test_at_the_strike_intrinsic_is_half():
    b = _chain([(100.0, "C", TODAY, 1), (100.0, "P", TODAY, 1)])
    _, d_close, _ = moc.close_deltas(b, 100.0, NOW)
    assert list(d_close) == [0.5, -0.5]
