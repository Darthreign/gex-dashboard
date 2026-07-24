"""Persistance Parquet à deux niveaux :

- snapshots/ : chaîne complète enrichie, un fichier par pull "lent" (10 min)
- flows/     : agrégats de flux delta par minute, un fichier par jour (réécrit)
- history/   : métriques de synthèse par run (GEX net, zero gamma, P/C...)
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import SETTINGS

log = logging.getLogger(__name__)


def _ensure(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def save_snapshot(symbol: str, df: pd.DataFrame, ts: datetime) -> Path:
    path = _ensure(
        SETTINGS.data_dir / "snapshots" / symbol / ts.strftime("%Y-%m-%d") / f"{ts:%H%M%S}.parquet"
    )
    df.to_parquet(path, index=False)
    return path


def append_daily(kind: str, symbol: str, row: dict, ts: datetime) -> Path:
    """Ajoute une ligne à un fichier journalier (flows) — petit, réécrit à chaque fois."""
    path = _ensure(SETTINGS.data_dir / kind / symbol / f"{ts:%Y-%m-%d}.parquet")
    new = pd.DataFrame([row])
    if path.exists():
        new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
    new.to_parquet(path, index=False)
    return path


def append_history(row: dict) -> Path:
    path = _ensure(SETTINGS.data_dir / "history" / "metrics.parquet")
    new = pd.DataFrame([row])
    if path.exists():
        new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
    new.to_parquet(path, index=False)
    return path


def load_flows(symbol: str, day: str) -> pd.DataFrame:
    path = SETTINGS.data_dir / "flows" / symbol / f"{day}.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def load_history(symbol: str | None = None) -> pd.DataFrame:
    path = SETTINGS.data_dir / "history" / "metrics.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    return df[df["symbol"] == symbol] if symbol else df
