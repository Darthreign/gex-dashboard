"""Validation de la pression MOC estimée (cf. gex/moc.py) sur TES données.

    python scripts/moc_report.py            # NQ et ES
    python scripts/moc_report.py NQ --days 60

Pour chaque séance où les snapshots de chaîne et les ticks du future
existent : estimation refaite telle qu'elle aurait été lue à 15h45 ET
(snapshots les plus proches de 15h45 sur toute la famille, flux d'options
preneur arrêté à cette heure — aucune donnée postérieure), puis mouvement
réalisé du future entre 15h50 (premières publications d'imbalance) et 16h00.

Question posée : le SIGNE de la pression anticipe-t-il le sens de la
dernière dizaine de minutes, et sa TAILLE l'ampleur ? Réponses : taux de
bon sens (global et sur les pressions au-dessus de la médiane),
corrélations, mouvement moyen par tercile de pression, intervalle à 90 %
de la corrélation par bootstrap sur les séances.

Historique écrit dans `data/reports/moc_history_<SYM>.csv` (affiché en bas
de la page /moc), rapport dans `data/reports/moc_<SYM>_<date>.md`.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import moc, positioning, store
from .config import SETTINGS
from .metrics import ET

log = logging.getLogger(__name__)

ASOF = "154500"
MAX_SNAPSHOT_GAP_S = 600
MIN_SESSIONS = 10


def history_path(symbol: str) -> Path:
    return SETTINGS.data_dir / "reports" / f"moc_history_{symbol}.csv"


def _secs(stem: str) -> int | None:
    try:
        return int(stem[0:2]) * 3600 + int(stem[2:4]) * 60 + int(stem[4:6])
    except (ValueError, IndexError):
        return None


def snapshot_at(symbol: str, day: str, target: str = ASOF,
                max_gap_s: int = MAX_SNAPSHOT_GAP_S) -> tuple[pd.DataFrame, datetime] | None:
    """Snapshot le plus proche de `target`, sans jamais le dépasser (pas de
    regard vers l'avenir), à moins de `max_gap_s` secondes."""
    root = SETTINGS.data_dir / "snapshots" / symbol / day
    goal = _secs(target)
    best = None
    for f in sorted(root.glob("*.parquet")) if root.exists() else []:
        s = _secs(f.stem)
        if s is not None and s <= goal and goal - s <= max_gap_s:
            best = (f, s)
    if best is None:
        return None
    f, s = best
    h, rem = divmod(s, 3600)
    when = datetime.fromisoformat(day).replace(hour=h, minute=rem // 60, second=rem % 60, tzinfo=ET)
    return pd.read_parquet(f), when


def flow_until(symbol: str, day: str, until: datetime) -> pd.Series:
    p = store.load_optprints(symbol, day, columns=["contract", "side", "size", "spread", "ttype"])
    if p.empty:
        return pd.Series(dtype=float)
    return positioning.taker_flow(p[p["ts"] < until.timestamp()])


def _price_at(ticks: pd.DataFrame, when: datetime) -> float | None:
    """Dernier prix échangé avant `when`."""
    before = ticks[ticks["ts"] < when.timestamp()]
    return float(before["price"].iloc[-1]) if not before.empty else None


def _ticks(symbol: str, day: str) -> pd.DataFrame:
    df = store.load_ticks(symbol, day)
    if df.empty:
        p = SETTINGS.data_dir / "import" / "ticks_full" / symbol / f"{day}.parquet"
        df = pd.read_parquet(p) if p.exists() else pd.DataFrame()
    return df.sort_values("ts", kind="stable") if not df.empty else df


def session_row(symbol: str, day: str) -> dict | None:
    fam = moc.FAMILIES[symbol]
    chains, flows, asof = [], {}, None
    for s in fam["chains"]:
        got = snapshot_at(s, day)
        if got is None:
            continue
        df, when = got
        if df.empty or "spot" not in df:
            continue
        asof = when if asof is None else max(asof, when)
        chains.append(moc.ChainInput(s, df, float(df["spot"].iloc[0])))
        flows[s] = flow_until(s, day, datetime.fromisoformat(day).replace(
            hour=15, minute=45, tzinfo=ET))
    if not chains:
        return None
    ticks = _ticks(symbol, day)
    if ticks.empty:
        return None
    d0 = datetime.fromisoformat(day)
    t1545, t1550, t1600 = (d0.replace(hour=h, minute=m, tzinfo=ET)
                           for h, m in ((15, 45), (15, 50), (16, 0)))
    fut, p50, p00 = (_price_at(ticks, w) for w in (t1545, t1550, t1600))
    if fut is None or p50 is None or p00 is None:
        return None
    idx = next((c.spot for c in chains if c.symbol == fam["index"]), None)
    ret = moc.day_return(idx, store.previous_close_spot(fam["index"], day))
    est = moc.estimate(symbol, chains, flows, fut, ret, t1545, with_profile=False)
    return {"day": day, "asof": asof.strftime("%H:%M:%S"), "chains": "+".join(est["options"]),
            "contracts": est["contracts"], "options": est["options_total"],
            "expiring": est["expiring_total"], "charm": est["charm_total"],
            "cash_unwind": est["cash_unwind_total"],
            "contracts_with_cash": (est["total"] + est["cash_unwind_total"])
            / (fam["fut_mult"] * fut),
            "letf": est["letf"]["total"], "day_return": ret,
            "fut_1545": fut, "px_1550": p50, "px_1600": p00,
            "move_pts": p00 - p50, "move_1545_pts": p00 - fut}


def available_days(symbol: str) -> list[str]:
    fam = moc.FAMILIES[symbol]
    snap = set().union(*(store.snapshot_days(s) for s in fam["chains"]))
    return sorted(d for d in snap if datetime.fromisoformat(d).weekday() < 5)


def build_history(symbol: str, days: list[str]) -> pd.DataFrame:
    rows = []
    for d in days:
        try:
            r = session_row(symbol, d)
        except Exception as e:  # noqa: BLE001 — une séance illisible n'arrête pas le rapport
            log.warning("MOC %s %s ignoré : %s", symbol, d, e)
            continue
        if r is not None and r["contracts"] is not None:
            rows.append(r)
    return pd.DataFrame(rows)


def stats(h: pd.DataFrame, n_boot: int = 2000, seed: int = 0) -> dict:
    x = h["contracts"].to_numpy(dtype=float)
    y = h["move_pts"].to_numpy(dtype=float)
    keep = (x != 0) & (y != 0)
    hit = float((np.sign(x[keep]) == np.sign(y[keep])).mean()) if keep.any() else float("nan")
    big = keep & (np.abs(x) >= np.median(np.abs(x)))
    hit_big = float((np.sign(x[big]) == np.sign(y[big])).mean()) if big.any() else float("nan")
    rng = np.random.default_rng(seed)
    n = len(x)
    boots = []
    for _ in range(n_boot):
        i = rng.integers(0, n, n)
        if np.std(x[i]) > 0 and np.std(y[i]) > 0:
            boots.append(np.corrcoef(x[i], y[i])[0, 1])
    terc = pd.qcut(pd.Series(x).rank(method="first"), 3, labels=["vente", "neutre", "achat"])
    by = pd.DataFrame({"tercile": terc, "move": y, "contracts": x}).groupby(
        "tercile", observed=True).agg(n=("move", "size"), pression=("contracts", "mean"),
                                      mouvement=("move", "mean"))
    return {
        "n": n, "hit": hit, "n_hit": int(keep.sum()), "hit_big": hit_big, "n_big": int(big.sum()),
        "pearson": float(pd.Series(x).corr(pd.Series(y))),
        "spearman": float(pd.Series(x).corr(pd.Series(y), method="spearman")),
        "ci90": (float(np.percentile(boots, 5)), float(np.percentile(boots, 95))) if boots else None,
        "by_tercile": by,
    }


def report(symbol: str, max_days: int | None = None) -> dict:
    days = available_days(symbol)
    if max_days:
        days = days[-max_days:]
    h = build_history(symbol, days)
    path = history_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not h.empty:
        h.to_csv(path, index=False)
    out = {"symbol": symbol, "sessions": len(h), "history": h}
    if len(h) < MIN_SESSIONS:
        out["error"] = f"{len(h)} séances exploitables : trop peu pour conclure"
        return out
    out["stats"] = stats(h)
    # variante incluant le débouclage cash des ITM (hypothèse forte, hors total)
    if h["cash_unwind"].abs().sum() > 0:
        out["stats_cash"] = stats(h.assign(contracts=h["contracts_with_cash"]))
    return out


def to_markdown(r: dict) -> str:
    lines = [f"# Pression MOC estimée — {r['symbol']}", ""]
    if "error" in r:
        return "\n".join(lines + [r["error"]])
    s = r["stats"]
    ci = s["ci90"]
    lines += [
        f"Séances : {r['sessions']} · estimation lue à 15h45 ET, mouvement mesuré 15h50 → 16h00",
        "",
        f"- Bon sens : **{s['hit']:.0%}** sur {s['n_hit']} séances "
        f"(pressions ≥ médiane : {s['hit_big']:.0%} sur {s['n_big']})",
        f"- Corrélation pression / mouvement : Pearson {s['pearson']:+.2f}, "
        f"Spearman {s['spearman']:+.2f}"
        + (f", intervalle à 90 % [{ci[0]:+.2f} ; {ci[1]:+.2f}]" if ci else ""),
        "",
        "Lecture : un bon sens nettement au-dessus de 50 % ET un intervalle de "
        "corrélation qui exclut 0 sont nécessaires avant de s'en servir.",
        "",
    ]
    if "stats_cash" in r:
        c = r["stats_cash"]
        lines += [f"- Variante avec débouclage cash des ITM : bon sens {c['hit']:.0%}, "
                  f"Pearson {c['pearson']:+.2f}, Spearman {c['spearman']:+.2f}", ""]
    lines += [
        "## Mouvement moyen par tercile de pression", "",
        "```\n" + s["by_tercile"].round(2).to_string() + "\n```", "",
        "## Séances", "",
        "```\n" + r["history"][["day", "asof", "chains", "contracts", "move_pts",
                                "move_1545_pts"]].round(2).to_string(index=False) + "\n```",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbols", nargs="*", default=["NQ", "ES"])
    ap.add_argument("--days", type=int, default=None, help="limiter aux N dernières séances")
    args = ap.parse_args(argv)
    outdir = SETTINGS.data_dir / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    for sym in args.symbols:
        md = to_markdown(report(sym.upper(), args.days))
        path = outdir / f"moc_{sym.upper()}_{datetime.now(ET):%Y-%m-%d}.md"
        path.write_text(md, encoding="utf-8")
        print(md)
        print(f"\n→ {path}\n")
