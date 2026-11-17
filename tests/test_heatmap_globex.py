"""Heatmap d'un future : séance CME complète (nuit Globex comprise) et bougie
en cours en direct (app._futures_overlay / futures_session_bars)."""
from datetime import datetime, timedelta

import pandas as pd

from gex import app as A
from gex import store
from gex.config import SETTINGS
from gex.metrics import ET


def _bars(start: datetime, n: int, px: float = 100.0) -> list[dict]:
    return [{"timestamp": start + timedelta(minutes=i), "open": px, "high": px + 1,
             "low": px - 1, "close": px, "source": "dxfeed"} for i in range(n)]


def test_jour_passe_de_18h_la_veille_a_17h(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    veille = datetime(2026, 10, 7, 16, 0)
    store.append_prices("NQ", _bars(veille, 180), veille)                 # 16h-19h le 07
    jour = datetime(2026, 10, 8, 0, 0)
    store.append_prices("NQ", _bars(jour, 24 * 60), jour)                 # tout le 08
    px = A._futures_overlay("NQ", "2026-10-08")
    assert px["timestamp"].iloc[0] == pd.Timestamp("2026-10-07 18:00")   # nuit Globex
    assert px["timestamp"].iloc[-1] == pd.Timestamp("2026-10-08 16:59")  # avant la maintenance


def test_seance_en_cours_avec_bougie_live(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    A._PRICES_CACHE.clear()
    A._LIVE_BARS.clear()
    now = datetime.now(ET).replace(tzinfo=None, second=0, microsecond=0)
    start = now - timedelta(minutes=30)
    store.append_prices("NQ", _bars(start, 25), now)                      # disque : 25 min
    monkeypatch.setattr(A, "_futures_last_price", lambda s: 123.0)
    px = A._futures_overlay("NQ", now.strftime("%Y-%m-%d"))
    assert px["timestamp"].iloc[-1] == now                                 # minute en cours
    assert px["close"].iloc[-1] == 123.0
