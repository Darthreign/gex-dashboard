"""Flux de gamma échangé (« CVD » du gamma).

Même formule que le GEX mais pondérée par le volume du pas de temps au lieu de
l'open interest : cumulé sur la séance, il montre si ce qui se traite ajoute du
gamma stabilisant (calls) ou déstabilisant (puts).
"""
from __future__ import annotations

import pandas as pd

from gex import metrics


def _pair(vol_prev: float, vol_cur: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Deux pulls successifs sur un call et un put de même strike."""
    def chain(vol):
        return pd.DataFrame([
            {"contract": "C1", "type": "C", "strike": 7400.0, "volume": vol,
             "gamma_bs": 2e-4, "delta_bs": 0.5, "expiry": pd.Timestamp("2026-12-18")},
            {"contract": "P1", "type": "P", "strike": 7400.0, "volume": vol,
             "gamma_bs": 2e-4, "delta_bs": -0.5, "expiry": pd.Timestamp("2026-12-18")},
        ])
    return chain(vol_prev), chain(vol_cur)


def test_calls_positifs_puts_negatifs():
    """Convention GEX : le gamma des calls compte +, celui des puts −."""
    prev, cur = _pair(100.0, 200.0)
    r = metrics.flow_delta(prev, cur, 7400.0)
    assert r["gflow_calls"] > 0
    assert r["gflow_puts"] < 0


def test_net_est_la_somme():
    prev, cur = _pair(100.0, 350.0)
    r = metrics.flow_delta(prev, cur, 7400.0)
    assert r["gflow_total"] == r["gflow_calls"] + r["gflow_puts"]


def test_volume_stable_ne_produit_aucun_flux():
    prev, cur = _pair(500.0, 500.0)
    r = metrics.flow_delta(prev, cur, 7400.0)
    assert r["gflow_total"] == 0.0
    assert r["gflow_calls"] == 0.0


def test_reset_de_volume_ignore():
    """Un volume qui recule (nouvelle séance, correction de la source) ne doit
    pas créer un flux négatif fantôme — le Δ est borné à zéro."""
    prev, cur = _pair(900.0, 100.0)
    r = metrics.flow_delta(prev, cur, 7400.0)
    assert r["gflow_total"] == 0.0


def test_proportionnel_au_volume_echange():
    petit = metrics.flow_delta(*_pair(100.0, 200.0), 7400.0)["gflow_calls"]
    gros = metrics.flow_delta(*_pair(100.0, 300.0), 7400.0)["gflow_calls"]
    assert gros == 2 * petit
