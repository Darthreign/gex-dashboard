"""Les niveaux GEX à gamma positif font-ils plus réagir le prix que les négatifs ?

    python scripts/level_reaction_report.py
    python scripts/level_reaction_report.py --start 2026-01-01 --perimetre 0dte

Pour chaque séance de l'année : les 5 plus gros strikes en |GEX net| (chaîne
reconstruite depuis l'open interest Databento, comme la piste 2 du rapport
d'excès), chacun avec le signe de son GEX ; premier contact du prix avec
chacun pendant la fenêtre, puis réaction ou traversée (cf. gex/levelreact.py).
Les mêmes mesures sont faites sur des niveaux TÉMOINS (les vrais niveaux
décalés de ±30 / ±40 pts) : c'est ce qui dit si le niveau compte, ou si c'est
le marché qui rebondit partout autant.

Aucun seuil n'est calé sur les données : toutes les séances sont lues. Les
intervalles et p-values tirent des SÉANCES entières (les niveaux d'une même
séance ne sont pas indépendants), avec correction de Holm sur l'ensemble des
comparaisons. Lecture seule ; rapport dans
data/reports/level_reaction_<SYMBOLE>_<date>.md.

Il faut les fichiers Databento `definition` + `statistics` (cache du rapport
d'excès) et les ticks du future (live ou import/ticks_full).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gex import dbgamma, levelreact, rates  # noqa: E402
from gex.config import SETTINGS  # noqa: E402
from gex.metrics import ET  # noqa: E402

import excess_report  # noqa: E402

SYMBOL = "NQ"


def window_bounds(day: str, fenetre: str) -> tuple[float, float]:
    """(début, fin) de la fenêtre étudiée, epoch s : séance cash 9h30-16h00 ET
    du jour `day`, ou séance Globex complète (18h00 ET la veille -> 17h00)."""
    d = date.fromisoformat(day)
    if fenetre == "globex":
        start = float(dbgamma.session_start(day).timestamp())
        end = datetime.combine(d, time(17, 0), tzinfo=ET).timestamp()
    else:
        start = datetime.combine(d, time(9, 30), tzinfo=ET).timestamp()
        end = datetime.combine(d, time(16, 0), tzinfo=ET).timestamp()
    return start, end


def collect(symbol: str, start: str | None, end: str | None, perimetre: str, fenetre: str,
            n_levels: int) -> pd.DataFrame:
    defs, stats = excess_report.gamma_context()
    if defs.empty or stats.empty:
        raise SystemExit("Il faut les fichiers Databento `definition` ET `statistics` "
                         "dans data/import/databento_raw/")
    all_days = sorted(d for d in excess_report._tick_days(symbol)
                      if date.fromisoformat(d).weekday() < 5)
    rate, cache, rows, skipped = rates.current_rate(), {}, [], 0
    for day in all_days:
        if (start and day < start) or (end and day > end):
            continue
        ts, px = excess_report.session_path(symbol, day)
        if len(px) < 1000:
            continue
        w0, w1 = window_bounds(day, fenetre)
        keep = (ts >= w0) & (ts < w1)
        if keep.sum() < 500:
            continue
        close, close_ts = excess_report._prev_close(symbol, day, all_days, cache)
        if close is None:
            skipped += 1
            continue
        chain = dbgamma.session_chain(stats, defs, day, close, close_ts, rate)
        levels = levelreact.session_levels(chain, w0, close, rate, perimetre, n_levels)
        if levels.empty:
            skipped += 1
            continue
        rows += levelreact.session_events(day, ts[keep], px[keep], levels)
    if skipped:
        print(f"{skipped} séance(s) sans clôture de la veille ou sans chaîne : écartées.")
    return pd.DataFrame(rows)


def _table(df: pd.DataFrame) -> str:
    return "```\n" + df.round(1).to_string() + "\n```"


def report(ev: pd.DataFrame, symbol: str, perimetre: str, fenetre: str, n_boot: int) -> str:
    lines = [f"# Niveaux GEX : signe et réaction — {symbol}",
             f"_{datetime.now(ET):%Y-%m-%d %H:%M}_", ""]
    if ev.empty:
        return "\n".join(lines + ["_(aucune séance exploitable)_"])
    real = ev[(ev["groupe"] != "témoin") & (ev["echelle"] == 0)]
    n_days = ev["day"].nunique()
    lines += [
        f"{n_days} séances, {len(real)} niveaux ({(real['groupe'] == 'positif').sum()} positifs, "
        f"{(real['groupe'] == 'négatif').sum()} négatifs). Périmètre des options : `{perimetre}` ; "
        f"fenêtre : {'séance cash 9h30-16h00 ET' if fenetre == 'cash' else 'séance Globex'}.", "",
        "Niveaux = les 5 strikes de plus fort |GEX net| à l'ouverture de la fenêtre (calls +, "
        "puts −, évalué à la clôture de la veille). **Réaction** : après le premier contact, le "
        "prix revient de R points avant de passer R points au-delà ; **traversée** : l'inverse ; "
        "**indécis** : ni l'un ni l'autre dans le délai. Seuils symétriques : une marche "
        "aléatoire donnerait 50 %. **Témoin** : les vrais niveaux décalés de ±30 / ±40 pts.", ""]
    res = levelreact.analyse(ev, n_boot)
    for (r, w, name), table in zip(levelreact.SCALES, res["tables"]):
        lines += [f"## Échelle {name}", "", _table(table), ""]
    tests = res["tests"]
    if not tests.empty:
        shown = tests.copy()
        shown["p"] = shown["p"].map(lambda x: f"{x:.2g}")
        shown["p_holm"] = shown["p_holm"].map(lambda x: f"{x:.2g}")
        lines += ["## Comparaisons (séances rééchantillonnées, correction de Holm)", "",
                  f"{len(shown)} comparaisons. `écart_pts_%` : différence des parts de "
                  "réaction (points de pourcentage). `tient` : significatif même après "
                  "correction ; `fragile` : significatif seul, pas après ; `pas démontré` : "
                  "compatible avec le hasard.", "",
                  _table(shown.set_index(["échelle", "comparaison"])), ""]
    lines += ["Lecture : l'affirmation « seuls les niveaux positifs font réagir » est étayée si "
              "positif − témoin est nettement > 0, négatif − témoin ≈ 0 ou < 0, et positif − "
              "négatif > 0 avec verdict `tient`. Si positif ≈ témoin, ce sont les niveaux GEX "
              "eux-mêmes qui n'apportent rien au-delà du bruit du marché."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--perimetre", default="tdb", choices=("tdb", "tdb2", "0dte", "45j"),
                    help="options retenues (défaut tdb : celles du tableau de bord)")
    ap.add_argument("--fenetre", default="cash", choices=("cash", "globex"))
    ap.add_argument("--niveaux", type=int, default=5)
    ap.add_argument("--boot", type=int, default=2000, help="tirages de séances")
    args = ap.parse_args(argv)
    ev = collect(args.symbol, args.start, args.end, args.perimetre, args.fenetre, args.niveaux)
    md = report(ev, args.symbol, args.perimetre, args.fenetre, args.boot)
    outdir = SETTINGS.data_dir / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"level_reaction_{args.symbol}_{datetime.now(ET):%Y-%m-%d}.md"
    path.write_text(md, encoding="utf-8")
    print(md)
    print(f"\n→ {path}")


if __name__ == "__main__":
    main()
