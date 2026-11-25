"""Second test d'excès en direct (gex/secondtest.py) : mêmes événements que
l'étude, avertissement de /scalp, relais capture -> tableau de bord."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gex import excess, secondtest


def _batch(ts, px):
    """Seconds tests trouvés par l'étude (excess.find_excesses + find_retest)."""
    out = []
    for e in excess.find_excesses(px, px, secondtest.EXCESS_PTS, secondtest.SWING_PTS):
        rt = excess.find_retest(px, px, e["i"], e["dir"], secondtest.REACTION_PTS,
                                secondtest.RETEST_PTS, secondtest.MAX_WAIT_S / 60, ts=ts)
        if rt is not None:
            out.append((ts[rt["t"]], e["dir"], rt["extreme"],
                        ts[rt["t"]] - ts[rt["extreme_i"]]))
    return sorted(out)


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_meme_resultat_que_letude(seed):
    rng = np.random.default_rng(seed)
    n = 40_000
    px = np.round((20000 + np.cumsum(rng.choice([-2.0, -0.5, 0.5, 2.0], n,
                                                p=[0.15, 0.35, 0.35, 0.15]))) * 4) / 4
    ts = np.cumsum(rng.uniform(0.2, 3.0, n))
    tr = secondtest.SecondTestTracker()
    live = []
    for t, p in zip(ts, px):
        ev = tr.feed(t, p)
        if ev:
            live.append((ev["ts"], ev["dir"], ev["extreme"], ev["duree_s"]))
    batch = _batch(ts, px)
    assert len(batch) > 0
    live = sorted(live)
    # pytest.approx refuse les structures imbriquées dans les versions récentes
    # (liste de tuples) : comparaison élément par élément.
    assert len(live) == len(batch)
    for got, want in zip(live, batch):
        assert got == pytest.approx(want)


def test_evenement_et_affichage_limite():
    tr = secondtest.SecondTestTracker()
    path = [20000, 20050] + list(np.arange(20050, 19889, -1.0)) + [19910, 19895]
    ev = None
    for k, p in enumerate(path):
        ev = tr.feed(float(k), float(p)) or ev
    assert ev and ev["sens"] == "baissier" and ev["extreme"] == 19890
    assert ev["duree_s"] == pytest.approx(2.0)                  # 19890 -> 19895
    assert tr.current(ev["ts"] + 10) is not None
    assert tr.current(ev["ts"] + secondtest.DISPLAY_S + 1) is None


def _stats(tmp_path, monkeypatch):
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    p = secondtest.stats_path()
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"duree_edges": [24, 103], "table": {
        "accélérateur": {"express": {"n": 303, "seances": 38, "cont50_pct": 75.2,
                                     "cont100_pct": 58.1, "cont_p90": 356.0,
                                     "retour10_pct": 96.9}},
        "frein": {"lente": {"n": 309, "seances": 82, "cont50_pct": 47.6,
                            "cont100_pct": 23.3, "cont_p90": 161.0,
                            "retour10_pct": 90.2}}}}), encoding="utf-8")


def test_evaluation_du_risque(tmp_path, monkeypatch):
    _stats(tmp_path, monkeypatch)
    jour = date(2026, 10, 7)
    ev = {"duree_s": 12.0, "sens": "baissier"}
    a = secondtest.assess(ev, "accélérateur", jour)
    assert a["risk"] == "eleve" and a["duree_class"] == "express"
    assert a["row"]["cont100_pct"] == 58.1
    b = secondtest.assess({"duree_s": 200.0}, "frein", jour)
    assert b["risk"] == "faible" and b["row"]["n"] == 309
    assert secondtest.assess({"duree_s": 12.0}, "frein", jour)["risk"] == "moyen"
    assert secondtest.assess({"duree_s": 60.0}, "transition", jour)["risk"] == "moyen"
    assert secondtest.assess(ev, None, jour)["risk"] == "inconnu"
    # semaine d'échéance trimestrielle (roll jeudi 10/09 -> échéance 18/09)
    assert secondtest.expiry_week(date(2026, 9, 15))
    assert not secondtest.expiry_week(date(2026, 9, 9)) and not secondtest.expiry_week(jour)
    assert secondtest.assess(ev, "accélérateur", date(2026, 9, 15))["expiry_week"]


def test_ligne_du_bandeau(tmp_path, monkeypatch):
    _stats(tmp_path, monkeypatch)
    from gex import app
    from gex.metrics import ET
    monkeypatch.setattr(app, "datetime", type("D", (), {
        "now": staticmethod(lambda tz=None: pd.Timestamp("2026-10-07 10:00", tz=ET))}))
    ev = {"ts": 0.0, "dir": -1, "extreme": 19890.0, "duree_s": 12.4, "sens": "baissier"}
    div = app.scalp_second_test_line(ev, "accelerateur", "fr")
    txt = str(div.children)
    assert "sc-risk-eleve" in div.className and "RISQUE ÉLEVÉ" in txt
    assert "58 %" in txt and "356 pts" in txt and "express (12 s)" in txt
    lente = app.scalp_second_test_line({**ev, "duree_s": 300.0}, "frein", "fr")
    assert "sc-risk-faible" in lente.className and "23 %" in str(lente.children)
    # pas de chiffres pour cette case : on le dit
    sans = app.scalp_second_test_line({**ev, "duree_s": 60.0}, "transition", "fr")
    assert "indisponibles" in str(sans.children)


def test_rapport_ecrit_la_table(tmp_path, monkeypatch):
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import excess_report
    rng = np.random.default_rng(0)
    n = 200
    test = pd.DataFrame({"day": rng.choice(["2026-05-01", "2026-05-02"], n),
                         "g_regime_tdb2": rng.choice(["frein", "accélérateur"], n),
                         "duree_reaction_s": rng.uniform(1, 300, n),
                         "continuation": rng.uniform(0, 200, n), "rempli": True,
                         "hit_10": True})
    path = excess_report.write_live_stats(test, (24.0, 103.0))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["duree_edges"] == [24.0, 103.0]
    row = data["table"]["frein"]["lente"]
    assert row["n"] >= 20 and 0 <= row["cont100_pct"] <= 100
    assert secondtest.load_stats()["table"]["frein"]["lente"] == row


def test_relais_vers_le_tableau_de_bord():
    from gex.capturebus import RemoteTape
    r = RemoteTape("ws://127.0.0.1:1")
    ev = {"ts": 1.0, "dir": 1, "extreme": 20100.0, "duree_s": 30.0, "sens": "haussier"}
    r.apply({"type": "delta", "second_test": {"NQ": ev}})
    assert r.second_test("NQ") == ev
    r.apply({"type": "delta", "second_test": {}})            # plus frais : effacé
    assert r.second_test("NQ") is None


def test_la_capture_detecte_le_second_test():
    """Bout en bout côté capture : transactions NQ -> TickCapture.second_test."""
    from gex.tickcapture import TickCapture
    univ = {"/NQZ26:XCME": ("NQ", "/NQZ6")}
    cap = TickCapture()
    t0 = 1_791_464_400_000                     # 2026-10-07 10:00 ET, en ms
    path = [20000, 20050] + list(np.arange(20050, 19889, -1.0)) + [19910, 19895]
    for k, p in enumerate(path):
        cap.record(univ, {"eventType": "TimeAndSale", "eventSymbol": "/NQZ26:XCME",
                          "price": float(p), "size": 1, "aggressorSide": "SELL",
                          "time": t0 + 1000 * k}, (t0 + 1000 * k) / 1000)
    ev = cap.second_test("NQ", now=(t0 + 1000 * len(path)) / 1000)
    assert ev and ev["sens"] == "baissier" and ev["extreme"] == 19890
    assert cap.second_test("ES") is None
