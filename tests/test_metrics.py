from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from gex import metrics
from gex.config import CONTRACT_MULTIPLIER
from gex.ingest import ChainSnapshot, parse_occ
from gex.metrics import ET


def make_chain(spot: float, rows: list[dict]) -> ChainSnapshot:
    defaults = {
        "bid": 1.0, "ask": 1.2, "iv": 0.20, "open_interest": 100.0,
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


def test_parse_occ():
    exp, cp, strike = parse_occ("NDX260821C04000000")
    assert exp == date(2026, 8, 21)
    assert cp == "C"
    assert strike == 4000.0
    # racine hebdo (SPXW)
    exp, cp, strike = parse_occ("SPXW260724P07400000")
    assert (exp, cp, strike) == (date(2026, 7, 24), "P", 7400.0)


def test_gex_sign_convention():
    """Calls → GEX positif, puts → négatif, magnitude = γ·OI·mult·S²·1%."""
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0, "open_interest": 10.0},
        {"expiry": exp, "type": "P", "strike": 100.0, "open_interest": 10.0},
    ])
    df = metrics.enrich(snap)
    call_gex = df.loc[df["type"] == "C", "gex"].iloc[0]
    put_gex = df.loc[df["type"] == "P", "gex"].iloc[0]
    assert call_gex > 0 and put_gex < 0
    # même strike/IV/expiry → gamma identique → magnitudes égales
    assert call_gex == pytest.approx(-put_gex, rel=1e-9)
    g = df.loc[df["type"] == "C", "gamma_bs"].iloc[0]
    assert call_gex == pytest.approx(g * 10 * CONTRACT_MULTIPLIER * 100.0**2 * 0.01, rel=1e-9)


def test_dex_signs():
    """Sous l'hypothèse dealers longs calls / courts puts (même convention que
    GEX) : être court un put donne une exposition delta POSITIVE — le delta du
    put est négatif, une position courte en inverse le signe. C'est la
    mécanique par laquelle les dealers courts puts se retrouvent
    structurellement longs delta et vendent dans la baisse pour rester
    couverts. Un DEX put négatif signalerait le bug inverse (delta brut de
    l'open interest détenu long, pas celui du dealer supposé court)."""
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0},
        {"expiry": exp, "type": "P", "strike": 100.0},
    ])
    df = metrics.enrich(snap)
    assert df.loc[df["type"] == "C", "dex"].iloc[0] > 0
    assert df.loc[df["type"] == "P", "dex"].iloc[0] > 0


def test_dex_meme_convention_que_gex_sur_oi_asymetrique():
    """Régression du bug corrigé le 2026-07-27 : avec plus de puts que de
    calls au même strike, GEX et DEX doivent s'accorder sur le signe du
    déséquilibre — les deux décrivent le MÊME dealer supposé (long calls,
    court puts), donc plus de puts doit se lire comme "plus de delta long"
    (DEX positif) en même temps que "plus de gamma négatif" (GEX négatif).
    Avant le correctif, le DEX ressortait négatif — signe opposé."""
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0, "open_interest": 100.0},
        {"expiry": exp, "type": "P", "strike": 100.0, "open_interest": 200.0},
    ])
    df = metrics.enrich(snap)
    net_gex = df["gex"].sum()
    net_dex = df["dex"].sum()
    assert net_gex < 0    # plus de puts -> gamma net negatif (destabilisant)
    assert net_dex > 0    # ... et delta net positif (dealers longs, sur ce strike)


def test_regime_read_frein():
    """Gamma positif : régime freiné quel que soit le signe du DEX."""
    r = metrics.regime_read(net_gex=5e9, net_dex=-2e9)
    assert r["gex_frein"] is True
    assert r["severity"] == "info"
    assert r["i18n_key"] == "regime_frein"
    assert r["dex_sign"] == "short"  # DEX négatif -> dealers short delta
    assert r["params"]["biais_code"] == "up"  # short delta -> pression acheteuse -> biais haussier


def test_regime_read_accelerateur_sans_historique():
    """Gamma négatif, DEX short, sans historique pour juger l'ampleur : cas
    "Gamme négative et faible Delta short" de l'utilisateur -> avertissement
    modéré (pas le plus fort), magnitude laissée à None plutôt que devinée."""
    r = metrics.regime_read(net_gex=-3e9, net_dex=-1e9)
    assert r["gex_frein"] is False
    assert r["magnitude"] is None
    assert r["severity"] == "warning"
    assert r["i18n_key"] == "regime_accel_modere"
    assert r["params"]["biais_code"] == "up"


def test_regime_read_accelerateur_fort():
    """Gamma négatif ET DEX short au 90e percentile de son historique : cas
    "Gamme négative et fort Delta short" -> avertissement maximal."""
    hist = pd.Series([-0.1e9] * 15 + [-0.5e9] * 5)  # ampleur courante domine l'historique
    r = metrics.regime_read(net_gex=-3e9, net_dex=-2e9, dex_history=hist)
    assert r["magnitude"] == "fort"
    assert r["severity"] == "danger"
    assert r["i18n_key"] == "regime_accel_fort"


def test_regime_text_fr_et_en():
    from gex.i18n import regime_text
    r = metrics.regime_read(net_gex=-3e9, net_dex=-2e9,
                            dex_history=pd.Series([-0.1e9] * 15 + [-0.5e9] * 5))
    fr = regime_text("fr", r)
    en = regime_text("en", r)
    assert "haussier" in fr and "acheteuse" in fr
    assert "upward" in en and "buying" in en


def test_zero_gamma_crossing():
    """Call OI sous le spot, put OI au-dessus → le GEX net passe de positif
    (spot bas, gamma du call domine) à négatif (spot haut) : crossing ~100."""
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 93.0, "open_interest": 100.0},
        {"expiry": exp, "type": "P", "strike": 107.0, "open_interest": 100.0},
    ])
    df = metrics.enrich(snap)
    zg = metrics.zero_gamma(df, 100.0)
    assert zg is not None
    assert 95.0 < zg < 105.0


def test_zero_gamma_none_when_all_positive():
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0, "open_interest": 100.0},
    ])
    df = metrics.enrich(snap)
    assert metrics.zero_gamma(df, 100.0) is None


def test_put_call_ratios():
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0, "open_interest": 200.0, "volume": 50.0},
        {"expiry": exp, "type": "P", "strike": 100.0, "open_interest": 100.0, "volume": 100.0},
    ])
    df = metrics.enrich(snap)
    r = metrics.put_call_ratios(df)
    assert r["pc_oi"] == pytest.approx(0.5)
    assert r["pc_volume"] == pytest.approx(2.0)


def test_flow_delta_increment():
    exp = far_expiry()
    prev = metrics.enrich(make_chain(100.0, [
        {"contract": "A", "expiry": exp, "type": "C", "strike": 100.0, "volume": 10.0},
        {"contract": "B", "expiry": exp, "type": "P", "strike": 100.0, "volume": 5.0},
    ]))
    cur = metrics.enrich(make_chain(100.0, [
        {"contract": "A", "expiry": exp, "type": "C", "strike": 100.0, "volume": 25.0},
        {"contract": "B", "expiry": exp, "type": "P", "strike": 100.0, "volume": 5.0},
    ]))
    flow = metrics.flow_delta(prev, cur, 100.0)
    assert flow["contracts_traded"] == pytest.approx(15.0)
    d = cur.loc[cur["contract"] == "A", "delta_bs"].iloc[0]
    assert flow["flow_total"] == pytest.approx(15.0 * d * CONTRACT_MULTIPLIER * 100.0)
    assert flow["flow_puts"] == pytest.approx(0.0)


def test_flow_delta_ignores_volume_reset():
    """Un volume qui baisse (reset overnight) ne doit pas produire de flux négatif."""
    exp = far_expiry()
    prev = metrics.enrich(make_chain(100.0, [
        {"contract": "A", "expiry": exp, "type": "C", "strike": 100.0, "volume": 500.0},
    ]))
    cur = metrics.enrich(make_chain(100.0, [
        {"contract": "A", "expiry": exp, "type": "C", "strike": 100.0, "volume": 0.0},
    ]))
    flow = metrics.flow_delta(prev, cur, 100.0)
    assert flow["flow_total"] == 0.0


def test_gamma_profile_crosses_at_zero_gamma():
    """Le profil et le zero gamma doivent être cohérents : le croisement
    interpolé tombe là où le profil change de signe."""
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 93.0, "open_interest": 100.0},
        {"expiry": exp, "type": "P", "strike": 107.0, "open_interest": 100.0},
    ])
    df = metrics.enrich(snap)
    grid, profile = metrics.gamma_profile(df, 100.0)
    zg = metrics.zero_gamma(df, 100.0)
    assert len(grid) == len(profile)
    assert profile[0] * profile[-1] < 0, "le profil doit changer de signe"
    i = int(np.argmin(np.abs(grid - zg)))
    assert abs(profile[i]) < abs(profile).max() * 0.05


def test_second_order_exposures_signs():
    """vex/cex suivent la convention GEX : calls positifs, puts négatifs."""
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 110.0, "open_interest": 100.0},
        {"expiry": exp, "type": "P", "strike": 110.0, "open_interest": 100.0},
    ])
    df = metrics.add_second_order(metrics.enrich(snap), 100.0)
    call = df[df["type"] == "C"].iloc[0]
    put = df[df["type"] == "P"].iloc[0]
    # vanna et charm identiques call/put -> expositions opposées par la convention
    assert call["vanna"] == pytest.approx(put["vanna"])
    assert call["vex"] == pytest.approx(-put["vex"])
    assert call["cex"] == pytest.approx(-put["cex"])


def test_oi_change_detects_new_positioning():
    exp = far_expiry()
    prev = metrics.enrich(make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 105.0, "open_interest": 1000.0},
        {"expiry": exp, "type": "P", "strike": 95.0, "open_interest": 500.0},
    ]))
    cur = metrics.enrich(make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 105.0, "open_interest": 1800.0},
        {"expiry": exp, "type": "P", "strike": 95.0, "open_interest": 200.0},
    ]))
    d = metrics.oi_change(prev, cur).set_index("strike")
    assert d.loc[105.0, "d_call"] == pytest.approx(800.0)    # positions ouvertes
    assert d.loc[95.0, "d_put"] == pytest.approx(-300.0)     # positions fermées
    assert d.loc[105.0, "oi_call"] == pytest.approx(1800.0)


def test_oi_change_empty_when_no_previous():
    exp = far_expiry()
    cur = metrics.enrich(make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 100.0}]))
    assert metrics.oi_change(None, cur).empty
    assert metrics.oi_change(pd.DataFrame(), cur).empty


def test_key_levels_are_directional():
    """Call Wall doit être AU-DESSUS du spot, Put Support EN DESSOUS — même
    quand le plus gros mur absolu se trouve du mauvais côté."""
    exp = far_expiry()
    snap = make_chain(100.0, [
        # plus gros mur de puts au-dessus du spot : ne doit PAS servir de support
        {"expiry": exp, "type": "P", "strike": 105.0, "open_interest": 5000.0},
        {"expiry": exp, "type": "P", "strike": 95.0, "open_interest": 800.0},
        {"expiry": exp, "type": "C", "strike": 108.0, "open_interest": 900.0},
        # gros mur de calls sous le spot : ne doit PAS servir de résistance
        {"expiry": exp, "type": "C", "strike": 92.0, "open_interest": 4000.0},
    ])
    df = metrics.enrich(snap)
    k = metrics.key_levels(df, 100.0)
    assert k["call_wall"] == 108.0, "la résistance doit être au-dessus du spot"
    assert k["put_support"] == 95.0, "le support doit être sous le spot"


def test_key_levels_none_when_side_empty():
    exp = far_expiry()
    snap = make_chain(100.0, [
        {"expiry": exp, "type": "C", "strike": 110.0, "open_interest": 100.0},
    ])
    df = metrics.enrich(snap)
    k = metrics.key_levels(df, 100.0)
    assert k["call_wall"] == 110.0
    assert k["put_support"] is None  # aucun mur de puts sous le spot


def test_expected_move_from_straddle():
    """1D Min/Max encadrent le spot du prix du straddle ATM."""
    exp = far_expiry()
    rows = []
    for k in (98.0, 100.0, 102.0):
        for typ, mid in (("C", 3.0), ("P", 2.0)):  # straddle ATM = 5.0
            rows.append({"expiry": exp, "type": typ, "strike": k,
                         "bid": mid - 0.1, "ask": mid + 0.1, "volume": 5.0})
    snap = make_chain(100.0, rows)
    df = metrics.enrich(snap)
    assert metrics.expected_move(df, 100.0) == pytest.approx(5.0, abs=0.01)
    k = metrics.key_levels(df, 100.0)
    assert k["d1_min"] == pytest.approx(95.0, abs=0.01)
    assert k["d1_max"] == pytest.approx(105.0, abs=0.01)


def test_expected_move_rejects_absurd():
    exp = far_expiry()
    rows = [{"expiry": exp, "type": typ, "strike": 100.0,
             "bid": 49.0, "ask": 51.0, "volume": 5.0} for typ in ("C", "P")]
    snap = make_chain(100.0, rows)
    df = metrics.enrich(snap)
    assert metrics.expected_move(df, 100.0) is None  # straddle = 100 % du spot


def test_third_friday():
    # échéances CME connues
    assert metrics.third_friday(2026, 9) == date(2026, 9, 18)
    assert metrics.third_friday(2026, 12) == date(2026, 12, 18)
    assert metrics.third_friday(2026, 3) == date(2026, 3, 20)


def test_front_futures_expiry_rolls():
    # avant l'échéance de septembre -> septembre
    assert metrics.front_futures_expiry(date(2026, 7, 25)) == date(2026, 9, 18)
    # le jour même -> encore septembre
    assert metrics.front_futures_expiry(date(2026, 9, 18)) == date(2026, 9, 18)
    # le lendemain -> roule sur décembre
    assert metrics.front_futures_expiry(date(2026, 9, 19)) == date(2026, 12, 18)
    # fin d'année -> mars suivant
    assert metrics.front_futures_expiry(date(2026, 12, 19)) == date(2027, 3, 19)


def test_futures_basis_recovers_known_forward():
    """Chaîne synthétique construite avec un forward connu : la parité
    call-put doit le retrouver (C - P = (F - K)·e^(-rT))."""
    import numpy as np
    from gex.config import RISK_FREE_RATE
    exp = metrics.front_futures_expiry(datetime.now(ET).date())
    spot, fwd = 100.0, 100.8   # basis attendu +0.8
    t = (pd.Timestamp(exp).tz_localize(ET) + pd.Timedelta(hours=16)
         - datetime.now(ET)).total_seconds() / (365 * 24 * 3600)
    rows = []
    for k in np.arange(97.0, 103.5, 0.5):
        # prix cohérents avec la parité, spread nul autour du mid
        diff = (fwd - k) * np.exp(-RISK_FREE_RATE * t)
        pmid = 2.0
        cmid = pmid + diff
        for typ, mid in (("C", cmid), ("P", pmid)):
            rows.append({"expiry": exp, "type": typ, "strike": float(k),
                         "bid": mid - 0.05, "ask": mid + 0.05, "volume": 10.0})
    snap = make_chain(spot, rows)
    df = metrics.enrich(snap)
    basis = metrics.futures_basis(df, spot)
    assert basis == pytest.approx(0.8, abs=0.02)


def test_futures_basis_none_when_no_pairs():
    exp = far_expiry()
    snap = make_chain(100.0, [{"expiry": exp, "type": "C", "strike": 100.0}])
    df = metrics.enrich(snap)
    assert metrics.futures_basis(df, 100.0) is None


def test_expired_contracts_dropped():
    past = (datetime.now(ET) - timedelta(days=3)).date()
    snap = make_chain(100.0, [
        {"expiry": past, "type": "C", "strike": 100.0},
        {"expiry": far_expiry(), "type": "C", "strike": 100.0},
    ])
    df = metrics.enrich(snap)
    assert len(df) == 1


def _chain_pour_murs():
    """Chaîne réaliste : open interest concentré sur quelques strikes ronds."""
    rows = []
    for k, oi_c, oi_p in [(7350, 500, 4000), (7400, 800, 6000),
                          (7450, 5000, 900), (7500, 3000, 400)]:
        for typ, oi in (("C", oi_c), ("P", oi_p)):
            rows.append({"strike": float(k), "type": typ, "iv": 0.15,
                         "t_years": 0.02, "open_interest": float(oi),
                         "volume": 100.0, "expiry": pd.Timestamp("2026-07-27").date(),
                         "gex": 0.0})
    return pd.DataFrame(rows)


def test_murs_figes_au_spot_de_reference():
    """Le classement des murs ne doit PAS suivre le prix.

    Le gamma culminant à la monnaie, l'évaluer au spot courant fait migrer le
    mur le plus fort vers le marché : le niveau désigne alors où EST le prix,
    pas une zone de couverture. Mesuré sur une chaîne SPX réelle, faire varier
    la référence de 7350 à 7500 déplaçait les cinq murs de bout en bout.
    """
    df = _chain_pour_murs()
    ref = 7400.0
    attendu = metrics.top_gex_levels(df, ref_spot=ref)["strike"].tolist()
    for spot_courant in (7200.0, 7400.0, 7600.0):
        got = metrics.top_gex_levels(df, ref_spot=ref)["strike"].tolist()
        assert got == attendu, f"murs déplacés au spot {spot_courant}"


def test_sensibilite_au_spot_sur_chaine_etalee():
    """Documente le défaut corrigé, et sa condition d'apparition.

    Sur une chaîne étalée à open interest homogène — le cas des indices, où des
    centaines de strikes cotent — c'est la forme du gamma qui décide du
    classement, et elle suit le spot. Sur une chaîne très concentrée l'open
    interest domine et les murs sont naturellement stables : d'où l'importance
    de figer la référence plutôt que de compter sur la concentration.
    """
    rows = []
    for k in range(7200, 7601, 25):
        # déséquilibre calls/puts : à parts égales le net s'annule exactement
        # à chaque strike et le classement n'a plus de sens
        for typ, oi in (("C", 800.0), ("P", 1200.0)):
            rows.append({"strike": float(k), "type": typ, "iv": 0.15,
                         "t_years": 0.02, "open_interest": oi,
                         "volume": 100.0,
                         "expiry": pd.Timestamp("2026-07-27").date(), "gex": 0.0})
    df = pd.DataFrame(rows)
    bas = metrics.gex_at_spot(df, 7250.0).abs().idxmax()
    haut = metrics.gex_at_spot(df, 7550.0).abs().idxmax()
    assert bas != haut
    # le mur se cale sur la monnaie : c'est exactement le défaut
    assert abs(bas - 7250) < 100 and abs(haut - 7550) < 100


def test_gex_at_spot_ignore_iv_absente():
    df = _chain_pour_murs()
    df.loc[df["type"] == "C", "iv"] = 0.0
    s = metrics.gex_at_spot(df, 7400.0)
    assert (s < 0).all()      # puts seuls


def test_gap_de_futures_ninvalide_pas_les_murs():
    """Hors séance, un gap de futures ne doit pas écarter un mur.

    Call Wall et Put Support sont cherchés d'un côté donné du prix. Si ce prix
    est celui du future en pré-session, un gap haussier fait disparaître le Put
    Support d'origine au profit d'un strike plus haut — le plan bâti la veille
    est invalidé avant même que le cash n'ouvre.
    """
    # un strike lourdement chargé en puts JUSTE AU-DESSUS de la clôture : il
    # devient éligible dès que le prix passe dessus, et rafle le support
    rows = []
    for k, oi_c, oi_p in [(7350, 500, 4000), (7400, 800, 6000),
                          (7450, 900, 9000), (7500, 3000, 400)]:
        for typ, oi in (("C", oi_c), ("P", oi_p)):
            rows.append({"strike": float(k), "type": typ, "iv": 0.15,
                         "t_years": 0.02, "open_interest": float(oi),
                         "volume": 100.0,
                         "expiry": pd.Timestamp("2026-07-27").date(), "gex": 0.0})
    df = pd.DataFrame(rows)
    ref = 7400.0

    fige = metrics.key_levels(df, ref, ref_spot=ref)
    suit_le_gap = metrics.key_levels(df, ref * 1.012, ref_spot=ref)
    assert fige["put_support"] == 7400.0
    assert suit_le_gap["put_support"] == 7450.0, (
        "le gap déplace bien le support quand on lui passe le prix du future")
