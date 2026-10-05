"""Données du graphique Lightweight Charts de /scalp v2 (gex/app.py::scalp_v2_chart_data)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pandas as pd
import pytest

from gex import app, store, tickcapture
from gex.metrics import ET


def _ticks(n: int, start_price: float = 30000.0, end: float | None = None) -> pd.DataFrame:
    end = end if end is not None else time.time()
    rows = [{"ts": end - (n - i) * 1.0, "price": start_price + i * 0.25,
             "side": "BUY" if i % 2 == 0 else "SELL", "volume": 1}
            for i in range(n)]
    return pd.DataFrame(rows)


def _price_bars(n: int, start_price: float = 30000.0) -> pd.DataFrame:
    """Bougies 1 min (heure ET naïve), les plus récentes en dernier."""
    now = pd.Timestamp(datetime.now(ET).replace(tzinfo=None)).floor("min")
    rows = [{"timestamp": now - pd.Timedelta(minutes=(n - i)), "open": start_price + i,
             "high": start_price + i + 1, "low": start_price + i - 1, "close": start_price + i}
            for i in range(n)]
    return pd.DataFrame(rows)


def _price_bars_with_swing(n: int, start_price: float = 30000.0) -> pd.DataFrame:
    """Bougies 1 min formant un aller-retour net (monte puis redescend de
    plus que move_threshold) — pour vérifier qu'un vrai pivot swing est
    détecté sur base temps, pas seulement l'absence de faux positif."""
    now = pd.Timestamp(datetime.now(ET).replace(tzinfo=None)).floor("min")
    half = n // 2
    prices = [start_price + i * 3.0 for i in range(half)] + \
        [start_price + (half - 1) * 3.0 - i * 3.0 for i in range(n - half)]
    rows = [{"timestamp": now - pd.Timedelta(minutes=(n - i)), "open": p,
             "high": p + 1, "low": p - 1, "close": p} for i, p in enumerate(prices)]
    return pd.DataFrame(rows)


def _only_today(bars: pd.DataFrame):
    """`store.load_prices` réaliste : seule la date calendaire ET du jour a
    ces bougies, tout autre jour (notamment la veille, lue depuis le
    2026-10-05 pour compléter la séance avant 18h ET — cf. `today_cal` dans
    `scalp_v2_chart_data`) est vide. Un mock aveugle au jour dupliquait les
    mêmes bougies sur "hier" ET "aujourd'hui", faussant les pivots swing."""
    today_cal = datetime.now(ET).strftime("%Y-%m-%d")
    return lambda symbol, day: bars if day == today_cal else pd.DataFrame()


_CTX = {"zg": 29900.0, "hvl": 29950.0, "keys": {"call_wall": 30200.0}, "walls": []}


@pytest.fixture(autouse=True)
def _clear_prices_cache():
    """`_load_prices_cached` (gex/app.py, ajouté le 2026-10-05) met en cache
    `store.load_prices` 2s dans un dict MODULE-LEVEL — qui survit donc entre
    deux tests de ce fichier. Comme ils s'enchaînent largement sous 2s avec la
    même clé (symbole + date calendaire du jour réel, identique pour tous les
    tests d'une même exécution), un test pollue le cache pour le suivant sans
    ce reset (ex. un premier test "pas de bougies" fait recevoir un résultat
    vide au test suivant, qui simule pourtant 200 bougies)."""
    app._PRICES_CACHE.clear()
    yield


# --- TF par défaut (temps, "t1") : lit store.load_prices, pas les ticks ----

def test_defaut_temps_sans_bougies_renvoie_des_niveaux_sans_candles(monkeypatch):
    monkeypatch.setattr(store, "load_ticks", lambda s, d: pd.DataFrame())
    monkeypatch.setattr(store, "load_prices", lambda s, d: pd.DataFrame())
    monkeypatch.setattr(store, "tick_days", lambda s: [])
    monkeypatch.setattr(tickcapture, "_session_day", lambda ts: "2026-10-03")
    out = app.scalp_v2_chart_data("NQ", _CTX, 30040.0)
    assert out["candles"] == [] and out["markers"] == []
    assert len(out["levels"]) == 3  # zg, hvl, call_wall


def test_defaut_temps_construit_des_bougies_1min(monkeypatch):
    # day_ticks non vide seulement pour satisfaire le repli éventuel —
    # le chemin "t" ne les utilise pas pour les candles, seulement pour
    # order_flow (vide ici, pas testé).
    monkeypatch.setattr(store, "load_ticks", lambda s, d: pd.DataFrame())
    monkeypatch.setattr(store, "load_prices", _only_today(_price_bars(200)))
    monkeypatch.setattr(store, "tick_days", lambda s: [])
    monkeypatch.setattr(tickcapture, "_session_day", lambda ts: "2026-10-03")
    # spot = dernière close de _price_bars(200) (30000 + 199) — depuis l'ajout
    # de la bougie live (2026-10-05, cf. _update_live_bar dans
    # scalp_v2_chart_data), un spot différent du dernier close créerait un
    # retracement artificiel sur cette seule bougie ajoutée, polluant le
    # compte de pivots que ce test vérifie précisément.
    out = app.scalp_v2_chart_data("NQ", _CTX, 30199.0)  # tf par défaut = "t1"
    assert len(out["candles"]) > 0
    c = out["candles"][0]
    assert set(c) == {"time", "open", "high", "low", "close"}
    # Prix monotone (_price_bars, +1pt/barre) : un seul pivot "initial" est
    # confirmé (le zigzag retient le point de départ comme un L dès que le
    # prix s'en est écarté de move_threshold, même sans vrai retracement —
    # comportement standard de l'algorithme, pas une limitation du TF temps,
    # cf. test_tf_temps_calcule_des_pivots_swing pour un vrai aller-retour).
    assert len(out["markers"]) == 1
    assert out["markers"][0]["kind"] == "L"
    assert out["levels"] and set(out["levels"][0]) == {"name", "price", "color"}


def test_tf_temps_calcule_des_pivots_swing(monkeypatch):
    """Demande explicite (2026-10-05) : les pivots swing (zigzag) ne sont
    plus réservés aux barres-volume — même moteur, mêmes seuils, appliqué
    aux bougies-temps aussi."""
    monkeypatch.setattr(store, "load_ticks", lambda s, d: pd.DataFrame())
    monkeypatch.setattr(store, "load_prices", _only_today(_price_bars_with_swing(60)))
    monkeypatch.setattr(store, "tick_days", lambda s: [])
    monkeypatch.setattr(tickcapture, "_session_day", lambda ts: "2026-10-03")
    out = app.scalp_v2_chart_data("NQ", _CTX, 30040.0, tf="t1")
    assert out["markers"], "un aller-retour net doit produire au moins un pivot confirmé"
    m = out["markers"][0]
    assert set(m) == {"time", "price", "kind"}
    assert m["kind"] in ("H", "L")


def test_tf_temps_reechantillonne(monkeypatch):
    monkeypatch.setattr(store, "load_ticks", lambda s, d: pd.DataFrame())
    monkeypatch.setattr(store, "load_prices", _only_today(_price_bars(200)))
    monkeypatch.setattr(store, "tick_days", lambda s: [])
    monkeypatch.setattr(tickcapture, "_session_day", lambda ts: "2026-10-03")
    out_1min = app.scalp_v2_chart_data("NQ", _CTX, 30040.0, tf="t1")
    out_5min = app.scalp_v2_chart_data("NQ", _CTX, 30040.0, tf="t5")
    assert len(out_5min["candles"]) < len(out_1min["candles"])


# --- TF volume ("v60" etc.) : lit store.load_ticks, chemin swing ----------

def test_tf_volume_construit_des_bougies_depuis_des_ticks_recents(monkeypatch):
    monkeypatch.setattr(store, "load_ticks", lambda s, d: _ticks(300))
    monkeypatch.setattr(tickcapture, "_session_day", lambda ts: "2026-10-03")
    out = app.scalp_v2_chart_data("NQ", _CTX, 30040.0, tf="v60")
    assert len(out["candles"]) > 0
    c = out["candles"][0]
    assert set(c) == {"time", "open", "high", "low", "close"}
    assert out["levels"] and set(out["levels"][0]) == {"name", "price", "color"}


def test_tf_volume_repli_sur_le_dernier_jour_dispo_si_la_seance_du_jour_est_vide(monkeypatch):
    """Séance en cours (ex. week-end) sans aucun tick -> au lieu d'un
    graphique vide, on remonte le dernier jour qui en a — demandé
    explicitement le 2026-10-03 ("L'idéal serait de charger à minima
    l'historique")."""
    old_day_end = time.time() - 3600 * 10  # séance d'il y a 10h, bien finie
    old_ticks = _ticks(300, end=old_day_end)

    def fake_load_ticks(symbol, day):
        return old_ticks if day == "2026-10-02" else pd.DataFrame()

    monkeypatch.setattr(store, "load_ticks", fake_load_ticks)
    monkeypatch.setattr(store, "tick_days", lambda s: ["2026-09-30", "2026-10-02"])
    monkeypatch.setattr(tickcapture, "_session_day", lambda ts: "2026-10-03")
    out = app.scalp_v2_chart_data("NQ", _CTX, 30040.0, tf="v60")
    assert len(out["candles"]) > 0  # pas vide : la séance de repli est montrée


def test_tf_volume_pas_de_jour_de_repli_disponible_reste_vide(monkeypatch):
    monkeypatch.setattr(store, "load_ticks", lambda s, d: pd.DataFrame())
    monkeypatch.setattr(store, "tick_days", lambda s: [])
    monkeypatch.setattr(tickcapture, "_session_day", lambda ts: "2026-10-03")
    out = app.scalp_v2_chart_data("NQ", _CTX, 30040.0, tf="v60")
    assert out["candles"] == [] and out["markers"] == []
