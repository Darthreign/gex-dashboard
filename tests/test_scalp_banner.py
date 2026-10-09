"""Bandeau d'amplification de la page Scalp : entrées (mouvement 5 min, flux) et rendu."""
from __future__ import annotations

from datetime import datetime, timedelta

import sys
import time

import pandas as pd
import pytest

from gex import app, flowtape, store, tickcapture
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


def test_is_scalp_path_reconnait_v2_et_v1():
    assert app.is_scalp_path("/scalp")
    assert app.is_scalp_path("/scalpv1")
    assert app.is_scalp_path("/scalp/")
    assert app.is_scalp_path("/scalpv1/")
    assert not app.is_scalp_path("/")
    assert not app.is_scalp_path(None)
    assert not app.is_scalp_path("/scalping-autre-chose")  # pas un simple préfixe


def test_mouvement_5_min_depuis_la_bougie_assez_ancienne(monkeypatch, flux):
    app._PRICES_CACHE.clear()
    monkeypatch.setattr(store, "load_prices",
                        lambda s, d: _bars({8: 30000.0, 6: 30010.0, 1: 30030.0}))
    flux += [(1.0, 200e6, 0), (2.0, 150e6, 0)]
    move, net, gross = app.scalp_inputs("NQ", 30040.0)
    assert move == 30.0                           # 30040 - clôture d'il y a 6 min (30010)
    assert net == pytest.approx(350.0) and gross == pytest.approx(350.0)


def test_pas_de_mouvement_si_aucune_bougie_recente(monkeypatch, flux):
    app._PRICES_CACHE.clear()
    monkeypatch.setattr(store, "load_prices", lambda s, d: _bars({120: 30000.0, 90: 30010.0}))
    assert app.scalp_inputs("NQ", 30040.0)[0] is None       # dernière bougie trop ancienne
    app._PRICES_CACHE.clear()
    monkeypatch.setattr(store, "load_prices", lambda s, d: pd.DataFrame())
    assert app.scalp_inputs("NQ", 30040.0)[0] is None


def test_scalp_inputs_swing_utilise_les_ticks_pas_les_bougies(monkeypatch, flux):
    """scalp_inputs_swing ne doit RIEN lire de store.load_prices (bougies 1 min,
    circuit de /scalpv1) — seulement store.load_ticks, via gex/bars.py."""
    monkeypatch.setattr(store, "load_prices",
                        lambda s, d: (_ for _ in ()).throw(AssertionError("ne doit pas être appelé")))
    now = time.time()
    prices = [30000.0 + i * 0.5 for i in range(100)] + [30050.0 - i * 0.5 for i in range(100)]
    ticks = pd.DataFrame([{"ts": now - (200 - i), "price": p,
                           "side": "BUY" if i % 2 == 0 else "SELL", "volume": 1}
                          for i, p in enumerate(prices)])
    monkeypatch.setattr(store, "load_ticks", lambda s, d: ticks)
    monkeypatch.setattr(tickcapture, "_session_day", lambda ts: "2026-10-03")
    flux += [(1.0, 200e6, 0)]
    move, net, gross = app.scalp_inputs_swing("NQ", 30020.0)
    assert net == pytest.approx(200.0) and gross == pytest.approx(200.0)  # flux inchangé


def test_scalp_inputs_swing_none_sans_ticks(monkeypatch, flux):
    monkeypatch.setattr(store, "load_ticks", lambda s, d: pd.DataFrame())
    monkeypatch.setattr(tickcapture, "_session_day", lambda ts: "2026-10-03")
    assert app.scalp_inputs_swing("NQ", 30020.0)[0] is None


def test_log_scalp_signal_cloisonne_par_basis(monkeypatch):
    """Deux bases différentes pour le même symbole ne doivent pas partager le
    même dédup/cooldown — sinon un onglet /scalp et un onglet /scalpv1 ouverts
    en même temps se marcheraient dessus (cf. commentaire dans app.py)."""
    app._SCALP_SIGNAL_SEEN.clear()
    app._SCALP_SIGNAL_LAST_LOGGED.clear()
    calls = []
    monkeypatch.setattr(app, "_journal", lambda: None)  # pas de vraie DB dans ce test
    a1 = {"state": "amplification", "tone": "alert", "direction": 1, "title": "t1"}
    app.log_scalp_signal("NQ", a1, 30000.0, 40.0, 100.0, 200.0, basis="fenetre_5min")
    app.log_scalp_signal("NQ", a1, 30000.0, 40.0, 100.0, 200.0, basis="swing_v60")
    # les deux bases ont chacune vu une PREMIÈRE transition -> les deux "vues"
    assert app._SCALP_SIGNAL_SEEN[("NQ", "fenetre_5min")] == ("amplification", 1)
    assert app._SCALP_SIGNAL_SEEN[("NQ", "swing_v60")] == ("amplification", 1)


def test_banner_amplification_rendu(monkeypatch, flux):
    app._PRICES_CACHE.clear()
    from gex import capturebus
    monkeypatch.setattr(capturebus, "remote_url", lambda: None)
    monkeypatch.setattr(store, "load_prices", lambda s, d: _bars({8: 30000.0, 1: 30030.0}))
    flux += [(1.0, 300e6, 0), (2.0, 80e6, 1)]
    ctx = {"zg": 29900.0, "gamma": "Gamma Positif"}
    div = app.scalp_banner("NQ", ctx, 30040.0, "fr")
    txt = str(div.to_plotly_json())
    assert "sc-tone-alert" in txt and "Amplification haussière" in txt
    assert "malgré un gamma positif" in txt


def test_banner_swing_amplification_rendu(monkeypatch, flux):
    """Bout en bout pour /scalp v2 (swing=True) : scalp_inputs_swing ->
    scalp.assess -> rendu, exactement comme test_banner_amplification_rendu
    mais sur la base swing — vérifie que le branchement `swing` de
    scalp_banner produit un bandeau cohérent, pas juste que les deux
    fonctions d'entrée existent séparément. C'est la vérification de bout en
    bout que le marché fermé (samedi, ctx=None en vrai) empêche de faire en
    conditions réelles ce soir."""
    app._PRICES_CACHE.clear()
    from gex import capturebus
    monkeypatch.setattr(capturebus, "remote_url", lambda: None)
    now = time.time()
    # poussée haussière franche sur assez de ticks pour >= 5 barres-volume=60
    # (swing_move l'exige, cf. gex/bars.py) : 420 prints, 1 pt tous les 10 ticks
    n = 420
    prices = [30000.0 + i * 0.1 for i in range(n)]
    ticks = pd.DataFrame([{"ts": now - (n + 10 - i), "price": p,
                           "side": "BUY" if i % 2 == 0 else "SELL", "volume": 1}
                          for i, p in enumerate(prices)])
    monkeypatch.setattr(store, "load_ticks", lambda s, d: ticks)
    monkeypatch.setattr(tickcapture, "_session_day", lambda ts: "2026-10-03")
    flux += [(1.0, 300e6, 0), (2.0, 80e6, 1)]
    ctx = {"zg": 29900.0, "gamma": "Gamma Positif"}
    div = app.scalp_banner("NQ", ctx, 30059.0, "fr", swing=True)
    txt = str(div.to_plotly_json())
    assert "sc-tone-alert" in txt and "Amplification haussière" in txt


def test_scalp_v2_hedge_data_mode_live(flux):
    """scalp_v2_hedge_data (JSON pour Lightweight Charts) doit porter la
    même donnée que hedge_fig (Plotly) — 4 catégories + le net cumulé,
    cf. _hedge_series partagée entre les deux."""
    flux += [(time.time() - 1.0, 200e6, 0), (time.time() - 0.5, -50e6, 2)]
    day = datetime.now(ET).strftime("%Y-%m-%d")
    out = app.scalp_v2_hedge_data("NQ", -1, day)  # -1 = mode live
    assert len(out["series"]) == 5
    names = {s["name"] for s in out["series"]}
    assert "Net cumulé" in names
    net = next(s for s in out["series"] if s["name"] == "Net cumulé")
    assert len(net["points"]) > 0
    assert set(net["points"][0]) == {"time", "value"}


def test_scalp_v2_hedge_data_vide_sans_flux(monkeypatch):
    monkeypatch.setattr(flowtape.TAPE, "live_points", lambda s, w=300, now=None: [])
    day = datetime.now(ET).strftime("%Y-%m-%d")
    out = app.scalp_v2_hedge_data("NQ", -1, day)
    assert out == {"series": []}


def test_banner_hors_seance_donnees_insuffisantes(monkeypatch, flux):
    app._PRICES_CACHE.clear()
    from gex import capturebus
    monkeypatch.setattr(capturebus, "remote_url", lambda: None)
    monkeypatch.setattr(store, "load_prices", lambda s, d: pd.DataFrame())
    div = app.scalp_banner("NQ", {"zg": None, "gamma": None}, 30040.0, "fr")
    assert "Données insuffisantes" in str(div.to_plotly_json())


def test_graphe_sous_jacent_bougies_et_niveaux_dans_la_plage(monkeypatch):
    app._LIVE_BARS.clear()
    app._PRICES_CACHE.clear()
    now = pd.Timestamp(datetime.now(ET).replace(tzinfo=None)).floor("min")
    bars = pd.DataFrame([{"timestamp": now - pd.Timedelta(minutes=m), "open": 30000.0 + m,
                          "high": 30005.0 + m, "low": 29995.0 + m, "close": 30001.0 + m}
                         for m in range(60, 0, -1)])
    monkeypatch.setattr(store, "load_prices", lambda s, d: bars)
    ctx = {"zg": 30020.0, "hvl": None, "keys": {"call_wall": 30100.0, "put_support": 20000.0},
           "walls": []}
    fig = app.scalp_price_fig("NQ", ctx, 30050.0)
    # 60 bougies achevées + la minute en cours reconstruite en direct (cf.
    # _update_live_bar) : sans elle, le graphe ne montrait rien de moins de 1-2 min.
    assert fig.data[0].type == "candlestick" and len(fig.data[0].x) == 61
    assert fig.data[0].close[-1] == 30050.0
    notes = [a.text for a in fig.layout.annotations]
    assert any("Gamma Flip" in n for n in notes) and any("Call Wall" in n for n in notes)
    assert not any("Put Support" in n for n in notes)          # 10 000 pts hors plage : pas de ligne


def test_graphe_sous_jacent_comble_le_trou_si_le_disque_a_du_retard(monkeypatch):
    """Bug constaté le 2026-09-30 en direct : le disque n'a pas encore écrit la
    minute qui vient de se terminer (flush_prices, jusqu'à 30 s de retard) alors
    que l'horloge a déjà basculé sur la suivante — sans repli, un trou d'une
    bougie apparaissait entre la dernière du disque et la minute en cours."""
    app._LIVE_BARS.clear()
    app._PRICES_CACHE.clear()
    now = pd.Timestamp(datetime.now(ET).replace(tzinfo=None)).floor("min")
    # le disque s'arrête 2 minutes avant la minute courante (now-4, now-3, now-2)
    bars = pd.DataFrame([{"timestamp": now - pd.Timedelta(minutes=m), "open": 30000.0,
                          "high": 30000.0, "low": 30000.0, "close": 30000.0}
                         for m in (4, 3, 2)])
    monkeypatch.setattr(store, "load_prices", lambda s, d: bars)
    ctx = {"zg": None, "hvl": None, "keys": {}, "walls": []}
    now_et = datetime.now(ET)
    # la minute qui vient de se terminer (now - 1 min) a déjà été vue en direct...
    # milieu de la minute précédente, quelle que soit la seconde d'exécution
    # (à now - 1 min 05 s, un test lancé dans les 5 premières secondes d'une
    # minute tombait deux minutes en arrière et créait lui-même le trou)
    prev_minute = now_et.replace(second=0, microsecond=0) - timedelta(minutes=1)
    app._update_live_bar("NQ", 30040.0, prev_minute + timedelta(seconds=30))
    # ...puis l'horloge avance d'une minute avant que le disque n'ait rattrapé
    fig = app.scalp_price_fig("NQ", ctx, 30050.0)
    xs = list(fig.data[0].x)
    assert len(xs) == 5, "3 bougies du disque + les 2 manquantes (now-1min, now), pas de trou"
    # pas de saut de plus d'une minute entre deux points consécutifs
    diffs = [(pd.Timestamp(xs[i]) - pd.Timestamp(xs[i - 1])) for i in range(1, len(xs))]
    assert all(d == pd.Timedelta(minutes=1) for d in diffs)
    assert fig.data[0].close[-1] == 30050.0          # la minute courante reflète le spot passé


def test_update_live_bar_accumule_puis_retient_apres_le_changement_de_minute():
    app._LIVE_BARS.clear()
    m0 = datetime(2026, 9, 30, 15, 32, 10, tzinfo=ET)
    app._update_live_bar("NQ", 30000.0, m0)
    app._update_live_bar("NQ", 30010.0, m0.replace(second=40))     # même minute : high/close bougent
    live = app._update_live_bar("NQ", 29995.0, m0.replace(second=50))
    minute0 = m0.replace(second=0, microsecond=0, tzinfo=None)
    assert live[minute0] == {"open": 30000.0, "high": 30010.0, "low": 29995.0, "close": 29995.0}
    live2 = app._update_live_bar("NQ", 30500.0, m0 + timedelta(minutes=1))
    minute1 = minute0 + timedelta(minutes=1)
    # nouvelle minute : nouvelle entrée qui démarre à ce spot, l'ancienne RESTE
    # (cf. test du trou ci-dessus) — pas remplacée, purgée seulement après
    # LIVE_BARS_KEEP_MIN minutes
    assert live2[minute1] == {"open": 30500.0, "high": 30500.0, "low": 30500.0, "close": 30500.0}
    assert minute0 in live2


def test_update_live_bar_purge_les_minutes_trop_vieilles():
    app._LIVE_BARS.clear()
    m0 = datetime(2026, 9, 30, 15, 32, 0, tzinfo=ET)
    app._update_live_bar("NQ", 30000.0, m0)
    live = app._update_live_bar("NQ", 30500.0, m0 + timedelta(minutes=app.LIVE_BARS_KEEP_MIN + 1))
    assert m0.replace(tzinfo=None) not in live


def test_graphe_sous_jacent_retombe_sur_le_dernier_jour(monkeypatch):
    app._PRICES_CACHE.clear()
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
    div = app.scalp_absorb_panel("NQ", [], None, "fr")
    assert div.className == "sc-absorb"
    txt = str(div.to_plotly_json())
    assert "Aucune absorption détectée" in txt


def test_panel_absorption_liste_les_derniers_niveaux_le_plus_recent_en_tete():
    recent = [{"side": "SELL", "price": 30910.25, "ratio": 8.3, "total": 40.0,
              "n_prints": 4, "ts": time.time()},
             {"side": "BUY", "price": 30500.0, "ratio": 5.0, "total": 30.0,
              "n_prints": 3, "ts": time.time() - 60}]
    div = app.scalp_absorb_panel("NQ", recent, fresh=recent[0], lang="fr")
    rows = div.children[1:]
    assert len(rows) == 2
    assert "sc-absorb-active" in rows[0].className   # la fraîche clignote
    assert "sc-absorb-active" not in rows[1].className
    assert "support" in rows[0].children and "30,910.25" in rows[0].children
    assert "résistance" in rows[1].children


def test_panel_absorption_lang_en():
    """Bug trouvé le 2026-09-30 : le panneau d'absorption était câblé en
    français en dur, sans passer par le système de traduction."""
    div = app.scalp_absorb_panel("NQ", [], None, "en")
    assert "No absorption detected" in str(div.to_plotly_json())
    recent = [{"side": "SELL", "price": 30910.25, "ratio": 8.3, "total": 40.0,
              "n_prints": 4, "ts": time.time()}]
    div = app.scalp_absorb_panel("NQ", recent, fresh=recent[0], lang="en")
    assert "support" in div.children[1].children      # SELL -> support (identique en/fr)
    div_buy = app.scalp_absorb_panel("NQ", [{**recent[0], "side": "BUY"}],
                                     fresh=recent[0], lang="en")
    assert "resistance" in div_buy.children[1].children and "résistance" not in div_buy.children[1].children


def test_scalp_head_lang_en():
    """Bug trouvé le 2026-10-01 : gamma/VIX restaient en français en EN,
    _GAMMA_EN existait mais n'était jamais appelé depuis scalp_head."""
    ctx = {"open": 30700.0, "zg": 30655.0, "gamma": "Gamma Positif", "vix": 16.3}
    div = app.scalp_head("NQ", "en", ctx, 30745.0)
    txt = str(div.to_plotly_json())
    assert "Positive Gamma" in txt and "Gamma Positif" not in txt
    assert "Normal-high" in txt and "Normal-haut" not in txt
    assert "favorable for whipsaws" in txt and "allers-retours" not in txt
    assert "pts since the open" in txt and "depuis l'open" not in txt


def test_banner_voyants_lang_en(monkeypatch, flux):
    app._PRICES_CACHE.clear()
    from gex import capturebus
    monkeypatch.setattr(capturebus, "remote_url", lambda: None)
    monkeypatch.setattr(store, "load_prices", lambda s, d: _bars({8: 30000.0, 1: 30030.0}))
    flux += [(1.0, 300e6, 0), (2.0, 80e6, 1)]
    ctx = {"zg": 29900.0, "gamma": "Gamma Positif"}
    div = app.scalp_banner("NQ", ctx, 30040.0, "en")
    txt = str(div.to_plotly_json())
    assert "movement" in txt and "flow" in txt
    assert "bullish" in txt and "haussière" not in txt


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


def test_bandeau_v2_affiche_la_lecture_normalisee(monkeypatch):
    """Excès de +1 EM au-dessus du flip, absorption d'acheteurs au prix :
    setup de rejet baissier, avec le statut « non validé » sans rapport."""
    import time as _t
    from gex import app as app_mod
    monkeypatch.setattr(app_mod, "scalp_inputs_swing", lambda s, spot: (30.0, 0.0, 0.0))
    monkeypatch.setattr(app_mod, "scalp_absorption_recent", lambda s: [])
    monkeypatch.setattr(app_mod, "_absorption_events",
                        lambda s: [{"ts": _t.time() - 30, "price": 30050.0, "side": "BUY"}])
    monkeypatch.setattr(app_mod, "_edge_params",
                        lambda s: (__import__("gex.edge").edge.EdgeParams(), False, ""))
    monkeypatch.setattr(app_mod, "_journal", lambda: None)
    ctx = {"zg": 29900.0, "gamma": "Gamma Positif", "open": 30000.0, "em": 50.0,
           "gex0": 1e9, "keys": {}, "walls": [], "hvl": None, "vix": None}
    out = str(app_mod.scalp_banner("NQ", ctx, 30050.0, "fr", None, edge=True).to_plotly_json())
    assert "Setup rejet baissier" in out and "absorption opposée" in out
    assert "edge NON validé" in out
    # /scalpv1 (edge=False) : inchangé, pas de lecture normalisée
    monkeypatch.setattr(app_mod, "scalp_inputs", lambda s, spot: (30.0, 0.0, 0.0))
    v1 = str(app_mod.scalp_banner("NQ", ctx, 30050.0, "fr", None).to_plotly_json())
    assert "sc-edge" not in v1


def test_bandeau_frein_active_par_la_couverture():
    from gex import app as A

    def texte(x):
        if isinstance(x, str):
            return x
        ch = getattr(x, "children", None)
        if isinstance(ch, list):
            return "".join(texte(c) for c in ch)
        return texte(ch) if ch is not None else ""
    r = {"ext_em": -0.8, "zone": "accelerateur", "setup": "fade", "fade_dir": 1,
         "confirmations": ["flux"], "excess_dir": -1, "validated": False, "test_days": None,
         "em": 200.0, "dist_flip_em": -0.5, "veto": None}
    assert "FREIN ACTIVÉ" in texte(A.scalp_edge_line(r, "fr"))
    assert "BRAKE ON" in texte(A.scalp_edge_line(r, "en"))
    # sans couverture à contre-sens : libellé habituel
    r["confirmations"] = ["absorption"]
    assert "FREIN ACTIVÉ" not in texte(A.scalp_edge_line(r, "fr"))
