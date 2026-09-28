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
    assert ib.flag_absorption(sw, "NQ") == []                # 3 < MIN_TOTAL(NQ)=20


def test_flag_absorption_rejette_si_le_niveau_cede():
    df = _df([{"ts": t, "volume": 15, "prev_bid_size": 30.0, "bid_size": 1.0}
             for t in (1.0, 1.5)])                            # gros volume, mais le bid s'effondre
    sw = ib.build_sweeps(df)
    assert ib.flag_absorption(sw, "NQ") == []


def test_analyze_resume_et_classe_par_ratio():
    df = _df([{"ts": t, "volume": 10, "prev_bid_size": 5.0, "bid_size": 5.0}
             for t in (1.0, 1.5, 2.0)])
    out = ib.analyze(df, "NQ")
    assert out["n_sweeps"] >= 1 and out["n_flags"] == 1
    assert out["top"][0]["ratio"] == 6.0


def test_liquidite_fine_de_nuit_nest_pas_signalee():
    """1 contrat affiché, 2 exécutés : bruit, pas de l'absorption (sous le seuil)."""
    df = _df([{"ts": 1.0, "volume": 2, "prev_bid_size": 1.0, "bid_size": 1.0}])
    sw = ib.build_sweeps(df)
    assert ib.flag_absorption(sw, "NQ") == []
