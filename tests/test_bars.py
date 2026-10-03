"""Barres tick/volume/range et détection zigzag (gex/bars.py)."""
from __future__ import annotations

import pandas as pd
import pytest

from gex import bars


def _df(rows: list[dict]) -> pd.DataFrame:
    base = {"side": "BUY", "volume": 1.0}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_tick_bars_regroupe_n_prints():
    df = _df([{"ts": i, "price": 100.0 + i} for i in range(5)])
    out = bars.tick_bars(df, n_ticks=2)
    assert len(out) == 3  # 2 + 2 + 1 (dernière incomplète incluse)
    assert out.iloc[0]["open"] == 100.0 and out.iloc[0]["close"] == 101.0
    assert out.iloc[0]["n_prints"] == 2


def test_tick_bars_cote_indetermine_exclu():
    df = _df([{"ts": 0, "price": 100.0, "side": "BUY"},
              {"ts": 1, "price": 999.0, "side": "UNDEFINED"},
              {"ts": 2, "price": 101.0, "side": "SELL"}])
    out = bars.tick_bars(df, n_ticks=2)
    assert len(out) == 1
    assert out.iloc[0]["high"] == 101.0  # le print à 999 n'a pas pollué la barre


def test_volume_bars_cloture_au_volume_cible():
    df = _df([{"ts": 0, "price": 100.0, "volume": 3},
              {"ts": 1, "price": 101.0, "volume": 4},
              {"ts": 2, "price": 102.0, "volume": 1}])
    out = bars.volume_bars(df, bar_volume=5.0)
    assert len(out) == 2
    assert out.iloc[0]["volume"] == 7.0  # 3 + 4 franchit le seuil de 5
    assert out.iloc[1]["volume"] == 1.0


def test_volume_bars_distingue_buy_sell():
    df = _df([{"ts": 0, "price": 100.0, "side": "BUY", "volume": 5},
              {"ts": 1, "price": 100.0, "side": "SELL", "volume": 3}])
    out = bars.volume_bars(df, bar_volume=100.0)  # une seule barre, pas clôturée
    assert len(out) == 1
    assert out.iloc[0]["buy_vol"] == 5.0 and out.iloc[0]["sell_vol"] == 3.0


def test_range_bars_cloture_sur_amplitude_prix():
    df = _df([{"ts": i, "price": p} for i, p in
              enumerate([100.0, 100.5, 101.0, 102.0, 102.2, 102.4])])
    out = bars.range_bars(df, bar_range=2.0)
    assert len(out) == 2
    assert out.iloc[0]["high"] - out.iloc[0]["open"] >= 2.0 or \
           out.iloc[0]["open"] - out.iloc[0]["low"] >= 2.0


def test_bar_sizes_invalides_rejetees():
    df = _df([{"ts": 0, "price": 100.0}])
    with pytest.raises(ValueError):
        bars.tick_bars(df, n_ticks=0)
    with pytest.raises(ValueError):
        bars.volume_bars(df, bar_volume=0)
    with pytest.raises(ValueError):
        bars.range_bars(df, bar_range=0)


def _bar_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "ts_close": range(len(closes)), "close": closes,
        "high": closes, "low": closes,
    })


def test_zigzag_detecte_un_sommet_puis_un_creux():
    # monte à 110 (sommet), retombe à 95 (creux), confirmés par un min_move=5
    closes = [100, 103, 106, 110, 108, 104, 99, 95, 98, 101]
    out = bars.zigzag(_bar_df(closes), min_move=5.0)
    kinds = out["kind"].tolist()
    assert "H" in kinds and "L" in kinds
    # le sommet confirmé doit être le vrai extremum (110), pas un point intermédiaire
    h_row = out[out["kind"] == "H"].iloc[0]
    assert h_row["price"] == 110


def test_zigzag_ignore_le_bruit_sous_le_seuil():
    # oscillations de 2 pts, jamais assez pour confirmer un swing à min_move=10
    closes = [100, 101, 99, 100, 102, 100, 101]
    out = bars.zigzag(_bar_df(closes), min_move=10.0)
    confirmed = out[~out["kind"].str.endswith("?")]
    assert confirmed.empty


def test_zigzag_dataframe_vide():
    out = bars.zigzag(_bar_df([]), min_move=5.0)
    assert out.empty


def test_zigzag_min_move_invalide():
    with pytest.raises(ValueError):
        bars.zigzag(_bar_df([1.0, 2.0]), min_move=0)


def test_trend_move_depuis_dernier_pivot_confirme():
    closes = [100, 103, 106, 110, 108, 104, 99, 95, 98, 101]
    sw = bars.zigzag(_bar_df(closes), min_move=5.0)
    # dernier pivot CONFIRMÉ (pas le "?") est le L à 95 -> mouvement depuis 95
    assert bars.trend_move(sw, current_price=103.0) == pytest.approx(8.0)


def test_trend_move_aucun_pivot_confirme():
    sw = bars.zigzag(_bar_df([100, 101, 102]), min_move=50.0)
    assert bars.trend_move(sw, current_price=103.0) is None


def test_swing_move_bout_en_bout():
    # 200 prints de 1 contrat : largement assez pour plusieurs barres-volume=10
    prices = [30000.0 + i * 0.5 for i in range(100)] + [30050.0 - i * 0.5 for i in range(100)]
    df = _df([{"ts": i, "price": p, "volume": 1} for i, p in enumerate(prices)])
    move = bars.swing_move(df, current_price=30020.0, bar_volume=10.0, min_move=5.0)
    assert move is not None


def test_swing_move_pas_assez_de_ticks():
    df = _df([{"ts": 0, "price": 100.0}])
    assert bars.swing_move(df, current_price=101.0, bar_volume=60.0) is None
