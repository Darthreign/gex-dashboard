"""Boucle d'ingestion : pull flux toutes les N secondes, snapshot complet
toutes les M secondes, pendant les heures de marché ET.

L'état courant (dernière chaîne enrichie + synthèse par sous-jacent) est
gardé en mémoire dans `STATE`, protégé par un lock — le dashboard Dash lit
cet état, la persistance Parquet assure l'historique.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, time

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler

from . import backup, metrics, store
from .config import SETTINGS, UNDERLYINGS
from .ingest import ChainSnapshot, fetch_chain
from .metrics import ET, SummaryMetrics
from .rtquote import QUOTES

log = logging.getLogger(__name__)

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 15)


def market_is_open(now_et: datetime | None = None) -> bool:
    now_et = now_et or datetime.now(ET)
    if now_et.weekday() >= 5:
        return False
    return MARKET_OPEN <= now_et.time() <= MARKET_CLOSE


@dataclass
class UnderlyingState:
    snapshot: ChainSnapshot | None = None
    enriched: pd.DataFrame | None = None
    summary: SummaryMetrics | None = None
    last_feed_ts: datetime | None = None


@dataclass
class GlobalState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    per_symbol: dict[str, UnderlyingState] = field(default_factory=dict)
    last_error: str | None = None

    def get(self, symbol: str) -> UnderlyingState:
        return self.per_symbol.setdefault(symbol, UnderlyingState())


STATE = GlobalState()


def pull_symbol(symbol: str, persist_snapshot: bool) -> None:
    u = UNDERLYINGS[symbol]
    snap = fetch_chain(symbol, u.cboe_symbol)
    enriched = metrics.enrich(snap)
    summary = metrics.summarize(snap, enriched, with_basis=u.future is not None)
    now = datetime.now(ET)

    st = STATE.get(symbol)
    with STATE.lock:
        prev = st.enriched
        prev_feed_ts = st.last_feed_ts

    # Flux delta : uniquement sur les cibles analysées, et seulement si le feed
    # a réellement avancé depuis le dernier pull (sinon Δvolume = bruit nul).
    # Sur un constituant, seuls comptent ses murs et son spot.
    if (u.role == "target" and prev is not None
            and prev_feed_ts != snap.feed_timestamp):
        flow = metrics.flow_delta(prev, enriched, snap.spot)
        flow["timestamp"] = snap.feed_timestamp
        store.append_daily("flows", symbol, flow, now)

    if persist_snapshot:
        store.save_snapshot(symbol, enriched, now)
        store.append_history(summary.as_row())

    with STATE.lock:
        st.snapshot = snap
        st.enriched = enriched
        st.summary = summary
        st.last_feed_ts = snap.feed_timestamp
        STATE.last_error = None
    log.info(
        "%s pull ok — spot=%.2f netGEX=%.2f Bn zeroG=%s basis=%s",
        symbol, snap.spot, summary.net_gex / 1e9,
        f"{summary.zero_gamma:.0f}" if summary.zero_gamma else "n/a",
        f"{summary.basis:+.1f}" if summary.basis is not None else "n/a",
    )


class _Cadence:
    """Déclenche une action toutes les N itérations de la boucle de pull.

    `interval_s` est ramené au nombre d'itérations correspondant : la boucle
    tourne à `flow_interval_s`, tout le reste s'exprime en multiples.
    """

    def __init__(self, interval_s: int | None = None) -> None:
        self.count = 0
        interval_s = SETTINGS.snapshot_interval_s if interval_s is None else interval_s
        self.every = max(1, interval_s // SETTINGS.flow_interval_s)

    def tick(self) -> bool:
        due = self.count % self.every == 0
        self.count += 1
        return due


_CADENCE = _Cadence()
# Les constituants suivent leur propre horloge : leurs murs reposent sur l'open
# interest, publié une fois par jour, donc les puller au rythme des cibles
# n'apporterait rien et quadruplerait la charge.
_CONSTITUENT_CADENCE = _Cadence(SETTINGS.constituent_interval_s)
_CONSTITUENT_SNAPSHOT = _Cadence(SETTINGS.constituent_snapshot_interval_s)


def pull_all(force: bool = False) -> None:
    if SETTINGS.market_hours_only and not market_is_open() and not force:
        return
    persist = _CADENCE.tick()
    due = _CONSTITUENT_CADENCE.tick()
    persist_constituent = _CONSTITUENT_SNAPSHOT.tick()
    for key, u in UNDERLYINGS.items():
        if not u.enabled:
            continue
        is_constituent = u.role == "constituent"
        if is_constituent and not (due or force):
            continue
        try:
            pull_symbol(key, persist_snapshot=(persist_constituent if is_constituent
                                               else persist))
        except Exception as e:  # noqa: BLE001 — la boucle doit survivre
            log.exception("Échec pull %s", key)
            with STATE.lock:
                STATE.last_error = f"{key}: {e}"


def push_data_repo() -> None:
    """Commit + push quotidien du repo data/ (historique + flux) après la
    clôture — backup hors-machine des données non reconstituables."""
    import subprocess

    repo = SETTINGS.data_dir
    if not SETTINGS.auto_push_data or not (repo / ".git").exists():
        return
    day = datetime.now(ET).strftime("%Y-%m-%d")
    try:
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
        diff = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"])
        if diff.returncode == 0:
            return  # rien de nouveau
        subprocess.run(["git", "-C", str(repo), "commit", "-m", f"data {day}"],
                       check=True, capture_output=True)
        has_remote = subprocess.run(["git", "-C", str(repo), "remote"],
                                    capture_output=True, text=True).stdout.strip()
        if has_remote:
            subprocess.run(["git", "-C", str(repo), "push"], check=True,
                           capture_output=True, timeout=120)
            log.info("Repo data poussé (%s)", day)
        else:
            log.info("Repo data commité localement (%s, pas de remote)", day)
    except Exception:
        log.exception("Échec du push du repo data — données locales intactes")


def flush_prices() -> None:
    """Écrit sur disque les bougies 1 min achevées par le flux temps réel.

    Sans identifiants courtier, `drain_bars` renvoie une liste vide et la
    fonction ne fait rien : la collecte de prix est optionnelle comme le reste
    de la couche temps réel.
    """
    bars = QUOTES.drain_bars()
    if not bars:
        return
    by_symbol: dict[str, list[dict]] = {}
    for symbol, bar in bars:
        ts = datetime.fromtimestamp(bar.minute, tz=UTC).astimezone(ET).replace(tzinfo=None)
        by_symbol.setdefault(symbol, []).append({
            "timestamp": ts, "open": bar.open, "high": bar.high,
            "low": bar.low, "close": bar.close, "ticks": bar.ticks,
            # provenance courtier : non redistribuable (cf. gex/export.py)
            "source": "dxfeed",
        })
    for symbol, rows in by_symbol.items():
        try:
            store.append_prices(symbol, rows, rows[0]["timestamp"])
        except Exception:  # noqa: BLE001 — une écriture ratée ne doit rien casser
            log.exception("Échec écriture des prix %s", symbol)


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="America/New_York")
    sched.add_job(
        pull_all,
        "interval",
        seconds=SETTINGS.flow_interval_s,
        max_instances=1,
        coalesce=True,
    )
    # Vidange plus fréquente que la minute : une bougie n'est écrite qu'une
    # fois close, ce décalage borne simplement la perte en cas d'arrêt brutal.
    sched.add_job(flush_prices, "interval", seconds=30, max_instances=1, coalesce=True)
    sched.add_job(push_data_repo, "cron", day_of_week="mon-fri", hour=16, minute=20)
    # Sauvegarde distante après le push git : elle porte ce que GitHub refuse
    # (archives Databento de plus de 100 Mo). Sans rclone configuré, l'appel
    # journalise et se retire.
    sched.add_job(backup.run, "cron", day_of_week="mon-fri", hour=16, minute=30)
    sched.start()
    # Premier pull immédiat (même hors marché : affiche le dernier état connu).
    threading.Thread(target=pull_all, kwargs={"force": True}, daemon=True).start()
    return sched
