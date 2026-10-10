"""Tape d'options NQ reconstruit depuis Databento (gex/dbtape.py) et sa
comparaison au tape live (scripts/databento_tape.py)."""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gex import dbtape, greeks
from gex.metrics import ET, YEAR_SECONDS

R = 0.04
F = 20000.0
T0 = pd.Timestamp("2026-10-07 14:00", tz="UTC")          # 10h00 ET
EXP = pd.Timestamp("2026-10-09 20:00", tz="UTC")          # vendredi 16h00 ET


def _defs(underlying="NQZ6"):
    return pd.DataFrame({
        "ts_recv": [T0 - pd.Timedelta(days=1)] * 4,
        "instrument_id": [1, 2, 3, 4],
        "instrument_class": ["C", "P", "C", "F"],
        "strike_price": [20100.0, 19900.0, 20000.0, 0.0],
        "expiration": [EXP, EXP, T0 - pd.Timedelta(hours=1), EXP],
        "underlying": [underlying, underlying, underlying, ""]}).set_index("ts_recv")


def _price(k, is_call, sigma=0.2, ts=T0):
    t = (EXP - ts).total_seconds() / YEAR_SECONDS
    f = greeks.call_price if is_call else greeks.put_price
    return float(f(F, k, t, R, sigma, R)), t


def _trades(rows):
    df = pd.DataFrame(rows, columns=["ts_recv", "instrument_id", "price", "size", "side"])
    return df.set_index("ts_recv")


def _spot(session, epoch):
    return np.full(len(epoch), F)


def test_cote_et_delta_depuis_le_prix():
    pc, t = _price(20100, True)
    pp, _ = _price(19900, False)
    tr = _trades([(T0, 1, pc, 2, "B"),                 # call acheté
                  (T0 + pd.Timedelta(seconds=5), 2, pp, 3, "A"),   # put vendu
                  (T0 + pd.Timedelta(seconds=9), 1, pc, 1, "N")])  # non attribué
    bars = dbtape.trades_to_bars(tr, dbtape.definitions_table(_defs()), _spot, R)
    assert len(bars) == 1
    b = bars.iloc[0]
    dc = float(greeks.call_delta(F, 20100, t, R, 0.2, R))
    dp = float(greeks.put_delta(F, 19900, t, R, 0.2, R))
    assert b["hedge_call_buy"] == pytest.approx(2 * dc * 20 * F, rel=1e-3)
    # put vendu : -1 × 3 × delta(<0) -> pression positive, comme flowtape
    assert b["hedge_put_sell"] == pytest.approx(-3 * dp * 20 * F, rel=1e-3)
    assert b["hedge_put_sell"] > 0
    assert b["net_delta"] == pytest.approx(-(b["hedge_call_buy"] + b["hedge_put_sell"]))
    assert b["buy_contracts"] == 2 and b["sell_contracts"] == 3
    assert b["undefined_prints"] == 1 and b["delta_prints"] == 2 and b["prints"] == 3
    assert b["session"] == "2026-10-07" and str(b["timestamp"]) == "2026-10-07 10:00:00"


def test_instruments_expires_futures_et_prix_absurdes():
    tr = _trades([(T0, 3, 50.0, 1, "B"),               # option déjà expirée
                  (T0, 4, 20000.0, 1, "B"),            # future : pas une option
                  (T0, 1, 0.0, 1, "B")])               # prix nul : pas d'IV
    bars = dbtape.trades_to_bars(tr, dbtape.definitions_table(_defs()), _spot, R)
    assert bars["prints"].sum() == 1 and bars["no_delta_prints"].sum() == 1
    assert bars["hedge_call_buy"].sum() == 0


def test_spot_inconnu_non_valorise():
    pc, _ = _price(20100, True)
    bars = dbtape.trades_to_bars(_trades([(T0, 1, pc, 1, "B")]),
                                 dbtape.definitions_table(_defs()),
                                 lambda s, e: np.full(len(e), np.nan), R)
    assert bars["no_delta_prints"].iloc[0] == 1 and bars["hedge_call_buy"].iloc[0] == 0


@pytest.mark.parametrize("jour,code,incertain", [
    ("2026-09-01", "NQU6", False), ("2026-09-10", "NQU6", True),
    ("2026-09-11", "NQZ6", True), ("2026-09-13", "NQZ6", False),
    ("2026-10-09", "NQZ6", False), ("2026-12-20", "NQH7", False),
    ("2025-12-29", "NQH6", False)])
def test_contrat_du_continu(jour, code, incertain):
    assert dbtape.continuous_contract(date.fromisoformat(jour)) == (code, incertain)


def test_meme_contrat_quel_que_soit_le_format():
    assert dbtape.same_contract("NQZ6", "NQZ6") and dbtape.same_contract("NQZ26", "NQZ6")
    assert not dbtape.same_contract("NQH7", "NQZ6") and not dbtape.same_contract("ESZ6", "NQZ6")


def _partial(session, und, contracts, ts="2026-10-07 10:00"):
    return pd.DataFrame({"session": [session], "timestamp": [pd.Timestamp(ts)],
                         "underlying": [und], **{c: [1.0] for c in dbtape.HEDGE},
                         "net_delta": [-4.0], "buy_contracts": [contracts],
                         "sell_contracts": [0.0], "delta_prints": [1.0],
                         "no_delta_prints": [0.0], "undefined_prints": [0.0], "prints": [1.0]})


def test_finalize_sous_jacent_et_roll():
    out = dbtape.finalize([
        _partial("2026-10-07", "NQZ6", 80.0), _partial("2026-10-07", "NQH7", 20.0),
        _partial("2026-10-07", "NQZ6", 10.0),                         # même minute : sommé
        _partial("2026-09-11", "NQZ6", 10.0, "2026-09-11 10:00"),     # roll incertain
        _partial("2026-10-06", "NQZ6", 10.0, "2026-10-06 10:00"),
        _partial("2026-10-06", "NQH7", 30.0, "2026-10-06 10:00")])    # autre sous-jacent > 50 %
    b = out["2026-10-07"]
    assert len(b) == 1 and b["buy_contracts"].iloc[0] == 90
    assert b["other_und_contracts"].iloc[0] == 20 and b["mult_source"].iloc[0] == "databento"
    assert out["2026-09-11"]["mult_source"].isna().all()
    assert out["2026-09-11"]["roll_session"].all()
    assert out["2026-10-06"]["mult_source"].isna().all()
    assert out["2026-10-06"]["other_share"].iloc[0] == pytest.approx(0.75)


def test_comparaison_au_tape_live(tmp_path, monkeypatch):
    """Deux tapes identiques : corrélation 1 et mêmes couleurs partout."""
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from test_excess import _ecrire_seance
    _ecrire_seance(tmp_path, "2026-10-07", labeled=True)
    live = pd.read_parquet(tmp_path / "tape" / "NQ" / "2026-10-07.parquet")
    rng = np.random.default_rng(1)
    live["hedge_call_buy"] = rng.normal(0, 5e6, len(live))      # flux varié
    live.to_parquet(tmp_path / "tape" / "NQ" / "2026-10-07.parquet")
    (tmp_path / "tape_databento" / "NQ").mkdir(parents=True)
    live.assign(mult_source="databento").to_parquet(
        tmp_path / "tape_databento" / "NQ" / "2026-10-07.parquet")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import databento_tape
    r = databento_tape.compare_session("2026-10-07")
    assert r["corr_net"] == pytest.approx(1.0) and r["meme_couleur_%"] == 100.0
    assert r["brut_db_sur_live"] == pytest.approx(1.0)
    md = databento_tape.compare()
    assert "100.0 %" in md and "2026-10-07" in md
    # le rapport d'excès sait lire le tape reconstruit
    import excess_report
    sessions, _ = excess_report.load_sessions("NQ", flux="databento")
    assert [s["day"] for s in sessions] == ["2026-10-07"]
    assert "tape reconstruit Databento" in excess_report.report("NQ", 100, 60, 120,
                                                                flux="databento")


class _FakeStore:
    def __init__(self, df):
        self.df = df

    def to_df(self, map_symbols=True, count=None):
        if count is None:
            return self.df
        return iter([self.df.iloc[i:i + count] for i in range(0, len(self.df), count)])


def test_reconstruction_ecrit_les_seances(tmp_path, monkeypatch):
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import databento_tape
    pc, _ = _price(20100, True)
    tr = _trades([(T0 + pd.Timedelta(seconds=s), 1, pc, 1, "B") for s in range(0, 300, 30)])
    monkeypatch.setattr(databento_tape, "_stores", lambda: {
        "definition": [(Path("d.dbn.zst"), _FakeStore(_defs()))],
        "trades": [(Path("t.dbn.zst"), _FakeStore(tr))]})
    monkeypatch.setattr(databento_tape, "CHUNK", 4)               # plusieurs morceaux
    monkeypatch.setattr(databento_tape.SpotCache, "__call__", lambda self, s, e: _spot(s, e))
    res = databento_tape.build(rate=R)
    assert set(res) == {"2026-10-07"} and res["2026-10-07"][1]
    b = pd.read_parquet(tmp_path / "tape_databento" / "NQ" / "2026-10-07.parquet")
    assert len(b) == 5 and b["buy_contracts"].sum() == 10      # 10 transactions, 5 minutes
    assert (b["mult_source"] == "databento").all() and (b["source"] == "databento").all()
    # bornes de séances
    assert databento_tape.build(start="2026-10-08", rate=R) == {}
