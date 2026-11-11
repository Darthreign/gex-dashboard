"""Murs de gamma avant / après le correctif du 10/10/2026, sur TES snapshots.

    python scripts/compare_walls.py                 # NQ, ES, SPX, NDX — 60 dernières séances
    python scripts/compare_walls.py NQ --days 20 --bucket 0DTE

Pour chaque séance enregistrée (dernier snapshot du jour), compare :
- l'ANCIENNE définition (GEX net le plus positif au-dessus du spot / le plus
  négatif en dessous, conservée sous `net_gex_max_above` / `net_gex_min_below`) ;
- la NOUVELLE (concentration de gamma call / put séparée : `call_wall` /
  `put_support`).

Lecture seule : n'écrit rien, ne change aucun réglage. Mesure l'écart entre
les deux définitions, PAS une amélioration de trading : seul un backtest peut
dire si l'une prédit mieux que l'autre.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gex import metrics, store  # noqa: E402

SYMBOLS = ("NQ", "ES", "SPX", "NDX")


def compare(symbol: str, days: int = 60, bucket: str = "Tout") -> pd.DataFrame:
    """Une ligne par séance : ancien et nouveau Call Wall / Put Support."""
    keys = [symbol, f"{symbol}_RT"]
    rows = []
    for key in keys:
        for day in store.snapshot_days(key)[-days:]:
            df = store.load_last_snapshot(key, day)
            if df is None or df.empty or "spot" not in df:
                continue
            spot = float(df["spot"].iloc[0])
            structural = store.previous_close_spot(symbol, day) or spot
            res = metrics.compute_levels(df, structural, spot, bucket=bucket,
                                         today=pd.Timestamp(day).date())
            k = res["keys"]
            rows.append({"source": key, "day": day, "spot": round(spot, 2),
                         "call_wall_net (avant)": k.get("net_gex_max_above"),
                         "call_wall (après)": k.get("call_wall"),
                         "put_support_net (avant)": k.get("net_gex_min_below"),
                         "put_support (après)": k.get("put_support")})
    return pd.DataFrame(rows)


def summary(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"séances": 0}

    def changed(a, b):
        return int(((t[a] != t[b]) & ~(t[a].isna() & t[b].isna())).sum())
    return {"séances": len(t),
            "Call Wall changé": changed("call_wall_net (avant)", "call_wall (après)"),
            "Put Support changé": changed("put_support_net (avant)", "put_support (après)"),
            "écart médian Call Wall (pts)": float((t["call_wall (après)"]
                                                   - t["call_wall_net (avant)"]).abs().median()),
            "écart médian Put Support (pts)": float((t["put_support (après)"]
                                                     - t["put_support_net (avant)"]).abs().median())}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbols", nargs="*", default=list(SYMBOLS))
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--bucket", default="Tout", help="0DTE | Semaine | Mois | Tout")
    args = ap.parse_args(argv)
    for sym in args.symbols:
        t = compare(sym.upper(), args.days, args.bucket)
        print(f"\n## {sym.upper()} (périmètre {args.bucket})\n")
        print(t.to_string(index=False) if not t.empty else "(aucun snapshot)")
        print("\n", summary(t))


if __name__ == "__main__":
    main()
