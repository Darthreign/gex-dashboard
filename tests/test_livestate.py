"""Processus moteur -> web : état publié dans data/live/ et relu par le miroir."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from gex import app as app_mod
from gex import capturebus, livestate, metrics, scheduler
from gex.config import SETTINGS
from gex.ingest import ChainSnapshot
from gex.metrics import ET


@pytest.fixture()
def racine(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    return tmp_path


def _chaine():
    exp = (datetime.now(ET) + timedelta(days=10)).date()
    rows = [{"contract": f"X{cp}{k}", "expiry": exp, "type": cp, "strike": float(k),
             "bid": 0.0, "ask": 0.0, "iv": 0.2, "open_interest": 100.0, "volume": 5.0,
             "delta_cboe": 0.0, "gamma_cboe": 0.0, "last_trade_price": 0.0}
            for k in (95, 100, 105) for cp in ("C", "P")]
    now = datetime.now(ET).replace(microsecond=0)
    snap = ChainSnapshot("TSTL", 100.0, now.replace(tzinfo=None), now.replace(tzinfo=None),
                         pd.DataFrame(rows))
    df = metrics.enrich(snap)
    return snap, df, metrics.summarize(snap, df, with_basis=False)


def test_etat_publie_puis_recopie_dans_state(racine, monkeypatch):
    snap, df, summary = _chaine()
    monkeypatch.setattr(scheduler, "PUBLISH_STATE", True)
    scheduler.set_state("TSTL", snap, df, summary)
    scheduler.STATE.per_symbol.pop("TSTL")          # le web part d'un STATE vide

    mirror = livestate.Mirror()
    assert mirror.poll_once() == 1
    st = scheduler.STATE.get("TSTL")
    assert st.summary.net_gex == pytest.approx(summary.net_gex)
    assert st.summary.timestamp == summary.timestamp
    assert st.snapshot.spot == 100.0
    pd.testing.assert_frame_equal(st.enriched.reset_index(drop=True),
                                  df.reset_index(drop=True), check_dtype=False)
    assert mirror.poll_once() == 0                   # rien de neuf : rien rechargé


def test_anciennes_versions_nettoyees(racine):
    snap, df, summary = _chaine()
    for _ in range(6):
        livestate.publish_state("TSTL", snap, df, summary)
    assert len(list((racine / "live").glob("TSTL.*.parquet"))) == livestate.KEEP_VERSIONS


def test_indicateurs_publies_lus_sans_recalcul(racine, monkeypatch):
    app_mod._CONFLUENCE_CACHE["NQ"] = (0.0, [{"price": 1.0, "rank": 1}])
    app_mod._ORDERFLOW_PROFILE_CACHE["NQ"] = (0.0, {"legs": [], "untested": [], "bucket_size": 1.0})
    app_mod.publish_scalp_indicators()
    app_mod._CONFLUENCE_CACHE.clear()
    app_mod._ORDERFLOW_PROFILE_CACHE.clear()

    mirror = livestate.Mirror()
    mirror.on_json("scalp-", app_mod.import_scalp_indicators)
    mirror.poll_once()
    monkeypatch.setattr(app_mod, "INDICATORS_REMOTE", True)
    # en mode moteur, le web lit la valeur publiée, quel que soit son âge,
    # et ne recalcule jamais (ni lecture de chaîne, ni de ticks)
    monkeypatch.setattr(app_mod, "chain_state", lambda s: pytest.fail("recalcul côté web"))
    assert app_mod.scalp_confluence_zones("NQ") == [{"price": 1.0, "rank": 1}]
    assert app_mod.scalp_orderflow_profile("NQ", pd.DataFrame({"ts": [1.0]}))["bucket_size"] == 1.0
    assert app_mod.scalp_order_flow_zones("ES", pd.DataFrame({"ts": [1.0]})) == []


def test_process_capture_ne_se_prend_pas_pour_un_client(monkeypatch):
    monkeypatch.setenv("GEX_CAPTURE_URL", "ws://127.0.0.1:8765")
    assert capturebus.remote_url() == "ws://127.0.0.1:8765"
    monkeypatch.setattr(capturebus, "IS_CAPTURE_PROCESS", True)
    assert capturebus.remote_url() is None


def test_mode_moteur_active_par_variable(monkeypatch):
    monkeypatch.setenv("GEX_ENGINE", "1")
    assert livestate.enabled()
    monkeypatch.setenv("GEX_ENGINE", "0")
    assert not livestate.enabled()
