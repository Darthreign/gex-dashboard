"""Produits en ticks seuls : mêmes colonnes que NQ/ES sur disque, sans analyse."""
from __future__ import annotations

from gex import roll
from gex.tickcapture import TickCapture

UNIVERSE = {"/NQZ26:XCME": ("NQ", "/NQZ6"), "/GCZ26:XCEC": ("GC", "/GCZ6")}


def _item(symbol: str, ts_ms: int = 1_800_000_000_000) -> dict:
    return {"eventSymbol": symbol, "price": 100.0, "size": 2, "time": ts_ms,
            "aggressorSide": "BUY"}


def test_ticks_seuls_bufferises_sans_analyse():
    tc = TickCapture()
    tc.record(UNIVERSE, _item("/GCZ26:XCEC"), 1_800_000_000.0)
    rows = tc.drain()["GC"]["/GCZ6"]
    assert len(rows) == 1 and rows[0]["side"] == "BUY" and rows[0]["volume"] == 2
    assert not tc._recent and not tc._vp and not tc._last and not tc._price_bar


def test_produit_analyse_inchange():
    tc = TickCapture()
    tc.record(UNIVERSE, _item("/NQZ26:XCME"), 1_800_000_000.0)
    assert tc.last_price("NQ") == 100.0 and tc._recent["NQ"] and "NQ" in tc._price_bar


def test_tous_les_produits_demandes_sont_suivis():
    assert {"NQ", "ES", "MNQ", "MES", "MGC", "GC", "CL", "MCL"} == set(TickCapture().symbols)


def test_tailles_du_carnet_comme_nq_es():
    tc = TickCapture()
    tc.quote({"eventSymbol": "/GCZ26:XCEC", "bidSize": 5, "askSize": 7})
    tc.record(UNIVERSE, _item("/GCZ26:XCEC"), 1_800_000_000.0)
    row = tc.drain()["GC"]["/GCZ6"][0]
    assert row["bid_size"] == 5 and row["ask_size"] == 7


def test_erreur_du_courtier_garde_les_derniers_contrats(monkeypatch):
    tc = TickCapture(("NQ",))
    monkeypatch.setattr(roll, "resolve_pair", lambda c, a: [("/NQZ26:XCME", "/NQZ6")])
    assert tc._build_universe("x") == {"/NQZ26:XCME": ("NQ", "/NQZ6")}

    def boom(c, a):
        raise RuntimeError("429")
    monkeypatch.setattr(roll, "resolve_pair", boom)
    assert tc._build_universe("x") == {"/NQZ26:XCME": ("NQ", "/NQZ6")}
