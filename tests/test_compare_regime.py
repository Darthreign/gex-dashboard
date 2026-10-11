"""Concordance du régime gamma : tableau de bord contre reconstruction
Databento (scripts/compare_regime.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from test_dbgamma import EXP, _stats  # noqa: E402
from test_excess import _ecrire_seance  # noqa: E402


def test_concordance(tmp_path, monkeypatch):
    from gex import store
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    monkeypatch.setattr(store, "_HISTORY_CACHE", None)
    for d in ("2026-10-06", "2026-10-07"):
        _ecrire_seance(tmp_path, d, labeled=True)
    import compare_regime
    import excess_report
    defs = pd.DataFrame({"instrument_id": [1, 2], "type": ["C", "P"],
                         "strike": [20100.0, 19700.0], "expiration": [EXP, EXP],
                         "underlying": ["NQZ6", "NQZ6"]})
    stats = _stats({1: 5000.0, 2: 500.0}, {1: 0.2, 2: 0.2})      # gros calls : frein
    monkeypatch.setattr(excess_report, "gamma_context", lambda raw=None: (defs, stats))
    monkeypatch.setattr(compare_regime, "_straddle_em", lambda s, d: 100.0)
    hist = pd.DataFrame({"timestamp": pd.to_datetime(["2026-10-07 10:00", "2026-10-07 11:00"]),
                         "symbol": "NQ", "spot": [19950.0, 19960.0],
                         "net_gex": [2e9, -1e9], "zero_gamma": [19900.0, 19900.0],
                         "net_gex_0dte": [1e9, 1e9], "source": "native"})
    (tmp_path / "history").mkdir()
    hist.to_parquet(tmp_path / "history" / "metrics.parquet")
    c = compare_regime.compare(compare_regime.dashboard_rows(), defs, stats)
    assert len(c) == 2 and (c["regime_etude"] == "frein").all()
    assert (c["zone_scalp"] == "frein").all()
    s = compare_regime.summarize(c)
    assert s["zone_scalp_egale_regime_%"] == 100.0
    assert s["meme_signe_gex_%"] == pytest.approx(50.0)          # 2e ligne : signe opposé
    md = compare_regime.report()
    assert "Synthèse" in md and "zone_scalp_egale_regime_%" in md
