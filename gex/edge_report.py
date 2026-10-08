"""Rapport d'edge de la lecture /scalp v2 (cf. gex/edge.py) sur TES données.

    python scripts/edge_report.py            # NQ et ES
    python scripts/edge_report.py NQ --days 120

Pour chaque séance disponible (ticks live `data/ticks/`, sinon l'import
historique `data/import/ticks_full/`), en séance cash 9h30-16h00 ET :
- mouvement attendu (EM) : straddle du premier snapshot de chaîne du jour,
  sinon 0,5 × la médiane des étendues des 10 séances précédentes ;
- Gamma Flip et GEX 0DTE : historique des métriques (data/history) ;
- absorptions : détectées sur les ticks bruts (si les tailles affichées y
  sont) ;
- flux de couverture : barres de tape signé (si capturées ce jour-là).

Puis : grille de seuils choisie sur les 60 % de séances les plus anciennes
(critère prudent : borne basse de l'intervalle à 90 %), et résultat HORS
ÉCHANTILLON sur les 40 % suivantes, comparé au rejet naïf et au hasard.
Les paramètres retenus sont écrits dans `data/reports/edge_params_<SYM>.json`
avec `validated: true` seulement si, hors échantillon, l'espérance du setup
est positive avec une borne basse > 0, supérieure au hasard et au moins
égale au rejet naïf.
Le bandeau /scalp v2 ne s'en sert que dans ce cas.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import edge, metrics, store
from .config import SETTINGS
from .metrics import ET

log = logging.getLogger(__name__)

MIN_TRAIN_TRADES = 30
TRAIN_FRACTION = 0.6
RTH_START_MIN, RTH_END_MIN = 9 * 60 + 30, 16 * 60


def params_path(symbol: str) -> Path:
    return SETTINGS.data_dir / "reports" / f"edge_params_{symbol}.json"


def _session_ticks(symbol: str, day: str) -> pd.DataFrame:
    df = store.load_ticks(symbol, day)
    if df.empty:
        p = SETTINGS.data_dir / "import" / "ticks_full" / symbol / f"{day}.parquet"
        df = pd.read_parquet(p) if p.exists() else pd.DataFrame()
    if df.empty:
        return df
    et = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(ET)
    mins = et.dt.hour * 60 + et.dt.minute
    rth = (et.dt.strftime("%Y-%m-%d") == day) & (mins >= RTH_START_MIN) & (mins < RTH_END_MIN)
    return df[rth.to_numpy()].sort_values("ts", kind="stable").reset_index(drop=True)


def available_days(symbol: str) -> list[str]:
    live = set(store.tick_days(symbol))
    root = SETTINGS.data_dir / "import" / "ticks_full" / symbol
    hist = {p.stem for p in root.glob("*.parquet")} if root.exists() else set()
    return sorted(d for d in live | hist if date.fromisoformat(d).weekday() < 5)


def _straddle_em(symbol: str, day: str) -> float | None:
    try:
        df = store.load_first_snapshot(symbol, day)
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty or "spot" not in df:
        return None
    return metrics.expected_move(df, float(df["spot"].iloc[0]))


_UNIT = {"s": 1.0, "ms": 1e3, "us": 1e6, "ns": 1e9}


def _epoch(ts: pd.Series) -> np.ndarray:
    """Secondes Unix quelle que soit la résolution (pandas 3 : souvent µs)."""
    return ts.astype("int64").to_numpy() / _UNIT[ts.dt.unit]


def _context(symbol: str) -> pd.DataFrame:
    h = store.load_history(symbol)
    if h.empty:
        return pd.DataFrame(columns=["ts", "zg", "gex0"])
    ts = pd.to_datetime(h["timestamp"])
    ts = ts.dt.tz_localize(ET) if ts.dt.tz is None else ts.dt.tz_convert(ET)
    out = pd.DataFrame({"ts": _epoch(ts),
                        "zg": h.get("zero_gamma"),
                        "gex0": h.get("net_gex_0dte", h.get("net_gex"))})
    return out.sort_values("ts").reset_index(drop=True)


def _flow(symbol: str, day: str, window_min: int = 5) -> pd.DataFrame | None:
    tape = store.load_tape(symbol, day)
    cols = ["hedge_call_buy", "hedge_call_sell", "hedge_put_buy", "hedge_put_sell"]
    if tape.empty or not all(c in tape for c in cols):
        return None
    ts = pd.to_datetime(tape["timestamp"])
    ts = ts.dt.tz_localize(ET) if ts.dt.tz is None else ts.dt.tz_convert(ET)
    net = tape[cols].sum(axis=1)
    gross = tape[cols].abs().sum(axis=1)
    return pd.DataFrame({"ts": _epoch(ts) + 60,
                         "net": net.rolling(window_min, min_periods=1).sum().to_numpy() / 1e6,
                         "gross": gross.rolling(window_min, min_periods=1).sum().to_numpy() / 1e6})


def _absorptions(symbol: str, ticks: pd.DataFrame) -> list[dict]:
    from . import iceberg as ib
    if not {"prev_bid_size", "prev_ask_size"} <= set(ticks.columns):
        return []
    a = ib.detect_absorptions(ticks, symbol)
    return [{"ts": float(r.end_ts), "price": float(r.price), "side": r.side}
            for r in a.itertuples()]


def build_sessions(symbol: str, days: list[str]) -> list[dict]:
    """Données prêtes à rejouer, indépendantes des paramètres."""
    ctx = _context(symbol)
    sessions, ranges = [], []
    for day in days:
        ticks = _session_ticks(symbol, day)
        bars = edge.minute_bars(ticks)
        if len(bars) < 120:
            continue
        em, src = _straddle_em(symbol, day), "straddle"
        if not em:
            prev = [r for r in ranges[-10:] if r > 0]
            em, src = (0.5 * float(np.median(prev)), "realise") if prev else (None, None)
        ranges.append(float(bars["high"].max() - bars["low"].min()))
        if not em:
            continue
        sessions.append({"day": day, "bars": bars, "open": float(bars["open"].iloc[0]),
                         "em": em, "em_source": src, "ctx": ctx,
                         "absorptions": _absorptions(symbol, ticks),
                         "flow": _flow(symbol, day)})
    return sessions


def replay(sessions: list[dict], p: edge.EdgeParams) -> pd.DataFrame:
    rows = []
    for s in sessions:
        for t in edge.run_session(s["bars"], s["open"], s["em"], s["ctx"],
                                  s["absorptions"], s["flow"], p):
            rows.append({**t, "day": s["day"], "em_source": s["em_source"]})
    return pd.DataFrame(rows)


def choose_params(train: list[dict], base: edge.EdgeParams) -> tuple[edge.EdgeParams, pd.DataFrame]:
    """Seuils dont la borne basse (90 %) de l'espérance du setup est la plus
    haute sur l'échantillon d'apprentissage, avec au moins MIN_TRAIN_TRADES."""
    best, best_lo, table = base, -np.inf, []
    for p in edge.param_grid(base):
        tr = replay(train, p)
        if tr.empty:
            continue
        s = edge.summarize(tr[tr["kind"] == "setup"], n_boot=300)
        if s.empty or s.loc["setup", "n"] < MIN_TRAIN_TRADES:
            continue
        lo = s.loc["setup", "ci90_lo"]
        table.append({**{k: getattr(p, k) for k in edge.GRID}, "n": int(s.loc["setup", "n"]),
                      "expectancy_em": s.loc["setup", "expectancy_em"], "ci90_lo": lo})
        if lo > best_lo:
            best, best_lo = p, lo
    return best, pd.DataFrame(table)


def report(symbol: str, max_days: int | None = None) -> dict:
    days = available_days(symbol)
    if max_days:
        days = days[-max_days:]
    sessions = build_sessions(symbol, days)
    out = {"symbol": symbol, "sessions": len(sessions)}
    if len(sessions) < 10:
        out["error"] = f"{len(sessions)} séances exploitables : trop peu pour conclure"
        return out
    cut = int(len(sessions) * TRAIN_FRACTION)
    train, test = sessions[:cut], sessions[cut:]
    base = edge.EdgeParams()
    chosen, grid = choose_params(train, base)
    test_trades = replay(test, chosen)
    test_sum = edge.summarize(test_trades) if not test_trades.empty else pd.DataFrame()
    default_all = edge.summarize(replay(sessions, base))

    def _get(df, kind, col):
        return float(df.loc[kind, col]) if not df.empty and kind in df.index else float("nan")
    # validé : edge positif avec borne basse > 0, meilleur que le hasard et
    # pas moins bon que le rejet naïf (sinon les filtres ne servent à rien)
    setup_e = _get(test_sum, "setup", "expectancy_em")
    validated = bool(setup_e > 0 and _get(test_sum, "setup", "ci90_lo") > 0
                     and setup_e > _get(test_sum, "random", "expectancy_em")
                     and setup_e >= _get(test_sum, "naive", "expectancy_em"))
    by_zone = (test_trades[test_trades["kind"] == "naive"].groupby("zone")["pnl_em"]
               .agg(["count", "mean"]) if not test_trades.empty else pd.DataFrame())
    by_conf = (test_trades[test_trades["kind"] == "setup"].groupby("conf")["pnl_em"]
               .agg(["count", "mean"]) if not test_trades.empty else pd.DataFrame())
    out.update(train_days=f"{train[0]['day']} → {train[-1]['day']}",
               test_days=f"{test[0]['day']} → {test[-1]['day']}",
               em_sources=pd.Series([s["em_source"] for s in sessions]).value_counts().to_dict(),
               chosen=chosen.to_json(), validated=validated,
               test=test_sum, default_all=default_all, grid=grid,
               by_zone=by_zone, by_conf=by_conf)
    path = params_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**chosen.to_json(), "validated": validated,
                                "generated": datetime.now(ET).isoformat(),
                                "test_days": out["test_days"]}, indent=2))
    return out


def to_markdown(r: dict) -> str:
    lines = [f"# Edge /scalp v2 — {r['symbol']}", ""]
    if "error" in r:
        return "\n".join(lines + [r["error"]])
    lines += [f"Séances : {r['sessions']} (apprentissage {r['train_days']}, "
              f"test {r['test_days']}) · EM : {r['em_sources']}", "",
              f"**Validé hors échantillon : {'OUI' if r['validated'] else 'NON'}**", "",
              "Paramètres retenus : " + ", ".join(f"{k}={v}" for k, v in r["chosen"].items()
                                                  if k != "version"), "",
              "## Hors échantillon (séances de test)", "",
              _table(r["test"]), "",
              "`setup` = lecture complète · `naive` = tout excès · `random` = rejet au hasard. "
              "Espérance en EM par trade, intervalle à 90 % par bootstrap par séance.", "",
              "## Rejet naïf par zone (test) — la zone filtre-t-elle ?", "", _table(r["by_zone"]), "",
              "## Setup par confirmations (test)", "", _table(r["by_conf"]), "",
              "## Paramètres par défaut, toutes séances", "", _table(r["default_all"]), ""]
    return "\n".join(lines)


def _table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_(aucune donnée)_"
    return "```\n" + df.round(3).to_string() + "\n```"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbols", nargs="*", default=["NQ", "ES"])
    ap.add_argument("--days", type=int, default=None, help="limiter aux N dernières séances")
    args = ap.parse_args(argv)
    outdir = SETTINGS.data_dir / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    for sym in args.symbols:
        md = to_markdown(report(sym.upper(), args.days))
        path = outdir / f"edge_{sym.upper()}_{datetime.now(ET):%Y-%m-%d}.md"
        path.write_text(md, encoding="utf-8")
        print(md)
        print(f"\n→ {path}\n")
