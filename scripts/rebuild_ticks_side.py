"""Ajoute le côté agresseur aux séances de ticks_full qui n'en ont pas.

    python scripts/rebuild_ticks_side.py                    # tout ce qui manque
    python scripts/rebuild_ticks_side.py --days 2024-08-06 2026-08-04 --dry-run

Lit les fichiers bruts Databento déjà sur le disque (aucun achat) et rattache
`side` à chaque ligne de data/import/ticks_full/NQ, par (ts, price, volume) et
rang d'apparition : des transactions distinctes partagent couramment ces trois
valeurs. Lignes, ordre et valeurs d'origine sont conservés.

Écrit à part dans data/import/ticks_full_side/NQ/ ; ticks_full n'est pas modifié.
Une séance dont une part trop grande reste sans correspondance n'est pas écrite.
Données Databento : usage personnel, jamais commitées.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gex.config import SETTINGS  # noqa: E402
from gex.metrics import ET  # noqa: E402
from gex.store import _write_atomic  # noqa: E402

MIN_MATCH = 0.999
CHUNK = 2_000_000
# Databento : côté de l'agresseur d'une transaction (A = vendeur, B = acheteur).
SIDE_MAP = {b"A": "SELL", b"B": "BUY"}


def session_days(ts_s: np.ndarray) -> np.ndarray:
    et = pd.to_datetime(ts_s, unit="s", utc=True).tz_convert(ET)
    return (et + pd.Timedelta(hours=6)).strftime("%Y-%m-%d").to_numpy()


def _keys(ts: np.ndarray, price: np.ndarray, volume: np.ndarray) -> pd.DataFrame:
    """Clé de rattachement : microseconde, centième de point, taille, rang."""
    k = pd.DataFrame({"t": np.rint(ts * 1e6).astype("int64"),
                      "p": np.rint(price * 100).astype("int64"),
                      "v": volume.astype("int64")})
    k["n"] = k.groupby(["t", "p", "v"], sort=False).cumcount()
    return k


def attach_side(ticks: pd.DataFrame, raw: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """`raw` : ts (s), price, size, side (BUY/SELL/UNDEFINED), dans l'ordre du
    fichier. Renvoie les ticks avec `side` et la part de lignes rattachées."""
    a = _keys(ticks["ts"].to_numpy(), ticks["price"].to_numpy(), ticks["volume"].to_numpy())
    b = _keys(raw["ts"].to_numpy(), raw["price"].to_numpy(), raw["size"].to_numpy())
    b["side"] = raw["side"].to_numpy()
    b = b.drop_duplicates(["t", "p", "v", "n"])
    side = a.merge(b, on=["t", "p", "v", "n"], how="left")["side"]
    out = ticks.copy()
    out["side"] = side.fillna("UNDEFINED").to_numpy()
    return out, float(side.notna().mean()) if len(side) else 0.0


def raw_files() -> list[Path]:
    imp = SETTINGS.data_dir / "import"
    return sorted(list(imp.glob("*.trades.dbn.zst")) + list(imp.glob("databento_NQ_*.dbn.zst"))
                  + list((imp / "databento_raw").glob("NQ_v0_trades_*.dbn.zst")),
                  key=lambda f: _start_of(f))


def _start_of(f: Path) -> int:
    import databento as db
    return db.DBNStore.from_file(f).metadata.start


def missing_days() -> list[str]:
    root = SETTINGS.data_dir / "import" / "ticks_full" / "NQ"
    return sorted(f.stem for f in root.glob("*.parquet")
                  if "side" not in pq.read_schema(f).names)


def _process(day: str, chunks: list[pd.DataFrame], out_dir: Path, dry: bool) -> tuple[str, float, int]:
    src = SETTINGS.data_dir / "import" / "ticks_full" / "NQ" / f"{day}.parquet"
    ticks = pd.read_parquet(src)
    done, share = attach_side(ticks, pd.concat(chunks, ignore_index=True))
    if share >= MIN_MATCH and not dry:
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_atomic(done, out_dir / f"{day}.parquet")
    counts = done["side"].value_counts().to_dict()
    print(f"{day} {len(done):>8,} lignes, rattachées {share:7.3%}, {counts}"
          + ("" if share >= MIN_MATCH else "  -> NON ÉCRITE"), flush=True)
    return day, share, len(done)


def main(argv: list[str] | None = None) -> int:
    import databento as db
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    out_dir = SETTINGS.data_dir / "import" / "ticks_full_side" / "NQ"
    wanted = set(args.days or missing_days())
    todo = {d for d in wanted if not (out_dir / f"{d}.parquet").exists()} if not args.dry_run else wanted
    print(f"{len(todo)} séances à traiter sur {len(wanted)}", flush=True)
    pending: dict[str, list[pd.DataFrame]] = defaultdict(list)
    results = []
    for f in raw_files():
        if not todo:
            break
        print(f"Lecture {f.name}", flush=True)
        last_seen = None
        for arr in db.DBNStore.from_file(f).to_ndarray(count=CHUNK):
            ts = arr["ts_event"].astype("int64") / 1e9
            days = session_days(ts)
            frame = pd.DataFrame({"ts": ts, "price": arr["price"] / 1e9, "size": arr["size"],
                                  "side": [SIDE_MAP.get(bytes(s), "UNDEFINED") for s in arr["side"]],
                                  "day": days})
            for day, g in frame.groupby("day", sort=False):
                if day in todo:
                    pending[day].append(g.drop(columns="day"))
            last_seen = max(days) if len(days) else last_seen
            # Une séance déjà dépassée dans ce fichier est complète.
            for day in sorted(d for d in pending if last_seen and d < last_seen):
                results.append(_process(day, pending.pop(day), out_dir, args.dry_run))
                todo.discard(day)
            if not todo and not pending:
                break
    for day in sorted(pending):
        results.append(_process(day, pending[day], out_dir, args.dry_run))
        todo.discard(day)
    bad = [r[0] for r in results if r[1] < MIN_MATCH]
    print(f"\nTraitées : {len(results)} ; non écrites : {len(bad)} {bad[:20]} ; sans fichier brut : "
          f"{len(todo)} {sorted(todo)[:20]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
