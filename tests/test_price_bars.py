"""Bougies 1 min construites depuis le flux temps réel.

Agréger à la réception de chaque tick donne des extrêmes exacts, contrairement
à un échantillonnage périodique qui raterait les mèches — or ce sont
précisément les mèches qui disent si un niveau a été touché.
"""
from __future__ import annotations

import numpy as np

from gex.rtquote import Bar, RealtimeQuotes


def _feed(q: RealtimeQuotes, prices: list[float], minute: int) -> None:
    """Injecte des prix dans une minute donnée.

    On appelle l'agrégation directement : `_ingest` lit l'horloge système,
    ce qui rendrait le test dépendant de l'instant où il tourne.
    """
    for px in prices:
        q._accumulate("ES", px, minute)


def test_ingest_alimente_bien_la_bougie():
    """Le routage depuis le flux jusqu'à l'agrégation doit être branché."""
    q = RealtimeQuotes()
    q._by_stream = {"ES": "ES"}
    q._ingest([{"eventType": "Trade", "eventSymbol": "ES", "price": 100.0}])
    assert "ES" in q._bar and q._bar["ES"].close == 100.0


def test_extremes_exacts_dans_la_minute():
    q = RealtimeQuotes()
    q._by_stream = {"ES": "ES"}
    _feed(q, [100.0, 103.0, 98.0, 101.0], minute=60)
    bar = q._bar["ES"]
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 103.0, 98.0, 101.0)
    assert bar.ticks == 4


def test_changement_de_minute_cloture_la_bougie():
    q = RealtimeQuotes()
    q._by_stream = {"ES": "ES"}
    _feed(q, [100.0, 102.0], minute=60)
    # tick de la minute suivante
    q._accumulate("ES", 105.0, minute=120)
    done = q.drain_bars(now=125)
    assert len(done) == 1
    sym, bar = done[0]
    assert sym == "ES" and bar.minute == 60 and bar.high == 102.0
    # la bougie en cours n'est pas encore livrée
    assert q._bar["ES"].minute == 120
    assert q.drain_bars(now=125) == []


def test_minute_ecoulee_cloture_sans_nouveau_tick():
    """Sans cette clôture par le temps, un symbole qui cesse de coter garderait
    sa dernière bougie indéfiniment — rien ne serait écrit un jour férié, ni à
    la dernière minute d'une séance."""
    q = RealtimeQuotes()
    q._by_stream = {"ES": "ES"}
    _feed(q, [100.0, 101.0], minute=60)
    # aucun tick depuis, mais la minute est passée
    done = q.drain_bars(now=180)
    assert len(done) == 1 and done[0][1].minute == 60
    assert "ES" not in q._bar


def test_minute_en_cours_conservee():
    q = RealtimeQuotes()
    q._by_stream = {"ES": "ES"}
    _feed(q, [100.0], minute=120)
    assert q.drain_bars(now=150) == []   # on est encore dans la minute 120
    assert q._bar["ES"].minute == 120


def test_flush_livre_la_bougie_en_cours():
    """En fin de séance ou à l'arrêt, la dernière minute ne doit pas être
    perdue."""
    q = RealtimeQuotes()
    q._by_stream = {"ES": "ES"}
    _feed(q, [100.0], minute=60)
    assert q.drain_bars(now=90) == []      # minute encore en cours
    done = q.drain_bars(flush=True, now=90)
    assert len(done) == 1 and done[0][1].open == 100.0


def test_prix_absent_ignore():
    """Un indice sans carnet ni dernier échange ne doit pas créer de bougie."""
    q = RealtimeQuotes()
    q._by_stream = {"NDX": "NDX"}
    q._ingest([{"eventType": "Quote", "eventSymbol": "NDX",
                "bidPrice": float("nan"), "askPrice": float("nan")}])
    assert "NDX" not in q._bar


def test_bar_update():
    b = Bar(60, 10.0, 10.0, 10.0, 10.0)
    b.update(12.0)
    b.update(9.0)
    assert (b.high, b.low, b.close, b.ticks) == (12.0, 9.0, 9.0, 3)


def test_depliage_ohlc_expose_les_meches():
    """Le backtest doit voir le plus haut et le plus bas, pas seulement les
    clôtures : c'est toute la raison d'enregistrer des bougies."""
    from gex import backtest as bt
    # une minute dont la mèche haute touche 105 sans y clôturer
    path = np.array([100.0, 105.0, 99.0, 100.5])
    o = bt.evaluate_level("Call Wall", 105.0, path, open_px=100.0)
    assert o.tested
