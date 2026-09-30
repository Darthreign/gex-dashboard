"""Volume profile de séance (HVL) côté TickCapture — accumulation, reset au
changement de séance CME, et confirmation attachée aux salves d'absorption
(cf. gex/iceberg.py::update_profile/hvl_levels/hvl_near)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from gex.tickcapture import TickCapture, _session_day

ET = ZoneInfo("America/New_York")
UNIV = {"/NQZ26:XCME": ("NQ", "/NQZ6")}
SYM = "/NQZ26:XCME"


def _et_epoch(y, m, d, h, mi) -> float:
    return datetime(y, m, d, h, mi, tzinfo=ET).timestamp()


def _sale(ts_ms, price=30000.0, size=10, side="SELL"):
    return {"eventType": "TimeAndSale", "eventSymbol": SYM, "price": price, "size": size,
            "aggressorSide": side, "bidPrice": price, "askPrice": price + 0.25, "time": ts_ms}


def test_session_day_bascule_a_18h_et():
    avant = _et_epoch(2026, 9, 29, 17, 59)
    apres = _et_epoch(2026, 9, 29, 18, 0)
    assert _session_day(avant) == "2026-09-29"
    assert _session_day(apres) == "2026-09-30"     # ouverture de la séance du lendemain


def test_record_accumule_le_profil_de_la_seance():
    cap = TickCapture()
    t0 = _et_epoch(2026, 9, 29, 10, 0)
    cap.record(UNIV, _sale(int(t0 * 1000), price=30000.0, size=10, side="BUY"), t0)
    cap.record(UNIV, _sale(int((t0 + 1) * 1000), price=30001.0, size=4, side="SELL"), t0 + 1)
    prof = cap.session_profile("NQ")
    lvl = prof[30000.0]                              # même palier (5 pts) pour 30000/30001
    assert lvl["vol"] == 14.0 and lvl["ask_vol"] == 10.0 and lvl["bid_vol"] == 4.0


def test_changement_de_seance_remet_le_profil_a_zero():
    cap = TickCapture()
    t0 = _et_epoch(2026, 9, 29, 10, 0)
    cap.record(UNIV, _sale(int(t0 * 1000)), t0)
    assert cap.session_profile("NQ")                # non vide avant le reset
    t1 = _et_epoch(2026, 9, 29, 18, 30)              # séance suivante
    cap.record(UNIV, _sale(int(t1 * 1000), price=31000.0), t1)
    prof = cap.session_profile("NQ")
    assert 30000.0 not in prof and 31000.0 in prof


def test_absorption_now_attache_le_hvl_le_plus_proche():
    cap = TickCapture()
    t0 = _et_epoch(2026, 9, 29, 10, 0)
    # gros volume acheteur concentré à 30000, sur plusieurs paliers de bruit
    for i, price in enumerate((29000.0, 29500.0, 30500.0, 31000.0)):
        cap.record(UNIV, _sale(int((t0 + i) * 1000), price=price, size=10, side="BUY"), t0 + i)
    for i in range(20):
        cap.record(UNIV, _sale(int((t0 + 10 + i) * 1000), price=30000.0, size=20, side="BUY"),
                   t0 + 10 + i)
    # salve d'absorption détectée juste à côté du HVL (même palier, 5 pts) —
    # deux cotations DISTINCTES pour que prev_ask_size soit renseigné (cf.
    # TickCapture.quote : un doublon exact est ignoré, cf. test_tickcapture_sizes)
    q = {"eventType": "Quote", "eventSymbol": SYM, "bidPrice": 30000.0, "bidSize": 5.0,
        "askPrice": 30000.25, "askSize": 5.0}
    cap.quote(q)
    cap.quote({**q, "bidSize": 6.0})                 # ask inchangé, juste assez pour fixer prev_*
    for i in range(5):
        cap.record(UNIV, _sale(int((t0 + 40 + i) * 1000), price=30002.0, size=10, side="BUY"),
                   t0 + 40 + i)
    a = cap.absorption_now("NQ", now=t0 + 45)
    assert a is not None
    assert a["hvl"] is not None and a["hvl"]["side"] == "BUY"
