"""Cadre d'amplitude de l'open US + mouvement attendu (gex/openframe.py),
portage de l'indicateur Pine de l'utilisateur."""
from __future__ import annotations

import math
from datetime import datetime, time

import pandas as pd
import pytest

from gex import openframe, store


def _day(symbol, day, start, end, path_fn):
    """Bougies 1 min de `start` à `end` (heures NY) ; path_fn(minute) -> prix."""
    ts = pd.date_range(f"{day} {start}", f"{day} {end}", freq="1min", inclusive="left")
    rows = [{"timestamp": t, "open": path_fn(i), "high": path_fn(i) + 1,
             "low": path_fn(i) - 1, "close": path_fn(i)} for i, t in enumerate(ts)]
    store.append_prices(symbol, rows, ts[0].to_pydatetime())


@pytest.fixture()
def data(tmp_path, monkeypatch):
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    openframe._CACHE.clear()
    # trois séances : 1re heure qui monte de 40, 60, 80 pts depuis l'open
    for k, (day, rise) in enumerate((("2026-10-13", 40), ("2026-10-14", 60),
                                     ("2026-10-15", 80))):
        _day("NQ", day, "09:30", "16:00", lambda i, r=rise: 30000 + min(i, 59) * r / 59)
        _day("VXN", day, "09:30", "16:15", lambda i, k=k: 20.0 + k)
    _day("NQ", "2026-10-16", "09:30", "11:00", lambda i: 31000 + i)
    _day("VXN", "2026-10-16", "09:30", "11:00", lambda i: 25.0)
    return tmp_path


def test_fenetre_par_defaut_et_saisie_invalide():
    assert openframe.parse_window("0930-1030") == (time(9, 30), time(10, 30))
    assert openframe.parse_window("1030-0930") == (time(9, 30), time(10, 30))
    assert openframe.parse_window("abc") == (time(9, 30), time(10, 30))
    assert openframe.parse_window("0930-1000") == (time(9, 30), time(10, 0))


def test_moyennes_de_la_premiere_heure(data):
    s = openframe.window_stats("NQ", "2026-10-16", time(9, 30), time(10, 30), 60)
    # montée r + 1 (mèche), baisse 1, amplitude r + 2
    assert s["count"] == 3
    assert s["avg_up"] == pytest.approx(61) and s["avg_dn"] == pytest.approx(1)
    assert s["avg_range"] == pytest.approx(62)
    deux = openframe.window_stats("NQ", "2026-10-16", time(9, 30), time(10, 30), 2)
    assert deux["count"] == 2 and deux["avg_range"] == pytest.approx(72)   # les plus récentes


def test_cadre_et_mouvement_attendu_depuis_la_cloture(data):
    now = datetime(2026, 10, 16, 10, 45)
    r = openframe.compute("NQ", "0930-1030", 60, "close", straddle_em=150.0, now=now)
    f = r["frame"]
    assert f["open"] == 31000 and f["count"] == 3
    assert f["iv_prev"] == 22.0 and f["iv_avg"] == pytest.approx(21.0)
    e = r["em"]
    assert e["base"] == 30080.0                         # clôture du 15/10 (dernière bougie)
    assert e["iv"] == 22.0                              # VXN connu à la clôture
    assert e["iv_em"] == pytest.approx(30080 * 22 / 100 / math.sqrt(252))
    assert e["straddle_em"] == 150.0
    assert e["end"] - e["start"] == 24 * 3600           # jusqu'à la clôture du jour


def test_points_de_depart():
    lundi_matin = datetime(2026, 10, 19, 8, 0)
    start, end = openframe.anchor_time("close", lundi_matin)
    assert start == datetime(2026, 10, 16, 16, 0) and end == datetime(2026, 10, 19, 16, 0)
    samedi = datetime(2026, 10, 17, 20, 0)
    assert openframe.anchor_time("close", samedi)[0] == datetime(2026, 10, 16, 16, 0)
    # open 9h30 : rien avant l'open, affiché jusqu'à 16h00, éteint après
    assert openframe.anchor_time("open", datetime(2026, 10, 16, 9, 0)) is None
    assert openframe.anchor_time("open", datetime(2026, 10, 16, 12, 0)) == (
        datetime(2026, 10, 16, 9, 30), datetime(2026, 10, 16, 16, 0))
    assert openframe.anchor_time("open", datetime(2026, 10, 16, 17, 0)) is None
    # Globex 18h00 : jusqu'à la clôture du lendemain
    assert openframe.anchor_time("globex", datetime(2026, 10, 15, 22, 0)) == (
        datetime(2026, 10, 15, 18, 0), datetime(2026, 10, 16, 16, 0))


def test_pas_de_cadre_avant_l_open(data):
    r = openframe.compute("NQ", None, 60, "close", now=datetime(2026, 10, 16, 9, 0))
    # avant 9h30 : pas encore de cadre ; le mouvement attendu (depuis la
    # clôture de la veille) est déjà là, comme dans le script Pine
    assert r["frame"] is None
    assert r["em"] is not None and r["em"]["base"] == 30080.0
