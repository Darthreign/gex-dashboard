"""Bougies 1 min construites depuis les VRAIES transactions (TimeAndSale) —
cf. TickCapture.record/drain_price_bars, PriceBar. Remplace, pour NQ/ES, les
bougies de rtquote.QUOTES (conflaté, cf. gex/scheduler.py::flush_prices) :
agréger à la réception de chaque tick donne des extrêmes exacts, contrairement
à un échantillonnage périodique qui raterait les mèches (constaté le
2026-09-30, écart de 10 pts sur un plus bas face au relevé Tradovate réel)."""
from __future__ import annotations

from gex.tickcapture import TickCapture

UNIV = {"/NQZ26:XCME": ("NQ", "/NQZ6")}
SYM = "/NQZ26:XCME"


def _sale(price: float, t_ms: int, side="BUY", size=1):
    return {"eventType": "TimeAndSale", "eventSymbol": SYM, "price": price, "size": size,
            "aggressorSide": side, "bidPrice": price, "askPrice": price + 0.25, "time": t_ms}


def test_record_alimente_la_bougie_du_prix():
    cap = TickCapture()
    cap.record(UNIV, _sale(100.0, 60_000), 60.0)
    cap.record(UNIV, _sale(103.0, 60_500), 60.5)
    cap.record(UNIV, _sale(98.0, 61_000), 61.0)
    cap.record(UNIV, _sale(101.0, 61_500), 61.5)
    bar = cap._price_bar["NQ"]
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 103.0, 98.0, 101.0)
    assert bar.ticks == 4


def test_changement_de_minute_cloture_la_bougie():
    cap = TickCapture()
    cap.record(UNIV, _sale(100.0, 60_000), 60.0)
    cap.record(UNIV, _sale(102.0, 60_500), 60.5)
    cap.record(UNIV, _sale(105.0, 120_000), 120.0)     # minute suivante
    done = cap.drain_price_bars(now=125.0)
    assert len(done) == 1
    sym, bar = done[0]
    assert sym == "NQ" and bar.minute == 60 and bar.high == 102.0
    # la bougie en cours n'est pas encore livrée
    assert cap._price_bar["NQ"].minute == 120
    assert cap.drain_price_bars(now=125.0) == []


def test_minute_ecoulee_cloture_sans_nouveau_tick():
    """NQ/ES ne s'arrêtent jamais de coter en séance, mais la même garantie que
    rtquote.RealtimeQuotes.drain_bars évite qu'un arrêt de process laisse la
    dernière bougie coincée en mémoire sans jamais être écrite."""
    cap = TickCapture()
    cap.record(UNIV, _sale(100.0, 60_000), 60.0)
    cap.record(UNIV, _sale(101.0, 60_500), 60.5)
    done = cap.drain_price_bars(now=180.0)
    assert len(done) == 1 and done[0][1].minute == 60
    assert "NQ" not in cap._price_bar


def test_minute_en_cours_conservee():
    cap = TickCapture()
    cap.record(UNIV, _sale(100.0, 120_000), 120.0)
    assert cap.drain_price_bars(now=150.0) == []     # on est encore dans la minute 120
    assert cap._price_bar["NQ"].minute == 120


def test_flush_livre_la_bougie_en_cours():
    cap = TickCapture()
    cap.record(UNIV, _sale(100.0, 60_000), 60.0)
    assert cap.drain_price_bars(now=90.0) == []      # minute encore en cours
    done = cap.drain_price_bars(flush=True, now=90.0)
    assert len(done) == 1 and done[0][1].open == 100.0


def test_contrat_suivant_exclu_des_agregats_mais_bufferise():
    """Constaté le 2026-09-30 : un print du contrat SUIVANT (largement moins
    liquide, prix pas comparable) a fait bondir le high d'une bougie de ~370
    pts en une seule ligne — l'agrégat par sous-jacent ne doit voir que le
    contrat ACTIF déclaré par le courtier. Le tampon brut le garde quand même
    (nécessaire à la comparaison de volume qui décide du roll, cf. gex.roll)."""
    cap = TickCapture()
    univ = {"/NQZ26:XCME": ("NQ", "/NQZ6"), "/NQH27:XCME": ("NQ", "/NQH7")}
    cap._order["NQ"] = ["/NQZ6", "/NQH7"]          # actif déclaré : /NQZ6
    cap.record(univ, {**_sale(30800.0, 60_000), "eventSymbol": "/NQZ26:XCME"}, 60.0)
    # print aberrant du contrat suivant, hors de toute fourchette réelle
    cap.record(univ, {**_sale(31200.0, 60_500), "eventSymbol": "/NQH27:XCME"}, 60.5)
    cap.record(univ, {**_sale(30805.0, 61_000), "eventSymbol": "/NQZ26:XCME"}, 61.0)
    bar = cap._price_bar["NQ"]
    assert bar.high == 30805.0 and bar.low == 30800.0          # pas 31200
    assert cap.last_price("NQ") == 30805.0
    buf = cap.drain()
    assert buf["NQ"]["/NQH7"][0]["price"] == 31200.0           # bufferisé quand même


def test_cote_indetermine_ignore_pour_prix_et_bougie_mais_bufferise():
    """Constaté le 2026-10-01 : un print side="UNDEFINED" à +150 pts du marché
    (10:06 ET) a fait exploser une bougie (range 166 pts contre ~13 pts de
    moyenne). Aucune plateforme grand public n'affiche ce genre de tick —
    purement ignoré pour le spot affiché et la bougie, mais toujours bufferisé
    (brut jamais altéré) et déjà filtré en aval pour l'absorption/volume
    profile (build_sweeps/update_profile excluent un côté indéterminé)."""
    cap = TickCapture()
    cap.record(UNIV, _sale(30900.0, 60_000, side="BUY"), 60.0)
    cap.record(UNIV, _sale(31035.0, 60_500, side="UNDEFINED"), 60.5)
    cap.record(UNIV, _sale(30905.0, 61_000, side="BUY"), 61.0)
    assert cap.last_price("NQ") == 30905.0              # jamais passé par 31035
    bar = cap._price_bar["NQ"]
    assert bar.high == 30905.0 and bar.low == 30900.0   # pas 31035
    buf = cap.drain()
    assert 31035.0 in [r["price"] for r in buf["NQ"]["/NQZ6"]]   # bufferisé quand même


def test_ordre_pas_encore_connu_naexclut_rien():
    """Avant la résolution de l'univers (self._order vide), on n'a aucun moyen
    de savoir lequel est actif — mieux vaut tout garder que tout perdre."""
    cap = TickCapture()
    univ = {"/NQZ26:XCME": ("NQ", "/NQZ6")}
    cap.record(univ, _sale(30800.0, 60_000), 60.0)
    assert cap._price_bar["NQ"].open == 30800.0


def test_deux_symboles_independants():
    cap = TickCapture()
    univ = {"/NQZ26:XCME": ("NQ", "/NQZ6"), "/ESZ26:XCME": ("ES", "/ESZ6")}
    cap.record(univ, {**_sale(100.0, 60_000), "eventSymbol": "/NQZ26:XCME"}, 60.0)
    cap.record(univ, {**_sale(5000.0, 60_000), "eventSymbol": "/ESZ26:XCME"}, 60.0)
    assert cap._price_bar["NQ"].open == 100.0
    assert cap._price_bar["ES"].open == 5000.0
