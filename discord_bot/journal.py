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
    q1_directionnel INTEGER, q1_retracement INTEGER,   -- 😰 / 🧘
    q2_haussier     INTEGER, q2_baissier    INTEGER,   -- 📈 / 📉
    q3_b1 INTEGER, q3_b2 INTEGER, q3_b3 INTEGER, q3_b4 INTEGER,  -- 1️⃣..4️⃣
    q4_repr_high    INTEGER, q4_repr_mid INTEGER, q4_repr_low INTEGER  -- 🎯/😐/🤷
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

-- Mémoire du labo : les hypothèses qu'on teste, et leur statut. Dans un an,
-- c'est ce qui dira POURQUOI telle donnée existe et si elle a été tranchée.
CREATE TABLE IF NOT EXISTS research_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created    TEXT NOT NULL,
    date       TEXT,                 -- séance concernée (optionnel)
    hypothesis TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending | confirmed | refuted
    note       TEXT
);
"""

# Nom de métrique réservé au tag métier « setup MOC » du jour (cf. set_setup).
SETUP_METRIC = "setup_moc"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Ouvre (et crée au besoin) la base, schéma garanti présent."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # lectures concurrentes sereines
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


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

# Colonnes de comptage du sondage, dans l'ordre d'affichage / des réactions.
POLL_COLS = ("q1_directionnel", "q1_retracement", "q2_haussier", "q2_baissier",
             "q3_b1", "q3_b2", "q3_b3", "q3_b4",
             "q4_repr_high", "q4_repr_mid", "q4_repr_low")


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
    """Écrit les comptages de réactions et marque le sondage dépouillé.

    `counts` : dict {colonne: nombre} pour tout ou partie de POLL_COLS. Les
    votes BRUTS sont conservés (pas un booléen dérivé) : tu pourras redéfinir
    un seuil plus tard et recalculer.
    """
    cols = [c for c in POLL_COLS if c in counts]
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


def add_note(conn: sqlite3.Connection, *, hypothesis: str, created: str,
             date: str | None = None, status: str = "pending",
             note: str | None = None) -> int:
    """Consigne une hypothèse de recherche. Renvoie son id."""
    cur = conn.execute(
        "INSERT INTO research_notes (created, date, hypothesis, status, note) "
        "VALUES (?,?,?,?,?)", (created, date, hypothesis, status, note))
    conn.commit()
    return int(cur.lastrowid)


def list_notes(conn: sqlite3.Connection,
               status: str | None = None) -> list[sqlite3.Row]:
    if status:
        return conn.execute("SELECT * FROM research_notes WHERE status=? "
                            "ORDER BY id DESC", (status,)).fetchall()
    return conn.execute("SELECT * FROM research_notes ORDER BY id DESC").fetchall()


def set_note_status(conn: sqlite3.Connection, note_id: int, status: str,
                    note: str | None = None) -> None:
    if note is None:
        conn.execute("UPDATE research_notes SET status=? WHERE id=?",
                     (status, note_id))
    else:
        conn.execute("UPDATE research_notes SET status=?, note=? WHERE id=?",
                     (status, note, note_id))
    conn.commit()


# --------------------------------------------------------------------------
# Helpers internes
# --------------------------------------------------------------------------

def _dumps(obj: Any) -> str | None:
    return None if obj is None else json.dumps(obj, ensure_ascii=False, default=str)


def _loads(s: str | None) -> Any:
    return None if not s else json.loads(s)


def _is_row(x: Any) -> bool:
    return isinstance(x, sqlite3.Row)
