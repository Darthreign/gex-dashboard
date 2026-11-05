"""Flux des niveaux et du profil de /scalp pour un indicateur distant."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import json

from gex import app, broadcast

CTX = {"snap_spot": 20000.0, "zg": 20100.0, "hvl": None,
       "keys": {"call_wall": 20200.0, "put_support": 19800.0},
       "walls": [("GEX1", 20200.0, 4e7), ("GEX2", 19900.0, -2e7)],
       "keys_0dte": {}, "walls_0dte": [("GEX1", 20100.0, -1.2e7)]}


def _chain(monkeypatch, at=datetime(2026, 11, 5, 14, 30, tzinfo=UTC)):
    monkeypatch.setattr(app, "scalp_context", lambda s: CTX)
    monkeypatch.setattr(app, "chain_state", lambda s: SimpleNamespace(snapshot=SimpleNamespace(fetched_at=at)))
    monkeypatch.setattr(app, "_scalp_live_spot", lambda s, c: 20000.0)


def test_niveaux_conformes_au_contrat(monkeypatch):
    _chain(monkeypatch)
    d = app.scalp_feed_levels("NQ")
    assert d["as_of"] == "2026-11-05T14:30:00Z"
    assert set(d) == {"symbol", "as_of", "levels", "levels_0dte"}
    assert set(d["levels"][0]) == {"name", "price", "color", "kind", "gex"}
    by_name = {r["name"]: r for r in d["levels"]}
    assert by_name["GEX1"]["gex"] == 4e7 and by_name["Call Wall"]["gex"] is None
    assert by_name["Call Wall"]["kind"] == "cw" and by_name["Gamma Flip"]["kind"] == "zg"
    assert "HVL" not in by_name                 # absent : omis, jamais inventé
    assert d["levels_0dte"][0]["gex"] == -1.2e7


def test_as_of_toujours_en_utc(monkeypatch):
    _chain(monkeypatch, datetime(2026, 11, 5, 14, 30))
    assert app.scalp_feed_levels("NQ")["as_of"] == "2026-11-05T14:30:00Z"


def test_profil_et_absence_de_spot(monkeypatch):
    _chain(monkeypatch)
    monkeypatch.setattr(app, "scalp_gex_profile", lambda s, spot: [{"price": 20000.0, "oi": 1.25, "vol": -0.3}])
    d = app.scalp_feed_profile("NQ")
    assert d["profile"] == [{"price": 20000.0, "oi": 1.25, "vol": -0.3}]
    texte = json.dumps([d, app.scalp_feed_levels("NQ")])
    assert "spot" not in texte and "candles" not in texte


def test_profil_vide_rien_a_publier(monkeypatch):
    _chain(monkeypatch)
    monkeypatch.setattr(app, "scalp_gex_profile", lambda s, spot: [])
    assert app.scalp_feed_profile("NQ") is None


def test_produits_servis_et_canal_unique():
    assert set(app.scalp_feed_channels(None)) == {"levels:NQ", "profile:NQ", "levels:ES", "profile:ES"}
    assert set(app.scalp_feed_channels("NQ,CL,MNQ")) == {"levels:NQ", "profile:NQ"}
    assert app.scalp_feed_channels("CL") == {} and app.scalp_feed_channels("") == {}
    assert app.scalp_feed_channels("NQ")["levels:NQ"] is app.scalp_feed_channels("NQ")["levels:NQ"]


def test_rien_de_publie_si_le_contenu_ne_change_pas(monkeypatch):
    broadcast._channels.pop(("scalp-feed", "levels", "ES"), None)
    contenu = {"v": {"symbol": "ES", "levels": []}}
    monkeypatch.setitem(app.SCALP_FEED_BUILDERS, "levels", lambda s: contenu["v"])
    ch = app.scalp_feed_channel("levels", "ES")
    p = ch._producer
    assert p().startswith("data: ")
    assert p() is None
    contenu["v"] = {"symbol": "ES", "levels": [1]}
    assert p() is not None
    broadcast._channels.pop(("scalp-feed", "levels", "ES"), None)
