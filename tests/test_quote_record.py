"""Enregistrement des changements de cotation (premier niveau du carnet) pour le replay."""
from __future__ import annotations

import pandas as pd

from gex import scheduler, store, tickcapture
from gex.config import SETTINGS
from gex.tickcapture import QUOTE_COLUMNS, TickCapture

T0 = 1_794_600_000.0          # 2026-11-13 20:00:00 UTC, séance CME du 2026-11-13


def _capture(monkeypatch, enabled=True):
    monkeypatch.setattr(tickcapture, "quote_recording_enabled", lambda: enabled)
    c = TickCapture()
    c._active = {"/NQZ26:XCME": "NQ"}
    return c


def _q(sym, bid, bs, ask, as_, **extra):
    return {"eventSymbol": sym, "bidPrice": bid, "bidSize": bs, "askPrice": ask, "askSize": as_, **extra}


def test_desactive_par_defaut(monkeypatch):
    monkeypatch.setattr(tickcapture, "_env", lambda k: None)
    assert tickcapture.quote_recording_enabled() is False
    c = _capture(monkeypatch, enabled=False)
    c.quote(_q("/NQZ26:XCME", 28000.0, 3, 28000.25, 5), T0)
    assert c.drain_quotes() == ({}, 0)


def test_chaque_changement_du_contrat_actif(monkeypatch):
    c = _capture(monkeypatch)
    c.quote(_q("/NQZ26:XCME", 28000.0, 3, 28000.25, 5, bidTime=1_794_599_999_000, askTime=1_794_599_998_000), T0)
    c.quote(_q("/NQZ26:XCME", 28000.0, 3, 28000.25, 5), T0 + 0.1)        # doublon : rien
    c.quote(_q("/NQZ26:XCME", 28000.0, 4, 28000.25, 5), T0 + 0.2)        # taille seule : enregistré
    c.quote(_q("/NQH27:XCME", 28300.0, 1, 28301.0, 1), T0 + 0.3)         # contrat suivant : ignoré
    rows, dropped = c.drain_quotes()
    assert dropped == 0 and rows == {"NQ": [
        (T0, 1_794_599_999.0, 28000.0, 3.0, 28000.25, 5.0),
        (T0 + 0.2, None, 28000.0, 4.0, 28000.25, 5.0)]}
    assert c.drain_quotes() == ({}, 0)                                   # tampon vidé
    # les tailles restent rattachées aux transactions comme avant
    assert c._quotes["/NQZ26:XCME"]["prev_bid_size"] == 3.0


def test_tampon_plafonne(monkeypatch):
    monkeypatch.setattr(tickcapture, "QUOTE_BUFFER_MAX", 2)
    c = _capture(monkeypatch)
    for i in range(5):
        c.quote(_q("/NQZ26:XCME", 28000.0 + i, 1, 28001.0 + i, 1), T0 + i)
    rows, dropped = c.drain_quotes()
    assert len(rows["NQ"]) == 2 and dropped == 3
    c.quote(_q("/NQZ26:XCME", 1.0, 1, 2.0, 1), T0 + 9)                   # après vidange : ça reprend
    assert len(c.drain_quotes()[0]["NQ"]) == 1


def test_vidange_par_seance_et_par_heure(monkeypatch, tmp_path):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    rows = [(T0, None, 1.0, 1.0, 2.0, 1.0), (T0 + 1, None, 1.0, 2.0, 2.0, 1.0),
            (T0 + 3 * 3600, None, 1.5, 1.0, 2.5, 1.0)]                   # 23:00 UTC = 18:00 New York : séance suivante
    monkeypatch.setattr(scheduler.CAPTURE, "drain_quotes", lambda: ({"NQ": rows}, 0))
    scheduler.flush_quotes()
    monkeypatch.setattr(scheduler.CAPTURE, "drain_quotes", lambda: ({"NQ": [(T0 + 2, None, 1.0, 3.0, 2.0, 1.0)]}, 0))
    scheduler.flush_quotes()
    a = pd.read_parquet(tmp_path / "quotes" / "NQ" / "2026-11-13" / "20.parquet")
    assert list(a.columns) == list(QUOTE_COLUMNS) and a["bid_size"].tolist() == [1.0, 2.0, 3.0]
    b = pd.read_parquet(tmp_path / "quotes" / "NQ" / "2026-11-14" / "23.parquet")
    assert b["bid"].tolist() == [1.5]


def test_vidange_vide_n_ecrit_rien(monkeypatch, tmp_path):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    monkeypatch.setattr(scheduler.CAPTURE, "drain_quotes", lambda: ({}, 0))
    scheduler.flush_quotes()
    assert not (tmp_path / "quotes").exists()
    assert store.append_quotes("NQ", "2026-11-13", "22", [], QUOTE_COLUMNS) is None
