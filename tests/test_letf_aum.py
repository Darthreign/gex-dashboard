"""Encours des ETF à levier (gex/letf_aum.py) : sources, garde-fous, fichier."""
import json
from datetime import datetime

import pytest

from gex import letf_aum, moc
from gex.config import SETTINGS
from gex.metrics import ET

NOW = datetime(2026, 10, 8, 18, 10, tzinfo=ET)


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    return tmp_path


def src(name, values):
    return (name, lambda tickers: {t: v for t, v in values.items() if t in tickers})


def test_first_run_writes_compatible_file():
    r = letf_aum.update([src("yahoo", {"TQQQ": 2.6e10, "SPXL": 5.5e9})], NOW)
    assert set(r["updated"]) == {"TQQQ", "SPXL"}
    cfg, custom = moc.letf_config("NQ")
    assert custom and cfg["TQQQ"] == (3.0, 2.6e10)
    assert cfg["SQQQ"] == moc.DEFAULT_LETF["NQ"]["SQQQ"]        # défaut gardé
    meta = json.loads(letf_aum.path().read_text())["_meta"]["etf"]
    assert meta["TQQQ"]["source"] == "yahoo" and meta["SQQQ"]["source"] == "défaut"


def test_fallback_source_fills_what_first_missed():
    r = letf_aum.update([src("yahoo", {"TQQQ": 2.6e10}), src("proshares", {"TQQQ": 1.0, "QLD": 9e9})], NOW)
    meta = letf_aum.load()["_meta"]["etf"]
    assert meta["TQQQ"]["source"] == "yahoo" and meta["QLD"]["source"] == "proshares"
    assert "TQQQ" in r["updated"] and "QLD" in r["updated"]


def test_implausible_values_never_replace_good_ones():
    letf_aum.update([src("yahoo", {"TQQQ": 2.6e10})], NOW)
    r = letf_aum.update([src("yahoo", {"TQQQ": 2.6e7, "SQQQ": 1e3})], NOW)   # ÷1000, et absurde
    assert set(r["rejected"]) == {"TQQQ", "SQQQ"}
    assert moc.letf_config("NQ")[0]["TQQQ"] == (3.0, 2.6e10)


def test_failing_source_keeps_previous_value_and_date():
    letf_aum.update([src("yahoo", {"TQQQ": 2.6e10})], NOW)

    def boom(_):
        raise RuntimeError("Yahoo a changé")
    r = letf_aum.update([("yahoo", boom)], NOW.replace(day=9))
    assert "TQQQ" in r["kept"]
    assert letf_aum.load()["_meta"]["etf"]["TQQQ"]["asof"].startswith("2026-10-08")


def test_freshness_and_staleness():
    assert letf_aum.freshness("NQ", NOW) == (None, True)
    every = {t: 1e9 for fam in moc.DEFAULT_LETF.values() for t in fam}
    letf_aum.update([src("yahoo", every)], NOW)
    assert letf_aum.freshness("NQ", NOW) == ("2026-10-08", False)
    assert not letf_aum.is_stale(NOW)
    assert letf_aum.is_stale(NOW.replace(day=20))


@pytest.mark.parametrize("html,expected", [
    ("<td>Net Assets</td><td>$27.41 billion</td>", 27.41e9),
    ("<div>Net Assets as of 10/07/2026</div><span>$ 812.5M</span>", 812.5e6),
    ("Net Assets: $27,410,123,456", 27410123456.0),
    ("<p>Pas de chiffre ici</p>", None),
])
def test_parse_net_assets(html, expected):
    v = letf_aum.parse_net_assets(html)
    assert v == (pytest.approx(expected) if expected else None)
