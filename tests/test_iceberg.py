"""Détection d'absorption (candidat iceberg) sur les ticks NQ/ES (gex/iceberg.py)."""
from __future__ import annotations

import pandas as pd
import pytest

from gex import iceberg as ib


def _df(rows: list[dict]) -> pd.DataFrame:
    base = {"price": 30000.0, "volume": 1.0, "side": "SELL", "bid_size": 1.0,
            "ask_size": 1.0, "prev_bid_size": 1.0, "prev_ask_size": 1.0}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_prints_consecutifs_meme_prix_regroupes_en_une_salve():
    df = _df([{"ts": 1.0, "volume": 5}, {"ts": 1.5, "volume": 5}, {"ts": 2.0, "volume": 5}])
    sw = ib.build_sweeps(df)
    assert len(sw) == 1 and sw[0].total_size == 15.0 and sw[0].n_prints == 3


def test_changement_de_prix_ou_de_sens_coupe_la_salve():
    df = _df([{"ts": 1.0, "price": 30000.0}, {"ts": 1.5, "price": 30001.0},
              {"ts": 2.0, "price": 30001.0, "side": "BUY"}])
    sw = ib.build_sweeps(df)
    assert len(sw) == 3


def test_ecart_de_temps_trop_grand_coupe_la_salve():
    df = _df([{"ts": 1.0}, {"ts": 1.0 + ib.MAX_GAP_S + 0.1}])
    sw = ib.build_sweeps(df)
    assert len(sw) == 2


def test_cote_indetermine_coupe_et_est_ignore():
    df = _df([{"ts": 1.0, "volume": 3}, {"ts": 1.2, "side": "?", "volume": 9},
              {"ts": 1.4, "volume": 3}])
    sw = ib.build_sweeps(df)
    assert [round(s.total_size) for s in sw] == [3, 3]


def test_before_et_after_pris_au_bon_endroit_de_la_salve():
    df = _df([{"ts": 1.0, "prev_bid_size": 40.0, "bid_size": 20.0, "volume": 20},
              {"ts": 1.5, "prev_bid_size": 20.0, "bid_size": 4.0, "volume": 16}])
    sw = ib.build_sweeps(df)
    assert sw[0].size_before == 40.0 and sw[0].size_after == 4.0    # avant la 1ère, après la dernière


def test_ratio_et_refilled():
    plein = ib.Sweep("SELL", 30000.0, 1.0, 2.0, 3, 30.0, 10.0, 8.0)
    assert plein.ratio == 3.0 and plein.refilled is True             # 8 >= 0.5*10
    cede = ib.Sweep("SELL", 30000.0, 1.0, 2.0, 3, 30.0, 10.0, 2.0)
    assert cede.refilled is False                                    # 2 < 0.5*10
    sans_avant = ib.Sweep("SELL", 30000.0, 1.0, 2.0, 1, 5.0, None, 5.0)
    assert sans_avant.ratio is None and sans_avant.refilled is False


def test_flag_absorption_filtre_taille_ratio_et_rechargement():
    df = _df([{"ts": t, "volume": 10, "prev_bid_size": 5.0, "bid_size": 4.0}
             for t in (1.0, 1.5, 2.0, 2.5)])                 # 40 sur 5 affichés, ratio 8, recharge
    sw = ib.build_sweeps(df)
    flags = ib.flag_absorption(sw, "NQ")
    assert len(flags) == 1 and flags[0].total_size == 40.0


def test_flag_absorption_rejette_volume_sous_le_seuil():
    df = _df([{"ts": 1.0, "volume": 3, "prev_bid_size": 1.0, "bid_size": 1.0}])
    sw = ib.build_sweeps(df)
    assert ib.flag_absorption(sw, "NQ") == []                # 3 < MIN_TOTAL(NQ)=40


def test_flag_absorption_rejette_si_le_niveau_cede():
    df = _df([{"ts": t, "volume": 15, "prev_bid_size": 30.0, "bid_size": 1.0}
             for t in (1.0, 1.5)])                            # gros volume, mais le bid s'effondre
    sw = ib.build_sweeps(df)
    assert ib.flag_absorption(sw, "NQ") == []


def test_analyze_resume_et_classe_par_ratio():
    df = _df([{"ts": t, "volume": 15, "prev_bid_size": 5.0, "bid_size": 5.0}
             for t in (1.0, 1.5, 2.0)])                        # 45 sur 5 affichés
    out = ib.analyze(df, "NQ")
    assert out["n_sweeps"] >= 1 and out["n_flags"] == 1
    assert out["top"][0]["ratio"] == 9.0


def test_liquidite_fine_de_nuit_nest_pas_signalee():
    """1 contrat affiché, 2 exécutés : bruit, pas de l'absorption (sous le seuil)."""
    df = _df([{"ts": 1.0, "volume": 2, "prev_bid_size": 1.0, "bid_size": 1.0}])
    sw = ib.build_sweeps(df)
    assert ib.flag_absorption(sw, "NQ") == []


def test_seuils_v2_distincts_par_instrument():
    """v2 : ES a un seuil de volume plus haut que NQ (bien plus de ticks en RTH à
    liquidité comparable) — sans ça, ES noyait le détecteur (~3x plus de salves
    retenues que NQ à seuil égal, cf. recalibrage du 2026-09-28)."""
    assert ib.MIN_TOTAL["ES"] > ib.MIN_TOTAL["NQ"]
    assert ib.THRESHOLDS_VERSION == "v2-2026-09-29"


# --- Volume profile de séance (HVL) -----------------------------------------

def test_bucket_price_regroupe_par_palier():
    assert ib.bucket_price(30002.0, "NQ") == 30000.0    # palier NQ = 5 pts
    assert ib.bucket_price(30003.0, "NQ") == 30005.0    # arrondi au plus proche


def test_update_profile_accumule_vol_et_delta():
    levels: dict = {}
    ib.update_profile(levels, 30001.0, "BUY", 10.0, "NQ")
    ib.update_profile(levels, 30002.0, "SELL", 4.0, "NQ")
    lvl = levels[30000.0]
    assert lvl["vol"] == 14.0 and lvl["ask_vol"] == 10.0 and lvl["bid_vol"] == 4.0


def test_update_profile_ignore_cote_indetermine():
    levels: dict = {}
    ib.update_profile(levels, 30000.0, "?", 10.0, "NQ")
    assert levels == {}


def test_hvl_levels_retient_le_palier_hors_norme():
    # bruit de fond ~10 sur plusieurs paliers, un palier concentre 10x plus,
    # tout du même côté (delta = 100% du volume)
    levels = {p: {"vol": 10.0, "bid_vol": 5.0, "ask_vol": 5.0} for p in range(0, 5)}
    levels[999] = {"vol": 100.0, "bid_vol": 0.0, "ask_vol": 100.0}
    out = ib.hvl_levels(levels)
    assert len(out) == 1 and out[0]["price"] == 999 and out[0]["side"] == "BUY"


def test_hvl_levels_rejette_gros_volume_sans_delta():
    """Beaucoup de volume mais équilibré (achat = vente) : pas un HVL directionnel."""
    levels = {p: {"vol": 10.0, "bid_vol": 5.0, "ask_vol": 5.0} for p in range(0, 5)}
    levels[999] = {"vol": 100.0, "bid_vol": 50.0, "ask_vol": 50.0}
    assert ib.hvl_levels(levels) == []


def test_hvl_levels_vide_si_trop_peu_de_paliers_actifs():
    assert ib.hvl_levels({0: {"vol": 100.0, "bid_vol": 0.0, "ask_vol": 100.0}}) == []


def test_hvl_near_trouve_le_palier_proche():
    levels = {p: {"vol": 10.0, "bid_vol": 5.0, "ask_vol": 5.0} for p in range(0, 5)}
    levels[30000.0] = {"vol": 100.0, "bid_vol": 100.0, "ask_vol": 0.0}
    hv = ib.hvl_near(levels, 30002.0, "NQ")            # même palier (arrondi à 30000)
    assert hv is not None and hv["side"] == "SELL"
    assert ib.hvl_near(levels, 30500.0, "NQ") is None  # trop loin


def _ticks_aleatoires(n, seed, avec_indetermines=True):
    import numpy as np
    rng = np.random.default_rng(seed)
    sides = ["BUY", "SELL", "UNDEFINED"] if avec_indetermines else ["BUY", "SELL"]
    p = [0.47, 0.47, 0.06] if avec_indetermines else [0.5, 0.5]
    return pd.DataFrame({
        "ts": np.cumsum(rng.exponential(0.4, n)),
        "price": 100 + np.cumsum(rng.choice([-0.25, 0, 0, 0, 0.25], n)),
        "volume": rng.integers(1, 40, n).astype(float),
        "side": rng.choice(sides, n, p=p),
        **{c: rng.integers(1, 30, n).astype(float)
           for c in ("bid_size", "ask_size", "prev_bid_size", "prev_ask_size")}})


@pytest.mark.parametrize("seed,indetermines", [(1, True), (2, False), (3, True)])
def test_detect_absorptions_vectorise_egal_a_la_version_iterative(seed, indetermines):
    df = _ticks_aleatoires(4000, seed, indetermines)
    ref = ib.flag_absorption(ib.build_sweeps(df), "NQ")
    v = ib.detect_absorptions(df, "NQ")
    assert [(s.side, s.price, s.start_ts, s.end_ts, s.n_prints, s.total_size,
             s.size_before, s.size_after) for s in ref] == \
        [(r.side, r.price, r.start_ts, r.end_ts, r.n_prints, r.total_size,
          r.size_before, r.size_after) for r in v.itertuples()]


def test_detect_absorptions_ecarte_une_taille_affichee_manquante():
    df = _ticks_aleatoires(4000, 4)
    df["prev_bid_size"] = float("nan")
    df["prev_ask_size"] = float("nan")
    assert ib.detect_absorptions(df, "NQ").empty


def test_detect_absorptions_vide():
    assert ib.detect_absorptions(pd.DataFrame(), "NQ").empty
