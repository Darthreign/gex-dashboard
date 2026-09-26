"""Bandeau d'amplification de la page Scalp : entrées (mouvement 5 min, flux) et rendu."""
from __future__ import annotations

from datetime import datetime

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
    monkeypatch.setattr(store, "load_prices", lambda s, d: _bars({8: 30000.0, 1: 30030.0}))
    flux += [(1.0, 300e6, 0), (2.0, 80e6, 1)]
    ctx = {"zg": 29900.0, "gamma": "Gamma Positif"}
    div = app.scalp_banner("NQ", ctx, 30040.0)
    txt = str(div.to_plotly_json())
    assert "sc-tone-alert" in txt and "Amplification haussière" in txt
    assert "malgré un gamma positif" in txt


def test_banner_hors_seance_donnees_insuffisantes(monkeypatch, flux):
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
