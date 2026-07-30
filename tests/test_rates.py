"""Taux sans risque chargé automatiquement (gex/rates.py).

Ce qui doit tenir : current_rate() ne fait jamais de réseau, refresh() met en
cache le SOFR du jour, et TOUTE défaillance (réseau, JSON, valeur aberrante)
retombe silencieusement sur la valeur courante — le calcul ne doit jamais
dépendre de la disponibilité d'une API externe.
"""
from __future__ import annotations

import pytest

from gex import rates
from gex.config import RISK_FREE_RATE


@pytest.fixture(autouse=True)
def _reset():
    """Chaque test repart du repli, sans jour mémorisé."""
    rates._rate = RISK_FREE_RATE
    rates._day = None
    yield
    rates._rate = RISK_FREE_RATE
    rates._day = None


class _Resp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p


def _payload(pct):
    return {"refRates": [{"effectiveDate": "2026-07-29", "type": "SOFR",
                          "percentRate": pct}]}


def test_current_rate_ne_fait_pas_de_reseau(monkeypatch):
    """Appelé dans les chemins de calcul : jamais d'appel réseau."""
    def boom(*a, **k):
        raise AssertionError("current_rate ne doit PAS toucher le réseau")
    monkeypatch.setattr(rates.requests, "get", boom)
    assert rates.current_rate() == RISK_FREE_RATE


def test_refresh_met_en_cache_le_sofr(monkeypatch):
    monkeypatch.setattr(rates.requests, "get", lambda *a, **k: _Resp(_payload(3.65)))
    assert rates.refresh() == pytest.approx(0.0365)
    assert rates.current_rate() == pytest.approx(0.0365)


def test_refresh_idempotent_dans_la_journee(monkeypatch):
    appels = []
    monkeypatch.setattr(rates.requests, "get",
                        lambda *a, **k: appels.append(1) or _Resp(_payload(3.65)))
    rates.refresh()
    rates.refresh()          # même jour : ne redemande pas
    assert len(appels) == 1
    rates.refresh(force=True)  # forçage explicite
    assert len(appels) == 2


def test_repli_sur_erreur_reseau(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("offline")
    monkeypatch.setattr(rates.requests, "get", boom)
    assert rates.refresh() == RISK_FREE_RATE      # pas d'exception, repli
    assert rates.current_rate() == RISK_FREE_RATE


def test_conserve_la_valeur_precedente_si_le_refresh_echoue(monkeypatch):
    monkeypatch.setattr(rates.requests, "get", lambda *a, **k: _Resp(_payload(3.65)))
    rates.refresh(force=True)                     # cache 3,65 %
    def boom(*a, **k):
        raise TimeoutError()
    monkeypatch.setattr(rates.requests, "get", boom)
    assert rates.refresh(force=True) == pytest.approx(0.0365)  # garde le dernier bon


@pytest.mark.parametrize("pct", [-1.0, 25.0])
def test_valeur_aberrante_rejetee(monkeypatch, pct):
    """Un taux hors [0, 20 %] est une réponse cassée : on garde le repli plutôt
    que d'empoisonner tous les niveaux."""
    monkeypatch.setattr(rates.requests, "get", lambda *a, **k: _Resp(_payload(pct)))
    assert rates.refresh(force=True) == RISK_FREE_RATE


def test_metrics_utilise_le_taux_courant(monkeypatch):
    """Le taux courant doit réellement irriguer le calcul (enrich passe r aux
    greeks) : on le vérifie en contrôlant que metrics lit rates.current_rate."""
    from gex import metrics
    vu = []
    vraie_gamma = metrics.greeks.gamma
    monkeypatch.setattr(metrics.greeks, "gamma",
                        lambda s, k, t, r, sig: vu.append(r) or vraie_gamma(s, k, t, r, sig))
    monkeypatch.setattr(rates, "_rate", 0.0365)
    monkeypatch.setattr(rates, "_day", None)

    import numpy as np
    import pandas as pd
    from gex.ingest import ChainSnapshot
    snap = ChainSnapshot(
        symbol="SPX", spot=7400.0,
        feed_timestamp=pd.Timestamp("2026-07-29 10:00"),
        fetched_at=pd.Timestamp("2026-07-29 10:00"),
        options=pd.DataFrame([{
            "contract": "x", "expiry": (pd.Timestamp.now() + pd.Timedelta(days=7)).date(),
            "type": "C", "strike": 7400.0, "bid": 1.0, "ask": 1.2, "iv": 0.2,
            "open_interest": 100.0, "volume": 10.0, "delta_cboe": 0.5,
            "gamma_cboe": 0.0, "last_trade_price": 1.1,
        }]))
    metrics.enrich(snap)
    assert vu and vu[0] == pytest.approx(0.0365)
