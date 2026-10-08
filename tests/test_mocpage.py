from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from gex import greeks, moc, mocpage
from gex.config import SETTINGS
from gex.metrics import ET, YEAR_SECONDS

NOW = datetime(2026, 10, 7, 15, 30, tzinfo=ET)


def _enriched(spot, strikes, day="2026-10-07"):
    t = 1800 / YEAR_SECONDS
    d = pd.DataFrame({"strike": strikes, "type": "C", "expiry": day,
                      "open_interest": 1000.0, "t_years": t, "iv": 0.2,
                      "multiplier": 20.0, "spot": spot})
    d["delta_bs"] = greeks.call_delta(spot, d["strike"], t, 0.0, 0.2)
    d["gamma_bs"] = greeks.gamma(spot, d["strike"], t, 0.0, 0.2)
    d["gex"] = d["gamma_bs"] * 1000 * 20 * spot ** 2 * 0.01
    d["dex"] = -d["delta_bs"] * 1000 * 20 * spot
    return d


@pytest.fixture()
def est(monkeypatch):
    monkeypatch.setattr(moc.rates, "current_rate", lambda: 0.0)
    ch = [moc.ChainInput("NQ", _enriched(20000.0, [19990.0, 20010.0]), 20000.0)]
    return {**moc.estimate("NQ", ch, {}, 20000.0, 0.01, NOW, {"TQQQ": (3.0, 1e9)}),
            "phase": "fenetre", "letf_custom": False}


def _text(c) -> str:
    if isinstance(c, (list, tuple)):
        return " ".join(_text(x) for x in c)
    if hasattr(c, "children"):
        return _text(c.children)
    return "" if c is None else str(c)


def test_banner_shows_side_size_and_countdown(est):
    b = mocpage.banner("NQ", est, "fr", NOW)
    s = _text(b)
    assert "MOC NQ" in s and "−30:00" in s
    assert ("ACHAT" in s) == (est["total"] > 0)
    assert "sc-tone-" in b.className


def test_banner_waiting_without_estimate():
    assert "attente" in _text(mocpage.banner("NQ", None, "fr", NOW))


def test_cards_cover_every_chain_and_letf(est):
    s = _text(mocpage.cards("NQ", est, "fr"))
    assert "NDX" in s and "QQQ" in s and "Couverture NQ" in s
    assert "ETF à levier" in s and "Aimant 0DTE" in s


def test_profile_in_contracts_around_futures_price(est):
    fig = mocpage.profile_fig("NQ", est, "fr")
    x = np.asarray(fig.data[0].x)
    assert x.min() < 20000 < x.max() and fig.layout.yaxis.title.text == "NQ"


def test_countdown_after_close_and_weekend():
    assert mocpage.countdown(NOW.replace(hour=16, minute=1)) == "—"
    assert mocpage.countdown(datetime(2026, 10, 10, 15, 0, tzinfo=ET)) == "—"
    assert mocpage.countdown(NOW.replace(hour=13, minute=0)) == "−3h00"


def test_trail_only_inside_window(est):
    mocpage._TRAIL.clear()
    mocpage._record_trail("NQ", {**est, "phase": "avant"}, NOW)
    assert mocpage.trail("NQ").empty
    mocpage._record_trail("NQ", est, NOW)
    mocpage._record_trail("NQ", est, NOW.replace(second=5))      # trop rapproché
    assert len(mocpage.trail("NQ")) == 1


def test_history_table(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    assert "moc_report" in _text(mocpage.history_table("NQ", "fr"))
    (tmp_path / "reports").mkdir()
    pd.DataFrame({"day": ["2026-10-05", "2026-10-06"], "contracts": [120.0, -80.0],
                  "move_pts": [6.5, 3.0]}).to_csv(tmp_path / "reports" / "moc_history_NQ.csv")
    s = _text(mocpage.history_table("NQ", "fr"))
    assert "2 séances" in s and "50%" in s


def test_layout_and_routing():
    assert mocpage.is_moc_path("/moc") and not mocpage.is_moc_path("/scalp")
    ids = str(mocpage.layout())
    for i in ("moc-tick", "moc-banner", "moc-cards", "moc-tape", "moc-profile", "moc-history"):
        assert i in ids
