"""Liaison process capture -> dashboard (gex/capturebus.py)."""
from __future__ import annotations

import socket
import time

import pytest

from gex import capturebus
from gex.capturebus import RemoteTape
from gex.flowtape import FlowTape


def _tape() -> FlowTape:
    t = FlowTape()
    t._by_stream = {".SPXW260729C7400": "SPX", ".SPXW260729P7400": "SPX"}
    t._spot["SPX"] = 7400.0
    t._delta[".SPXW260729C7400"] = 0.5
    t._delta[".SPXW260729P7400"] = -0.5
    return t


def _p(sym, side, size=5):
    return {"eventSymbol": sym, "aggressorSide": side, "size": size, "price": 10.0,
            "spreadLeg": False}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _attendre(cond, timeout=8.0):
    fin = time.time() + timeout
    while time.time() < fin:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_instantane_puis_seulement_le_nouveau():
    t = _tape()
    now = time.time()
    t.ingest_print(_p(".SPXW260729C7400", "BUY"), now=now - 2)
    t.ingest_print(_p(".SPXW260729P7400", "SELL"), now=now - 1)
    snap, marks = t.export_delta(None)
    assert snap["snapshot"] is True and len(snap["prints"]["SPX"]) == 2
    assert len(snap["pts"]["SPX"]) == 2
    # rien de nouveau : rien n'est rejoué
    d0, marks = t.export_delta(marks)
    assert d0["snapshot"] is False and d0["prints"] == {} and d0["pts"] == {}
    t.ingest_print(_p(".SPXW260729C7400", "SELL"), now=now)
    d1, marks = t.export_delta(marks)
    assert len(d1["prints"]["SPX"]) == 1 and len(d1["pts"]["SPX"]) == 1
    assert d1["rows"]["SPX"][0]["timestamp"]            # ISO, sérialisable en JSON


def test_miroir_reproduit_prints_points_et_barres():
    t = _tape()
    now = time.time()
    t.ingest_print(_p(".SPXW260729C7400", "BUY"), now=now - 3)
    t.ingest_print(_p(".SPXW260729P7400", "BUY", 8), now=now - 1)
    snap, _ = t.export_delta(None)
    r = RemoteTape("ws://x")
    r.apply(snap)
    assert [p["type"] for p in r.recent_prints("SPX")] == ["P", "C"]     # récent d'abord
    assert len(r.recent_prints("SPX", min_size=6)) == 1
    pts = r.live_points("SPX", 300)
    assert [p[2] for p in pts] == [0, 2]                                  # call acheté, put acheté
    rows = r.live_rows("SPX")
    assert rows and hasattr(rows[0]["timestamp"], "year")                 # redevenu datetime
    assert r._online()


def test_miroir_hors_ligne_si_plus_de_message():
    r = RemoteTape("ws://x")
    assert r.status() == ("off", 0)
    r.apply({"snapshot": True, "status": ["connected", 42]})
    assert r.status() == ("connected", 42)
    r._last_msg -= capturebus.STALE_AFTER_S + 1
    assert r.status() == ("off", 0)


def test_snapshot_remplace_le_miroir_et_le_delta_ajoute():
    r = RemoteTape("ws://x")
    now = time.time()
    rec = lambda s: {"t": now, "symbol": "SPX", "strike": 1, "type": "C", "price": 1.0,
                     "size": s, "side": "BUY", "notional": 1.0, "combo": False}
    r.apply({"snapshot": True, "prints": {"SPX": [rec(1), rec(2)]}})
    r.apply({"snapshot": False, "prints": {"SPX": [rec(3)]}})
    assert [p["size"] for p in r.recent_prints("SPX")] == [3, 2, 1]
    r.apply({"snapshot": True, "prints": {"SPX": [rec(9)]}})              # reconnexion
    assert [p["size"] for p in r.recent_prints("SPX")] == [9]


def test_liaison_reelle_serveur_et_miroir():
    """Serveur et miroir sur un vrai port local : un print ingéré côté capture
    apparaît côté dashboard ; un second miroir reçoit l'instantané."""
    t = _tape()
    port = _free_port()
    capturebus.serve_in_thread(t, "127.0.0.1", port)
    t.ingest_print(_p(".SPXW260729C7400", "BUY"), now=time.time())
    r = RemoteTape(f"ws://127.0.0.1:{port}")
    r.start()
    assert _attendre(lambda: len(r.recent_prints("SPX")) == 1), "instantané non reçu"
    t.ingest_print(_p(".SPXW260729P7400", "SELL", 7), now=time.time())
    assert _attendre(lambda: len(r.recent_prints("SPX")) == 2), "delta non reçu"
    assert len(r.live_points("SPX")) == 2
    assert r._online()                                  # des messages arrivent
    r2 = RemoteTape(f"ws://127.0.0.1:{port}")
    r2.start()
    assert _attendre(lambda: len(r2.recent_prints("SPX")) == 2), "instantané du 2e abonné"


def test_miroir_survit_a_une_capture_absente_puis_se_connecte():
    """Capture arrêtée au démarrage du dashboard : pas de plantage, hors ligne ;
    dès que la capture revient, le miroir se reconnecte et se remplit."""
    port = _free_port()
    r = RemoteTape(f"ws://127.0.0.1:{port}")
    r.start()
    time.sleep(0.5)
    assert r.status() == ("off", 0) and r.recent_prints("SPX") == []
    t = _tape()
    t.ingest_print(_p(".SPXW260729C7400", "BUY"), now=time.time())
    capturebus.serve_in_thread(t, "127.0.0.1", port)
    assert _attendre(lambda: len(r.recent_prints("SPX")) == 1, timeout=12.0), \
        "pas de reconnexion"


def test_remote_url_absente_par_defaut(monkeypatch):
    monkeypatch.delenv("GEX_CAPTURE_URL", raising=False)
    monkeypatch.setattr(capturebus, "_env", lambda n: None)
    assert capturebus.remote_url() is None


def test_attendre_spot_rend_la_main_des_que_tous_les_prix_sont_la():
    from gex import capture

    class Q:
        def __init__(self):
            self.n = 0

        def price(self, s):
            self.n += 1
            return 1.0 if self.n > 6 else None      # les prix arrivent au 2e passage

    t = [0.0]
    assert capture.attendre_spot(Q(), ("SPX", "NDX"), timeout=10, pas=1,
                                 horloge=lambda: t[0],
                                 dormir=lambda d: t.__setitem__(0, t[0] + d))
    assert t[0] < 10                                # rendu avant l'échéance


def test_attendre_spot_abandonne_apres_le_delai_sans_bloquer():
    from gex import capture
    class Q:
        def price(self, s):
            return None
    t = [0.0]
    assert capture.attendre_spot(Q(), ("SPX",), timeout=5, pas=1,
                                 horloge=lambda: t[0],
                                 dormir=lambda d: t.__setitem__(0, t[0] + d)) is False
    assert t[0] >= 5
