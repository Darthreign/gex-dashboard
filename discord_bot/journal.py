"""Journal de recherche — base SQLite locale pour le backtest.

Objectif : accumuler, jour après jour, de quoi étudier *a posteriori* quels
scénarios se sont joués et comment. On sépare strictement :

- les **faits bruts** (régimes horodatés, votes du sondage, contexte OHLC,
  snapshots existants du dashboard en Parquet) — sacrés, non recréables ;
- les **features** dérivées (`daily_metrics`, format long) — recalculables à
  volonté, y compris rétroactivement, quand les définitions évoluent.

Volontairement **stdlib pure** (`sqlite3`, aucune dépendance) : ce module tourne
dans le process du bot Discord, qui n'a pas forcément pandas/pyarrow. Il ne fait
que du stockage et quelques lectures ; toute la *math* de marché est calculée
côté dashboard et lue via l'API.

Idempotence : les gardes (`heatmap_done`, `regime_open_done`, `poll_posted`)
s'appuient sur la base, pas sur un état mémoire — un redémarrage du bot pile à
l'heure ne crée donc pas de doublon, et un `message_id` de sondage stocké
survit au redémarrage pour que le dépouillement ait toujours lieu.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Ordre canonique des couleurs de régime, pour les agrégats « minutes par état ».
COLORS = ("green", "orange", "red")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS regime_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,          -- 'YYYY-MM-DD' (jour de séance, Paris)
    ts            TEXT NOT NULL,          -- ISO 8601 avec tz
    kind          TEXT NOT NULL,          -- 'open' | 'change' | 'heartbeat'
    color         TEXT,                   -- 'green' | 'orange' | 'red'
    confidence    TEXT,                   -- 'forte' | 'moyenne' | 'faible'
    verdict       TEXT,
    reason        TEXT,                   -- pour 'change' : ce qui a bougé
    families_json TEXT,                   -- {famille: {score, statut, confiance}}
    digest_json   TEXT,                   -- digest complet au moment T
    -- état du marché à l'instant de l'événement (contexte, quelques nombres) :
    nq_price      REAL, nq_dist_open REAL, nq_dist_high REAL, nq_dist_low REAL,
    es_price      REAL, es_dist_open REAL, es_dist_high REAL, es_dist_low REAL
);
CREATE INDEX IF NOT EXISTS idx_regime_date ON regime_events(date);

CREATE TABLE IF NOT EXISTS polls (
    date            TEXT PRIMARY KEY,      -- un sondage par jour de séance
    message_id      TEXT NOT NULL,
    posted_ts       TEXT NOT NULL,
    tally_due_ts    TEXT NOT NULL,         -- quand dépouiller (J+1 12h)
    tallied_ts      TEXT,                  -- NULL tant que non dépouillé
    -- Comptages par option (une colonne = une réaction). Bornées et stables :
    -- des colonnes = lisibles/triables d'un coup d'œil (1 ligne/jour). Doit
    -- rester aligné avec POLL_COUNT_COLS + POLL_QUESTIONS (côté bot).
    q1_directionnel INTEGER, q1_retracement INTEGER,   -- 😰 / 🧘
    q2_haussier INTEGER, q2_baissier INTEGER, q2_neutre INTEGER,   -- 📈 / 📉 / ➡️
    q3_dir_oui INTEGER, q3_dir_non INTEGER,            -- ✅ / ❌ (phase directionnelle ?)
    q4_avant_1615 INTEGER, q4_apres_1615 INTEGER,      -- 🌅 / 🌆 (à partir de quand)
    q5_b1 INTEGER, q5_b2 INTEGER, q5_b3 INTEGER, q5_b4 INTEGER,  -- 1️⃣..4️⃣ (ampleur)
    q6_repr_high INTEGER, q6_repr_mid INTEGER, q6_repr_low INTEGER  -- 🎯/😐/🤷
);

CREATE TABLE IF NOT EXISTS heatmaps (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    date    TEXT NOT NULL,
    slot    TEXT NOT NULL,                 -- '15h30' | '16h00' | '18h00' | '22h00'
    symbol  TEXT NOT NULL,
    path    TEXT NOT NULL,
    ts      TEXT NOT NULL,
    UNIQUE(date, slot, symbol)
);

CREATE TABLE IF NOT EXISTS market_context (
    date       TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    open       REAL, high REAL, low REAL, close REAL,
    prev_close REAL, gap REAL, prev_atr REAL,
    weekday    INTEGER,                    -- 0 = lundi
    PRIMARY KEY (date, symbol)
);

-- Features évolutives, format long (EAV) : ajouter un indicateur = une INSERT,
-- jamais un ALTER TABLE. `symbol` NULL pour une métrique globale au jour.
-- Principe assumé : on privilégie le BRUT compact (bougies déjà en Parquet,
-- événements de régime) et on ne pose ici que les agrégats ou tags qui ne sont
-- PAS recalculables — surtout pas un cimetière de dérivés qu'on saurait refaire.
CREATE TABLE IF NOT EXISTS daily_metrics (
    date        TEXT NOT NULL,
    symbol      TEXT,
    metric_name TEXT NOT NULL,
    value_num   REAL,
    value_txt   TEXT,
    ts          TEXT,
    PRIMARY KEY (date, symbol, metric_name)
);

-- Déclenchements du bandeau d'amplification (page /scalp du dashboard) : un
-- signal-état (amplification/unsupported/brake, cf. gex/scalp.py) qui débute,
-- horodaté avec le prix du moment. Résolu après coup (resolved_ts NULL tant
-- que non résolu) : `outcome` dit si le mouvement s'est confirmé ou retourné,
-- pour calibrer les seuils sur des faits plutôt qu'au jugé. Écrit par le
-- dashboard (gex/app.py) ET par lui-même relu/résolu (gex/scheduler.py) — WAL
-- permet cet accès concurrent avec le bot Discord sur le même fichier.
CREATE TABLE IF NOT EXISTS scalp_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,          -- 'YYYY-MM-DD' (séance, heure locale)
    ts          TEXT NOT NULL,          -- ISO 8601 avec tz, déclenchement
    symbol      TEXT NOT NULL,          -- 'NQ' | 'ES'
    state       TEXT NOT NULL,          -- amplification | unsupported | brake
    tone        TEXT NOT NULL,
    direction   INTEGER NOT NULL,       -- +1 haussier, -1 baissier
    title       TEXT NOT NULL,
    spot        REAL NOT NULL,          -- prix au déclenchement
    move_pts    REAL, net_musd REAL, gross_musd REAL,
    resolved_ts       TEXT,             -- NULL tant que non résolu
    outcome_move_pts  REAL,             -- prix(résolution) - spot, signé
    outcome           TEXT              -- continued | reversed | flat
);
CREATE INDEX IF NOT EXISTS idx_scalp_signals_date ON scalp_signals(date);
CREATE INDEX IF NOT EXISTS idx_scalp_signals_open ON scalp_signals(resolved_ts);

-- Niveaux d'absorption (candidats iceberg, cf. gex/iceberg.py + la zone
-- permanente de /scalp) — mêmes conventions date/ts/symbol/price que
-- scalp_signals à dessein : ce qu'on veut, c'est pouvoir croiser après coup
-- « y avait-il une absorption près de ce prix juste avant ce signal ? ».
-- UNIQUE(symbol, ts) : une salve = une ligne, jamais de doublon même si elle
-- reste dans la fenêtre glissante (donc relue) plusieurs cycles de suite.
-- hvl_* : confirmation par le volume profile de séance (cf. gex/iceberg.py
-- hvl_near) — le niveau HVL le plus proche de `price`, s'il y en a un, au
-- moment où la salve a été détectée. hvl_price NULL = pas de HVL à proximité,
-- une salve normale n'en a pas forcément.
CREATE TABLE IF NOT EXISTS absorption_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    date      TEXT NOT NULL,
    ts        TEXT NOT NULL,          -- ISO 8601 avec tz, fin de la salve
    symbol    TEXT NOT NULL,          -- 'NQ' | 'ES'
    side      TEXT NOT NULL,          -- 'BUY' | 'SELL' (sens agresseur)
    price     REAL NOT NULL,
    ratio     REAL, total REAL, n_prints INTEGER,
    hvl_price REAL, hvl_delta REAL, hvl_side TEXT,
    UNIQUE(symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_absorption_date ON absorption_events(date);
CREATE INDEX IF NOT EXISTS idx_absorption_symbol_price ON absorption_events(symbol, price);

-- Setups de la lecture normalisée /scalp v2 (cf. gex/edge.py) : chaque
-- transition vers « rejet » ou « à éviter », avec les mesures qui l'ont
-- produite — pour vérifier en direct, après coup, ce que le rapport
-- (scripts/edge_report.py) a mesuré sur l'historique.
CREATE TABLE IF NOT EXISTS scalp_setups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    ts            TEXT NOT NULL,          -- ISO 8601 avec tz
    symbol        TEXT NOT NULL,
    setup         TEXT NOT NULL,          -- fade | avoid
    fade_dir      INTEGER NOT NULL,       -- +1 rejet haussier, -1 baissier, 0 (avoid)
    spot          REAL NOT NULL,
    ext_em        REAL, zone TEXT, dist_flip_em REAL, em REAL,
    confirmations TEXT,
    params_version TEXT, validated INTEGER
);
CREATE INDEX IF NOT EXISTS idx_scalp_setups_date ON scalp_setups(date);

-- Mémoire du labo : hypothèses, observations, conclusions, décisions, bugs…
-- Dans un an, c'est ce qui dira POURQUOI telle donnée existe et si elle a été
-- tranchée. `linked_date` = la séance CONCERNÉE (≠ `created`, quand c'est
-- écrit) ; `author` capturé automatiquement pour distinguer les traders sans
-- migration future.
CREATE TABLE IF NOT EXISTS research_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created     TEXT NOT NULL,
    linked_date TEXT,                 -- séance concernée (optionnel)
    author      TEXT NOT NULL DEFAULT 'Emilien',
    type        TEXT NOT NULL DEFAULT 'note',   -- hypothesis|observation|conclusion|decision|idea|bug|note
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | confirmed | refuted (surtout pour hypothesis)
    note        TEXT
);

-- Préférences /scalp par utilisateur (2026-10-04, piste 7 roadmap-scalp-v2) :
-- identité = l'e-mail renvoyé par Cloudflare Access (en-tête
-- Cf-Access-Authenticated-User-Email, authentification par code à usage
-- unique — confirmé en production). Un JSON par utilisateur plutôt qu'une
-- colonne par préférence : évite une migration de schéma à chaque nouveau
-- réglage, cohérent avec les clés déjà utilisées côté localStorage
-- (gex-scalp-layout/ergo/order) qu'il remplace pour qui est identifié.
-- Accès SANS Cloudflare (dev local, 127.0.0.1) : pas d'e-mail dans l'en-tête
-- -> pas de synchronisation serveur, repli silencieux sur le localStorage
-- seul, comportement inchangé.
CREATE TABLE IF NOT EXISTS user_prefs (
    email      TEXT PRIMARY KEY,
    prefs_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Types d'entrées du journal de recherche (indicatif, la colonne reste libre).
LOG_TYPES = ("hypothesis", "observation", "conclusion", "decision", "idea", "bug", "note")

# Nom de métrique réservé au tag métier « setup MOC » du jour (cf. set_setup).
SETUP_METRIC = "setup_moc"

# Colonnes de comptage du sondage (une par réaction), dans l'ordre d'affichage.
# Source de vérité pour la table `polls` ET la migration. Doit rester alignée
# avec POLL_QUESTIONS côté bot (mêmes clés = mêmes colonnes).
POLL_COUNT_COLS = (
    "q1_directionnel", "q1_retracement",
    "q2_haussier", "q2_baissier", "q2_neutre",
    "q3_dir_oui", "q3_dir_non",
    "q4_avant_1615", "q4_apres_1615",
    "q5_b1", "q5_b2", "q5_b3", "q5_b4",
    "q6_repr_high", "q6_repr_mid", "q6_repr_low",
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Ouvre (et crée au besoin) la base, schéma garanti présent."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False : cette connexion est mise en cache au niveau
    # module côté dashboard (gex/app.py::_journal), appelée depuis des
    # callbacks Dash qui tournent sur des threads différents du pool Flask —
    # sans ce réglage, toute écriture hors du thread créateur lève
    # sqlite3.ProgrammingError (constaté le 2026-09-29 : plus aucune écriture
    # de 14h40 à 23h10 en séance RTH). Les appelants sérialisent leurs
    # écritures (verrou côté app.py) car une connexion sqlite3 n'est pas sûre
    # pour un accès concurrent réel, seulement pour un accès depuis des
    # threads différents en alternance.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # lectures concurrentes sereines
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    _ensure_poll_columns(conn)
    _ensure_absorption_columns(conn)
    _ensure_scalp_signal_columns(conn)
    conn.commit()
    return conn


def _ensure_poll_columns(conn: sqlite3.Connection) -> None:
    """Ajoute au besoin les colonnes de comptage manquantes à `polls`.

    Le sondage évolue rarement, mais quand il change on ajoute une colonne : sur
    une base déjà créée, `CREATE TABLE IF NOT EXISTS` ne suffit pas, d'où ce
    petit ALTER idempotent (non destructif : colonnes nullables, données
    intactes). Zéro migration à écrire à la main."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(polls)")}
    for col in POLL_COUNT_COLS:
        if col not in existing:
            conn.execute(f"ALTER TABLE polls ADD COLUMN {col} INTEGER")


# Colonnes de confirmation HVL ajoutées le 2026-09-30 (cf. ABSORPTION_HVL_COLS)
# à une table `absorption_events` qui existait déjà en production.
ABSORPTION_HVL_COLS = (("hvl_price", "REAL"), ("hvl_delta", "REAL"), ("hvl_side", "TEXT"))


def _ensure_absorption_columns(conn: sqlite3.Connection) -> None:
    """Même logique que `_ensure_poll_columns`, pour `absorption_events`."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(absorption_events)")}
    for col, sqltype in ABSORPTION_HVL_COLS:
        if col not in existing:
            conn.execute(f"ALTER TABLE absorption_events ADD COLUMN {col} {sqltype}")


# Colonne ajoutée le 2026-10-03 : exclure une ligne polluée d'une calibration
# SANS la supprimer (donnée brute gardée pour audit) — ex. le bug du spot
# figé à 30040 (saturation 1-thread Werkzeug, cf. mémoire du projet
# audit-bug-spot-amplification-30040). NULL = ligne saine, utilisable.
# "basis" ajoutée le 2026-10-03 : quelle mesure de mouvement a produit la
# ligne — "fenetre_5min" (historique, /scalpv1) ou "swing_v60" (/scalp v2,
# cf. gex/app.py::scalp_inputs_swing). NULL = lignes d'avant cette colonne,
# toutes "fenetre_5min" implicitement (seul moteur qui existait alors).
# Distinguer dès l'écriture, PAS après coup — c'est exactement l'absence de
# cette distinction qui a rendu le bug spot=30040 difficile à isoler (cf.
# mémoire du projet audit-bug-spot-amplification-30040).
SCALP_SIGNALS_EXTRA_COLS = (("excluded_reason", "TEXT"), ("basis", "TEXT"))


def _ensure_scalp_signal_columns(conn: sqlite3.Connection) -> None:
    """Même logique que `_ensure_poll_columns`, pour `scalp_signals`."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(scalp_signals)")}
    for col, sqltype in SCALP_SIGNALS_EXTRA_COLS:
        if col not in existing:
            conn.execute(f"ALTER TABLE scalp_signals ADD COLUMN {col} {sqltype}")


# --------------------------------------------------------------------------
# Régimes
# --------------------------------------------------------------------------

def record_regime(conn: sqlite3.Connection, *, date: str, ts: str, kind: str,
                  color: str | None = None, confidence: str | None = None,
                  verdict: str | None = None, reason: str | None = None,
                  families: dict | None = None, digest: dict | None = None,
                  market: dict | None = None) -> None:
    """Enregistre un événement de régime.

    `market` : dict optionnel {nq: {price, dist_open, dist_high, dist_low},
    es: {...}} — l'état du marché à l'instant T (surtout utile sur 'change').
    """
    m = market or {}
    nq, es = m.get("nq") or {}, m.get("es") or {}
    conn.execute(
        """INSERT INTO regime_events
           (date, ts, kind, color, confidence, verdict, reason,
            families_json, digest_json,
            nq_price, nq_dist_open, nq_dist_high, nq_dist_low,
            es_price, es_dist_open, es_dist_high, es_dist_low)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date, ts, kind, color, confidence, verdict, reason,
         _dumps(families), _dumps(digest),
         nq.get("price"), nq.get("dist_open"), nq.get("dist_high"), nq.get("dist_low"),
         es.get("price"), es.get("dist_open"), es.get("dist_high"), es.get("dist_low")),
    )
    conn.commit()


def regime_open_done(conn: sqlite3.Connection, date: str) -> bool:
    """Le snapshot d'ouverture du jour est-il déjà posé ? (idempotence)"""
    row = conn.execute(
        "SELECT 1 FROM regime_events WHERE date=? AND kind='open' LIMIT 1", (date,)
    ).fetchone()
    return row is not None


def last_regime(conn: sqlite3.Connection, date: str) -> sqlite3.Row | None:
    """Dernier événement de régime du jour (pour calculer la `reason`)."""
    return conn.execute(
        "SELECT * FROM regime_events WHERE date=? ORDER BY ts DESC, id DESC LIMIT 1",
        (date,),
    ).fetchone()


def regime_timeline(conn: sqlite3.Connection, date: str) -> list[sqlite3.Row]:
    """Tous les événements du jour, dans l'ordre chronologique."""
    return conn.execute(
        "SELECT * FROM regime_events WHERE date=? ORDER BY ts ASC, id ASC", (date,)
    ).fetchall()


def minutes_par_couleur(events: Iterable[sqlite3.Row],
                        fin_ts: str | None = None) -> dict[str, float]:
    """Minutes passées dans chaque couleur sur une journée.

    Reconstruit la fonction en escalier à partir des événements horodatés :
    chaque couleur tient jusqu'à l'événement suivant. `fin_ts` borne le dernier
    segment (typiquement la clôture 22h) ; sans lui, le dernier segment est
    ignoré (durée inconnue).
    """
    from datetime import datetime

    rows = [e for e in events if e["color"]]
    out = {c: 0.0 for c in COLORS}
    for i, e in enumerate(rows):
        start = datetime.fromisoformat(e["ts"])
        if i + 1 < len(rows):
            end = datetime.fromisoformat(rows[i + 1]["ts"])
        elif fin_ts is not None:
            end = datetime.fromisoformat(fin_ts)
        else:
            break
        out[e["color"]] = out.get(e["color"], 0.0) + max(
            0.0, (end - start).total_seconds() / 60.0)
    return out


def compute_reason(prev: sqlite3.Row | dict | None, color: str,
                   confidence: str | None, families: dict | None) -> str | None:
    """Décrit CE QUI a changé entre le régime précédent et le courant.

    Renvoie None si rien de significatif n'a bougé (même couleur, mêmes statuts
    de famille). La confiance seule n'est pas un « changement » (elle ne pilote
    pas la couleur), mais on la mentionne si elle bouge en même temps.
    """
    if prev is None:
        return None
    prev_color = prev["color"] if _is_row(prev) else prev.get("color")
    prev_conf = prev["confidence"] if _is_row(prev) else prev.get("confidence")
    prev_fam = _loads(prev["families_json"] if _is_row(prev) else prev.get("families_json"))

    bits = []
    if prev_color != color:
        bits.append(f"couleur {prev_color}→{color}")
    for fam in sorted((families or {})):
        old = (prev_fam or {}).get(fam, {}).get("statut")
        new = (families or {}).get(fam, {}).get("statut")
        if old != new:
            bits.append(f"{fam} {old}→{new}")
    if prev_conf != confidence:
        bits.append(f"confiance {prev_conf}→{confidence}")
    return " · ".join(bits) or None


# --------------------------------------------------------------------------
# Sondage
# --------------------------------------------------------------------------

def poll_posted(conn: sqlite3.Connection, date: str) -> bool:
    """Un sondage a-t-il déjà été posté pour ce jour ? (anti-doublon)"""
    return conn.execute("SELECT 1 FROM polls WHERE date=? LIMIT 1",
                        (date,)).fetchone() is not None


def poll_open(conn: sqlite3.Connection, *, date: str, message_id: str,
              posted_ts: str, tally_due_ts: str) -> None:
    """Enregistre un sondage fraîchement posté (comptages à NULL)."""
    conn.execute(
        "INSERT OR IGNORE INTO polls (date, message_id, posted_ts, tally_due_ts) "
        "VALUES (?,?,?,?)", (date, message_id, posted_ts, tally_due_ts))
    conn.commit()


def polls_a_depouiller(conn: sqlite3.Connection, now_ts: str) -> list[sqlite3.Row]:
    """Sondages non dépouillés dont l'échéance de dépouillement est atteinte."""
    return conn.execute(
        "SELECT * FROM polls WHERE tallied_ts IS NULL AND tally_due_ts <= ? "
        "ORDER BY date ASC", (now_ts,)).fetchall()


def poll_tally(conn: sqlite3.Connection, *, date: str, counts: dict[str, int],
               tallied_ts: str) -> None:
    """Écrit les comptages de réactions (une colonne par option) et marque le
    sondage dépouillé. `counts` : dict {colonne: nombre}. Votes BRUTS conservés
    (pas un booléen) : tu pourras redéfinir un seuil et recalculer."""
    cols = [c for c in POLL_COUNT_COLS if c in counts]   # whitelist = pas d'injection
    sets = ", ".join(f"{c}=?" for c in cols) + ", tallied_ts=?"
    values = [int(counts[c]) for c in cols] + [tallied_ts, date]
    conn.execute(f"UPDATE polls SET {sets} WHERE date=?", values)
    conn.commit()


# --------------------------------------------------------------------------
# Heatmaps
# --------------------------------------------------------------------------

def heatmap_done(conn: sqlite3.Connection, date: str, slot: str, symbol: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM heatmaps WHERE date=? AND slot=? AND symbol=? LIMIT 1",
        (date, slot, symbol)).fetchone() is not None


def record_heatmap(conn: sqlite3.Connection, *, date: str, slot: str,
                   symbol: str, path: str, ts: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO heatmaps (date, slot, symbol, path, ts) "
        "VALUES (?,?,?,?,?)", (date, slot, symbol, path, ts))
    conn.commit()


# --------------------------------------------------------------------------
# Contexte de marché + features
# --------------------------------------------------------------------------

def upsert_market_context(conn: sqlite3.Connection, *, date: str, symbol: str,
                          ctx: dict) -> None:
    """Enregistre les faits OHLC stables du jour pour un symbole."""
    conn.execute(
        """INSERT OR REPLACE INTO market_context
           (date, symbol, open, high, low, close, prev_close, gap, prev_atr, weekday)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (date, symbol, ctx.get("open"), ctx.get("high"), ctx.get("low"),
         ctx.get("close"), ctx.get("prev_close"), ctx.get("gap"),
         ctx.get("prev_atr"), ctx.get("weekday")))
    conn.commit()


def set_metric(conn: sqlite3.Connection, *, date: str, name: str,
               value_num: float | None = None, value_txt: str | None = None,
               symbol: str | None = None, ts: str | None = None) -> None:
    """Pose une feature dans `daily_metrics` (format long, extensible).

    Upsert manuel (DELETE + INSERT) car la clé primaire contient `symbol`, qui
    peut être NULL pour une métrique globale — et SQLite traite deux NULL comme
    DISTINCTS dans une contrainte d'unicité : un simple INSERT OR REPLACE
    dupliquerait au lieu de remplacer. `symbol IS ?` gère le NULL au DELETE.
    """
    conn.execute("DELETE FROM daily_metrics WHERE date=? AND symbol IS ? "
                 "AND metric_name=?", (date, symbol, name))
    conn.execute(
        "INSERT INTO daily_metrics "
        "(date, symbol, metric_name, value_num, value_txt, ts) VALUES (?,?,?,?,?,?)",
        (date, symbol, name, value_num, value_txt, ts))
    conn.commit()


def get_metric(conn: sqlite3.Connection, date: str, name: str,
               symbol: str | None = None) -> Any:
    """Lit une feature (value_num si présente, sinon value_txt)."""
    row = conn.execute(
        "SELECT value_num, value_txt FROM daily_metrics "
        "WHERE date=? AND metric_name=? AND symbol IS ?",
        (date, name, symbol)).fetchone()
    if row is None:
        return None
    return row["value_num"] if row["value_num"] is not None else row["value_txt"]


# --------------------------------------------------------------------------
# Tag métier « setup MOC » + notes de recherche
# --------------------------------------------------------------------------

def set_setup(conn: sqlite3.Connection, *, date: str, value: str,
              ts: str | None = None) -> None:
    """Tag le setup MOC du jour (ex. 'MOC A', 'NONE'). Corrigible : ré-écrire
    remplace. Info métier NON recalculable, contrairement aux dérivés."""
    set_metric(conn, date=date, name=SETUP_METRIC, value_txt=value, ts=ts)


def get_setup(conn: sqlite3.Connection, date: str) -> str | None:
    return get_metric(conn, date, SETUP_METRIC)


def add_entry(conn: sqlite3.Connection, *, text: str, created: str,
              type: str = "note", author: str = "Emilien",
              linked_date: str | None = None, status: str = "pending",
              note: str | None = None) -> int:
    """Consigne une entrée du journal de recherche. Renvoie son id."""
    cur = conn.execute(
        "INSERT INTO research_log (created, linked_date, author, type, text, status, note) "
        "VALUES (?,?,?,?,?,?,?)", (created, linked_date, author, type, text, status, note))
    conn.commit()
    return int(cur.lastrowid)


def list_entries(conn: sqlite3.Connection, *, type: str | None = None,
                 status: str | None = None,
                 author: str | None = None) -> list[sqlite3.Row]:
    q, where, args = "SELECT * FROM research_log", [], []
    for col, val in (("type", type), ("status", status), ("author", author)):
        if val:
            where.append(f"{col}=?")
            args.append(val)
    if where:
        q += " WHERE " + " AND ".join(where)
    return conn.execute(q + " ORDER BY id DESC", args).fetchall()


def set_entry_status(conn: sqlite3.Connection, entry_id: int, status: str,
                     note: str | None = None) -> None:
    if note is None:
        conn.execute("UPDATE research_log SET status=? WHERE id=?", (status, entry_id))
    else:
        conn.execute("UPDATE research_log SET status=?, note=? WHERE id=?",
                     (status, note, entry_id))
    conn.commit()


# --------------------------------------------------------------------------
# Signaux de la page /scalp (bandeau d'amplification)
# --------------------------------------------------------------------------

def record_scalp_signal(conn: sqlite3.Connection, *, date: str, ts: str, symbol: str,
                        state: str, tone: str, direction: int, title: str, spot: float,
                        move_pts: float | None = None, net_musd: float | None = None,
                        gross_musd: float | None = None, basis: str | None = None) -> int:
    """Enregistre le DÉBUT d'un signal-état (transition) — jamais un doublon
    à chaque cycle où l'état n'a pas changé, c'est à l'appelant de le garantir
    (cf. gex.scalp.should_log_signal). Renvoie l'id, pour la résolution.

    `basis` : cf. SCALP_SIGNALS_EXTRA_COLS — quelle mesure de mouvement a
    produit cette ligne ("fenetre_5min" | "swing_v60"), None pour un appelant
    qui ne le précise pas (compat)."""
    cur = conn.execute(
        """INSERT INTO scalp_signals
           (date, ts, symbol, state, tone, direction, title, spot,
            move_pts, net_musd, gross_musd, basis)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date, ts, symbol, state, tone, int(direction), title, spot,
         move_pts, net_musd, gross_musd, basis),
    )
    conn.commit()
    return int(cur.lastrowid)


def unresolved_scalp_signals(conn: sqlite3.Connection, *,
                             older_than_ts: str) -> list[sqlite3.Row]:
    """Signaux pas encore résolus, déclenchés avant `older_than_ts` (ISO 8601) —
    assez vieux pour qu'on sache si le mouvement s'est confirmé ou retourné."""
    return conn.execute(
        "SELECT * FROM scalp_signals WHERE resolved_ts IS NULL AND ts <= ? "
        "ORDER BY ts ASC", (older_than_ts,),
    ).fetchall()


def resolve_scalp_signal(conn: sqlite3.Connection, *, signal_id: int, resolved_ts: str,
                         outcome_move_pts: float, outcome: str) -> None:
    conn.execute(
        "UPDATE scalp_signals SET resolved_ts=?, outcome_move_pts=?, outcome=? WHERE id=?",
        (resolved_ts, outcome_move_pts, outcome, signal_id),
    )
    conn.commit()


def record_scalp_setup(conn: sqlite3.Connection, *, date: str, ts: str, symbol: str,
                       setup: str, fade_dir: int, spot: float, ext_em: float | None,
                       zone: str | None, dist_flip_em: float | None, em: float | None,
                       confirmations: str, params_version: str, validated: bool) -> int:
    """Enregistre une TRANSITION de la lecture /scalp v2 (cf. gex/edge.py)."""
    cur = conn.execute(
        """INSERT INTO scalp_setups
           (date, ts, symbol, setup, fade_dir, spot, ext_em, zone, dist_flip_em, em,
            confirmations, params_version, validated)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date, ts, symbol, setup, int(fade_dir), spot, ext_em, zone, dist_flip_em, em,
         confirmations, params_version, int(validated)),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_absorption(conn: sqlite3.Connection, *, date: str, ts: str, symbol: str,
                      side: str, price: float, ratio: float | None = None,
                      total: float | None = None, n_prints: int | None = None,
                      hvl_price: float | None = None, hvl_delta: float | None = None,
                      hvl_side: str | None = None) -> None:
    """Journalise une salve d'absorption détectée (cf. gex/iceberg.py). INSERT
    OR IGNORE : une salve relue plusieurs cycles de suite (fenêtre glissante,
    cf. TickCapture.absorption_recent) ne crée jamais de doublon (UNIQUE(symbol,
    ts)) — sert de filet, l'appelant est censé dédoublonner lui-même en amont.

    `hvl_*` : confirmation par le volume profile de séance (cf.
    gex/iceberg.py::hvl_near), None si aucun HVL n'était à proximité."""
    conn.execute(
        """INSERT OR IGNORE INTO absorption_events
           (date, ts, symbol, side, price, ratio, total, n_prints,
            hvl_price, hvl_delta, hvl_side)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (date, ts, symbol, side, price, ratio, total, n_prints,
         hvl_price, hvl_delta, hvl_side),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Préférences /scalp par utilisateur (cf. user_prefs dans _SCHEMA)
# --------------------------------------------------------------------------

def get_user_prefs(conn: sqlite3.Connection, email: str) -> dict:
    """Préférences de `email`, {} si jamais enregistrées."""
    row = conn.execute("SELECT prefs_json FROM user_prefs WHERE email = ?", (email,)).fetchone()
    return _loads(row["prefs_json"]) or {} if row else {}


def set_user_pref(conn: sqlite3.Connection, email: str, key: str, value: Any) -> dict:
    """Fusionne `{key: value}` dans les préférences de `email` (upsert) et
    renvoie l'objet complet résultant — un seul réglage à la fois, jamais tout
    remplacer d'un coup (deux onglets du même utilisateur ne doivent pas
    s'écraser mutuellement les autres réglages)."""
    prefs = get_user_prefs(conn, email)
    prefs[key] = value
    conn.execute(
        """INSERT INTO user_prefs (email, prefs_json, updated_at) VALUES (?,?,?)
           ON CONFLICT(email) DO UPDATE SET prefs_json=excluded.prefs_json,
                                             updated_at=excluded.updated_at""",
        (email, _dumps(prefs), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return prefs


# --------------------------------------------------------------------------
# Helpers internes
# --------------------------------------------------------------------------

def _dumps(obj: Any) -> str | None:
    return None if obj is None else json.dumps(obj, ensure_ascii=False, default=str)


def _loads(s: str | None) -> Any:
    return None if not s else json.loads(s)


def _is_row(x: Any) -> bool:
    return isinstance(x, sqlite3.Row)
