"""Script de détection/correction des barres de tape NQ/ES mal valorisées
(scripts/fix_tape_multiplier.py), sur des données produites par le vrai code
de capture puis dégradées comme le faisait l'ancien défaut (×5 sur NQ)."""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from gex import store
from gex.config import SETTINGS
from gex.flowtape import FlowTape
from gex.metrics import ET
from gex.rtquote import QUOTES, Tick

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_tape_multiplier as fix  # noqa: E402

NQ_C = "./QNEZ26C21000:XCME"
NQ_P = "./QNEZ26P20900:XCME"
DAY = "2026-10-08"
T0 = datetime(2026, 10, 8, 10, 0, tzinfo=ET).timestamp()       # 10h00 ET


@pytest.fixture()
def donnees(tmp_path, monkeypatch):
    from gex import futopt
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    monkeypatch.setattr(futopt, "_multiplier_cache", {})
    monkeypatch.setattr(QUOTES, "ticks", {"NQ": Tick(bid=21000, ask=21000, last=21000,
                                                     ts=time.time())})
    t = FlowTape()
    t._by_stream = {NQ_C: "NQ", NQ_P: "NQ"}
    t._spot["NQ"] = 21000.0
    t._delta.update({NQ_C: 0.4, NQ_P: -0.3})
    for m in range(4):                                  # 4 minutes de prints
        for k, (c, side) in enumerate(((NQ_C, "BUY"), (NQ_P, "SELL"), (NQ_C, "SELL"))):
            t.ingest_print({"eventSymbol": c, "aggressorSide": side, "size": 2 + k,
                            "price": 50.0, "spreadLeg": False},
                           now=T0 + 60 * m + 5 + k)
    bars = t.drain_bars(flush=True)
    rows = [b.as_row("NQ", datetime.fromtimestamp(b.minute, tz=timezone.utc)
                     .astimezone(ET).replace(tzinfo=None)) for _, b in bars]
    tape = pd.DataFrame(rows).drop(columns=["mult", "mult_source"])  # format d'avant
    # minutes 0 et 1 : valorisées à 100 au lieu de 20 (×5), comme l'ancien défaut
    for c in fix.DOLLAR_COLS:
        tape.loc[[0, 1], c] = tape.loc[[0, 1], c] * 5
    raw = t.drain_raw()
    # minute 3 : prints bruts incomplets (un print perdu)
    raw = [r for r in raw if not (r["ts_recv"] == T0 + 180 + 5 + 0)]
    store.append_tape("NQ", tape.to_dict("records"), datetime(2026, 10, 8))
    store.append_optprints("NQ", raw, datetime(2026, 10, 8, 10))
    return tape


def test_detection_sans_modification(donnees):
    t, fixed = fix.audit_day("NQ", DAY)
    assert t["statut"].tolist() == ["fausse (×5)", "fausse (×5)", "juste",
                                    "indéterminée (prints bruts incomplets)"]
    # rapport seul : le fichier de données n'a pas bougé
    sur_disque = store.load_tape("NQ", DAY)
    assert sur_disque["hedge_call_buy"].iloc[0] == pytest.approx(donnees["hedge_call_buy"].iloc[0])
    assert not (SETTINGS.data_dir / "backups").exists()


def test_correction_exacte_avec_sauvegarde(donnees):
    juste = donnees.copy()
    for c in fix.DOLLAR_COLS:
        juste.loc[[0, 1], c] = juste.loc[[0, 1], c] / 5
    fix.run("NQ", apply=True)
    corr = store.load_tape("NQ", DAY)
    for c in fix.DOLLAR_COLS:
        assert corr[c].tolist() == pytest.approx(juste[c].tolist())
    assert corr["mult_source"].tolist()[:3] == ["corrige", "corrige", "verifie"]
    assert pd.isna(corr["mult_source"].iloc[3])                     # indéterminée : intacte
    assert corr["net_contracts"].tolist() == donnees["net_contracts"].tolist()
    backup = SETTINGS.data_dir / "backups" / "tape" / "NQ" / f"{DAY}.parquet"
    assert pd.read_parquet(backup)["hedge_call_buy"].iloc[0] == pytest.approx(
        donnees["hedge_call_buy"].iloc[0])
    # relancer ne corrige pas deux fois
    fix.run("NQ", apply=True)
    assert store.load_tape("NQ", DAY)["net_delta"].tolist() == pytest.approx(
        juste["net_delta"].tolist())
