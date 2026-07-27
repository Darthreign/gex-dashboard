"""NQ et ES comme cibles natives à part entière.

Deux garanties à verrouiller : `pull_all` (boucle CBOE à 60 s) ne doit jamais
tenter de les pull — une collecte native prend ~90 s — et la synthèse
construite depuis une chaîne native doit être exploitable par le reste du
dashboard sans transformation supplémentaire.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from gex import scheduler, store
from gex.metrics import ET


def _native_chain(spot: float = 28700.0) -> pd.DataFrame:
    """Colonnes de sortie de futopt.enrich_native, jeu asymétrique calls/puts
    (sinon le net s'annule exactement à chaque strike — cas dégénéré déjà
    rencontré sur les fixtures de metrics.py)."""
    rows = []
    for k, oi_c, oi_p in [(28500.0, 400, 1500), (28650.0, 600, 1200),
                          (28750.0, 1500, 500), (28900.0, 2000, 300)]:
        for typ, oi in (("C", oi_c), ("P", oi_p)):
            rows.append({
                "strike": k, "type": typ, "expiry": date(2026, 7, 27),
                "iv": 0.15, "t_years": 0.02, "gamma_bs": 2e-4,
                "delta_bs": 0.4 if typ == "C" else -0.6,
                "open_interest": float(oi), "volume": 50.0,
                "bid": 10.0, "ask": 11.0, "spot": spot,
                "gex": (1.0 if typ == "C" else -1.0) * oi * 1e6,
                "dex": 1.0,
            })
    return pd.DataFrame(rows)


def test_pull_all_ignore_les_cibles_futopt(monkeypatch):
    """Le vrai risque : si pull_all appelait pull_symbol("NQ", ...), une
    collecte de ~90 s bloquerait la boucle CBOE à 60 s pour tout le monde."""
    called = []
    monkeypatch.setattr(scheduler, "market_is_open", lambda *a, **k: True)
    monkeypatch.setattr(scheduler, "pull_symbol",
                        lambda key, **kw: called.append(key))
    scheduler.pull_all(force=True)
    assert "NQ" not in called and "ES" not in called
    assert "SPX" in called and "NDX" in called


def test_build_native_summary_colonnes_exploitables():
    """La synthèse doit se brancher directement sur ce que le dashboard
    attend : .spot, .zero_gamma, .net_gex, sans étape de conversion."""
    df = _native_chain()
    now = pd.Timestamp("2026-07-27 10:00", tz=ET).to_pydatetime()
    snap, summary = scheduler.build_native_summary("NQ", df, now_et=now)
    assert snap.symbol == "NQ" and snap.spot == pytest.approx(28700.0)
    assert snap.feed_timestamp.tzinfo is None  # naïf en ET, comme le feed CBOE
    assert summary.symbol == "NQ"
    assert summary.net_gex == pytest.approx(df["gex"].sum())
    assert summary.basis is None   # pas de further-future à convertir
    assert summary.source == "dxfeed"  # donnée courtier, exclue de l'export


def test_summary_source_dxfeed_exclue_de_lexport(tmp_path, monkeypatch):
    """Garde-fou de licence : une ligne d'historique native ne doit jamais
    devenir exportable."""
    from gex import export, store
    from gex.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    df = _native_chain()
    _, summary = scheduler.build_native_summary("NQ", df)
    store.append_history(summary.as_row())

    out = tmp_path / "export"
    export.export(out)
    hist_path = out / "history" / "metrics.parquet"
    if hist_path.exists():
        exported = pd.read_parquet(hist_path)
        assert "NQ" not in set(exported["symbol"])


def test_pull_native_options_cache_frais_evite_la_collecte_live(tmp_path, monkeypatch):
    """Un redémarrage perd STATE (mémoire) mais pas le disque : si le dernier
    snapshot persisté a moins de NATIVE_CACHE_FRESH_S, pull_native_options ne
    doit PAS relancer une collecte dxFeed (~90-280 s) — juste réamorcer STATE
    depuis ce qui est déjà sur disque."""
    from gex.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    monkeypatch.setattr(scheduler, "credentials_present", lambda: True)
    called = []
    monkeypatch.setattr(scheduler.futopt, "build_native_chain",
                        lambda code, **kw: called.append(code) or _native_chain())

    now = datetime.now(ET)
    fresh_ts = now - timedelta(seconds=60)
    store.save_snapshot("NQ", _native_chain(), fresh_ts)
    store.save_snapshot("ES", _native_chain(), fresh_ts)

    scheduler.pull_native_options()

    assert called == []  # aucune collecte live déclenchée pour l'un ou l'autre
    assert scheduler.STATE.get("NQ").summary is not None
    assert scheduler.STATE.get("ES").summary is not None


def test_pull_native_options_cache_perime_relance_la_collecte(tmp_path, monkeypatch):
    """Symétrique : passé le seuil de fraîcheur, la collecte live doit bien
    repartir — le court-circuit ne doit jamais masquer une vraie donnée
    périmée."""
    from gex.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    monkeypatch.setattr(scheduler, "credentials_present", lambda: True)
    called = []

    def _fake_build(code, **kw):
        called.append(code)
        return _native_chain()

    monkeypatch.setattr(scheduler.futopt, "build_native_chain", _fake_build)

    stale_ts = datetime.now(ET) - timedelta(seconds=scheduler.NATIVE_CACHE_FRESH_S + 60)
    store.save_snapshot("NQ", _native_chain(), stale_ts)
    store.save_snapshot("ES", _native_chain(), stale_ts)

    scheduler.pull_native_options()

    assert called == ["NQ", "ES"]
