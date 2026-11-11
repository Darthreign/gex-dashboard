"""Validation MOC de bout en bout sur des séances synthétiques
(gex/moc_report.py) : plomberie, absence de regard vers l'avenir, stats."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from gex import greeks, moc, moc_report, store
from gex.config import SETTINGS
from gex.metrics import ET, YEAR_SECONDS


def _chain(day: str, spot: float, strike: float) -> pd.DataFrame:
    """Chaîne NQ réduite à un gros call du jour (dealers longs l'OI)."""
    t = 900 / YEAR_SECONDS
    d = pd.DataFrame({"strike": [strike], "type": ["C"], "expiry": [day],
                      "open_interest": [5000.0], "t_years": [t], "iv": [0.2],
                      "multiplier": [20.0], "spot": [spot]})
    d["delta_bs"] = greeks.call_delta(spot, strike, t, 0.0, 0.2)
    d["gamma_bs"] = greeks.gamma(spot, strike, t, 0.0, 0.2)
    return d


def _ticks(day: str, p50: float, p00: float) -> pd.DataFrame:
    d0 = datetime.fromisoformat(day).replace(tzinfo=ET)
    ts = [d0.replace(hour=15, minute=44).timestamp(), d0.replace(hour=15, minute=49).timestamp(),
          d0.replace(hour=15, minute=59, second=59).timestamp(),
          d0.replace(hour=16, minute=5).timestamp()]
    return pd.DataFrame({"ts": ts, "price": [p50, p50, p00, p00 + 50], "volume": 1.0,
                         "side": "BUY"})


@pytest.fixture()
def donnees(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    monkeypatch.setattr(moc.rates, "current_rate", lambda: 0.0)
    store._HISTORY_CACHE = None
    rng = np.random.default_rng(3)
    d, days = date(2026, 6, 1), []
    while len(days) < 14:
        if d.weekday() < 5:
            day = d.isoformat()
            spot = 20000.0
            k = spot + rng.choice([-1, 1]) * rng.uniform(5, 15)
            store.save_snapshot("NQ", _chain(day, spot, k),
                                datetime.fromisoformat(day).replace(hour=15, minute=44))
            # snapshot POSTÉRIEUR à 15h45 au strike opposé : ne doit jamais servir
            store.save_snapshot("NQ", _chain(day, spot, 2 * spot - k),
                                datetime.fromisoformat(day).replace(hour=15, minute=46))
            # dealer long un call du jour, règlement physique : au-dessus du
            # strike il revend son reliquat (pression vendeuse), en dessous il
            # rachète sa couverture (acheteuse) — le marché « obéit » ici
            move = -10.0 if spot > k else 10.0
            p = SETTINGS.data_dir / "ticks" / "NQ"
            p.mkdir(parents=True, exist_ok=True)
            _ticks(day, spot, spot + move).to_parquet(p / f"{day}.parquet")
            days.append(day)
        d += timedelta(days=1)
    monkeypatch.setattr(moc, "letf_config", lambda s: ({}, False))
    yield days
    store._HISTORY_CACHE = None      # ne pas laisser l'historique vide en cache


def test_snapshot_at_never_looks_ahead(donnees):
    df, when = moc_report.snapshot_at("NQ", donnees[0])
    assert when.strftime("%H%M") == "1544"
    assert moc_report.snapshot_at("NQ", donnees[0], "154300") is None


def test_session_row_reads_1545_and_measures_last_ten_minutes(donnees):
    r = moc_report.session_row("NQ", donnees[0])
    assert r["asof"] == "15:44:00" and r["chains"] == "NQ"
    assert r["px_1550"] == 20000.0 and abs(r["move_pts"]) == 10.0
    assert np.sign(r["contracts"]) == np.sign(r["move_pts"])


def test_report_writes_history_and_finds_the_planted_relation(donnees):
    r = moc_report.report("NQ")
    assert r["sessions"] == 14 and "error" not in r
    s = r["stats"]
    assert s["hit"] == 1.0 and s["pearson"] > 0.5
    h = pd.read_csv(moc_report.history_path("NQ"))
    assert len(h) == 14 and {"contracts", "move_pts"} <= set(h.columns)
    md = moc_report.to_markdown(r)
    assert "Bon sens : **100%**" in md


def test_too_few_sessions(donnees):
    r = moc_report.report("NQ", max_days=3)
    assert "error" in r


def test_prix_fige_exclu():
    from gex.moc_report import _price_at
    d = datetime(2026, 9, 7, tzinfo=ET)
    ticks = pd.DataFrame({"ts": [d.replace(hour=13).timestamp()], "price": [100.0]})
    assert _price_at(ticks, d.replace(hour=15, minute=50)) is None      # 2 h50 sans tick
    ticks = pd.DataFrame({"ts": [d.replace(hour=15, minute=49).timestamp()], "price": [101.0]})
    assert _price_at(ticks, d.replace(hour=15, minute=50)) == 101.0
