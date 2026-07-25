"""Serveur MCP local : expose les données GEX calculées à Claude Code.

Lit les Parquet écrits par le dashboard (processus séparé) — le dashboard
doit tourner (ou avoir tourné) pour que les données existent.

Enregistrement : voir .mcp.json à la racine du projet.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from mcp.server.fastmcp import FastMCP

from .config import SETTINGS, UNDERLYINGS
from .metrics import ET

mcp = FastMCP("gex-data")


def _latest_snapshot_path(symbol: str) -> Path | None:
    root = SETTINGS.data_dir / "snapshots" / symbol
    if not root.exists():
        return None
    files = sorted(root.rglob("*.parquet"))
    return files[-1] if files else None


def _check_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if symbol not in UNDERLYINGS:
        raise ValueError(f"Symbole inconnu {symbol} — choix : {list(UNDERLYINGS)}")
    return symbol


@mcp.tool()
def get_gex_summary(symbol: str = "SPX") -> str:
    """Dernières métriques de synthèse (GEX net, zero gamma, P/C ratios, spot)
    pour un sous-jacent (SPX, NDX, SPY, QQQ), plus leur évolution sur la journée."""
    symbol = _check_symbol(symbol)
    path = SETTINGS.data_dir / "history" / "metrics.parquet"
    if not path.exists():
        return "Aucun historique — le dashboard n'a pas encore tourné."
    df = pd.read_parquet(path)
    df = df[df["symbol"] == symbol].sort_values("timestamp")
    if df.empty:
        return f"Aucune donnée pour {symbol}."
    last = df.iloc[-1].to_dict()
    last["timestamp"] = str(last["timestamp"])
    out = {"dernier": last, "nb_snapshots": len(df)}
    if len(df) > 1:
        first = df.iloc[0]
        out["variation_du_jour"] = {
            "net_gex": float(df.iloc[-1]["net_gex"] - first["net_gex"]),
            "spot": float(df.iloc[-1]["spot"] - first["spot"]),
        }
    return json.dumps(out, default=str)


@mcp.tool()
def get_gex_by_strike(symbol: str = "SPX", top_n: int = 15) -> str:
    """Les top_n strikes par |GEX| du dernier snapshot — les murs de gamma.
    Colonnes : strike, GEX net ($), côté dominant (calls/puts), open interest."""
    symbol = _check_symbol(symbol)
    path = _latest_snapshot_path(symbol)
    if path is None:
        return "Aucun snapshot — le dashboard n'a pas encore tourné."
    df = pd.read_parquet(path)
    agg = df.groupby("strike").agg(
        gex_net=("gex", "sum"), oi=("open_interest", "sum")
    ).reset_index()
    agg["abs_gex"] = agg["gex_net"].abs()
    top = agg.nlargest(top_n, "abs_gex").sort_values("strike")
    rows = [
        {
            "strike": float(r.strike),
            "gex_net_dollars": float(r.gex_net),
            "cote": "calls (support/pin)" if r.gex_net > 0 else "puts (accélération)",
            "open_interest": float(r.oi),
        }
        for r in top.itertuples()
    ]
    return json.dumps({"snapshot": path.name, "murs_de_gamma": rows})


@mcp.tool()
def get_flow_delta(symbol: str = "SPX", day: str | None = None) -> str:
    """Flux delta options intraday (proxy Δvolume×δ, barres ~1 min, délayé 15 min).
    day au format YYYY-MM-DD, défaut aujourd'hui. Retourne les dernières 30 barres
    et le cumul du jour."""
    symbol = _check_symbol(symbol)
    day = day or datetime.now(ET).strftime("%Y-%m-%d")
    path = SETTINGS.data_dir / "flows" / symbol / f"{day}.parquet"
    if not path.exists():
        return f"Aucun flux pour {symbol} le {day}."
    df = pd.read_parquet(path).sort_values("timestamp")
    recent = df.tail(30)[["timestamp", "flow_total", "flow_0dte"]]
    recent["timestamp"] = recent["timestamp"].astype(str)
    return json.dumps(
        {
            "jour": day,
            "cumul_flow_total": float(df["flow_total"].sum()),
            "cumul_flow_0dte": float(df["flow_0dte"].sum()),
            "dernieres_barres": recent.to_dict("records"),
        }
    )


@mcp.tool()
def get_history(symbol: str = "SPX", last_n: int = 50) -> str:
    """Historique des métriques de synthèse (une ligne par snapshot ~10 min) :
    spot, GEX net, zero gamma, P/C ratios. Pour analyser l'évolution du régime."""
    symbol = _check_symbol(symbol)
    path = SETTINGS.data_dir / "history" / "metrics.parquet"
    if not path.exists():
        return "Aucun historique."
    df = pd.read_parquet(path)
    df = df[df["symbol"] == symbol].sort_values("timestamp").tail(last_n)
    df["timestamp"] = df["timestamp"].astype(str)
    return json.dumps(df.to_dict("records"))


@mcp.tool()
def get_reports(last_n: int = 5) -> str:
    """Derniers rapports des tâches planifiées (backfills, vérifications) écrits
    dans logs/reports.md. À consulter pour savoir ce qui s'est passé pendant une
    exécution automatique, dont la sortie vit dans une conversation séparée."""
    from .logsetup import read_reports
    return read_reports(last_n)


@mcp.tool()
def get_log_tail(lines: int = 50, level: str | None = None) -> str:
    """Fin du log technique (logs/gex.log) : pulls CBOE, erreurs, backfills.
    level optionnel pour filtrer (ex. 'ERROR', 'WARNING')."""
    from .logsetup import LOG_FILE
    if not LOG_FILE.exists():
        return "Aucun log (le dashboard n'a pas encore tourné avec la journalisation)."
    rows = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    if level:
        rows = [r for r in rows if f" {level.upper()} " in r]
    return "\n".join(rows[-lines:]) or "Aucune ligne correspondante."


if __name__ == "__main__":
    mcp.run()
