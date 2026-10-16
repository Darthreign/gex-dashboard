"""État publié par le processus MOTEUR, lu par le(s) processus web.

Le moteur (gex.capture lancé avec GEX_ENGINE=1) est le seul à parler aux
sources (CBOE, dxFeed) et à calculer : chaînes enrichies, résumés,
indicateurs /scalp. À chaque mise à jour il publie ici, dans `data/live/` :

- `<clé>.json`     : métadonnées (version, spot, horodatages, résumé) et le
                     nom du fichier de chaîne correspondant ;
- `<clé>.<v>.parquet` : la chaîne enrichie de cette version ;
- `scalp-<SYM>.json`  : les caches d'indicateurs /scalp déjà calculés.

Écriture atomique (temporaire puis remplacement) : le JSON n'est publié
qu'après son parquet, un lecteur ne voit donc jamais une version à moitié
écrite. Les fichiers marchent sous Windows, survivent à un redémarrage du
web et servent autant de processus web qu'on veut, sans protocole à
maintenir.

Côté web, `Mirror` relit les JSON toutes les secondes et ne charge une
chaîne que si sa version a changé : le processus web ne calcule plus rien.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from .config import SETTINGS
from .ingest import ChainSnapshot
from .metrics import SummaryMetrics

log = logging.getLogger(__name__)

POLL_S = 1.0
KEEP_VERSIONS = 3
_publish_lock = threading.Lock()
_versions: dict[str, int] = {}


def enabled() -> bool:
    """Mode moteur séparé (GEX_ENGINE=1), à définir pour le processus capture
    ET pour le dashboard."""
    from .rtquote import _env
    return (_env("GEX_ENGINE") or "").strip() in ("1", "true", "yes", "on")


def live_dir() -> Path:
    d = SETTINGS.data_dir / "live"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_bytes(data)
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # Windows : lecteur qui tient le fichier
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def publish_state(key: str, snap: ChainSnapshot, enriched: pd.DataFrame,
                  summary: SummaryMetrics) -> None:
    with _publish_lock:
        v = _versions.get(key, int(time.time() * 1000)) + 1
        _versions[key] = v
    d = live_dir()
    chain_name = f"{key}.{v}.parquet"
    tmp = d / f"{chain_name}.{uuid.uuid4().hex[:8]}.tmp"
    enriched.to_parquet(tmp, index=False)
    os.replace(tmp, d / chain_name)
    meta = {
        "key": key, "version": v, "chain": chain_name,
        "symbol": snap.symbol, "spot": snap.spot,
        "feed_timestamp": _iso(snap.feed_timestamp), "fetched_at": _iso(snap.fetched_at),
        "summary": {k: _iso(x) for k, x in dataclasses.asdict(summary).items()},
    }
    _atomic_write_bytes(d / f"{key}.json", json.dumps(meta, default=str).encode())
    for old in sorted(d.glob(f"{key}.*.parquet"),
                      key=lambda p: int(p.name.split(".")[-2]))[:-KEEP_VERSIONS]:
        try:
            old.unlink()
        except OSError:                  # encore ouvert par un lecteur : au prochain tour
            pass


def publish_json(name: str, payload: dict) -> None:
    _atomic_write_bytes(live_dir() / f"{name}.json",
                        json.dumps({"published": time.time(), "data": payload},
                                   default=_json_default).encode())


def _json_default(o):
    # scalaires numpy (int64, float32…) : leur valeur Python, pas leur repr
    item = getattr(o, "item", None)
    return item() if callable(item) else str(o)


def _parse_dt(v):
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def load_state(meta: dict) -> tuple[ChainSnapshot, pd.DataFrame, SummaryMetrics]:
    df = pd.read_parquet(live_dir() / meta["chain"])
    s = dict(meta["summary"])
    s["timestamp"] = _parse_dt(s["timestamp"])
    summary = SummaryMetrics(**s)
    snap = ChainSnapshot(symbol=meta["symbol"], spot=float(meta["spot"]),
                         feed_timestamp=_parse_dt(meta["feed_timestamp"]),
                         fetched_at=_parse_dt(meta["fetched_at"]), options=df)
    return snap, df, summary


class Mirror:
    """Recopie l'état publié dans `scheduler.STATE` et transmet les JSON
    nommés aux gestionnaires enregistrés (`on_json`)."""

    def __init__(self):
        self._seen: dict[str, object] = {}
        self._handlers: dict[str, Callable[[dict], None]] = {}
        self._thread: threading.Thread | None = None

    def on_json(self, prefix: str, handler: Callable[[str, dict], None]) -> None:
        self._handlers[prefix] = handler

    def poll_once(self) -> int:
        from .scheduler import STATE
        changed = 0
        for path in live_dir().glob("*.json"):
            name = path.stem
            try:
                st = path.stat()
                sig = (st.st_mtime_ns, st.st_size)
                if self._seen.get(name) == sig:
                    continue
                payload = json.loads(path.read_bytes())
            except (OSError, ValueError):
                continue                 # en cours de remplacement : au prochain tour
            handler = next((h for p, h in self._handlers.items() if name.startswith(p)), None)
            try:
                if handler is not None:
                    handler(name, payload)
                elif "chain" in payload:
                    snap, df, summary = load_state(payload)
                    entry = STATE.get(payload["key"])
                    with STATE.lock:
                        entry.snapshot, entry.enriched, entry.summary = snap, df, summary
                        entry.last_feed_ts = snap.feed_timestamp
                else:
                    continue
            except Exception:  # noqa: BLE001 — une publication illisible ne casse pas le miroir
                log.exception("État publié illisible : %s", path.name)
                continue
            self._seen[name] = sig
            changed += 1
        return changed

    def _run(self) -> None:
        while True:
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("Miroir de l'état moteur en échec")
            time.sleep(POLL_S)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="livestate", daemon=True)
            self._thread.start()


MIRROR = Mirror()
