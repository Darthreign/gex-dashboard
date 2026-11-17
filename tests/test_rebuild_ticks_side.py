"""Rattachement du côté agresseur Databento aux ticks (scripts/rebuild_ticks_side.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import rebuild_ticks_side as r  # noqa: E402


def _ticks(rows):
    return pd.DataFrame(rows, columns=["ts", "price", "volume"]).assign(source="databento")


def _raw(rows):
    return pd.DataFrame(rows, columns=["ts", "price", "size", "side"])


def test_conserve_lignes_et_ordre():
    ticks = _ticks([(2.0, 100.25, 1), (1.0, 100.5, 3)])
    out, share = r.attach_side(ticks, _raw([(1.0, 100.5, 3, "SELL"), (2.0, 100.25, 1, "BUY")]))
    assert share == 1.0
    assert out["side"].tolist() == ["BUY", "SELL"]
    assert out[["ts", "price", "volume", "source"]].equals(ticks)


def test_transactions_identiques_rattachees_par_rang():
    ticks = _ticks([(1.0, 100.0, 1)] * 3)
    raw = _raw([(1.0, 100.0, 1, "BUY"), (1.0, 100.0, 1, "SELL"), (1.0, 100.0, 1, "BUY")])
    out, share = r.attach_side(ticks, raw)
    assert share == 1.0 and out["side"].tolist() == ["BUY", "SELL", "BUY"]


def test_doublons_entre_fichiers_bruts_sans_effet():
    ticks = _ticks([(1.0, 100.0, 1)])
    out, share = r.attach_side(ticks, _raw([(1.0, 100.0, 1, "SELL"), (1.0, 100.0, 1, "SELL")]))
    assert len(out) == 1 and share == 1.0


def test_sans_correspondance_side_inconnu_et_part_mesuree():
    out, share = r.attach_side(_ticks([(1.0, 100.0, 1), (2.0, 100.0, 1)]),
                               _raw([(1.0, 100.0, 1, "BUY")]))
    assert share == 0.5 and out["side"].tolist() == ["BUY", "UNDEFINED"]


def test_autre_contrat_ignore():
    out, share = r.attach_side(_ticks([(1.0, 100.0, 1)]),
                               _raw([(1.0, 300.0, 1, "SELL"), (1.0, 100.0, 1, "BUY")]))
    assert share == 1.0 and out["side"].tolist() == ["BUY"]
