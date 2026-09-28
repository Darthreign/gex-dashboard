"""Capture des ticks : tailles affichées au meilleur bid / ask attachées à chaque print."""
from __future__ import annotations

import time

import pandas as pd
import pytest

from gex import store
from gex.config import SETTINGS
from gex.tickcapture import TickCapture

UNIV = {"/NQZ26:XCME": ("NQ", "/NQZ6")}
SYM = "/NQZ26:XCME"


def _quote(bid=30910.0, bs=5.0, ask=30910.25, asz=7.0):
    return {"eventType": "Quote", "eventSymbol": SYM, "bidPrice": bid, "bidSize": bs,
            "askPrice": ask, "askSize": asz}


def _sale(price=30910.0, size=3, side="SELL", t=1_700_000_000_000):
    return {"eventType": "TimeAndSale", "eventSymbol": SYM, "price": price, "size": size,
            "aggressorSide": side, "bidPrice": 30910.0, "askPrice": 30910.25, "time": t}


def _rows(cap):
    return cap.drain()["NQ"]["/NQZ6"]


def test_print_sans_cotation_recue_taille_absente_pas_zero():
    cap = TickCapture()
    cap.record(UNIV, _sale(), 1.0)
    r = _rows(cap)[0]
    assert r["bid_size"] is None and r["ask_size"] is None
    assert r["prev_bid_size"] is None and r["prev_ask_size"] is None


def test_print_recoit_les_tailles_courantes_et_precedentes():
    cap = TickCapture()
    cap.quote(_quote(bs=40.0, asz=12.0))            # état avant
    cap.quote(_quote(bs=4.0, asz=12.0))             # le bid vient d'être consommé
    cap.record(UNIV, _sale(size=36), 1.0)
    r = _rows(cap)[0]
    assert (r["bid_size"], r["ask_size"]) == (4.0, 12.0)
    assert (r["prev_bid_size"], r["prev_ask_size"]) == (40.0, 12.0)   # 40 affichés, 36 exécutés


def test_cotation_identique_ne_decale_pas_letat_precedent():
    cap = TickCapture()
    cap.quote(_quote(bs=40.0))
    cap.quote(_quote(bs=4.0))
    cap.quote(_quote(bs=4.0))                       # doublon : rien ne change
    cap.record(UNIV, _sale(), 1.0)
    assert _rows(cap)[0]["prev_bid_size"] == 40.0


def test_taille_invalide_devient_none():
    cap = TickCapture()
    cap.quote({**_quote(), "bidSize": float("nan"), "askSize": None})
    cap.record(UNIV, _sale(), 1.0)
    r = _rows(cap)[0]
    assert r["bid_size"] is None and r["ask_size"] is None


def test_cotation_dun_autre_contrat_ignoree():
    cap = TickCapture()
    cap.quote({**_quote(), "eventSymbol": "/ESZ26:XCME"})
    cap.record(UNIV, _sale(), 1.0)
    assert _rows(cap)[0]["bid_size"] is None


def test_ecriture_sur_fichier_existant_sans_les_nouvelles_colonnes(tmp_path, monkeypatch):
    """Les fichiers déjà écrits n'ont pas ces colonnes : la suite du jour s'y ajoute
    sans erreur, et les anciennes lignes gardent NaN."""
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    from datetime import datetime
    ts = datetime(2026, 9, 28, 9, 0)
    ancien = [{"ts": 1.0, "price": 30000.0, "volume": 2, "bid": 1.0, "ask": 2.0,
               "side": "BUY", "ts_recv": 1.1, "source": "dxfeed"}]
    store.append_ticks("NQ", ancien, ts)
    cap = TickCapture()
    cap.quote(_quote())
    cap.record(UNIV, _sale(), 2.0)
    store.append_ticks("NQ", _rows(cap), ts)
    df = pd.read_parquet(tmp_path / "ticks" / "NQ" / "2026-09-28.parquet")
    assert len(df) == 2 and pd.isna(df["bid_size"].iloc[0]) and df["bid_size"].iloc[1] == 5.0
    assert df["volume"].dtype.kind == "i"                         # le schéma de base reste entier


def test_last_price_mis_a_jour_a_chaque_print():
    cap = TickCapture()
    assert cap.last_price("NQ") is None
    cap.record(UNIV, _sale(price=30910.0), 1.0)
    assert cap.last_price("NQ") == 30910.0
    cap.record(UNIV, _sale(price=30910.25), 2.0)
    assert cap.last_price("NQ") == 30910.25
    assert cap.last_price("ES") is None                    # jamais vu -> None, pas 0


def _sale_at(price, size, side, prev_size, cur_size, t):
    """Print agressif avec ses tailles avant/après (via une cotation posée
    juste avant), pour construire une salve d'absorption reproductible."""
    return {"eventType": "TimeAndSale", "eventSymbol": SYM, "price": price, "size": size,
            "aggressorSide": side, "time": int(t * 1000)}


def test_recent_rows_bornees_a_la_fenetre_iceberg():
    from gex.tickcapture import ICEBERG_WINDOW_S
    cap = TickCapture()
    cap.record(UNIV, _sale(t=1_700_000_000_000), 1.0)
    cap.record(UNIV, _sale(t=int((1_700_000_000 + ICEBERG_WINDOW_S + 5) * 1000)),
              1_700_000_000 + ICEBERG_WINDOW_S + 5)
    rows = cap.recent_rows("NQ")
    assert len(rows) == 1                       # le premier print est sorti de la fenêtre


def test_absorption_now_detecte_une_salve_fraiche_et_ignore_les_vieilles():
    cap = TickCapture()
    cap.quote(_quote(bid=30910.0, bs=5.0))
    cap.quote(_quote(bid=30910.0, bs=3.0))      # prev=5 (avant), courant=3 (après, >= 50% -> recharge)
    base = time.time() - 5.0                   # récente
    for i in range(4):
        cap.record(UNIV, _sale(price=30910.0, size=10, side="SELL",
                               t=int((base + i * 0.3) * 1000)), base + i * 0.3)
    a = cap.absorption_now("NQ", now=time.time())
    assert a is not None and a["side"] == "SELL" and a["price"] == 30910.0
    assert a["total"] == 40.0 and a["n_prints"] == 4


def test_absorption_now_ignore_une_salve_trop_ancienne():
    from gex.tickcapture import ABSORPTION_FRESH_S
    cap = TickCapture()
    cap.quote(_quote(bid=30910.0, bs=5.0))
    cap.quote(_quote(bid=30910.0, bs=3.0))
    vieux = time.time() - ABSORPTION_FRESH_S - 30.0
    for i in range(4):
        cap.record(UNIV, _sale(price=30910.0, size=10, side="SELL",
                               t=int((vieux + i * 0.3) * 1000)), vieux + i * 0.3)
    assert cap.absorption_now("NQ", now=time.time()) is None


def test_absorption_now_rien_sans_ticks():
    cap = TickCapture()
    assert cap.absorption_now("NQ") is None


def test_absorption_now_met_en_cache_le_resultat():
    from gex.tickcapture import ABSORPTION_RECOMPUTE_S
    cap = TickCapture()
    t0 = time.time()
    first = cap.absorption_now("NQ", now=t0)
    with cap._lock:
        cap._absorb_cache["NQ"] = (t0, {"side": "SELL", "price": 1.0, "ratio": 9.0,
                                        "total": 9.0, "n_prints": 1, "ts": t0})
    still_cached = cap.absorption_now("NQ", now=t0 + ABSORPTION_RECOMPUTE_S - 0.1)
    assert still_cached is not None and still_cached["price"] == 1.0
    recomputed = cap.absorption_now("NQ", now=t0 + ABSORPTION_RECOMPUTE_S + 0.1)
    assert recomputed is None                   # pas de ticks réels -> retombe à None
