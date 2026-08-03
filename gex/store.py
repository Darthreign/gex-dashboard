"""Persistance Parquet à deux niveaux :

- snapshots/ : chaîne complète enrichie, un fichier par pull "lent" (10 min)
- flows/     : agrégats de flux delta par minute, un fichier par jour (réécrit)
- history/   : métriques de synthèse par run (GEX net, zero gamma, P/C...)
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
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

    ⚠️ Le nom du temporaire doit être UNIQUE par écriture, pas dérivé de la
    seule destination. Avec un `.tmp` partagé, deux threads qui écrivent le
    même fichier entrelacent leurs octets dans ce temporaire, puis `os.replace`
    publie le résultat : la destination devient illisible alors que chaque
    écriture était correcte prise isolément. C'est exactement ce qui a détruit
    history/metrics.parquet le 2026-07-29 (« Page was smaller than expected »),
    ce fichier ayant trois producteurs dans trois threads APScheduler
    différents — pull_all, pull_native_options et pull_native_index.
    """
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}"
                           f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    except BaseException:
        # ne jamais laisser traîner un temporaire à moitié écrit
        tmp.unlink(missing_ok=True)
        raise


# Un verrou par fichier : les fonctions append_* font un lire-modifier-écrire,
# que deux threads peuvent entrelacer même avec des temporaires distincts —
# le second relit alors un état d'avant l'écriture du premier et l'écrase.
# Le temporaire unique évite la corruption, ce verrou évite la perte de lignes.
# (Portée intra-processus : deux instances du dashboard sur le même dossier
# resteraient exposées au dernier-qui-écrit-gagne, sans corruption pour
# autant. Ce n'est pas un mode d'emploi prévu.)
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.Lock())


def save_snapshot(symbol: str, df: pd.DataFrame, ts: datetime) -> Path:
    path = _ensure(
        SETTINGS.data_dir / "snapshots" / symbol / ts.strftime("%Y-%m-%d") / f"{ts:%H%M%S}.parquet"
    )
    _write_atomic(df, path)
    return path


def append_daily(kind: str, symbol: str, row: dict, ts: datetime) -> Path:
    """Ajoute une ligne à un fichier journalier (flows) — petit, réécrit à chaque fois."""
    path = _ensure(SETTINGS.data_dir / kind / symbol / f"{ts:%Y-%m-%d}.parquet")
    with _lock_for(path):
        new = pd.DataFrame([row])
        if path.exists():
            new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
        _write_atomic(new, path)
    return path


def append_history(row: dict) -> Path:
    """⚠️ Trois producteurs distincts écrivent ici, chacun dans son thread
    APScheduler : pull_all (CBOE), pull_native_options (NQ/ES) et
    pull_native_index (SPX/NDX). D'où le verrou — cf. _write_atomic pour la
    corruption que leur concurrence a provoquée le 2026-07-29."""
    path = _ensure(SETTINGS.data_dir / "history" / "metrics.parquet")
    with _lock_for(path):
        new = pd.DataFrame([row])
        if path.exists():
            new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
        _write_atomic(new, path)
    return path


def append_index_spot(key: str, row: dict) -> Path:
    """Historique léger d'un indice de contexte (ex. VIX) — un spot horodaté,
    pas une chaîne d'options. Alimente get_market_context (MCP), distinct de
    history/metrics.parquet qui suppose le schéma SummaryMetrics.as_row()."""
    path = _ensure(SETTINGS.data_dir / "history" / f"{key}.parquet")
    with _lock_for(path):
        new = pd.DataFrame([row])
        if path.exists():
            new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
        _write_atomic(new, path)
    return path


def load_index_spot(key: str) -> pd.DataFrame:
    path = SETTINGS.data_dir / "history" / f"{key}.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def append_prices(symbol: str, rows: list[dict], ts: datetime) -> Path:
    """Ajoute des bougies 1 min au fichier du jour.

    ⚠️ Provenance marquée à l'ÉCRITURE (`source="dxfeed"`), pas devinée après
    coup : ces cotations viennent du courtier et ne sont pas redistribuables.
    Le filtre d'export ne laisse passer que `source == "cboe"`, donc les
    oublier ici les rendrait partageables par défaut.
    """
    path = _ensure(SETTINGS.data_dir / "prices" / symbol / f"{ts:%Y-%m-%d}.parquet")
    with _lock_for(path):
        new = pd.DataFrame(rows)
        if path.exists():
            new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
        new = new.drop_duplicates(subset="timestamp", keep="last").sort_values("timestamp")
        _write_atomic(new, path)
    return path


def previous_close_spot(symbol: str, day: str | None = None) -> float | None:
    """Spot de clôture de la séance précédant `day`.

    C'est la référence à laquelle évaluer les murs de gamma : l'open interest
    qu'on lit le matin décrit les positions arrêtées à cette clôture. Évaluer
    le gamma au spot courant ferait glisser les murs avec le prix
    (cf. metrics.gex_at_spot).
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    h = load_history(symbol)
    if h.empty or "spot" not in h.columns:
        return None
    ts = pd.to_datetime(h["timestamp"])
    prev = h[ts.dt.strftime("%Y-%m-%d") < day].sort_values("timestamp")
    return float(prev["spot"].iloc[-1]) if not prev.empty else None


def price_days(symbol: str) -> list[str]:
    """Jours (YYYY-MM-DD) pour lesquels des bougies existent."""
    root = SETTINGS.data_dir / "prices" / symbol
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.parquet"))


def load_prices(symbol: str, day: str) -> pd.DataFrame:
    path = SETTINGS.data_dir / "prices" / symbol / f"{day}.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def append_tape(symbol: str, rows: list[dict], ts: datetime) -> Path:
    """Ajoute des barres d'order flow signé (1 min) au fichier du jour.

    Séparé de `flows/` à dessein : `flows/` contient le proxy non signé
    calculé sur la source publique CBOE (redistribuable), `tape/` le flux
    réellement signé issu du courtier (usage personnel). Les mélanger
    rendrait impossible de dire, en relisant un fichier, si le signe est
    observé ou déduit.
    """
    path = _ensure(SETTINGS.data_dir / "tape" / symbol / f"{ts:%Y-%m-%d}.parquet")
    with _lock_for(path):
        new = pd.DataFrame(rows)
        if path.exists():
            new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
        new = new.drop_duplicates(subset="timestamp", keep="last").sort_values("timestamp")
        _write_atomic(new, path)
    return path


def load_tape(symbol: str, day: str) -> pd.DataFrame:
    path = SETTINGS.data_dir / "tape" / symbol / f"{day}.parquet"
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def tape_days(symbol: str) -> list[str]:
    root = SETTINGS.data_dir / "tape" / symbol
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.parquet"))


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


def load_snapshot_near(symbol: str, day: str,
                       target_hhmmss: str = "160000") -> pd.DataFrame | None:
    """Snapshot de chaîne du jour le plus PROCHE d'une heure cible (défaut 16h00
    ET = clôture cash). Les fichiers sont nommés `HHMMSS.parquet` en heure ET —
    on choisit celui dont l'écart à la cible est minimal. Sert au pinning de
    clôture, où c'est la structure des strikes à ~16h qui compte."""
    root = SETTINGS.data_dir / "snapshots" / symbol / day
    files = sorted(root.glob("*.parquet")) if root.exists() else []
    if not files:
        return None

    def _secs(stem: str) -> int:
        try:
            return int(stem[0:2]) * 3600 + int(stem[2:4]) * 60 + int(stem[4:6])
        except (ValueError, IndexError):
            return 0

    target = _secs(target_hhmmss)
    best = min(files, key=lambda f: abs(_secs(f.stem) - target))
    return pd.read_parquet(best)


def load_latest_snapshot(symbol: str) -> tuple[pd.DataFrame, datetime] | None:
    """Dernier snapshot toutes séances confondues, avec son horodatage (naïf
    en ET, comme le reste du feed — cf. gex.metrics.ET).

    Sert à réamorcer STATE au démarrage sans redéclencher une collecte
    complète si la donnée persistée est encore fraîche (cf.
    scheduler.pull_native_options) : un redémarrage du process perd STATE
    (mémoire pure) même quand le disque a une donnée vieille de quelques
    minutes seulement.
    """
    days = snapshot_days(symbol)
    if not days:
        return None
    day = days[-1]
    root = SETTINGS.data_dir / "snapshots" / symbol / day
    files = sorted(root.glob("*.parquet")) if root.exists() else []
    if not files:
        return None
    f = files[-1]
    try:
        ts = datetime.strptime(f"{day} {f.stem}", "%Y-%m-%d %H%M%S")
    except ValueError:
        return None
    return pd.read_parquet(f), ts


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
