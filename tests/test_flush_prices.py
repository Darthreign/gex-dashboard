"""flush_prices() doit vider QUOTES (compte courtier) ET PUBLIC_QUOTES (repli
gratuit délayé) — un oubli du 2026-07-28 laissait les bougies du repli
s'accumuler en mémoire sans jamais être écrites sur disque, ce qui faisait
retomber le Heatmap sur le point-par-pull (~10 min) même avec un spot délayé
actif sur NQ/ES.
"""
from __future__ import annotations

import time

import pandas as pd

from gex import scheduler, store
from gex.rtquote import Bar


def test_flush_prices_vide_les_deux_sources(tmp_path, monkeypatch):
    from gex.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    minute = int(time.time() // 60) * 60

    monkeypatch.setattr(scheduler.QUOTES, "drain_bars",
                        lambda: [("SPX", Bar(minute, 100.0, 101.0, 99.0, 100.5))])
    monkeypatch.setattr(scheduler.PUBLIC_QUOTES, "drain_bars",
                        lambda: [("NQ", Bar(minute, 28000.0, 28010.0, 27990.0, 28005.0))])

    scheduler.flush_prices()

    day = pd.Timestamp.now().strftime("%Y-%m-%d")
    spx = store.load_prices("SPX", day)
    nq = store.load_prices("NQ", day)
    assert not spx.empty and spx["source"].iloc[0] == "dxfeed"
    assert not nq.empty and nq["source"].iloc[0] == "dxfeed_public"


def test_flush_prices_rien_a_faire_sans_bougies(tmp_path, monkeypatch):
    from gex.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    monkeypatch.setattr(scheduler.QUOTES, "drain_bars", lambda: [])
    monkeypatch.setattr(scheduler.PUBLIC_QUOTES, "drain_bars", lambda: [])

    scheduler.flush_prices()  # ne doit pas lever, ni rien écrire

    assert not (tmp_path / "prices").exists()
