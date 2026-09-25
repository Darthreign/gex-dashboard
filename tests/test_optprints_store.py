"""Écriture disque des prints bruts d'options (par heure ET)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gex import flowtape, scheduler, store
from gex.config import SETTINGS


def _row(ts, sym="SPX", **kw):
    return {"ts": ts, "ts_recv": ts + 0.05, "symbol": sym, "contract": ".SPXW260925C7700",
            "price": 5.0, "size": 2.0, "bid": 4.9, "ask": 5.1, "side": "BUY",
            "spread": False, "exch": None, "cond": None, "ttype": "NEW",
            "delta": 0.4, "gamma": 0.001, "und": 7700.0, "source": "dxfeed", **kw}


@pytest.fixture()
def racine(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    return tmp_path


def test_append_reecrit_le_fichier_de_lheure_sans_perte(racine):
    h = datetime(2026, 9, 25, 10)
    store.append_optprints("SPX", [_row(1.0), _row(2.0)], h)
    store.append_optprints("SPX", [_row(3.0)], h)
    p = racine / "optprints" / "SPX" / "2026-09-25" / "10.parquet"
    assert p.exists()
    df = store.load_optprints("SPX", "2026-09-25")
    assert list(df["ts"]) == [1.0, 2.0, 3.0]
    assert df["delta"].iloc[0] == 0.4 and df["und"].iloc[0] == 7700.0


def test_load_trie_stable_les_ex_aequo_gardent_leur_ordre(racine):
    h = datetime(2026, 9, 25, 10)
    store.append_optprints("SPX", [_row(5.0, size=1.0), _row(5.0, size=2.0),
                                   _row(5.0, size=3.0), _row(4.0, size=9.0)], h)
    df = store.load_optprints("SPX", "2026-09-25")
    assert list(df["size"]) == [9.0, 1.0, 2.0, 3.0]


def test_flush_range_par_symbole_et_par_heure_et(racine, monkeypatch):
    t = flowtape.FlowTape()
    monkeypatch.setattr(flowtape, "TAPE", t)
    # 2026-09-25 14:59:59 UTC = 10:59:59 ET ; +1 s = 11:00:00 ET (heure suivante)
    base = datetime(2026, 9, 25, 14, 59, 59, tzinfo=timezone.utc).timestamp()
    t._raw = [_row(base), _row(base + 1), _row(base, sym="NDX")]
    scheduler.flush_optprints()
    assert (racine / "optprints" / "SPX" / "2026-09-25" / "10.parquet").exists()
    assert (racine / "optprints" / "SPX" / "2026-09-25" / "11.parquet").exists()
    assert (racine / "optprints" / "NDX" / "2026-09-25" / "10.parquet").exists()
    assert t._raw == []


def test_flush_sans_rien_a_ecrire_ne_cree_rien(racine, monkeypatch):
    monkeypatch.setattr(flowtape, "TAPE", flowtape.FlowTape())
    scheduler.flush_optprints()
    assert not (racine / "optprints").exists()
