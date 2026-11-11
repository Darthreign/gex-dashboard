"""Backtest : ancienne (GEX net) contre nouvelle (concentration call / put)
définition du Call Wall et du Put Support, sur TES séances.

    python scripts/backtest_walls.py                       # NQ et ES, Tout / Semaine / 0DTE
    python scripts/backtest_walls.py NQ --buckets Tout
    python scripts/backtest_walls.py SPX --chain SPX_RT    # chaîne native OPRA de SPX

Mesure (gex/backtest.py, inchangée) : niveaux du DÉBUT de séance, parcours en
bougies 1 min ; un niveau est « testé » si le prix le touche, « cassé » s'il
est dépassé de plus de 0,15 %, « tenu » s'il est testé sans être cassé. Le
taux de tenue ne se calcule QUE sur les séances où le niveau a été testé.

Deux tableaux par périmètre :
- toutes les séances ;
- seulement les séances où les deux définitions donnent un strike différent :
  c'est là, et seulement là, qu'elles peuvent se départager.

Lecture seule ; rapport dans data/reports/backtest_murs_<SYM>_<date>.md.
Ce n'est pas une performance de trading : un taux de tenue sur quelques
dizaines de séances a une marge d'erreur large (colonne n_tested).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gex import backtest  # noqa: E402
from gex.config import SETTINGS  # noqa: E402
from gex.metrics import ET  # noqa: E402

COLS = ["name", "n_sessions", "n_tested", "test_rate", "hold_rate", "break_rate",
        "close_beyond_rate", "median_move_after_break", "median_distance_pct"]


def _table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_(aucune séance exploitable)_"
    out = df[[c for c in COLS if c in df]].copy()
    for c in ("test_rate", "hold_rate", "break_rate", "close_beyond_rate",
              "median_move_after_break", "median_distance_pct"):
        if c in out:
            out[c] = (out[c] * 100).round(1)
    return "```\n" + out.to_string(index=False) + "\n```"


def report(symbol: str, buckets: list[str], chain: str | None = None) -> str:
    lines = [f"# Backtest des murs — {symbol}"
             + (f" (chaîne {chain})" if chain else ""),
             f"_{datetime.now(ET):%Y-%m-%d %H:%M} — taux en %, distance = |niveau − ouverture| "
             "en % de l'ouverture ; tenue et cassure rapportées aux séances testées._", ""]
    for b in buckets:
        df = backtest.compare_walls(symbol, bucket=b, chain_key=chain)
        lines += [f"## Périmètre {b}", ""]
        if df.empty:
            lines += ["_(aucune séance avec chaîne et bougies 1 min)_", ""]
            continue
        n_days = df["day"].nunique()
        diff_days = df.loc[df["definitions_differ"], "day"].nunique()
        lines += [f"Séances : {n_days} · définitions différentes au moins une fois : "
                  f"{diff_days}", "",
                  "Toutes les séances :", "", _table(backtest.summarize_walls(df)), "",
                  "Séances où les deux définitions diffèrent (les seules qui départagent) :",
                  "", _table(backtest.summarize_walls(df[df["definitions_differ"]])), ""]
    lines += ["Lecture : comparer d'abord `n_tested` (trop peu de séances = pas de "
              "conclusion), puis la distance (un niveau plus proche est plus souvent "
              "testé), et seulement ensuite la tenue / cassure."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbols", nargs="*", default=["NQ", "ES"])
    ap.add_argument("--buckets", default="Tout,Semaine,0DTE")
    ap.add_argument("--chain", default=None,
                    help="clé de chaîne si différente du symbole (ex. SPX_RT, NDX_RT)")
    args = ap.parse_args(argv)
    outdir = SETTINGS.data_dir / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    for sym in args.symbols:
        md = report(sym.upper(), [b.strip() for b in args.buckets.split(",") if b.strip()],
                    args.chain)
        path = outdir / f"backtest_murs_{sym.upper()}_{datetime.now(ET):%Y-%m-%d}.md"
        path.write_text(md, encoding="utf-8")
        print(md)
        print(f"\n→ {path}\n")


if __name__ == "__main__":
    main()
