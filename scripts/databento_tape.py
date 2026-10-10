"""Tape d'options NQ reconstruit depuis l'historique Databento, puis vérifié
contre le tape live.

    python scripts/databento_tape.py                  # reconstruit tout ce qui est téléchargé
    python scripts/databento_tape.py --start 2026-09-01 --end 2026-10-09
    python scripts/databento_tape.py --compare        # concordance avec le tape live

Entrées : fichiers bruts Databento GLBX.MDP3 (`definition` et `trades` des
options NQ) dans data/import/databento_raw/ (*.dbn.zst, schéma lu dans le
fichier), ticks NQ de l'utilisateur pour le prix du future (capture live,
sinon data/import/ticks_full). Logique : gex/dbtape.py.

Sortie : data/tape_databento/NQ/<séance>.parquet — À PART du tape live,
`mult_source = "databento"` (vide pour une séance de roll incertaine).
Données Databento : usage personnel, jamais commitées.

--compare : sur les séances présentes dans les deux tapes (live étiqueté),
corrélation du flux net minute par minute, rapport des flux bruts, et
concordance des COULEURS du voyant (excess.flow_color) minute par minute.
Rapport : data/reports/tape_databento_validation_NQ_<date>.md. Le tape
reconstruit ne sert au rapport d'excès (--flux databento) que si la
concordance est bonne.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gex import dbtape, excess  # noqa: E402
from gex.config import SETTINGS  # noqa: E402
from gex.metrics import ET  # noqa: E402

import excess_report  # noqa: E402

log = logging.getLogger("databento_tape")
SYMBOL = "NQ"
CHUNK = 500_000
MAX_SPOT_AGE_S = 300.0          # prix du future plus vieux : transaction non valorisée


def raw_dir() -> Path:
    return SETTINGS.data_dir / "import" / "databento_raw"


def out_dir() -> Path:
    return SETTINGS.data_dir / "tape_databento" / SYMBOL


def _stores():
    import databento as db
    files = sorted(raw_dir().glob("*.dbn.zst")) + sorted(raw_dir().glob("*.dbn"))
    out = {"definition": [], "trades": []}
    for f in files:
        st = db.DBNStore.from_file(f)
        schema = str(getattr(st.schema, "value", st.schema))
        if st.dataset != "GLBX.MDP3" or schema not in out:
            log.info("%s ignoré (%s, %s)", f.name, st.dataset, schema)
            continue
        out[schema].append((f, st))
    return out


class SpotCache:
    """Prix du future au moment d'une transaction : dernier tick de la
    séance, s'il a moins de MAX_SPOT_AGE_S secondes."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def __call__(self, session: str, epoch: np.ndarray) -> np.ndarray:
        if session not in self._cache:
            if len(self._cache) > 4:
                self._cache.pop(next(iter(self._cache)))
            self._cache[session] = excess_report.session_path(SYMBOL, session)
        ts, px = self._cache[session]
        if not len(px):
            return np.full(len(epoch), np.nan)
        j = np.searchsorted(ts, epoch, side="right") - 1
        jj = np.clip(j, 0, len(px) - 1)
        fresh = (j >= 0) & (epoch - ts[jj] <= MAX_SPOT_AGE_S)
        return np.where(fresh, px[jj], np.nan)


def build(start: str | None = None, end: str | None = None, rate: float | None = None) -> dict:
    stores = _stores()
    if not stores["definition"] or not stores["trades"]:
        raise SystemExit(f"Il faut des fichiers `definition` ET `trades` dans {raw_dir()}")
    # par morceaux : un an de définitions quotidiennes pèse plusieurs millions
    # de lignes ; on ne garde que les options, une ligne par instrument
    parts = [dbtape.definitions_table(chunk)
             for _, st in stores["definition"]
             for chunk in st.to_df(map_symbols=False, count=CHUNK) if not chunk.empty]
    defs = pd.concat(parts, ignore_index=True).drop_duplicates("instrument_id", keep="last")
    if rate is None:
        from gex import rates
        rate = rates.current_rate()
    spot = SpotCache()
    partials = []
    for f, st in stores["trades"]:
        log.info("Lecture %s", f.name)
        for chunk in st.to_df(map_symbols=False, count=CHUNK):
            if chunk.empty:
                continue
            sess = dbtape.session_of(pd.Series(chunk.index, index=chunk.index)).to_numpy()
            keep = np.ones(len(chunk), bool)
            if start:
                keep &= sess >= start
            if end:
                keep &= sess <= end
            if keep.any():
                partials.append(dbtape.trades_to_bars(chunk[keep], defs, spot, rate))
    sessions = dbtape.finalize(partials)
    out_dir().mkdir(parents=True, exist_ok=True)
    for s, bars in sessions.items():
        bars.to_parquet(out_dir() / f"{s}.parquet", index=False)
    return {s: (len(b), b["mult_source"].notna().all(), float(b["other_share"].iloc[0]))
            for s, b in sessions.items()}


# ------------------------------------------------------------------ comparaison

def _flow(day: str, flux: str, px_ts, px) -> pd.DataFrame:
    f = excess_report.session_flow(SYMBOL, day, px_ts, px, flux)
    return f[f["labeled"].astype(bool)] if not f.empty else f


def compare_session(day: str) -> dict | None:
    px_ts, px = excess_report.session_path(SYMBOL, day)
    live, dbt = _flow(day, "live", px_ts, px), _flow(day, "databento", px_ts, px)
    if live.empty or dbt.empty:
        return None
    m = live[["ts", "net", "gross"]].merge(dbt[["ts", "net", "gross"]], on="ts",
                                            suffixes=("_live", "_db"))
    out = {"seance": day, "minutes_communes": len(m),
           "corr_net": float(m["net_live"].corr(m["net_db"])) if len(m) > 2 else np.nan,
           "brut_db_sur_live": float((m["gross_db"] / m["gross_live"]).replace(
               [np.inf, -np.inf], np.nan).median())}
    # couleurs du voyant minute par minute (sens +1), même lecture que le rapport
    colors = []
    lo, hi = max(live["ts"].min(), dbt["ts"].min()) + 15 * 60, min(live["ts"].max(),
                                                                   dbt["ts"].max())
    for t in np.arange(lo, hi + 60, 60.0):
        c = []
        for f in (live, dbt):
            c.append(excess.flow_color(f["ts"].to_numpy(float), f["net"].to_numpy(float),
                                       f["gross"].to_numpy(float),
                                       np.ones(len(f), bool), t, 1)[0])
        colors.append(c)
    cdf = pd.DataFrame(colors, columns=["live", "databento"])
    out["minutes_voyant"] = len(cdf)
    out["meme_couleur_%"] = 100 * float((cdf["live"] == cdf["databento"]).mean()) \
        if len(cdf) else np.nan
    out["_colors"] = cdf
    return out


def compare() -> str:
    days = sorted(excess_report.tape_days(SYMBOL, "databento")
                  & excess_report.tape_days(SYMBOL, "live"))
    rows = [r for r in (compare_session(d) for d in days) if r]
    lines = [f"# Tape Databento contre tape live — {SYMBOL}",
             f"_{datetime.now(ET):%Y-%m-%d %H:%M} — séances communes (barres étiquetées des "
             "deux côtés)._", ""]
    if not rows:
        return "\n".join(lines + ["_(aucune séance commune)_"])
    tab = pd.DataFrame([{k: v for k, v in r.items() if k != "_colors"} for r in rows])
    allc = pd.concat([r["_colors"] for r in rows], ignore_index=True)
    conf = pd.crosstab(allc["live"], allc["databento"], margins=True)
    lines += ["## Par séance", "", "```\n" + tab.round(3).to_string(index=False) + "\n```", "",
              f"Même couleur, toutes séances : **{100 * (allc['live'] == allc['databento']).mean():.1f} %** "
              f"sur {len(allc)} minutes.", "",
              "## Couleurs : live (lignes) contre Databento (colonnes)", "",
              "```\n" + conf.to_string() + "\n```", "",
              "Lecture : `corr_net` proche de 1 et une forte concordance des couleurs "
              "(surtout rouge / vert) sont nécessaires avant d'utiliser `--flux databento`. "
              "Une corrélation NÉGATIVE signalerait un côté agresseur inversé. "
              "`brut_db_sur_live` éloigné de 1 : écart d'échelle (delta, multiplicateur, "
              "couverture des transactions) — sans effet sur les couleurs s'il est constant."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", help="première séance (AAAA-MM-JJ)")
    ap.add_argument("--end", help="dernière séance (AAAA-MM-JJ)")
    ap.add_argument("--rate", type=float, default=None, help="taux sans risque (défaut : SOFR)")
    ap.add_argument("--compare", action="store_true",
                    help="seulement comparer au tape live (sans reconstruire)")
    args = ap.parse_args(argv)
    if not args.compare:
        res = build(args.start, args.end, args.rate)
        ok = sum(1 for _, lab, _ in res.values() if lab)
        print(f"{len(res)} séances reconstruites dans {out_dir()} ; étiquetées : {ok} ; "
              f"non étiquetées (roll ou autre sous-jacent) : {len(res) - ok}")
    md = compare()
    outdir = SETTINGS.data_dir / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"tape_databento_validation_{SYMBOL}_{datetime.now(ET):%Y-%m-%d}.md"
    path.write_text(md, encoding="utf-8")
    print(md)
    print(f"\n→ {path}")


if __name__ == "__main__":
    main()
