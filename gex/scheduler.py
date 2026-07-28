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
from .ingest import ChainSnapshot, fetch_chain, fetch_index_spot
from .metrics import ET, SummaryMetrics
from . import futopt
from .rtquote import PUBLIC_QUOTES, QUOTES, credentials_present

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


def pull_vix() -> None:
    """VIX comme donnée de confluence (get_market_context, MCP) : pas un
    sous-jacent analysé, juste un spot horodaté — cf. store.append_index_spot.
    Cadence alignée sur les constituants (~10 min), un signal de fond n'a pas
    besoin d'une résolution à la minute."""
    try:
        spot, ts = fetch_index_spot("_VIX")
        store.append_index_spot("vix", {"timestamp": ts, "vix": spot})
    except Exception:  # noqa: BLE001 — un échec VIX ne doit rien casser d'autre
        log.exception("Échec pull VIX")


def pull_all(force: bool = False) -> None:
    if SETTINGS.market_hours_only and not market_is_open() and not force:
        return
    persist = _CADENCE.tick()
    due = _CONSTITUENT_CADENCE.tick()
    persist_constituent = _CONSTITUENT_SNAPSHOT.tick()
    if due or force:
        pull_vix()
    for key, u in UNDERLYINGS.items():
        if not u.enabled:
            continue
        if u.role == "context":
            # pas une chaîne d'options — pullé séparément (pull_vix), listé
            # dans UNDERLYINGS uniquement pour l'abonnement dxFeed live
            continue
        if u.source == "futopt":
            # collecte native séparée (pull_native_options) : une chaîne
            # coûte ~90 s, incompatible avec cette boucle à 60 s
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


def build_native_summary(code: str, df: pd.DataFrame,
                         now_et: datetime | None = None) -> tuple[ChainSnapshot, SummaryMetrics]:
    """(ChainSnapshot, SummaryMetrics) à partir d'une chaîne native (futopt).

    Fonction pure : ne touche ni STATE ni le disque, donc testable sans
    connexion. `df` doit porter les colonnes de sortie de
    `futopt.enrich_native` (strike, type, expiry, gex, open_interest, volume,
    spot…) — mêmes colonnes que `metrics.enrich`, d'où la réutilisation directe
    des fonctions de `metrics` plutôt qu'une resynthèse spécifique.

    `source="dxfeed"` sur la ligne d'historique : ces données viennent du
    courtier (open interest et IV CME), non redistribuables — même
    traitement que les bougies de prix (cf. gex/rtquote.py).
    """
    now_et = now_et or datetime.now(ET)
    spot = float(df["spot"].iloc[0])
    ratios = metrics.put_call_ratios(df)
    today = now_et.date()
    snap = ChainSnapshot(
        symbol=code, spot=spot,
        # naïf en ET, comme le feed CBOE : build_cards fait un .replace(tzinfo=ET)
        feed_timestamp=now_et.replace(tzinfo=None),
        fetched_at=datetime.now(UTC), options=df,
    )
    summary = SummaryMetrics(
        timestamp=snap.feed_timestamp, symbol=code, spot=spot,
        net_gex=float(df["gex"].sum()),
        zero_gamma=metrics.zero_gamma(df, spot),
        pc_oi=ratios["pc_oi"], pc_volume=ratios["pc_volume"],
        net_gex_0dte=float(df.loc[metrics.bucket_mask(df, "0DTE", today), "gex"].sum()),
        # pas de "basis" : ce sont déjà des options sur LE future, pas un
        # indice à convertir vers un contrat qui lui serait associé
        basis=None, source="dxfeed",
    )
    return snap, summary


NATIVE_CACHE_FRESH_S = 300  # cf. pull_native_options : redémarrer perd STATE,
# pas le disque — inutile de rejouer ~90-280 s de collecte dxFeed pour une
# donnée qui a moins de 5 min.


def _seed_native_state(code: str, df: pd.DataFrame, ts: datetime) -> SummaryMetrics:
    """Construit (ChainSnapshot, SummaryMetrics) depuis `df`/`ts` et peuple
    STATE — factorisé entre le chemin cache et le chemin collecte live."""
    snap, summary = build_native_summary(code, df, ts)
    st = STATE.get(code)
    with STATE.lock:
        st.snapshot = snap
        st.enriched = df
        st.summary = summary
        st.last_feed_ts = snap.feed_timestamp
    return summary


def pull_native_options() -> None:
    """Chaînes d'options natives NQ et ES : construit, met à jour STATE, et
    persiste — sans identifiants courtier, ne fait rien.

    Coûte du temps (~90-280 s par sous-jacent, dominé par le rythme de
    livraison du serveur, pas par notre code) : APScheduler l'exécute dans
    son propre thread, ce qui ne retarde pas les pulls CBOE de 60 s.

    Avant de payer ce coût, on regarde si un snapshot persisté a moins de
    `NATIVE_CACHE_FRESH_S` : un redémarrage du process perd STATE (mémoire
    pure) mais pas le disque — sans ce court-circuit, chaque redémarrage
    (déploiement, crash) rejouait une collecte complète même si la dernière
    date d'il y a deux minutes. La cadence normale (15 min) dépasse toujours
    ce seuil, donc ce court-circuit ne saute jamais un vrai cycle de
    rafraîchissement, seulement les redémarrages rapprochés.

    Limite connue : ne calcule pas de flux delta (`flow_delta` suppose une
    colonne `contract` façon CBOE, absente ici) — les onglets Flux et Gamma
    échangé restent vides pour NQ/ES natifs. Le reste (niveaux, profil,
    heatmap, positionnement) fonctionne, ces fonctions ne demandant que les
    colonnes déjà produites par `futopt.enrich_native`.
    """
    if not credentials_present():
        return
    for code in ("NQ", "ES"):
        cached = store.load_latest_snapshot(code)
        if cached is not None:
            df_cached, ts = cached
            age_s = (datetime.now(ET) - ts.replace(tzinfo=ET)).total_seconds()
            if 0 <= age_s < NATIVE_CACHE_FRESH_S:
                _seed_native_state(code, df_cached, ts.replace(tzinfo=ET))
                log.info("%s (natif) : cache de %.0f s, collecte live sautée",
                         code, age_s)
                continue
        try:
            df = futopt.build_native_chain(code)
            if df is None or df.empty:
                continue
            now = datetime.now(ET)
            store.save_snapshot(code, df, now)
            summary = _seed_native_state(code, df, now)
            store.append_history(summary.as_row())
            log.info("%s (natif) pull ok — spot=%.2f netGEX=%.2f Bn zeroG=%s",
                     code, summary.spot, summary.net_gex / 1e9,
                     f"{summary.zero_gamma:.0f}" if summary.zero_gamma else "n/a")
        except Exception:  # noqa: BLE001 — un échec ne doit rien casser d'autre
            log.exception("%s : échec de la collecte native", code)


def _flush_bars(bars: list, source: str) -> None:
    """Écrit sur disque les bougies 1 min achevées, quelle que soit la
    source (compte courtier ou repli public délayé — cf. flush_prices)."""
    if not bars:
        return
    by_symbol: dict[str, list[dict]] = {}
    for symbol, bar in bars:
        ts = datetime.fromtimestamp(bar.minute, tz=UTC).astimezone(ET).replace(tzinfo=None)
        by_symbol.setdefault(symbol, []).append({
            "timestamp": ts, "open": bar.open, "high": bar.high,
            "low": bar.low, "close": bar.close, "ticks": bar.ticks,
            "source": source,
        })
    for symbol, rows in by_symbol.items():
        try:
            store.append_prices(symbol, rows, rows[0]["timestamp"])
        except Exception:  # noqa: BLE001 — une écriture ratée ne doit rien casser
            log.exception("Échec écriture des prix %s", symbol)


def flush_prices() -> None:
    """Écrit sur disque les bougies 1 min achevées par le flux temps réel.

    Sans identifiants courtier, `QUOTES.drain_bars()` renvoie une liste vide
    (la couche temps réel payante est inerte), mais `PUBLIC_QUOTES` — le
    repli gratuit délayé sur NQ/ES (cf. rtquote.PublicDelayedQuotes) —
    construit ses propres bougies de la même façon et doit être vidé lui
    aussi : sans cette ligne, ses bougies s'accumulaient en mémoire sans
    jamais être écrites, et le Heatmap retombait sur le repli grossier
    (un point par pull, ~10 min) même là où un spot délayé existait déjà.

    Provenance marquée à l'écriture : "dxfeed" (courtier, licence usage
    personnel non redistribuable) ou "dxfeed_public" (flux démo public,
    délayé ~15-20 min) — les deux exclues de l'export par défaut (cf.
    gex/export.py, qui n'autorise que source == "cboe"), mais distinguées
    pour ne jamais laisser croire que l'une est l'autre.
    """
    _flush_bars(QUOTES.drain_bars(), "dxfeed")
    _flush_bars(PUBLIC_QUOTES.drain_bars(), "dxfeed_public")


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
    # Options natives NQ/ES : cadence lâche (chaque cycle prend lui-même
    # ~90 s x 2), max_instances=1 empêche un cycle en cours d'en chevaucher
    # un autre si jamais il dépassait l'intervalle.
    sched.add_job(pull_native_options, "interval", minutes=15,
                  max_instances=1, coalesce=True)
    sched.add_job(push_data_repo, "cron", day_of_week="mon-fri", hour=16, minute=20)
    # Sauvegarde distante après le push git : elle porte ce que GitHub refuse
    # (archives Databento de plus de 100 Mo). Sans rclone configuré, l'appel
    # journalise et se retire.
    sched.add_job(backup.run, "cron", day_of_week="mon-fri", hour=16, minute=30)
    sched.start()
    # Premier pull immédiat (même hors marché : affiche le dernier état connu).
    threading.Thread(target=pull_all, kwargs={"force": True}, daemon=True).start()
    # Idem pour NQ/ES natifs : sans cet appel, ils resteraient invisibles dans
    # l'interface jusqu'à la première exécution planifiée (jusqu'à 15 min).
    threading.Thread(target=pull_native_options, daemon=True).start()
    return sched
