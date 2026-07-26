"""L'export ne doit JAMAIS laisser passer une donnée payante : ces tests
vérifient le comportement d'exclusion par défaut."""
import pandas as pd
import pytest

from gex import export


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Isole SETTINGS.data_dir dans un dossier temporaire."""
    monkeypatch.setattr(export.SETTINGS, "data_dir", tmp_path)
    return tmp_path


def _write_history(root, rows):
    p = root / "history" / "metrics.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(p, index=False)


def _write_flow(root, symbol, day, rows):
    p = root / "flows" / symbol / f"{day}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(p, index=False)


def test_export_keeps_only_cboe(data_dir, tmp_path):
    _write_history(data_dir, [
        {"timestamp": "2026-07-24 19:00:00", "symbol": "SPX", "net_gex": 1.0, "source": "cboe"},
        {"timestamp": "2026-07-23 16:00:00", "symbol": "SPX", "net_gex": 2.0, "source": "databento"},
    ])
    r = export.export(tmp_path / "out")
    assert r["history_rows"] == 1
    assert r["history_excluded"] == 1
    got = pd.read_parquet(tmp_path / "out" / "history" / "metrics.parquet")
    assert set(got["source"]) == {"cboe"}
    assert got["net_gex"].tolist() == [1.0]


def test_export_skips_fully_paid_flow_files(data_dir, tmp_path):
    _write_flow(data_dir, "SPX", "2026-07-23",
                [{"timestamp": "2026-07-23 15:31:00", "flow_total": 5.0, "source": "databento"}])
    _write_flow(data_dir, "SPX", "2026-07-27",
                [{"timestamp": "2026-07-27 15:31:00", "flow_total": 7.0, "source": "cboe"}])
    r = export.export(tmp_path / "out")
    assert r["flow_files"] == 1, "le fichier 100 % Databento ne doit pas être exporté"
    assert r["days"] == ["2026-07-27"]
    assert not (tmp_path / "out" / "flows" / "SPX" / "2026-07-23.parquet").exists()
    assert (tmp_path / "out" / "flows" / "SPX" / "2026-07-27.parquet").exists()


def test_export_filters_mixed_flow_file(data_dir, tmp_path):
    _write_flow(data_dir, "NDX", "2026-07-27", [
        {"timestamp": "2026-07-27 15:31:00", "flow_total": 1.0, "source": "cboe"},
        {"timestamp": "2026-07-27 15:32:00", "flow_total": 2.0, "source": "databento"},
    ])
    r = export.export(tmp_path / "out")
    assert (r["flow_rows"], r["flow_excluded"]) == (1, 1)
    got = pd.read_parquet(tmp_path / "out" / "flows" / "NDX" / "2026-07-27.parquet")
    assert got["flow_total"].tolist() == [1.0]


def test_export_excludes_data_without_source_column(data_dir, tmp_path):
    """Exclusion par défaut : sans colonne de provenance, rien ne sort."""
    _write_history(data_dir, [
        {"timestamp": "2026-07-24 19:00:00", "symbol": "SPX", "net_gex": 1.0},
    ])
    _write_flow(data_dir, "SPX", "2026-07-24", [{"timestamp": "x", "flow_total": 1.0}])
    r = export.export(tmp_path / "out")
    assert r["history_rows"] == 0
    assert r["flow_files"] == 0
    # rien ne doit avoir été écrit
    assert not (tmp_path / "out" / "history").exists()
    assert not (tmp_path / "out" / "flows").exists()


def test_provenance_note_written(data_dir, tmp_path):
    _write_history(data_dir, [
        {"timestamp": "2026-07-24 19:00:00", "symbol": "SPX", "net_gex": 1.0, "source": "cboe"},
    ])
    export.export(tmp_path / "out")
    note = (tmp_path / "out" / export.PROVENANCE_FILE).read_text(encoding="utf-8")
    assert "CBOE" in note
    assert "Databento" in note  # doit expliciter ce qui est exclu


def test_migrate_marks_backfill_by_timestamp(data_dir):
    """16:00:00 pile = backfill Databento ; toute autre heure = live CBOE."""
    _write_history(data_dir, [
        {"timestamp": "2026-07-23 16:00:00", "symbol": "SPX", "net_gex": 1.0},
        {"timestamp": "2026-07-24 19:28:36", "symbol": "SPX", "net_gex": 2.0},
    ])
    _write_flow(data_dir, "SPX", "2026-07-23", [{"timestamp": "x", "flow_total": 1.0}])
    s = export.migrate()
    assert (s["history_cboe"], s["history_databento"]) == (1, 1)
    assert s["flow_files"] == 1
    h = pd.read_parquet(data_dir / "history" / "metrics.parquet")
    assert h.loc[h["net_gex"] == 1.0, "source"].iloc[0] == "databento"
    assert h.loc[h["net_gex"] == 2.0, "source"].iloc[0] == "cboe"
    # les flux préexistants sont marqués payants par prudence
    f = pd.read_parquet(data_dir / "flows" / "SPX" / "2026-07-23.parquet")
    assert set(f["source"]) == {"databento"}


def test_migrate_is_idempotent(data_dir):
    _write_history(data_dir, [
        {"timestamp": "2026-07-24 19:28:36", "symbol": "SPX", "net_gex": 2.0, "source": "cboe"},
    ])
    s = export.migrate()
    assert (s["history_cboe"], s["history_databento"]) == (1, 0)
    h = pd.read_parquet(data_dir / "history" / "metrics.parquet")
    assert h["source"].tolist() == ["cboe"]


def test_prix_courtier_jamais_exportes(tmp_path, monkeypatch):
    """Les bougies dxFeed viennent du courtier : non redistribuables.

    Deux garde-fous doivent tenir — le répertoire prices/ n'est pas parcouru,
    et le filtre de partage n'accepte que source == "cboe". Ce test vérifie le
    résultat final : rien de dxfeed ne sort.
    """
    from datetime import datetime

    from gex import export, store
    from gex.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    store.append_prices("SPX", [{
        "timestamp": datetime(2026, 7, 27, 10, 0), "open": 1.0, "high": 2.0,
        "low": 0.5, "close": 1.5, "ticks": 10, "source": "dxfeed",
    }], datetime(2026, 7, 27, 10, 0))

    out = tmp_path / "export"
    export.export(out)
    exported = list(out.rglob("*.parquet")) if out.exists() else []
    assert not any("prices" in p.parts for p in exported)
    for p in exported:
        df = pd.read_parquet(p)
        if "source" in df.columns:
            assert set(df["source"].unique()) <= {"cboe"}
