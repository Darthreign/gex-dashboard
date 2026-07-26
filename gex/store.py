"""Persistance Parquet à deux niveaux :

- snapshots/ : chaîne complète enrichie, un fichier par pull "lent" (10 min)
- flows/     : agrégats de flux delta par minute, un fichier par jour (réécrit)
- history/   : métriques de synthèse par run (GEX net, zero gamma, P/C...)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import SETTINGS

log = logging.getLogger(__name__)


def _ensure(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _write_atomic(df: pd.DataFrame, path: Path) -> None:
    """Écrit via un fichier temporaire puis remplace.

    Indispensable : le dashboard lit ces fichiers pendant que le scheduler les
    réécrit. Sans atomicité, une lecture peut tomber sur un fichier
    partiellement écrit — pyarrow lève alors « Invalid column metadata
    (corrupt file?) » alors que les données sont saines. os.replace est
    atomique sur un même système de fichiers.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def save_snapshot(symbol: str, df: pd.DataFrame, ts: datetime) -> Path:
    path = _ensure(
        SETTINGS.data_dir / "snapshots" / symbol / ts.strftime("%Y-%m-%d") / f"{ts:%H%M%S}.parquet"
    )
    _write_atomic(df, path)
    return path


def append_daily(kind: str, symbol: str, row: dict, ts: datetime) -> Path:
    """Ajoute une ligne à un fichier journalier (flows) — petit, réécrit à chaque fois."""
    path = _ensure(SETTINGS.data_dir / kind / symbol / f"{ts:%Y-%m-%d}.parquet")
    new = pd.DataFrame([row])
    if path.exists():
        new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
    _write_atomic(new, path)
    return path


def append_history(row: dict) -> Path:
    path = _ensure(SETTINGS.data_dir / "history" / "metrics.parquet")
    new = pd.DataFrame([row])
    if path.exists():
        new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
    _write_atomic(new, path)
    return path


def append_prices(symbol: str, rows: list[dict], ts: datetime) -> Path:
    """Ajoute des bougies 1 min au fichier du jour.

    ⚠️ Provenance marquée à l'ÉCRITURE (`source="dxfeed"`), pas devinée après
    coup : ces cotations viennent du courtier et ne sont pas redistribuables.
    Le filtre d'export ne laisse passer que `source == "cboe"`, donc les
    oublier ici les rendrait partageables par défaut.
    """
    path = _ensure(SETTINGS.data_dir / "prices" / symbol / f"{ts:%Y-%m-%d}.parquet")
    new = pd.DataFrame(rows)
    if path.exists():
        new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
    new = new.drop_duplicates(subset="timestamp", keep="last").sort_values("timestamp")
    _write_atomic(new, path)
    return path


def price_days(symbol: str) -> list[str]:
    """Jours (YYYY-MM-DD) pour lesquels des bougies existent."""
    root = SETTINGS.data_dir / "prices" / symbol
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.parquet"))


def load_prices(symbol: str, day: str) -> pd.DataFrame:
    path = SETTINGS.data_dir / "prices" / symbol / f"{day}.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def load_flows(symbol: str, day: str) -> pd.DataFrame:
    path = SETTINGS.data_dir / "flows" / symbol / f"{day}.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def snapshot_days(symbol: str) -> list[str]:
    """Jours (YYYY-MM-DD) pour lesquels au moins un snapshot existe."""
    root = SETTINGS.data_dir / "snapshots" / symbol
    if not root.exists():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and any(d.glob("*.parquet")))


def load_last_snapshot(symbol: str, day: str) -> pd.DataFrame | None:
    """Dernier snapshot de chaîne enregistré pour un jour donné."""
    root = SETTINGS.data_dir / "snapshots" / symbol / day
    files = sorted(root.glob("*.parquet")) if root.exists() else []
    return pd.read_parquet(files[-1]) if files else None


def load_first_snapshot(symbol: str, day: str) -> pd.DataFrame | None:
    """Premier snapshot de la séance — celui sur lequel un plan se construit.

    L'open interest est publié le matin : les niveaux du début de séance sont
    ceux qu'un trader avait réellement sous les yeux, et donc les seuls qu'il
    soit honnête de tester a posteriori.
    """
    root = SETTINGS.data_dir / "snapshots" / symbol / day
    files = sorted(root.glob("*.parquet")) if root.exists() else []
    return pd.read_parquet(files[0]) if files else None


def load_day_snapshots(symbol: str, day: str,
                       columns: list[str] | None = None) -> list[tuple[datetime, pd.DataFrame]]:
    """Tous les snapshots d'une séance, horodatés depuis le nom de fichier.

    `columns` restreint les colonnes lues : une chaîne SPX pèse ~30 000 lignes
    et une séance en compte une quarantaine, donc lire les 17 colonnes quand
    trois suffisent multiplie le temps de chargement par cinq.
    """
    root = SETTINGS.data_dir / "snapshots" / symbol / day
    if not root.exists():
        return []
    out = []
    for f in sorted(root.glob("*.parquet")):
        try:
            ts = datetime.strptime(f"{day} {f.stem}", "%Y-%m-%d %H%M%S")
        except ValueError:
            continue          # fichier au nom inattendu : ignoré, pas fatal
        cols = columns
        if cols is not None:
            # Les snapshots antérieurs à l'ajout d'une colonne ne la portent
            # pas ; réclamer une colonne absente fait échouer la lecture, donc
            # on n'en demande que l'intersection avec ce que le fichier a.
            import pyarrow.parquet as pq
            have = set(pq.ParquetFile(f).schema_arrow.names)
            cols = [c for c in columns if c in have]
        out.append((ts, pd.read_parquet(f, columns=cols)))
    return out


def load_previous_snapshot(symbol: str, before_day: str) -> tuple[str, pd.DataFrame] | None:
    """Dernier snapshot de la séance précédant `before_day` (jour + données)."""
    days = [d for d in snapshot_days(symbol) if d < before_day]
    if not days:
        return None
    prev = days[-1]
    df = load_last_snapshot(symbol, prev)
    return (prev, df) if df is not None else None


def load_history(symbol: str | None = None) -> pd.DataFrame:
    path = SETTINGS.data_dir / "history" / "metrics.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    return df[df["symbol"] == symbol] if symbol else df
