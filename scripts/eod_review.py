"""Job de FIN de séance — chaque soir Lun-Ven 22h50 Paris (cash US clos à 22h00).

Mesure ce que la séance a réellement fait (mouvements directionnels propres,
range, heure de départ vs la fenêtre contrarienne d'avant 16h15) et le compare
à ce que les briefs annonçaient. Résultat : lignes `daily_metrics` (symbole NQ)
dans le journal + un court bilan `data/reviews/AAAA-MM-JJ.md`. Aucun post Discord.

Archive aussi les briefs du jour dans `data/briefs/archive/AAAA-MM-JJ/` : ils sont
écrasés chaque matin, sans cette copie on ne pourrait plus les juger a posteriori.

Usage :
    python scripts/eod_review.py                      # séance du jour (Paris)
    python scripts/eod_review.py --date 2026-09-23
    python scripts/eod_review.py --backfill 2026-07-16 2026-09-24   # historique (sans briefs)
    ... --dry-run                                     # n'écrit rien

Idempotent : relancer remplace les lignes du jour. Journal : logs/eod_review.log.
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "discord_bot"))

from gex import eodreview  # noqa: E402
import journal  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")
SYMBOL = "NQ"
PRICES = ROOT / "data" / "prices" / SYMBOL
BRIEFS = ROOT / "data" / "briefs"
REVIEWS = ROOT / "data" / "reviews"
JOURNAL_DB = ROOT / "data" / "journal" / "journal.sqlite"
LOG = ROOT / "logs" / "eod_review.log"


def log(msg: str) -> None:
    line = f"{dt.datetime.now(PARIS):%Y-%m-%d %H:%M:%S} | {msg}"
    print(line)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def weekdays(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def read_and_archive_briefs(day: dt.date) -> dict[str, str]:
    """Briefs ÉCRITS CE JOUR-LÀ (un fichier d'hier n'est pas celui d'aujourd'hui),
    copiés dans l'archive datée avant qu'un prochain run ne les écrase."""
    out: dict[str, str] = {}
    dest = BRIEFS / "archive" / day.isoformat()
    for nom in eodreview.BRIEF_NOMS:
        f = BRIEFS / f"{nom}.md"
        if not f.exists():
            continue
        if dt.datetime.fromtimestamp(f.stat().st_mtime, PARIS).date() != day:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest / f.name)
        out[nom] = f.read_text(encoding="utf-8")
    return out


def prev_atr(conn: sqlite3.Connection, day: dt.date) -> float | None:
    r = conn.execute("SELECT prev_atr FROM market_context WHERE date=? AND symbol=?",
                     (day.isoformat(), SYMBOL)).fetchone()
    return r["prev_atr"] if r and r["prev_atr"] else None


def review_day(conn: sqlite3.Connection, day: dt.date, *, with_briefs: bool,
               dry_run: bool) -> dict | None:
    f = PRICES / f"{day.isoformat()}.parquet"
    if not f.exists():
        log(f"{day} : pas de bougies {SYMBOL} — ignoré")
        return None
    bars = pd.read_parquet(f)
    briefs = read_and_archive_briefs(day) if with_briefs and not dry_run else {}
    metrics = eodreview.build_metrics(bars, briefs, prev_atr(conn, day))
    if metrics is None:
        log(f"{day} : séance trop lacunaire (< {eodreview.MIN_BARS} bougies RTH) — ignoré")
        return None
    if not dry_run:
        ts = dt.datetime.now(PARIS).isoformat()
        for name, (num, txt) in metrics.items():
            journal.set_metric(conn, date=day.isoformat(), name=name, value_num=num,
                               value_txt=txt, symbol=SYMBOL, ts=ts)
        REVIEWS.mkdir(parents=True, exist_ok=True)
        (REVIEWS / f"{day.isoformat()}.md").write_text(
            eodreview.review_markdown(day.isoformat(), metrics), encoding="utf-8")
    dirpts = metrics["dir_pts"][0]
    log(f"{day} : {metrics['dir_tier'][1]:>7} {dirpts:+6.0f} pts"
        f" départ {metrics.get('dir_debut', (None, '-'))[1]}"
        f" | early={int(metrics['dir_early'][0])}"
        f" | range {metrics['range_pts'][0]:.0f}"
        + (f" | briefs: {', '.join(sorted(briefs))}" if briefs else "")
        + (" [dry-run]" if dry_run else ""))
    return metrics


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", type=dt.date.fromisoformat)
    ap.add_argument("--backfill", nargs=2, type=dt.date.fromisoformat,
                    metavar=("DEBUT", "FIN"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    conn = journal.connect(JOURNAL_DB)
    try:
        if a.backfill:
            n = 0
            for day in weekdays(*a.backfill):
                n += review_day(conn, day, with_briefs=False, dry_run=a.dry_run) is not None
            log(f"backfill {a.backfill[0]} -> {a.backfill[1]} : {n} séance(s) évaluée(s)")
            return
        day = a.date or dt.datetime.now(PARIS).date()
        if day.weekday() >= 5:
            log(f"{day} : week-end, rien à évaluer")
            return
        log(f"===== Bilan de séance {day} — DÉBUT =====")
        review_day(conn, day, with_briefs=True, dry_run=a.dry_run)
        log(f"===== Bilan de séance {day} — FIN =====")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
