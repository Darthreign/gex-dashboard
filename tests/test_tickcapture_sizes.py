"""Capture des ticks : tailles affichées au meilleur bid / ask attachées à chaque print."""
from __future__ import annotations

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
