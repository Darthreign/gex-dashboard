from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from gex import hedge_pressure as hp
from gex import metrics
from gex.ingest import ChainSnapshot
from gex.metrics import ET


def make_chain(spot: float, rows: list[dict]) -> ChainSnapshot:
    """Même helper que tests/test_metrics.py — dupliqué plutôt qu'importé :
    pas de tests/__init__.py dans ce repo, un import inter-fichiers de tests
    serait fragile (dépendrait de la résolution de `tests` comme package)."""
    defaults = {
        # pas de cotation par défaut : l'IV vient alors du champ "iv" (repli
        # de calibrate_chain) ; un test qui veut des prix les fixe lui-même
        "bid": 0.0, "ask": 0.0, "iv": 0.20, "open_interest": 100.0,
        "volume": 0.0, "delta_cboe": 0.0, "gamma_cboe": 0.0,
        "last_trade_price": 0.0,
    }
    full = []
    for i, r in enumerate(rows):
        d = {**defaults, **r}
        d.setdefault("contract", f"TST{i:06d}")
        full.append(d)
    now = datetime.now(ET)
    return ChainSnapshot(
        symbol="TST", spot=spot, feed_timestamp=now.replace(tzinfo=None),
        fetched_at=now.replace(tzinfo=None), options=pd.DataFrame(full),
    )


def far_expiry() -> date:
    return (datetime.now(ET) + timedelta(days=30)).date()


def test_net_gamma_per_point_sign_convention():
    """Chaîne call-only (GEX net positif, convention dealers longs calls) :
    la position GAGNE du delta long quand le spot monte -> net_gamma_per_point
    positif. Symétrique pour une chaîne put-only."""
    exp = far_expiry()
    calls = metrics.enrich(make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0, "open_interest": 50.0},
    ]))
    puts = metrics.enrich(make_chain(100.0, [
        {"expiry": exp, "type": "P", "strike": 100.0, "open_interest": 50.0},
    ]))
    assert hp.net_gamma_per_point(calls, 100.0) > 0
    assert hp.net_gamma_per_point(puts, 100.0) < 0


def test_dealer_hedge_flow_oppose_au_mouvement_quand_gex_positif():
    """Cas canonique : GEX net positif (call-only), spot qui monte -> la
    POSITION gagne du delta long (total > 0) mais les dealers doivent VENDRE
    pour rester neutres (dealer_hedge_flow < 0) — même convention que
    `metrics.regime_read` ("GEX positif = les dealers vendent les hausses").
    Un bug de signe ici inverserait silencieusement tout le diagnostic."""
    exp = far_expiry()
    df = metrics.enrich(make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0, "open_interest": 100.0},
    ]))
    res = hp.hedge_pressure(df, spot0=100.0, spot1=105.0, iv0=None, iv1=None, dt_days=0.0)
    assert res.gamma_flow > 0          # la position gagne du delta long
    assert res.total > 0
    assert res.dealer_hedge_flow < 0   # ... donc les dealers vendent pour se re-hedger


def test_dealer_hedge_flow_amplifie_quand_gex_negatif():
    """Symétrique : GEX net négatif (put-only), spot qui monte -> la position
    PERD du delta long (devient plus courte), les dealers doivent ACHETER
    pour rester neutres (dealer_hedge_flow > 0) -> amplifie la hausse,
    cohérent avec "GEX négatif = accélérateur" de `regime_read`."""
    exp = far_expiry()
    df = metrics.enrich(make_chain(100.0, [
        {"expiry": exp, "type": "P", "strike": 100.0, "open_interest": 100.0},
    ]))
    res = hp.hedge_pressure(df, spot0=100.0, spot1=105.0, iv0=None, iv1=None, dt_days=0.0)
    assert res.gamma_flow < 0
    assert res.dealer_hedge_flow > 0


def test_vanna_flow_zero_sans_iv():
    """iv0/iv1 absents (dsigma inconnu) -> composante vanna omise, pas
    devinée à zéro de façon trompeuse mélangée au reste : le champ existe
    mais reste 0.0 explicitement, total = gamma+charm seulement."""
    exp = far_expiry()
    df = metrics.enrich(make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0, "open_interest": 100.0},
    ]))
    res = hp.hedge_pressure(df, spot0=100.0, spot1=100.0, iv0=None, iv1=None, dt_days=1.0)
    assert res.vanna_flow == 0.0
    assert res.gamma_flow == 0.0  # dS = 0
    assert res.charm_flow != 0.0  # dt > 0, charm non nul sur un contrat à 30j


def test_vanna_flow_non_nul_avec_iv():
    exp = far_expiry()
    df = metrics.enrich(make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0, "open_interest": 100.0, "iv": 0.20},
    ]))
    res = hp.hedge_pressure(df, spot0=100.0, spot1=100.0, iv0=0.20, iv1=0.25, dt_days=0.0)
    assert res.vanna_flow != 0.0
    assert res.gamma_flow == 0.0
    assert res.charm_flow == 0.0


def test_atm_iv_prend_le_strike_le_plus_proche_echeance_la_plus_proche():
    exp_near = far_expiry()
    exp_far = exp_near + timedelta(days=60)
    df = metrics.enrich(make_chain(100.0, [
        {"expiry": exp_near, "type": "C", "strike": 100.0, "iv": 0.18},
        {"expiry": exp_near, "type": "P", "strike": 100.0, "iv": 0.22},
        {"expiry": exp_near, "type": "C", "strike": 150.0, "iv": 0.50},  # loin -> ignoré
        {"expiry": exp_far, "type": "C", "strike": 100.0, "iv": 0.90},   # échéance lointaine -> ignorée
    ]))
    iv = hp.atm_iv(df, 100.0)
    assert iv == pytest.approx((0.18 + 0.22) / 2, rel=1e-6)


def _empty_enriched() -> pd.DataFrame:
    # `metrics.enrich` ne supporte pas une ChainSnapshot à 0 ligne (pas de
    # colonne "expiry" sur un DataFrame vide) — on construit directement la
    # forme déjà enrichie que consomment net_gamma_per_point/atm_iv.
    return pd.DataFrame(columns=["type", "strike", "open_interest", "iv", "t_years"])


def test_atm_iv_none_sans_chaine_exploitable():
    assert hp.atm_iv(_empty_enriched(), 100.0) is None


def test_net_gamma_per_point_vide_sans_chaine():
    assert hp.net_gamma_per_point(_empty_enriched(), 100.0) == 0.0
