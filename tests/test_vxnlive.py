"""VXN recalculé en continu : univers, bougies 1 min, publication au dashboard."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from gex import cashbars, livestate, scheduler, vxnlive
from gex.config import SETTINGS
from gex.metrics import ET
from tests.test_vxn import NOW, R, _chain


def _live(sigma=0.20) -> vxnlive.VxnLive:
    """Un calculateur dont l'univers et les cotations viennent de la chaîne synthétique."""
    c = _chain(sigma)
    c["streamer_symbol"] = [f".X{i}" for i in range(len(c))]
    live = vxnlive.VxnLive()
    live._universe = c.drop(columns=["bid", "ask"])
    for s, b, a in zip(c["streamer_symbol"], c["bid"], c["ask"]):
        live.ingest_quote({"eventType": "Quote", "eventSymbol": s, "bidPrice": b, "askPrice": a})
    return live


def test_univers_limite_aux_echeances_autour_de_30_jours():
    rows = [{"expiry": NOW.date() + timedelta(days=d), "settle_am": False, "streamer_symbol": str(d)}
            for d in (5, 19, 21, 30, 39, 41, 60)]
    out = vxnlive.select_universe(pd.DataFrame(rows), NOW)
    assert out["streamer_symbol"].tolist() == ["21", "30", "39"]


def test_cotation_nan_devient_bid_nul():
    live = _live()
    live.ingest_quote({"eventType": "Quote", "eventSymbol": ".X0", "bidPrice": float("nan"),
                       "askPrice": float("nan")})
    assert live._quotes[".X0"] == (0.0, 0.0)
    live.ingest_quote({"eventType": "Quote", "eventSymbol": ".X1", "bidPrice": None})
    assert live._quotes[".X1"] != (None, None)          # ignorée, l'ancienne reste


def test_bougie_construite_puis_ecrite_a_la_minute_suivante():
    live = _live(0.20)
    written = []
    t0 = NOW
    assert live.tick(t0, R, write=lambda b, r: written.append(b))["vxn"] == pytest.approx(20.0, abs=0.3)
    live.tick(t0 + timedelta(seconds=5), R, write=lambda b, r: written.append(b))
    assert written == [] and live._bar.ticks == 2
    live.tick(t0 + timedelta(seconds=60), R, write=lambda b, r: written.append(b))
    assert len(written) == 1 and written[0].minute == int(t0.timestamp() // 60) * 60
    assert live.fresh()


def test_ecriture_sur_disque_sous_vxn_calc(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    live = _live(0.20)
    live.tick(NOW, R)
    live.tick(NOW + timedelta(seconds=60), R)
    df = pd.read_parquet(tmp_path / "prices" / "VXN_CALC" / f"{NOW:%Y-%m-%d}.parquet")
    assert len(df) == 1 and df["source"].iloc[0] == "live_dxfeed"
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-10-26 11:00")


def test_publication_relue_par_le_dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    live = _live(0.20)
    live.tick(NOW, R)
    live.publish()
    cur, done = vxnlive.LiveBars().bar_state("VXN")
    assert done is None and cur.close == pytest.approx(live._bar.close)
    # publication trop ancienne : plus de bougie en cours
    path = livestate.live_dir() / f"{vxnlive.LIVE_NAME}.json"
    doc = json.loads(path.read_text())
    doc["published"] -= 1000
    path.write_text(json.dumps(doc))
    assert vxnlive.LiveBars().bar_state("VXN") == (None, None)


def test_hors_seance():
    assert vxnlive.in_session(datetime(2026, 11, 27, 10, 0, tzinfo=ET))
    assert not vxnlive.in_session(datetime(2026, 11, 27, 16, 20, tzinfo=ET))
    assert not vxnlive.in_session(datetime(2026, 11, 28, 10, 0, tzinfo=ET))   # samedi


def test_repli_15_min_seulement_si_le_flux_est_muet(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "credentials_present", lambda: True)
    monkeypatch.setattr(scheduler, "market_is_open", lambda: True)
    monkeypatch.setattr(scheduler.vxn, "update_live", lambda: calls.append(1))
    monkeypatch.setattr(vxnlive.LIVE, "_last_ok", 0.0)
    scheduler.pull_vxn_live()
    assert calls == [1]
    monkeypatch.setattr(vxnlive.LIVE, "_last_ok", __import__("time").time())
    scheduler.pull_vxn_live()
    assert calls == [1]


def test_vxn_servi_par_les_routes_cash(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    cashbars._has_session.clear()
    d = tmp_path / "prices" / "VXN_CALC"
    d.mkdir(parents=True)
    pd.DataFrame([{"timestamp": datetime(2026, 11, 27, 10, 0), "open": 30.0, "high": 31.0,
                   "low": 29.5, "close": 30.5, "source": "live_dxfeed"}]
                 ).to_parquet(d / "2026-11-27.parquet", index=False)
    assert cashbars.available_days("VXN") == ["2026-11-27"]
    assert cashbars.available_days("VXN_CALC") == []
    assert set(cashbars.feed_channels("VXN,VIX")) == {"bar:VXN", "bar:VIX"}


def test_vxn_servi_seulement_en_bougies_du_calcul_continu(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    cashbars._has_session.clear()
    cashbars._built.clear()
    d = tmp_path / "prices" / "VXN_CALC"
    d.mkdir(parents=True)
    row = lambda h, m, src: {"timestamp": datetime(2026, 11, 27, h, m), "open": 30.0,  # noqa: E731
                             "high": 30.0, "low": 30.0, "close": 30.0, "source": src}
    pd.DataFrame([row(10, 0, "calc_dxfeed")]).to_parquet(d / "2026-11-26.parquet", index=False)
    pd.DataFrame([row(10, 0, "live_dxfeed"), row(10, 15, "calc_dxfeed")]
                 ).to_parquet(d / "2026-11-27.parquet", index=False)
    assert cashbars.available_days("VXN") == ["2026-11-27"]      # jour à 15 min seulement : absent
    _, data = cashbars.session_parquet(d / "2026-11-27.parquet", lambda e: False)
    import io
    assert len(pd.read_parquet(io.BytesIO(data))) == 1          # le point de secours n'est pas servi
