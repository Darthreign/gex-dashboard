"""Bandeau d'amplification de la page Scalp : entrées (mouvement 5 min, flux) et rendu."""
from __future__ import annotations

from datetime import datetime

import sys
import time

import pandas as pd
import pytest

from gex import app, flowtape, store
from gex.metrics import ET


def _bars(closes_by_age_min: dict[int, float]) -> pd.DataFrame:
    """Bougies 1 min (heure ET naïve) : {âge en minutes: clôture}."""
    now = pd.Timestamp(datetime.now(ET).replace(tzinfo=None)).floor("min")
    rows = [{"timestamp": now - pd.Timedelta(minutes=a), "open": c, "high": c, "low": c,
             "close": c} for a, c in sorted(closes_by_age_min.items(), reverse=True)]
    return pd.DataFrame(rows)


@pytest.fixture()
def flux(monkeypatch):
    """Faux tape : points (epoch, pression $, catégorie) fournis par le test."""
    pts: list = []
    monkeypatch.setattr(flowtape.TAPE, "live_points",
                        lambda s, w=300, now=None: list(pts))
    return pts


def test_mouvement_5_min_depuis_la_bougie_assez_ancienne(monkeypatch, flux):
    monkeypatch.setattr(store, "load_prices",
                        lambda s, d: _bars({8: 30000.0, 6: 30010.0, 1: 30030.0}))
    flux += [(1.0, 200e6, 0), (2.0, 150e6, 0)]
    move, net, gross = app.scalp_inputs("NQ", 30040.0)
    assert move == 30.0                           # 30040 - clôture d'il y a 6 min (30010)
    assert net == pytest.approx(350.0) and gross == pytest.approx(350.0)


def test_pas_de_mouvement_si_aucune_bougie_recente(monkeypatch, flux):
    monkeypatch.setattr(store, "load_prices", lambda s, d: _bars({120: 30000.0, 90: 30010.0}))
    assert app.scalp_inputs("NQ", 30040.0)[0] is None       # dernière bougie trop ancienne
    monkeypatch.setattr(store, "load_prices", lambda s, d: pd.DataFrame())
    assert app.scalp_inputs("NQ", 30040.0)[0] is None


def test_banner_amplification_rendu(monkeypatch, flux):
    from gex import capturebus
    monkeypatch.setattr(capturebus, "remote_url", lambda: None)
    monkeypatch.setattr(store, "load_prices", lambda s, d: _bars({8: 30000.0, 1: 30030.0}))
    flux += [(1.0, 300e6, 0), (2.0, 80e6, 1)]
    ctx = {"zg": 29900.0, "gamma": "Gamma Positif"}
    div = app.scalp_banner("NQ", ctx, 30040.0)
    txt = str(div.to_plotly_json())
    assert "sc-tone-alert" in txt and "Amplification haussière" in txt
    assert "malgré un gamma positif" in txt


def test_banner_hors_seance_donnees_insuffisantes(monkeypatch, flux):
    from gex import capturebus
    monkeypatch.setattr(capturebus, "remote_url", lambda: None)
    monkeypatch.setattr(store, "load_prices", lambda s, d: pd.DataFrame())
    div = app.scalp_banner("NQ", {"zg": None, "gamma": None}, 30040.0)
    assert "Données insuffisantes" in str(div.to_plotly_json())


def test_graphe_sous_jacent_bougies_et_niveaux_dans_la_plage(monkeypatch):
    now = pd.Timestamp(datetime.now(ET).replace(tzinfo=None)).floor("min")
    bars = pd.DataFrame([{"timestamp": now - pd.Timedelta(minutes=m), "open": 30000.0 + m,
                          "high": 30005.0 + m, "low": 29995.0 + m, "close": 30001.0 + m}
                         for m in range(60, 0, -1)])
    monkeypatch.setattr(store, "load_prices", lambda s, d: bars)
    ctx = {"zg": 30020.0, "hvl": None, "keys": {"call_wall": 30100.0, "put_support": 20000.0},
           "walls": []}
    fig = app.scalp_price_fig("NQ", ctx, 30050.0)
    assert fig.data[0].type == "candlestick" and len(fig.data[0].x) == 60
    notes = [a.text for a in fig.layout.annotations]
    assert any("Gamma Flip" in n for n in notes) and any("Call Wall" in n for n in notes)
    assert not any("Put Support" in n for n in notes)          # 10 000 pts hors plage : pas de ligne


def test_graphe_sous_jacent_retombe_sur_le_dernier_jour(monkeypatch):
    bars = pd.DataFrame([{"timestamp": pd.Timestamp("2026-09-25 15:00") + pd.Timedelta(minutes=m),
                          "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5} for m in range(30)])
    monkeypatch.setattr(store, "load_prices",
                        lambda s, d: bars if d == "2026-09-25" else pd.DataFrame())
    monkeypatch.setattr(store, "price_days", lambda s: ["2026-09-24", "2026-09-25"])
    fig = app.scalp_price_fig("NQ", {"zg": None, "hvl": None, "keys": {}, "walls": []}, 1.5)
    assert "dernier jour disponible (2026-09-25)" in fig.layout.title.text


def test_scalp_absorption_autonome_lit_capture(monkeypatch):
    from gex import capturebus
    from gex.tickcapture import CAPTURE
    monkeypatch.setattr(capturebus, "remote_url", lambda: None)
    monkeypatch.setattr(CAPTURE, "absorption_now", lambda s: {"side": "SELL", "price": 1.0}
                        if s == "NQ" else None)
    assert app.scalp_absorption("NQ") == {"side": "SELL", "price": 1.0}


def test_scalp_absorption_separe_lit_le_miroir(monkeypatch):
    from gex import capturebus, flowtape

    class _Faux:
        def absorption(self, s):
            return {"side": "BUY", "price": 2.0} if s == "ES" else None

    monkeypatch.setattr(capturebus, "remote_url", lambda: "ws://x")
    monkeypatch.setattr(flowtape, "TAPE", _Faux())
    assert app.scalp_absorption("ES") == {"side": "BUY", "price": 2.0}


def test_scalp_absorption_recent_autonome_lit_capture(monkeypatch):
    from gex import capturebus
    from gex.tickcapture import CAPTURE
    monkeypatch.setattr(capturebus, "remote_url", lambda: None)
    monkeypatch.setattr(CAPTURE, "absorption_recent", lambda s: [{"price": 1.0}]
                        if s == "NQ" else [])
    assert app.scalp_absorption_recent("NQ") == [{"price": 1.0}]


def test_panel_absorption_toujours_visible_meme_vide():
    """La zone reste dans la page (jamais masquée) même sans détection."""
    div = app.scalp_absorb_panel("NQ", [], None)
    assert div.className == "sc-absorb"
    txt = str(div.to_plotly_json())
    assert "Aucune absorption détectée" in txt


def test_panel_absorption_liste_les_derniers_niveaux_le_plus_recent_en_tete():
    recent = [{"side": "SELL", "price": 30910.25, "ratio": 8.3, "total": 40.0,
              "n_prints": 4, "ts": time.time()},
             {"side": "BUY", "price": 30500.0, "ratio": 5.0, "total": 30.0,
              "n_prints": 3, "ts": time.time() - 60}]
    div = app.scalp_absorb_panel("NQ", recent, fresh=recent[0])
    rows = div.children[1:]
    assert len(rows) == 2
    assert "sc-absorb-active" in rows[0].className   # la fraîche clignote
    assert "sc-absorb-active" not in rows[1].className
    assert "support" in rows[0].children and "30,910.25" in rows[0].children
    assert "résistance" in rows[1].children


def test_log_scalp_signal_journalise_une_fois_par_transition(monkeypatch):
    calls = []
    app._SCALP_SIGNAL_SEEN.clear()
    app._SCALP_SIGNAL_LAST_LOGGED.clear()
    monkeypatch.setattr(app, "_journal", lambda: object())     # connexion factice non-None
    monkeypatch.setitem(sys.modules, "journal",
                        type("J", (), {"record_scalp_signal": staticmethod(
                            lambda *a, **kw: calls.append(kw))})())
    a = {"state": "amplification", "direction": 1, "tone": "alert", "title": "t"}
    app.log_scalp_signal("NQ", a, 30500.0, 40.0, 200.0, 250.0)
    app.log_scalp_signal("NQ", a, 30510.0, 41.0, 210.0, 250.0)  # même état -> pas de doublon
    a2 = {"state": "brake", "direction": 1, "tone": "ok", "title": "t2"}
    app.log_scalp_signal("NQ", a2, 30520.0, 10.0, -50.0, 100.0)  # transition -> nouvelle ligne
    assert len(calls) == 2
    assert calls[0]["symbol"] == "NQ" and calls[0]["state"] == "amplification"
    assert calls[1]["state"] == "brake"


def test_log_scalp_signal_ignore_les_etats_calmes(monkeypatch):
    calls = []
    app._SCALP_SIGNAL_SEEN.clear()
    app._SCALP_SIGNAL_LAST_LOGGED.clear()
    monkeypatch.setattr(app, "_journal", lambda: object())
    monkeypatch.setitem(sys.modules, "journal",
                        type("J", (), {"record_scalp_signal": staticmethod(
                            lambda *a, **kw: calls.append(kw))})())
    app.log_scalp_signal("NQ", {"state": "calm", "direction": 0, "tone": "neutral",
                                "title": "t"}, 1.0, 0.0, 0.0, 0.0)
    assert calls == []


def test_log_scalp_signal_sans_journal_ne_leve_pas(monkeypatch):
    app._SCALP_SIGNAL_SEEN.clear()
    app._SCALP_SIGNAL_LAST_LOGGED.clear()
    monkeypatch.setattr(app, "_journal", lambda: None)
    app.log_scalp_signal("NQ", {"state": "amplification", "direction": 1, "tone": "alert",
                                "title": "t"}, 1.0, 0.0, 0.0, 0.0)   # ne doit pas lever


def test_log_absorption_levels_ecrit_les_nouvelles_uniquement(monkeypatch):
    calls = []
    app._ABSORB_LOGGED.clear()
    monkeypatch.setattr(app, "_journal", lambda: object())
    monkeypatch.setitem(sys.modules, "journal",
                        type("J", (), {"record_absorption": staticmethod(
                            lambda *a, **kw: calls.append(kw))})())
    a1 = {"side": "SELL", "price": 30500.0, "ratio": 8.0, "total": 40.0,
         "n_prints": 4, "ts": time.time()}
    a2 = {"side": "BUY", "price": 30600.0, "ratio": 5.0, "total": 30.0,
         "n_prints": 3, "ts": time.time() - 60}
    app.log_absorption_levels("NQ", [a1, a2])
    app.log_absorption_levels("NQ", [a1, a2])              # même fenêtre relue -> rien de plus
    assert len(calls) == 2
    assert {c["price"] for c in calls} == {30500.0, 30600.0}

    a3 = {"side": "SELL", "price": 30700.0, "ratio": 9.0, "total": 45.0,
         "n_prints": 5, "ts": time.time()}
    app.log_absorption_levels("NQ", [a3, a1, a2])           # une seule vraiment nouvelle
    assert len(calls) == 3 and calls[-1]["price"] == 30700.0


def test_log_absorption_levels_vide_ne_fait_rien(monkeypatch):
    calls = []
    app._ABSORB_LOGGED.clear()
    monkeypatch.setattr(app, "_journal", lambda: object())
    monkeypatch.setitem(sys.modules, "journal",
                        type("J", (), {"record_absorption": staticmethod(
                            lambda *a, **kw: calls.append(kw))})())
    app.log_absorption_levels("NQ", [])
    assert calls == []


def test_log_absorption_levels_sans_journal_marque_vu_quand_meme(monkeypatch):
    app._ABSORB_LOGGED.clear()
    monkeypatch.setattr(app, "_journal", lambda: None)
    a = {"side": "SELL", "price": 1.0, "ratio": 1.0, "total": 1.0, "n_prints": 1, "ts": 1.0}
    app.log_absorption_levels("NQ", [a])          # ne doit pas lever
    assert 1.0 in app._ABSORB_LOGGED["NQ"]
