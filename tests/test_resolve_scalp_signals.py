"""Résolution des signaux /scalp après coup (gex/scheduler.resolve_scalp_signals)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "discord_bot"))
import journal  # noqa: E402

from gex import api, scheduler  # noqa: E402
from gex.config import SETTINGS  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    return tmp_path / "journal" / "journal.sqlite"


def test_resout_un_signal_assez_vieux_et_ignore_le_trop_recent(db, monkeypatch):
    now = datetime.now(PARIS)
    conn = journal.connect(db)
    vieux = (now - timedelta(minutes=20)).isoformat()
    recent = (now - timedelta(minutes=2)).isoformat()
    id_vieux = journal.record_scalp_signal(conn, date="d", ts=vieux, symbol="NQ",
                                           state="amplification", tone="alert", direction=1,
                                           title="t", spot=30500.0)
    id_recent = journal.record_scalp_signal(conn, date="d", ts=recent, symbol="NQ",
                                             state="amplification", tone="alert", direction=1,
                                             title="t", spot=30500.0)
    conn.close()

    monkeypatch.setattr(api, "_futures_last_price", lambda s: 30520.0)
    scheduler.resolve_scalp_signals(now=now)

    conn = journal.connect(db)
    vieux_row = conn.execute("SELECT * FROM scalp_signals WHERE id=?", (id_vieux,)).fetchone()
    recent_row = conn.execute("SELECT * FROM scalp_signals WHERE id=?", (id_recent,)).fetchone()
    assert vieux_row["resolved_ts"] is not None
    assert vieux_row["outcome"] == "continued" and vieux_row["outcome_move_pts"] == 20.0
    assert recent_row["resolved_ts"] is None                  # trop récent, pas encore jugé


def test_sans_prix_disponible_reste_non_resolu(db, monkeypatch):
    now = datetime.now(PARIS)
    conn = journal.connect(db)
    ts = (now - timedelta(minutes=20)).isoformat()
    sid = journal.record_scalp_signal(conn, date="d", ts=ts, symbol="NQ", state="brake",
                                      tone="ok", direction=-1, title="t", spot=30500.0)
    conn.close()

    monkeypatch.setattr(api, "_futures_last_price", lambda s: None)
    scheduler.resolve_scalp_signals(now=now)

    conn = journal.connect(db)
    row = conn.execute("SELECT * FROM scalp_signals WHERE id=?", (sid,)).fetchone()
    assert row["resolved_ts"] is None


def test_ne_leve_jamais_meme_si_le_journal_est_hors_service(monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", Path("Z:/chemin/qui/nexiste/pas"))
    scheduler.resolve_scalp_signals()          # ne doit pas lever
