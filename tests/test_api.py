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


def test_vix_endpoint(monkeypatch):
    """`/api/v1/vix` renvoie la valeur courante, le seuil, et si on est au-dessus."""
    from gex import digest
    monkeypatch.setattr(digest, "_current_vix", lambda: 16.5)
    r = _client().get("/api/v1/vix")
    assert r.status_code == 200
    body = r.get_json()
    assert body["available"] is True and body["vix"] == 16.5
    assert body["seuil"] == digest.VIX_SEUIL and body["above"] is True
    assert body["grade"]["label"] == "Normal-haut"      # 16.5 -> régime gradé
    # indisponible -> available False
    monkeypatch.setattr(digest, "_current_vix", lambda: None)
    assert _client().get("/api/v1/vix").get_json()["available"] is False


def test_digest_expose_les_familles():
    """Le digest doit exposer le détail par famille (score/statut/confiance) —
    c'est ce que le journal stocke pour le backtest."""
    from gex import digest as digest_mod

    def _row(sym, gex, dex):
        return {"symbol": sym, "net_gex": gex, "net_dex": dex, "hist": None}

    rows = [_row(s, +1e9, +1e9) for s in ("SPX", "SPY", "ES")]
    rows += [_row("NDX", -1e9, +1e9), _row("QQQ", -1e9, +1e9), _row("NQ", +1e9, +1e9)]
    d = digest_mod.build_digest(rows, vix=12.0)
    assert set(d.families) == {"S&P", "Nasdaq"}
    assert d.families["Nasdaq"]["statut"] == "neg"
    assert "score" in d.families["Nasdaq"] and "confiance" in d.families["Nasdaq"]


def test_count_reversals():
    from gex.api import _count_reversals
    # monte à 130 (établit la hausse), reflue à 95 (1er retournement), remonte
    # à 135 (2e). Le mouvement initial d'établissement ne compte pas.
    closes = [100, 110, 105, 130, 95, 100, 135]
    assert _count_reversals(closes, threshold=30) == 2
    assert _count_reversals(closes, threshold=100) == 0   # rien d'assez ample
    assert _count_reversals([], threshold=30) == 0


def test_session_context_depuis_les_bougies(monkeypatch):
    from gex import store
    from gex.api import _session_context

    jour = pd.DataFrame({
        "timestamp": pd.date_range("2026-08-03 09:30", periods=5, freq="min"),
        "open":  [20000, 20050, 20100, 20080, 20120],
        "high":  [20060, 20110, 20130, 20120, 20160],
        "low":   [19990, 20040, 20070, 20060, 20100],
        "close": [20050, 20100, 20080, 20110, 20150],
    })
    veille = pd.DataFrame({
        "timestamp": pd.date_range("2026-07-31 09:30", periods=2, freq="min"),
        "open": [19900, 19950], "high": [20000, 19980],
        "low": [19850, 19900], "close": [19950, 19970],
    })

    def fake_load(symbol, day):
        if day == "2026-08-03":
            return jour
        if day == "2026-07-31":
            return veille
        return pd.DataFrame()

    monkeypatch.setattr(store, "load_prices", fake_load)
    ctx = _session_context("NQ", "2026-08-03")
    assert ctx["available"] is True
    assert ctx["open"] == 20000 and ctx["close"] == 20150
    assert ctx["high"] == 20160 and ctx["low"] == 19990
    assert ctx["prev_close"] == 19970                 # dernière clôture de la veille
    assert ctx["gap"] == round(20000 - 19970, 2)
    assert ctx["max_up"] == 160 and ctx["max_down"] == 10
    assert ctx["weekday"] == 0                         # 2026-08-03 est un lundi


def test_session_context_indisponible(monkeypatch):
    from gex import store
    from gex.api import _session_context
    monkeypatch.setattr(store, "load_prices", lambda s, d: pd.DataFrame())
    ctx = _session_context("NQ", "2026-08-03")
    assert ctx["available"] is False
    assert ctx["weekday"] == 0


def test_cors_ouvert_car_le_garde_fou_est_le_scope_reseau():
    """cf. docstring du module : le CORS large est volontaire, la vraie
    protection est de ne jamais exposer ce serveur au-delà du poste local."""
    _seed("TST5")
    r = _client().get("/api/v1/symbols")
    assert r.headers.get("Access-Control-Allow-Origin") == "*"
