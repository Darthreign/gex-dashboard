"""Canal `quote:<PRODUIT>` : bid / ask / dernier prix en direct pour Squirrel X."""
from __future__ import annotations

import json

from gex import capturebus, livequote, ticksapi
from gex.tickcapture import TickCapture

UNIVERSE = {"/NQZ26:XCME": ("NQ", "NQZ6"), "/NQH27:XCME": ("NQ", "NQH7"), "/MNQZ26:XCME": ("MNQ", "MNQZ6")}


def _capture():
    c = TickCapture()
    c._pairs = {"NQ": [("/NQZ26:XCME", "NQZ6"), ("/NQH27:XCME", "NQH7")], "MNQ": [("/MNQZ26:XCME", "MNQZ6")]}
    c._order = {"NQ": ["NQZ6", "NQH7"], "MNQ": ["MNQZ6"]}
    return c


def _trade(sym, price, size=1, side="BUY", t=1_794_600_000_000):
    return {"eventSymbol": sym, "price": price, "size": size, "aggressorSide": side, "time": t}


def test_etat_du_contrat_actif_seulement():
    c = _capture()
    assert c.live_quote("NQ") is None
    c.quote({"eventSymbol": "/NQZ26:XCME", "bidPrice": 28000.0, "bidSize": 3, "askPrice": 28000.25, "askSize": 5})
    c.quote({"eventSymbol": "/NQH27:XCME", "bidPrice": 28300.0, "bidSize": 1, "askPrice": 28301.0, "askSize": 1})
    c.record(UNIVERSE, _trade("/NQZ26:XCME", 28000.25, 2), 1.0)
    c.record(UNIVERSE, _trade("/NQH27:XCME", 28300.5), 2.0)            # contrat suivant : ignoré
    c.record(UNIVERSE, _trade("/NQZ26:XCME", 28900.0, side="UNDEFINED"), 3.0)   # côté indéterminé : ignoré
    assert c.live_quote("NQ") == {"contract": "NQZ6", "bid": 28000.0, "ask": 28000.25, "bid_size": 3.0,
                                  "ask_size": 5.0, "last": 28000.25, "last_size": 2, "last_t": 1_794_600_000.0}


def test_produit_sans_scalp_a_aussi_son_dernier_prix():
    c = _capture()
    c.record(UNIVERSE, _trade("/MNQZ26:XCME", 28001.0, 4, "SELL"), 1.0)
    assert c.live_quote("MNQ") == {"contract": "MNQZ6", "last": 28001.0, "last_size": 4, "last_t": 1_794_600_000.0}
    assert c.live_quote("ES") is None


def test_miroir_remplace_les_cotations():
    tape = capturebus.RemoteTape("ws://x")
    tape.apply({"quotes": {"NQ": {"contract": "NQZ6", "bid": 1.0}}})
    assert tape.live_quote("NQ") == {"contract": "NQZ6", "bid": 1.0}
    tape.apply({"prints": {}})                                          # message sans cotations : inchangé
    assert tape.live_quote("NQ") == {"contract": "NQZ6", "bid": 1.0}
    tape.apply({"quotes": {}})
    assert tape.live_quote("NQ") is None


def test_producteur_emet_sur_changement_et_sans_nan():
    state = {"q": None}
    produce = livequote.quote_producer("NQ", read=lambda s: state["q"])
    assert produce() is None
    state["q"] = {"contract": "NQZ6", "bid": 28000.0, "ask": float("nan"), "bid_size": 3.0, "last": 28000.25}
    d = json.loads(produce()[len("data: "):])
    assert d == {"symbol": "NQ", "contract": "NQZ6", "bid": 28000.0, "ask": None, "bid_size": 3.0,
                 "ask_size": None, "last": 28000.25, "last_size": None, "last_t": None}
    assert produce() is None                                            # inchangé : rien
    state["q"] = {**state["q"], "bid_size": 4.0}
    assert json.loads(produce()[len("data: "):])["bid_size"] == 4.0


def test_canaux_du_flux(monkeypatch):
    from gex import app
    monkeypatch.setattr(app, "scalp_feed_channels", lambda p: {"levels:NQ": object()})
    assert set(livequote.feed_channels("NQ,MNQ,AAPL,")) == {"quote:NQ", "quote:MNQ"}
    assert livequote.feed_channels(None) == {}
    assert set(ticksapi.stream_channels(None, None, None, "NQ")) == {"quote:NQ"}          # seul : pas de niveaux
    assert set(ticksapi.stream_channels("NQ", None, None, "NQ")) == {"levels:NQ", "quote:NQ"}
    assert set(ticksapi.stream_channels(None, None)) == {"levels:NQ"}                     # inchangé
