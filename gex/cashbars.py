"""Bougies 1 minute des sous-jacents cash (QQQ, NDX, SPX, SPY) pour Squirrel X.

Affichage seulement : ni transactions ni volume. Deux lectures, toutes deux
bornées à la séance cash (09:30–16:00 New York, jours ouvrés) :

- l'historique, depuis `data/prices/<SYMBOLE>/<date>.parquet` (heure de New York
  naïve sur disque, cf. scheduler._flush_bars), resservi avec `ts` en secondes
  UTC ; ces fichiers ne sont que lus ;
- la bougie en cours, depuis le flux spot de CE processus (`rtquote.QUOTES`),
  poussée dans le flux SSE des ticks sous l'étiquette `bar:<SYMBOLE>`.

Données courtier : personnelles, non redistribuables.
"""
from __future__ import annotations

import io
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import broadcast, store
from .config import SETTINGS
from .rtquote import QUOTES

SYMBOLS = ("QQQ", "NDX", "SPX", "SPY")
NY = ZoneInfo("America/New_York")
OPEN_MIN, CLOSE_MIN = 9 * 60 + 30, 16 * 60
COLUMNS = ["ts", "open", "high", "low", "close"]
FEED_INTERVAL_S = 2.0
BUILT_MAX = 64

# chemin -> (mtime_ns, taille, a des bougies de séance)
_has_session: dict[Path, tuple[int, int, bool]] = {}
# chemin -> (ETag, parquet servi) ; le fichier du jour change toutes les 30 s
_built: dict[Path, tuple[str, bytes]] = {}


def _root(symbol: str) -> Path:
    return SETTINGS.data_dir / "prices" / symbol


def _weekday(day: str) -> bool:
    try:
        return datetime.strptime(day, "%Y-%m-%d").weekday() < 5
    except ValueError:
        return False


def _session(df: pd.DataFrame) -> pd.DataFrame:
    """Bougies de la séance cash, `ts` en secondes UTC (début de minute)."""
    if df.empty or not {"timestamp", *COLUMNS[1:]} <= set(df.columns):
        return pd.DataFrame(columns=COLUMNS)
    t = pd.to_datetime(df["timestamp"])
    minute = t.dt.hour * 60 + t.dt.minute
    keep = (minute >= OPEN_MIN) & (minute < CLOSE_MIN)
    ts = t[keep].dt.tz_localize(NY).dt.tz_convert("UTC").astype("datetime64[s, UTC]").astype("int64")
    out = df.loc[keep, COLUMNS[1:]].astype(float)
    out.insert(0, "ts", ts.values)
    return out.sort_values("ts", kind="stable").reset_index(drop=True)


def _etag(path: Path) -> tuple[str, tuple[int, int]]:
    st = path.stat()
    return f'"b{st.st_mtime_ns:x}-{st.st_size:x}"', (st.st_mtime_ns, st.st_size)


def _day_has_session(path: Path) -> bool:
    _, sig = _etag(path)
    hit = _has_session.get(path)
    if hit and hit[:2] == sig:
        return hit[2]
    try:
        ok = not _session(store._read_parquet_retry(path)).empty
    except Exception:  # noqa: BLE001 — fichier illisible : séance absente, pas une erreur 500
        ok = False
    _has_session[path] = (*sig, ok)
    return ok


def available_days(symbol: str) -> list[str]:
    """Séances cash disposant d'au moins une bougie ; week-ends et fériés absents."""
    if symbol not in SYMBOLS or not _root(symbol).exists():
        return []
    return sorted(p.stem for p in _root(symbol).glob("*.parquet")
                  if _weekday(p.stem) and _day_has_session(p))


def session_file(symbol: str, day: str) -> Path | None:
    path = _root(symbol) / f"{day}.parquet"
    if symbol not in SYMBOLS or not _weekday(day) or not path.exists():
        return None
    return path if _day_has_session(path) else None


def session_parquet(path: Path, matches) -> tuple[str, bytes | None]:
    """(ETag, parquet de la séance), contenu à None si `matches(etag)` : le client
    a déjà cette version."""
    etag, _ = _etag(path)
    if matches(etag):
        return etag, None
    hit = _built.get(path)
    if hit and hit[0] == etag:
        return hit
    buf = io.BytesIO()
    _session(store._read_parquet_retry(path)).to_parquet(buf, index=False)
    if len(_built) >= BUILT_MAX:
        _built.pop(next(iter(_built)))
    _built[path] = (etag, buf.getvalue())
    return _built[path]


def in_session(minute: int) -> bool:
    d = datetime.fromtimestamp(minute, NY)
    return d.weekday() < 5 and OPEN_MIN <= d.hour * 60 + d.minute < CLOSE_MIN


def _event(symbol: str, bar, closed: bool) -> str:
    content = {"symbol": symbol, "t": bar.minute, "o": round(bar.open, 4), "h": round(bar.high, 4),
               "l": round(bar.low, 4), "c": round(bar.close, 4)}
    if closed:
        content["closed"] = True
    return json.dumps(content, allow_nan=False)


def bar_producer(symbol: str, quotes=QUOTES, clock=time.time):
    """Producteur du canal `bar:<symbol>` : la bougie en cours quand elle change,
    et une fois chaque bougie close (`closed: true`) avant la suivante."""
    last = {"j": None, "closed_t": 0}

    def produce():
        cur, done = quotes.bar_state(symbol)
        current = int(clock() // 60) * 60
        if cur is not None and cur.minute < current:
            cur, done = None, cur            # minute passée, pas encore de nouveau prix
        if done is not None and done.minute > last["closed_t"]:
            last["closed_t"] = done.minute
            # une bougie close ancienne (flux resté sans abonné) n'est pas renvoyée
            if done.minute == current - 60 and in_session(done.minute):
                last["j"] = _event(symbol, done, True)
                return f"data: {last['j']}\n\n"
        if cur is None or not in_session(cur.minute):
            return None
        j = _event(symbol, cur, False)
        if j == last["j"]:
            return None
        last["j"] = j
        return f"data: {j}\n\n"
    return produce


def feed_channels(cash: str | None) -> dict[str, broadcast.Channel]:
    """Canaux `bar:<SYMBOLE>` demandés par `cash=QQQ,SPX` ; un symbole inconnu est ignoré."""
    return {f"bar:{s}": broadcast.channel(("cash-bar", s), lambda s=s: bar_producer(s), FEED_INTERVAL_S)
            for s in (cash or "").split(",") if s in SYMBOLS}
