"""Chaînes d'options d'indice natives (gex/idxopt.py).

Comme pour futopt, les parties réseau ne sont pas testées ici — ce qui casse
silencieusement, c'est l'aplatissement du référentiel imbriqué et la
condition d'arrêt de collecte, tous deux purement calculatoires.
"""
from __future__ import annotations

import pandas as pd
import pytest

from gex import idxopt
from gex.futopt import _all_have_iv


def _nested_payload() -> dict:
    """Forme réelle renvoyée par /option-chains/{symbol}/nested — deux racines
    (hebdomadaire et mensuelle), vérifiée contre l'API le 2026-07-29."""
    def strike(root, ymd, k):
        return {
            "strike-price": str(k),
            "call": f"{root:<4s}  {ymd}C{int(k)*1000:08d}",
            "call-streamer-symbol": f".{root}{ymd}C{int(k)}",
            "put": f"{root:<4s}  {ymd}P{int(k)*1000:08d}",
            "put-streamer-symbol": f".{root}{ymd}P{int(k)}",
        }
    return {"data": {"items": [
        {"root-symbol": "SPXW", "expirations": [
            {"expiration-date": "2026-07-29",
             "strikes": [strike("SPXW", "260729", 7400), strike("SPXW", "260729", 7405)]},
        ]},
        {"root-symbol": "SPX", "expirations": [
            {"expiration-date": "2026-08-21",
             "strikes": [strike("SPX", "260821", 7400)]},
        ]},
    ]}}


class _FakeResponse:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p


def test_aplatit_le_referentiel_imbrique(monkeypatch):
    monkeypatch.setattr(idxopt.requests, "get",
                        lambda *a, **k: _FakeResponse(_nested_payload()))
    df = idxopt.fetch_chain_instruments("SPX", "tok")

    # 2 strikes x 2 types (SPXW) + 1 strike x 2 types (SPX)
    assert len(df) == 6
    assert set(df["type"]) == {"C", "P"}
    assert set(df.columns) >= {"strike", "type", "expiry", "streamer_symbol"}
    assert df["streamer_symbol"].is_unique


def test_garde_les_deux_racines(monkeypatch):
    """SPXW (hebdo) et SPX (mensuel) sont des séries DISTINCTES, avec leur
    propre open interest — la chaîne CBOE les contient toutes les deux aussi.
    En déduire une seule fausserait le GEX par strike."""
    monkeypatch.setattr(idxopt.requests, "get",
                        lambda *a, **k: _FakeResponse(_nested_payload()))
    df = idxopt.fetch_chain_instruments("SPX", "tok")

    assert set(df["underlying_symbol"]) == {"SPXW", "SPX"}
    assert set(df["expiry"].astype(str)) == {"2026-07-29", "2026-08-21"}


def test_spot_prefere_le_temps_reel(monkeypatch):
    monkeypatch.setattr(idxopt.QUOTES, "price", lambda k: 7421.5 if k == "SPX" else None)
    assert idxopt.reference_spot("SPX") == pytest.approx(7421.5)


def test_spot_replie_sur_cboe_si_flux_muet(monkeypatch):
    """Au démarrage, le flux n'a pas encore reçu de cotation : mieux vaut une
    chaîne évaluée à un spot délayé que pas de chaîne du tout."""
    monkeypatch.setattr(idxopt.QUOTES, "price", lambda k: None)
    monkeypatch.setattr(idxopt.ingest, "fetch_index_spot",
                        lambda sym, **k: (7400.0, pd.Timestamp("2026-07-29 09:30")))
    assert idxopt.reference_spot("SPX") == pytest.approx(7400.0)


def test_spot_none_sur_symbole_inconnu(monkeypatch):
    monkeypatch.setattr(idxopt.QUOTES, "price", lambda k: None)
    assert idxopt.reference_spot("PASUNSYMBOLE") is None


def test_condition_darret_sur_iv_complete():
    """L'IV est le seul champ livré sur 100 % des contrats : l'OI ne convient
    pas comme critère (un contrat sans position ouverte n'émet rien), ce qui
    rendrait la condition indéclenchable."""
    check = _all_have_iv(["a", "b"])
    assert not check({"a": {"iv": 0.2}})
    assert not check({"a": {"iv": 0.2}, "b": {"oi": 10.0}})
    assert check({"a": {"iv": 0.2}, "b": {"iv": 0.3}})


def test_regle_unique_dxfeed_sinon_cboe():
    """Règle unique du projet : dxFeed dès qu'il est disponible, CBOE sinon —
    y compris SPY/QQQ, un temps exclus au motif du dividende. Mauvais
    argument : l'approximation q=0 vit dans le Black-Scholes maison, donc elle
    frappe les deux sources à égalité (cf. NATIVE_INDEX)."""
    assert set(idxopt.NATIVE_INDEX) == {"SPX", "NDX", "SPY", "QQQ"}
