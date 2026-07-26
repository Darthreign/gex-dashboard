"""Backtest de niveaux : un niveau a-t-il tenu, cédé, et de combien ensuite ?

Le cœur est constitué de fonctions pures prenant (niveau, parcours de prix) :
elles ne lisent aucun fichier, ce qui les rend testables sur des parcours
construits à la main et réutilisables quelle que soit la source — snapshots
maison, reconstruction Databento, ou tout ce qui viendra ensuite.

Trois précautions de méthode, parce qu'elles décident de la validité du
résultat bien plus que le code :

1. **Les niveaux testés sont ceux du DÉBUT de séance.** L'open interest est
   publié le matin ; utiliser les niveaux de clôture reviendrait à tester ce
   qu'on ne pouvait pas connaître, et gonflerait artificiellement les taux de
   réussite.

2. **Un niveau jamais approché n'a pas « tenu ».** Le taux de tenue n'a de sens
   que rapporté aux séances où le prix est effectivement venu au contact —
   sinon un niveau lointain afficherait 100 % de réussite sans rien démontrer.

3. **Toucher n'est pas casser.** Un dépassement d'un tick n'est pas une
   cassure : il faut une marge, sans quoi le bruit de cotation transforme
   chaque contact en rupture.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from . import metrics, store

# Marge au-delà de laquelle un dépassement compte comme une cassure, en
# fraction du prix. 0,05 % ≈ 3,7 pts sur SPX à 7400 — au-dessus du bruit de
# cotation, en dessous d'un mouvement significatif.
BREAK_TOL = 0.0005


@dataclass(frozen=True)
class LevelOutcome:
    day: str
    symbol: str
    name: str
    level: float
    side: str            # "resistance" (au-dessus de l'ouverture) ou "support"
    tested: bool         # le prix est venu au contact
    broke: bool          # dépassement franc, au-delà de BREAK_TOL
    closed_beyond: bool  # séance terminée de l'autre côté
    excursion_pct: float # dépassement maximal au-delà du niveau, en %
    move_after_break_pct: float | None  # parcours au-delà après la cassure


def evaluate_level(name: str, level: float, path: np.ndarray, open_px: float,
                   day: str = "", symbol: str = "",
                   tol: float = BREAK_TOL) -> LevelOutcome:
    """Confronte un niveau au parcours du prix d'une séance.

    `path` : prix ordonnés dans le temps (le premier est l'ouverture).
    """
    side = "resistance" if level >= open_px else "support"
    margin = level * tol

    if side == "resistance":
        tested = bool(np.any(path >= level))
        broken = path >= level + margin
        beyond = (path.max() - level) / level if tested else 0.0
        closed_beyond = bool(path[-1] > level)
    else:
        tested = bool(np.any(path <= level))
        broken = path <= level - margin
        beyond = (level - path.min()) / level if tested else 0.0
        closed_beyond = bool(path[-1] < level)

    broke = bool(np.any(broken))
    move_after = None
    if broke:
        # à partir de la première cassure, jusqu'où le prix est-il allé ?
        i = int(np.argmax(broken))
        rest = path[i:]
        move_after = float((rest.max() - level) / level if side == "resistance"
                           else (level - rest.min()) / level)

    return LevelOutcome(
        day=day, symbol=symbol, name=name, level=float(level), side=side,
        tested=tested, broke=broke, closed_beyond=closed_beyond,
        excursion_pct=float(max(beyond, 0.0)),
        move_after_break_pct=move_after,
    )


def evaluate_session(levels: dict[str, float], path: np.ndarray,
                     day: str = "", symbol: str = "") -> list[LevelOutcome]:
    """Évalue tous les niveaux d'une séance contre son parcours de prix."""
    if len(path) < 2:
        return []
    open_px = float(path[0])
    return [evaluate_level(n, lv, path, open_px, day, symbol)
            for n, lv in levels.items() if lv is not None and np.isfinite(lv)]


def summarize(outcomes: list[LevelOutcome] | pd.DataFrame) -> pd.DataFrame:
    """Agrège par type de niveau : fréquence de test, de tenue, de cassure.

    Le taux de tenue est conditionnel au test (cf. précaution 2) : `n_tested`
    dit sur combien de séances il se calcule, et une valeur reposant sur deux
    ou trois séances ne veut rien dire — la colonne est là pour qu'on puisse
    s'en rendre compte.
    """
    df = (pd.DataFrame([asdict(o) for o in outcomes])
          if not isinstance(outcomes, pd.DataFrame) else outcomes)
    if df.empty:
        return pd.DataFrame(columns=["name", "n_sessions", "n_tested",
                                     "test_rate", "hold_rate", "break_rate",
                                     "close_beyond_rate", "median_move_after_break"])
    rows = []
    for name, g in df.groupby("name", sort=False):
        tested = g[g["tested"]]
        n_t = len(tested)
        rows.append({
            "name": name,
            "n_sessions": len(g),
            "n_tested": n_t,
            "test_rate": len(tested) / len(g),
            # tenue = testé sans cassure franche
            "hold_rate": float((~tested["broke"]).mean()) if n_t else np.nan,
            "break_rate": float(tested["broke"].mean()) if n_t else np.nan,
            "close_beyond_rate": float(tested["closed_beyond"].mean()) if n_t else np.nan,
            "median_move_after_break": float(
                tested["move_after_break_pct"].dropna().median())
            if tested["move_after_break_pct"].notna().any() else np.nan,
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------- adaptateurs
def session_levels(symbol: str, day: str, spot: float | None = None) -> dict[str, float]:
    """Niveaux du début de séance, recalculés depuis le premier snapshot.

    `spot` peut être fourni par l'appelant : les snapshots antérieurs à
    l'ajout de la colonne `spot` ne le portent pas, et le premier prix du
    parcours de la séance fait tout aussi bien l'affaire.
    """
    df = store.load_first_snapshot(symbol, day)
    if df is None or df.empty:
        return {}
    if spot is None and "spot" in df.columns:
        spot = float(df["spot"].iloc[0])
    out: dict[str, float] = {}
    if spot is not None:
        keys = metrics.key_levels(df, spot)
        out.update({k: v for k, v in keys.items() if v is not None})
        zg = metrics.zero_gamma(df, spot)
        if zg is not None:
            out["gamma_flip"] = zg
    lv = metrics.top_gex_levels(df)
    for row in lv.itertuples():
        out[f"GEX{row.rank}"] = float(row.strike)
    return out


def session_path(symbol: str, day: str) -> np.ndarray:
    """Parcours du spot sur une séance, depuis l'historique des métriques.

    Résolution = celle des snapshots persistés. Grossière, mais suffisante
    pour dire si un niveau a été atteint ; elle sous-estime en revanche les
    mèches, donc les taux de cassure obtenus sont un plancher.
    """
    h = store.load_history(symbol)
    if h.empty:
        return np.array([])
    ts = pd.to_datetime(h["timestamp"])
    sel = h[ts.dt.strftime("%Y-%m-%d") == day].sort_values("timestamp")
    return sel["spot"].to_numpy(dtype=float)


def run(symbol: str, days: list[str] | None = None) -> pd.DataFrame:
    """Backtest sur toutes les séances disposant à la fois de niveaux et de prix."""
    days = days or store.snapshot_days(symbol)
    outcomes: list[LevelOutcome] = []
    for day in days:
        path = session_path(symbol, day)
        if len(path) < 2:
            continue
        levels = session_levels(symbol, day, spot=float(path[0]))
        if not levels:
            continue
        outcomes.extend(evaluate_session(levels, path, day, symbol))
    return pd.DataFrame([asdict(o) for o in outcomes])
