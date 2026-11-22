"""get_market_context (MCP, gex/mcp_server.py) : agrège synthèse GEX/DEX,
murs les plus proches du spot et VIX en confluence — un seul appel plutôt que
d'enchaîner get_gex_summary + get_gex_by_strike + une lecture VIX séparée.
"""
from __future__ import annotations

import json
from datetime import datetime, time as dtime

import pandas as pd

from gex import mcp_server, store
from gex.config import SETTINGS


def _seed_history(tmp_path, monkeypatch, symbol: str = "SPX") -> None:
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    now = datetime.now()
    store.append_history({
        "timestamp": now, "symbol": symbol, "spot": 100.0, "net_gex": -5e9,
        "zero_gamma": 105.0, "pc_oi": 1.2, "pc_volume": 1.1,
        "net_gex_0dte": -1e9, "basis": None, "source": "cboe", "net_dex": 2e9,
    })
    snap = pd.DataFrame([
        {"strike": 90.0, "gex": -3e9, "open_interest": 1000.0},
        {"strike": 95.0, "gex": -1e9, "open_interest": 500.0},
        {"strike": 105.0, "gex": 2e9, "open_interest": 800.0},
        {"strike": 110.0, "gex": 1e9, "open_interest": 300.0},
    ])
    store.save_snapshot(symbol, snap, now)


def test_sans_historique(tmp_path, monkeypatch):
    """Symbole connu mais aucun historique écrit (dashboard jamais lancé) :
    message clair, pas d'exception."""
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    out = mcp_server.get_market_context("SPX")
    assert "Aucun" in out


def test_murs_les_plus_proches_pas_les_plus_gros(tmp_path, monkeypatch):
    """95 (gex -1e9) est plus proche du spot que 90 (gex -3e9, plus gros en
    valeur absolue) : le mur retourné doit être le plus proche, pas le plus
    massif — cohérent avec get_gex_by_strike qui liste déjà les plus gros."""
    _seed_history(tmp_path, monkeypatch)
    out = json.loads(mcp_server.get_market_context("SPX"))
    assert out["mur_put_proche"]["strike"] == 95.0
    assert out["mur_call_proche"]["strike"] == 105.0


def test_regime_embarque_sans_recalcul_manuel(tmp_path, monkeypatch):
    _seed_history(tmp_path, monkeypatch)
    out = json.loads(mcp_server.get_market_context("SPX"))
    assert out["regime"]["gex_frein"] is False  # net_gex < 0
    assert out["net_gex"] == -5e9
    assert out["spot_vs_zero_gamma"] == -5.0


def test_pas_de_probabilite_ni_de_recommandation_dans_les_cles(tmp_path, monkeypatch):
    """Garde-fou du docstring : uniquement des données calculées, jamais un
    signal — vérifie qu'aucune clé de type scénario/proba ne s'est glissée."""
    _seed_history(tmp_path, monkeypatch)
    out = json.loads(mcp_server.get_market_context("SPX"))
    forbidden = {"probability", "probabilite", "recommendation", "recommandation", "signal"}
    assert forbidden.isdisjoint(out.keys())


def _vix_bar(ts, value):
    """Une bougie 1 min du VIX, comme les écrit la capture dxFeed (data/prices/VIX)."""
    store.append_prices("VIX", [{"timestamp": ts, "open": value, "high": value, "low": value,
                                 "close": value, "ticks": 1, "source": "dxfeed"}], ts)


def test_vix_absent_reste_none(tmp_path, monkeypatch):
    _seed_history(tmp_path, monkeypatch)
    out = json.loads(mcp_server.get_market_context("SPX"))
    assert out["vix"] is None


def test_vix_variation_du_jour(tmp_path, monkeypatch):
    _seed_history(tmp_path, monkeypatch)
    # deux points ancrés sur la MÊME journée : un simple `now - 2h` bascule
    # sur la veille quand le test tourne peu après minuit, et la variation du
    # jour devient alors indéterminable
    jour = datetime.now().date()
    _vix_bar(datetime.combine(jour, dtime(10, 0)), 15.0)
    _vix_bar(datetime.combine(jour, dtime(12, 0)), 17.8)
    out = json.loads(mcp_server.get_market_context("SPX"))
    assert out["vix"]["dernier"] == 17.8
    assert out["vix"]["source"] == "dxfeed_bougies"
    assert abs(out["vix"]["variation_du_jour"] - 2.8) < 1e-9


def test_vix_live_prioritaire_si_compte_courtier(tmp_path, monkeypatch):
    """Avec des identifiants courtier ET un tick VIX reçu, le direct dxFeed
    prime sur le pull délayé CBOE — repli sur le délayé sinon (cf. logique
    de _vix_context, qui ne dépend d'aucun état global au-delà de ça)."""
    _seed_history(tmp_path, monkeypatch)
    # deux points ancrés sur la MÊME journée : un simple `now - 2h` bascule
    # sur la veille quand le test tourne peu après minuit, et la variation du
    # jour devient alors indéterminable
    jour = datetime.now().date()
    _vix_bar(datetime.combine(jour, dtime(10, 0)), 15.0)
    _vix_bar(datetime.combine(jour, dtime(12, 0)), 17.8)
    monkeypatch.setattr(mcp_server, "credentials_present", lambda: True)
    monkeypatch.setattr(mcp_server.QUOTES, "price", lambda key: 19.5 if key == "VIX" else None)
    out = json.loads(mcp_server.get_market_context("SPX"))
    assert out["vix"]["dernier"] == 19.5
    assert out["vix"]["source"] == "dxfeed_live"
    assert abs(out["vix"]["variation_du_jour"] - 4.5) < 1e-9


def test_vix_repli_delaye_si_pas_de_tick_live(tmp_path, monkeypatch):
    """Identifiants présents mais VIX pas dans l'abonnement (price() = None,
    entitlement manquant) : repli sur le délayé, pas de trou silencieux."""
    _seed_history(tmp_path, monkeypatch)
    _vix_bar(datetime.combine(datetime.now().date(), dtime(11, 0)), 16.4)
    monkeypatch.setattr(mcp_server, "credentials_present", lambda: True)
    monkeypatch.setattr(mcp_server.QUOTES, "price", lambda key: None)
    out = json.loads(mcp_server.get_market_context("SPX"))
    assert out["vix"]["dernier"] == 16.4
    assert out["vix"]["source"] == "dxfeed_bougies"


def test_vix_du_digest_replie_sur_les_bougies_dxfeed(tmp_path, monkeypatch):
    """Sans cotation en direct (au demarrage), le VIX vient de la derniere bougie
    dxFeed, plus de l'historique CBOE qui n'est plus alimente."""
    from gex import digest
    _seed_history(tmp_path, monkeypatch)
    monkeypatch.setattr(digest, "store", store, raising=False)
    from gex import rtquote
    monkeypatch.setattr(rtquote, "credentials_present", lambda: False)
    store.append_index_spot("vix", {"timestamp": datetime(2026, 11, 19, 16, 9), "vix": 27.85})
    assert digest._current_vix() == 27.85            # dernier recours : rien d'autre
    _vix_bar(datetime(2026, 11, 20, 15, 59), 35.89)
    assert digest._current_vix() == 35.89
