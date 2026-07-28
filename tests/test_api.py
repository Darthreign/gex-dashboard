"""API JSON locale (gex/api.py) : vérifie le contrat de données exposé,
pas le réseau — utilise le client de test Flask directement, sans lancer de
vrai serveur.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from flask import Flask

from gex import metrics
from gex.api import register_api
from gex.ingest import ChainSnapshot
from gex.metrics import ET
from gex.scheduler import STATE


def _seed(symbol: str, source: str = "cboe") -> None:
    """Peuple STATE[symbol] avec une chaîne minimale mais réaliste (asymétrie
    call/put pour éviter le cas dégénéré où GEX/DEX net s'annulent, cf.
    test_metrics.py)."""
    exp = (datetime.now(ET) + pd.Timedelta(days=30)).date()
    rows = []
    for typ, oi in (("C", 100.0), ("P", 200.0)):
        rows.append({
            "contract": f"TST{typ}", "strike": 100.0, "type": typ, "expiry": exp,
            "bid": 1.0, "ask": 1.2, "iv": 0.2, "open_interest": oi,
            "volume": 10.0, "delta_cboe": 0.0, "gamma_cboe": 0.0,
            "last_trade_price": 0.0,
        })
    now = datetime.now(ET)
    snap = ChainSnapshot(symbol=symbol, spot=100.0, feed_timestamp=now.replace(tzinfo=None),
                        fetched_at=now.replace(tzinfo=None), options=pd.DataFrame(rows))
    df = metrics.enrich(snap)
    summary = metrics.summarize(snap, df, with_basis=False)
    summary.source = source
    st = STATE.get(symbol)
    st.snapshot, st.enriched, st.summary = snap, df, summary


def _client():
    app = Flask(__name__)
    register_api(app)
    return app.test_client()


def test_symbols_liste_ce_qui_a_un_summary():
    _seed("TST1")
    r = _client().get("/api/v1/symbols")
    assert r.status_code == 200
    assert "TST1" in r.get_json()


def test_summary_sert_toutes_les_sources_y_compris_dxfeed():
    """Portée volontaire de la licence (cf. docstring du module) : ce flux
    local sert TOUTES les sources, y compris dxfeed — contrairement à
    gex.export, qui lui filtre parce qu'il prépare un partage à des tiers."""
    _seed("TST2", source="dxfeed")
    r = _client().get("/api/v1/TST2/summary")
    assert r.status_code == 200
    body = r.get_json()
    assert body["source"] == "dxfeed"
    assert body["spot"] == 100.0
    assert "net_dex" in body


def test_symbole_sans_donnees_404():
    r = _client().get("/api/v1/UNSYMBOLEJAMAISVU/summary")
    assert r.status_code == 404


def test_levels_et_regime():
    _seed("TST3")
    c = _client()
    r = c.get("/api/v1/TST3/levels")
    assert r.status_code == 200
    body = r.get_json()
    assert "gex_walls" in body and "key_levels" in body

    r2 = c.get("/api/v1/TST3/regime")
    assert r2.status_code == 200
    body2 = r2.get_json()
    assert "severity" in body2 and "disclaimer" in body2


def test_strikes_colonnes_attendues():
    _seed("TST4")
    r = _client().get("/api/v1/TST4/strikes")
    assert r.status_code == 200
    rows = r.get_json()["rows"]
    assert rows
    assert set(rows[0]) == {"strike", "type", "expiry", "open_interest", "gex", "dex"}


def test_cors_ouvert_car_le_garde_fou_est_le_scope_reseau():
    """cf. docstring du module : le CORS large est volontaire, la vraie
    protection est de ne jamais exposer ce serveur au-delà du poste local."""
    _seed("TST5")
    r = _client().get("/api/v1/symbols")
    assert r.headers.get("Access-Control-Allow-Origin") == "*"
