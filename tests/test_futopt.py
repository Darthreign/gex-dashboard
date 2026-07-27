"""Ingestion native des options sur futures.

Les parties réseau (fetch_chain_instruments, _collect, get_multiplier) ne sont
pas testées ici — ce qui compte et qui casse silencieusement, c'est la
construction de la chaîne (filtre, calcul du gamma, colonnes de sortie).
"""
from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from gex import futopt
from gex.metrics import ET, gamma_profile, key_levels, top_gex_levels, zero_gamma


def _chain(spot: float = 28700.0) -> pd.DataFrame:
    rows = []
    for k in np.arange(spot - 2000, spot + 2001, 25):
        for typ in ("C", "P"):
            rows.append({"strike": float(k), "type": typ,
                         "expiry": date(2026, 7, 27),
                         "streamer_symbol": f"./NQ{typ}{k:.0f}:XCME"})
    return pd.DataFrame(rows)


def _raw(chain: pd.DataFrame, iv: float = 0.15, oi_call: float = 80.0,
         oi_put: float = 120.0, spread: float = 1.0) -> dict:
    """OI asymétrique call/put : à parts égales, le net s'annule exactement à
    chaque strike (même |gamma| des deux côtés) et le classement n'a plus de
    sens — c'est le cas dégénéré rencontré sur les tests de metrics.py."""
    out = {}
    for _, r in chain.iterrows():
        oi = oi_call if r["type"] == "C" else oi_put
        out[r["streamer_symbol"]] = {"iv": iv, "oi": oi, "volume": 50.0,
                                     "bidPrice": 10.0, "askPrice": 10.0 + spread}
    return out


def test_filtre_fenetre_strikes_et_echeance():
    chain = _chain(28700.0)
    # ajoute une échéance lointaine, hors fenêtre par défaut
    lointain = chain.iloc[[0]].copy()
    lointain["expiry"] = date(2027, 6, 18)
    chain = pd.concat([chain, lointain], ignore_index=True)

    out = futopt.filter_chain(chain, spot=28700.0, window=0.05, max_days=45)
    assert out["strike"].between(28700 * 0.95, 28700 * 1.05).all()
    assert (out["expiry"] <= date(2026, 7, 27) + pd.Timedelta(days=45)).all()
    assert date(2027, 6, 18) not in out["expiry"].values


def test_chaine_vide_geree():
    vide = pd.DataFrame(columns=["strike", "type", "expiry", "streamer_symbol"])
    assert futopt.filter_chain(vide, spot=100.0).empty


def test_multiplicateur_nq_distinct_des_indices():
    """Piège identifié : les options d'indice utilisent 100, les futures leur
    propre multiplicateur (20 pour NQ, 50 pour ES) — jamais 100 par défaut."""
    chain = _chain(28700.0)
    now = datetime(2026, 7, 27, 10, 0, tzinfo=ET)
    raw = _raw(chain)
    d100 = futopt.enrich_native(chain, raw, 28700.0, 100.0, now_et=now)
    d20 = futopt.enrich_native(chain, raw, 28700.0, 20.0, now_et=now)
    ratio = d100["gex"].abs().sum() / d20["gex"].abs().sum()
    assert ratio == pytest.approx(5.0)  # 100 / 20


def test_iv_manquante_traitee_comme_gamma_nul():
    """Un contrat sans IV dxFeed (pas encore coté) ne doit pas produire un
    gamma indéfini qui polluerait la somme."""
    chain = _chain(28700.0).iloc[:4].reset_index(drop=True)
    raw = _raw(chain)
    # une IV manquante sur un contrat
    raw[chain["streamer_symbol"].iloc[0]].pop("iv")
    now = datetime(2026, 7, 27, 10, 0, tzinfo=ET)
    df = futopt.enrich_native(chain, raw, 28700.0, 20.0, now_et=now)
    row = df[df["streamer_symbol"] == chain["streamer_symbol"].iloc[0]]
    assert (row["gamma_bs"] == 0.0).all() and (row["gex"] == 0.0).all()


def test_colonnes_compatibles_avec_metrics():
    """La chaîne native doit pouvoir nourrir directement les fonctions de
    metrics.py, sans transformation supplémentaire — c'est tout l'intérêt de
    ne pas dupliquer la logique de niveau."""
    spot = 28700.0
    # OI dominant côté call au-dessus du spot, côté put en dessous — comme le
    # positionnement réel, pour que Call Wall et Put Support soient résolvables
    strikes = {28500.0: (400, 1500), 28650.0: (600, 1200),
              28750.0: (1500, 500), 28900.0: (2000, 300)}
    rows, raw = [], {}
    for k, (oi_c, oi_p) in strikes.items():
        for typ, oi in (("C", oi_c), ("P", oi_p)):
            sym = f"./NQ{typ}{k:.0f}:XCME"
            rows.append({"strike": k, "type": typ, "expiry": date(2026, 7, 27),
                         "streamer_symbol": sym})
            raw[sym] = {"iv": 0.15, "oi": float(oi), "volume": 50.0,
                       "bidPrice": 10.0, "askPrice": 11.0}
    chain = pd.DataFrame(rows)
    now = datetime(2026, 7, 27, 10, 0, tzinfo=ET)
    df = futopt.enrich_native(chain, raw, spot, 20.0, now_et=now)

    assert not top_gex_levels(df).empty
    assert not top_gex_levels(df, ref_spot=spot).empty
    keys = key_levels(df, spot, ref_spot=spot)
    assert keys["call_wall"] is not None and keys["put_support"] is not None
    assert zero_gamma(df, spot) is not None
    assert gamma_profile(df, spot) is not None


def test_expiration_du_jour_exclue_apres_16h_et():
    """Convention partagée avec le reste de l'outil : un 0DTE expire à 16h00
    ET, comme les options d'indice CBOE."""
    chain = _chain(28700.0)
    raw = _raw(chain)
    avant = datetime(2026, 7, 27, 15, 0, tzinfo=ET)
    apres = datetime(2026, 7, 27, 16, 30, tzinfo=ET)
    assert not futopt.enrich_native(chain, raw, 28700.0, 20.0, now_et=avant).empty
    assert futopt.enrich_native(chain, raw, 28700.0, 20.0, now_et=apres).empty


def test_gex_lineaire_avec_le_spot_a_structure_egale():
    """GEX = γ(S,K,T) × OI × mult × S² × 1 %. Le gamma Black-Scholes varie en
    1/S ; à structure relative identique (même fenêtre en %, même IV), le
    facteur S² est donc en réalité compensé pour moitié : GEX scale
    LINÉAIREMENT avec le spot, pas au carré. Piège à ne pas réintroduire en
    modifiant la formule."""
    def chain_at(spot):
        rows = []
        for pct in np.arange(-0.05, 0.0501, 0.0025):
            k = spot * (1 + pct)         # pas de grille fixe : évite tout
            for typ in ("C", "P"):        # artefact de quantification
                rows.append({"strike": k, "type": typ,
                             "expiry": date(2026, 7, 27),
                             "streamer_symbol": f"./NQ{typ}{k:.4f}@{spot:.0f}:XCME"})
        return pd.DataFrame(rows)

    now = datetime(2026, 7, 27, 10, 0, tzinfo=ET)
    c1 = chain_at(1000.0)
    a = futopt.enrich_native(c1, _raw(c1, iv=0.20), 1000.0, 20.0, now_et=now)["gex"].abs().sum()
    c2 = chain_at(2000.0)
    b = futopt.enrich_native(c2, _raw(c2, iv=0.20), 2000.0, 20.0, now_et=now)["gex"].abs().sum()
    assert b / a == pytest.approx(2.0, rel=0.1)
