"""Courbes de flux d'options pour un client distant (gex/flowfeed.py)."""
from __future__ import annotations

import io
import json
from datetime import datetime

import pandas as pd
import pytest
from flask import Flask

from gex import app, flowfeed, ticksapi
from gex.config import SETTINGS

TOKEN = "jeton-de-test"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _bars(with_notional=True):
    d = pd.DataFrame({"timestamp": pd.to_datetime(["2026-11-20 09:30", "2026-11-20 09:31"]),
                      "net_delta": [1e6, -4e6], "net_premium": [2e4, 3e4]})
    if with_notional:
        d["notional_calls"], d["notional_puts"] = [5e7, 1e7], [2e7, 6e7]
    return d


def test_seance_par_minute_en_secondes_unix(monkeypatch):
    monkeypatch.setattr(app, "tape_day", lambda s, d: _bars())
    df = flowfeed.session("NQ", "2026-11-20")
    assert list(df.columns) == ["ts", "net_delta", "net_premium", "notional_calls", "notional_puts"]
    # 09:30 a New York le 20/11/2026 (heure d'hiver, UTC-5) = 14:30 UTC
    assert df.ts[0] == datetime.fromisoformat("2026-11-20T14:30:00+00:00").timestamp()
    assert df.ts[1] - df.ts[0] == 60 and list(df.net_delta) == [1e6, -4e6]


def test_evenement_cumul_et_minute_en_cours(monkeypatch):
    monkeypatch.setattr(app, "tape_day", lambda s, d: _bars())
    ev = flowfeed.event("NQ", clock=lambda: datetime(2026, 11, 20, 9, 31))
    assert ev["symbol"] == "NQ" and ev["day"] == "2026-11-20" and ev["t"] % 60 == 0
    assert ev["cum"] == {"net_delta": -3e6, "net_premium": 5e4, "notional_calls": 6e7, "notional_puts": 8e7}
    assert ev["bar"]["net_delta"] == -4e6 and ev["bar"]["notional_puts"] == 6e7
    produire = flowfeed.producer("NQ", build=lambda s: ev)
    assert json.loads(produire()[len("data: "):]) == ev
    assert produire() is None                                  # rien de neuf : rien d'envoye


def test_mesure_absente_reste_vide_jamais_zero(monkeypatch):
    """Barres d'avant l'ajout du notionnel brut : null, pas 0."""
    monkeypatch.setattr(app, "tape_day", lambda s, d: _bars(with_notional=False))
    ev = flowfeed.event("NQ", clock=lambda: datetime(2026, 11, 20, 9, 31))
    assert ev["cum"]["notional_calls"] is None and ev["bar"]["notional_puts"] is None
    assert ev["cum"]["net_delta"] == -3e6
    monkeypatch.setattr(app, "tape_day", lambda s, d: pd.DataFrame())
    assert flowfeed.event("NQ") is None and flowfeed.session_parquet("NQ", "2026-11-20") is None


def test_canaux_et_routes(tmp_path, monkeypatch):
    assert set(ticksapi.stream_channels(None, None, None, None, "NQ,QQQ,AAPL")) == {"flow:NQ", "flow:QQQ"}
    assert {"levels:NQ", "flow:NQ"} <= set(ticksapi.stream_channels("NQ", None, None, None, "NQ"))
    assert not any(k.startswith("flow:") for k in ticksapi.stream_channels(None, None))   # sans flow : inchange

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    monkeypatch.setenv("GEX_TICKS_TOKEN", TOKEN)
    ticksapi._hits.clear()
    monkeypatch.setattr(app, "tape_day", lambda s, d: _bars() if d == "2026-11-20" else pd.DataFrame())
    monkeypatch.setattr(flowfeed.store, "tape_days", lambda s: ["2026-11-20"])
    server = Flask(__name__)
    ticksapi.register_ticks_api(server)
    c = server.test_client()
    assert c.get("/api/v1/ticks/flow/NQ/days").status_code == 401
    assert c.get("/api/v1/ticks/flow/NQ/days", headers=AUTH).get_json() == {"days": ["2026-11-20"]}
    assert c.get("/api/v1/ticks/flow/AAPL/days", headers=AUTH).status_code == 404
    r = c.get("/api/v1/ticks/flow/NQ/2026-11-20.parquet", headers=AUTH)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert list(pd.read_parquet(io.BytesIO(r.data)).notional_calls) == [5e7, 1e7]
    assert c.get("/api/v1/ticks/flow/NQ/2026-11-19.parquet", headers=AUTH).status_code == 404
    assert c.get("/api/v1/ticks/flow/NQ/hier.parquet", headers=AUTH).status_code == 404
