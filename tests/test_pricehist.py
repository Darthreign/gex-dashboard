"""Backfill de bougies historiques : conversion et répartition.

La partie réseau n'est pas testée ici ; ce qui compte et qui casse
silencieusement, c'est la conversion des événements dxFeed en table exploitable
et leur répartition dans les fichiers journaliers.
"""
from __future__ import annotations

import pandas as pd

from gex import pricehist


def _candle(t_ms: int, close: float, **kw) -> dict:
    base = {"eventType": "Candle", "time": t_ms, "open": close, "high": close,
            "low": close, "close": close, "volume": 100.0}
    base.update(kw)
    return base


def test_symbole_de_bougie():
    assert pricehist.candle_symbol("/NQU26:XCME") == "/NQU26:XCME{=m}"
    assert pricehist.candle_symbol("NVDA", "5m") == "NVDA{=5m}"


def test_conversion_en_heure_de_new_york():
    """Le reste du stockage est horodaté en ET naïf : une bougie arrivant en
    epoch UTC doit rejoindre cette convention, sinon les journées se
    mélangent."""
    # 2026-07-24 13:30 UTC = 09:30 ET (heure d'été)
    ms = int(pd.Timestamp("2026-07-24 13:30", tz="UTC").timestamp() * 1000)
    df = pricehist.candles_to_frame([_candle(ms, 7400.0)])
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-07-24 09:30")
    assert df["timestamp"].dt.tz is None


def test_provenance_marquee():
    """Donnée courtier : sans cette marque, l'export ne pourrait pas l'exclure."""
    ms = int(pd.Timestamp("2026-07-24 13:30", tz="UTC").timestamp() * 1000)
    df = pricehist.candles_to_frame([_candle(ms, 1.0)])
    assert set(df["source"].unique()) == {"dxfeed"}


def test_bougies_de_synchronisation_ecartees():
    """dxFeed intercale des enregistrements dont tous les champs valent NaN ;
    les garder créerait des trous en pleine séance."""
    ms = int(pd.Timestamp("2026-07-24 13:30", tz="UTC").timestamp() * 1000)
    rows = [_candle(ms, 7400.0), _candle(ms + 60000, float("nan"))]
    df = pricehist.candles_to_frame(rows)
    assert len(df) == 1


def test_tri_chronologique():
    t0 = int(pd.Timestamp("2026-07-24 14:00", tz="UTC").timestamp() * 1000)
    rows = [_candle(t0 + 120000, 3.0), _candle(t0, 1.0), _candle(t0 + 60000, 2.0)]
    df = pricehist.candles_to_frame(rows)
    assert df["close"].tolist() == [1.0, 2.0, 3.0]


def test_liste_vide():
    assert pricehist.candles_to_frame([]).empty


def test_champs_manquants():
    """Un événement incomplet ne doit pas faire tomber le backfill entier."""
    assert pricehist.candles_to_frame([{"eventType": "Candle"}]).empty


def test_repartition_par_journee(tmp_path, monkeypatch):
    """Le stockage est journalier : deux séances doivent produire deux
    fichiers, pas un seul."""
    from gex.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    df = pd.DataFrame({
        "timestamp": [pd.Timestamp("2026-07-23 10:00"),
                      pd.Timestamp("2026-07-23 10:01"),
                      pd.Timestamp("2026-07-24 10:00"),
                      pd.Timestamp("2026-07-24 10:01")],
        "open": [1.0, 2.0, 3.0, 4.0], "high": [1.0, 2.0, 3.0, 4.0],
        "low": [1.0, 2.0, 3.0, 4.0], "close": [1.0, 2.0, 3.0, 4.0],
        "volume": [10.0] * 4, "source": ["dxfeed"] * 4,
    })
    days = pricehist.write_by_day("NQ", df)
    assert sorted(days) == ["2026-07-23", "2026-07-24"]
    from gex import store
    assert len(store.load_prices("NQ", "2026-07-23")) == 2
    assert len(store.load_prices("NQ", "2026-07-24")) == 2


def test_ecriture_idempotente(tmp_path, monkeypatch):
    """Relancer un backfill ne doit pas dupliquer les bougies déjà présentes."""
    from gex import store
    from gex.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    df = pd.DataFrame({
        "timestamp": [pd.Timestamp("2026-07-23 10:00"),
                      pd.Timestamp("2026-07-23 10:01")],
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
        "close": [1.0, 2.0], "volume": [10.0, 10.0], "source": ["dxfeed"] * 2,
    })
    pricehist.write_by_day("NQ", df)
    pricehist.write_by_day("NQ", df)
    assert len(store.load_prices("NQ", "2026-07-23")) == 2


def test_journee_isolee_ecartee(tmp_path, monkeypatch):
    """dxFeed renvoie une bougie isolée à la borne fromTime, séparée de
    plusieurs semaines du reste : elle fabriquerait une séance fantôme."""
    from gex import store
    from gex.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    df = pd.DataFrame({
        "timestamp": [pd.Timestamp("2026-06-12 20:00"),      # marqueur de borne
                      pd.Timestamp("2026-07-23 10:00"),
                      pd.Timestamp("2026-07-23 10:01")],
        "open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0],
        "low": [1.0, 2.0, 3.0], "close": [1.0, 2.0, 3.0],
        "volume": [1.0, 1.0, 1.0], "source": ["dxfeed"] * 3,
    })
    days = pricehist.write_by_day("NQ", df)
    assert days == ["2026-07-23"]
    assert store.load_prices("NQ", "2026-06-12").empty
