"""Bascule d'affichage vers les chaînes d'indice natives (app.chain_state).

Le point sensible : le natif doit porter les NIVEAUX sans jamais perturber le
flux delta, qui repose sur la chaîne CBOE (clé `contract` stable entre deux
pulls, cadence 60 s). Les deux sources coexistent donc sous des clés de
stockage distinctes, et c'est cette séparation qui est vérifiée ici.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from gex import app as gex_app
from gex import metrics, scheduler
from gex.ingest import ChainSnapshot
from gex.metrics import ET


def _seed(key: str, spot: float, age_s: float = 0.0) -> None:
    exp = (datetime.now(ET) + pd.Timedelta(days=7)).date()
    rows = []
    for typ, oi in (("C", 100.0), ("P", 250.0)):
        rows.append({"contract": f"{key}{typ}", "strike": spot, "type": typ,
                     "expiry": exp, "bid": 1.0, "ask": 1.2, "iv": 0.2,
                     "open_interest": oi, "volume": 10.0, "delta_cboe": 0.0,
                     "gamma_cboe": 0.0, "last_trade_price": 0.0})
    ts = (datetime.now(ET) - timedelta(seconds=age_s)).replace(tzinfo=None)
    snap = ChainSnapshot(symbol=key, spot=spot, feed_timestamp=ts,
                         fetched_at=ts, options=pd.DataFrame(rows))
    df = metrics.enrich(snap)
    summary = metrics.summarize(snap, df, with_basis=False)
    st = scheduler.STATE.get(key)
    st.snapshot, st.enriched, st.summary, st.last_feed_ts = snap, df, summary, ts


def test_cle_de_stockage_distincte():
    """Les deux sources ne doivent JAMAIS écrire au même endroit : CBOE est
    redistribuable, le natif ne l'est pas."""
    assert scheduler.native_index_key("SPX") == "SPX_RT"
    assert scheduler.native_index_key("SPX") != "SPX"


def test_natif_prefere_quand_frais(monkeypatch):
    monkeypatch.setattr(gex_app, "credentials_present", lambda: True)
    _seed("SPX", 7000.0)
    _seed("SPX_RT", 7400.0)
    assert gex_app.chain_state("SPX").summary.spot == 7400.0


def test_repli_sur_cboe_si_natif_dormant(monkeypatch):
    """Une donnée délayée mais vivante vaut mieux qu'une donnée fraîche figée
    il y a une heure."""
    monkeypatch.setattr(gex_app, "credentials_present", lambda: True)
    _seed("SPX", 7000.0)
    _seed("SPX_RT", 7400.0, age_s=gex_app.NATIVE_STALE_S + 60)
    assert gex_app.chain_state("SPX").summary.spot == 7000.0


def test_repli_sur_cboe_sans_compte(monkeypatch):
    """Sans identifiants, l'outil doit rester intégralement sur CBOE — c'est
    la promesse du README."""
    monkeypatch.setattr(gex_app, "credentials_present", lambda: False)
    _seed("SPX", 7000.0)
    _seed("SPX_RT", 7400.0)
    assert gex_app.chain_state("SPX").summary.spot == 7000.0


def test_spy_suit_la_meme_regle(monkeypatch):
    """SPY/QQQ sont désormais dans le périmètre natif : la règle
    dxFeed-sinon-CBOE ne souffre pas d'exception."""
    monkeypatch.setattr(gex_app, "credentials_present", lambda: True)
    _seed("SPY", 740.0)
    _seed("SPY_RT", 741.5)
    assert gex_app.chain_state("SPY").summary.spot == 741.5


def test_nq_es_ont_deja_leur_chaine_native_sous_leur_nom(monkeypatch):
    """NQ/ES ne passent pas par _RT : leur chaîne native EST stockée sous leur
    propre nom (pull_native_options), il n'y a pas de version CBOE à doubler."""
    monkeypatch.setattr(gex_app, "credentials_present", lambda: True)
    _seed("NQ", 27500.0)
    assert gex_app.chain_state("NQ").summary.spot == 27500.0
