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

from . import store
from .config import SETTINGS, UNDERLYINGS
from .metrics import ET, regime_read
from .rtquote import QUOTES, credentials_present

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


def _vix_context() -> dict | None:
    """VIX en direct via dxFeed si un compte courtier est configuré ET que
    l'abonnement inclut ce ticker (`QUOTES.price` renvoie None sinon, repli
    silencieux) — sinon le pull délayé CBOE (scheduler.pull_vix, ~15 min,
    cadence 10 min). La variation du jour reste calculée depuis l'historique
    délayé (seule série qui date un "premier point du jour") même quand le
    dernier niveau vient du direct : approximation assumée, pas une vraie
    variation intraday tick à tick.

    Le flux public/démo (celui du repli NQ/ES, cf. PublicDelayedQuotes) n'a
    volontairement PAS été étendu à VIX : testé hors séance, son dernier
    print datait pile de l'heure de clôture (MARKET_CLOSE) — cote arrêtée,
    pas un flux en retard. Sa latence réelle EN séance reste à vérifier avant
    de l'utiliser comme repli gratuit ici ; ne pas réinterpréter cette note
    comme "flux non fiable" sans nouveau test pendant les heures de marché."""
    vix_hist = store.load_index_spot("vix")
    day_open = None
    delayed_last = None
    delayed_nb_points = 0
    if not vix_hist.empty:
        vix_hist = vix_hist.sort_values("timestamp")
        today = str(pd.to_datetime(vix_hist["timestamp"].iloc[-1]).date())
        today_rows = vix_hist[pd.to_datetime(vix_hist["timestamp"]).dt.strftime("%Y-%m-%d") == today]
        if not today_rows.empty:
            day_open = float(today_rows["vix"].iloc[0])
            delayed_last = float(today_rows["vix"].iloc[-1])
            delayed_nb_points = len(today_rows)

    vix_live = QUOTES.price("VIX") if credentials_present() else None
    if vix_live is not None:
        return {
            "dernier": vix_live,
            "source": "dxfeed_live",
            "variation_du_jour": (vix_live - day_open) if day_open is not None else None,
        }
    if delayed_last is not None:
        return {
            "dernier": delayed_last,
            "source": "cboe_delaye",
            "variation_du_jour": (delayed_last - day_open) if delayed_nb_points > 1 else None,
        }
    return None


@mcp.tool()
def get_market_context(symbol: str = "SPX") -> str:
    """Vue d'ensemble condensée d'un sous-jacent : régime gamma (frein/
    accélération, cf. metrics.regime_read), position du spot par rapport au
    Zero Gamma, murs de gamma les plus proches (au-dessus et en-dessous du
    spot), P/C ratio, et VIX (dernier niveau + variation du jour) en
    confluence. Pensé pour éviter d'enchaîner get_gex_summary +
    get_gex_by_strike + une lecture VIX séparée sur une question du type
    « le contexte est-il propice à une journée directionnelle ? ».

    Ne renvoie ni probabilité de scénario ni recommandation de position —
    uniquement des données calculées, à interpréter, jamais un signal."""
    symbol = _check_symbol(symbol)
    metrics_path = SETTINGS.data_dir / "history" / "metrics.parquet"
    if not metrics_path.exists():
        return "Aucun historique — le dashboard n'a pas encore tourné."
    hist = pd.read_parquet(metrics_path)
    hist = hist[hist["symbol"] == symbol].sort_values("timestamp")
    if hist.empty:
        return f"Aucune donnée pour {symbol}."
    last = hist.iloc[-1]
    spot = float(last["spot"])
    zero_gamma = float(last["zero_gamma"]) if pd.notna(last.get("zero_gamma")) else None
    net_dex = float(last["net_dex"]) if pd.notna(last.get("net_dex")) else 0.0

    regime = regime_read(
        float(last["net_gex"]), net_dex,
        dex_history=hist["net_dex"] if "net_dex" in hist.columns else None,
    )

    walls = json.loads(get_gex_by_strike(symbol, 15))["murs_de_gamma"]
    calls_above = [w for w in walls if w["strike"] > spot and w["gex_net_dollars"] > 0]
    puts_below = [w for w in walls if w["strike"] < spot and w["gex_net_dollars"] < 0]
    call_wall = min(calls_above, key=lambda w: w["strike"]) if calls_above else None
    put_wall = max(puts_below, key=lambda w: w["strike"]) if puts_below else None

    vix_ctx = _vix_context()

    out = {
        "symbol": symbol,
        "spot": spot,
        "net_gex": float(last["net_gex"]),
        "zero_gamma": zero_gamma,
        "spot_vs_zero_gamma": (spot - zero_gamma) if zero_gamma is not None else None,
        "regime": {
            "severite": regime["severity"],
            "cle": regime["i18n_key"],
            "gex_frein": regime["gex_frein"],
            "magnitude_dex": regime["magnitude"],
        },
        "pc_oi": float(last["pc_oi"]) if pd.notna(last.get("pc_oi")) else None,
        "mur_call_proche": call_wall,
        "mur_put_proche": put_wall,
        "vix": vix_ctx,
    }
    return json.dumps(out, default=str)


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


def main() -> None:
    """Entrée de la commande ``gex-mcp`` (équivaut à ``python -m gex.mcp_server``)."""
    mcp.run()


if __name__ == "__main__":
    main()
