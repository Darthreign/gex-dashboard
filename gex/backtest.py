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
# fraction du prix.
#
# Une première version utilisait 0,05 %, valeur choisie pour filtrer le bruit
# de cotation. Mesuré sur 22 séances SPX, le dépassement médian d'un niveau
# testé est de 0,24 % : à 0,05 %, presque tout contact était compté comme une
# rupture et le taux de cassure montait à 90 % — un filtre de bruit ne fait pas
# une définition de cassure, ce sont deux besoins distincts.
#
# 0,15 % (≈ 11 pts sur SPX à 7400) reste au-dessus du bruit tout en exigeant un
# franchissement franc. Le seuil demeure une convention : `close_beyond`, qui ne
# dépend d'aucun réglage, est la mesure la plus robuste de l'échec d'un niveau.
BREAK_TOL = 0.0015


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

    À défaut de snapshot ce jour-là, on prend le DERNIER de la séance
    précédente. Ce n'est pas un pis-aller : l'open interest publié le matin
    reflète la clôture de la veille, donc les niveaux qu'un trader a sous les
    yeux à l'ouverture sont exactement ceux de cette chaîne. C'est aussi ce qui
    rend exploitables les chaînes reconstruites depuis Databento, qui sont des
    photos de clôture.
    """
    df = store.load_first_snapshot(symbol, day)
    if df is None or df.empty:
        prev = store.load_previous_snapshot(symbol, day)
        df = prev[1] if prev else None
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
    """Parcours du prix sur une séance.

    Deux sources, par ordre de préférence :

    1. **Bougies 1 min** du flux temps réel — les extrêmes y sont exacts, donc
       une mèche qui touche un niveau est vue. C'est la seule résolution qui
       permet de mesurer honnêtement un taux de cassure.
    2. **Historique des métriques** (un point par snapshot, soit 10 min) —
       repli quand les bougies manquent. Les mèches y disparaissent : les taux
       obtenus sont alors un plancher, jamais une mesure.

    Les bougies sont dépliées en open/high/low/close afin que `evaluate_level`
    voie les extrêmes ; l'ordre high-puis-low à l'intérieur d'une minute est
    une convention (on ne sait pas lequel est venu en premier), sans effet sur
    les indicateurs calculés.
    """
    px = store.load_prices(symbol, day)
    if not px.empty:
        px = px.sort_values("timestamp")
        return px[["open", "high", "low", "close"]].to_numpy(dtype=float).ravel()
    h = store.load_history(symbol)
    if h.empty:
        return np.array([])
    ts = pd.to_datetime(h["timestamp"])
    sel = h[ts.dt.strftime("%Y-%m-%d") == day].sort_values("timestamp")
    return sel["spot"].to_numpy(dtype=float)


def path_resolution(symbol: str, day: str) -> str:
    """Source réellement utilisée pour la séance — à afficher avec tout
    résultat, un taux calculé sur du 10 min ne valant pas un taux calculé sur
    des bougies."""
    return "1min" if not store.load_prices(symbol, day).empty else "snapshot"


def run(symbol: str, days: list[str] | None = None) -> pd.DataFrame:
    """Backtest sur toutes les séances disposant à la fois de niveaux et de prix.

    Les jours candidats sont ceux où l'on a un parcours de prix : les niveaux,
    eux, peuvent venir de la séance précédente (cf. `session_levels`). Se
    limiter aux jours porteurs d'un snapshot écarterait des séances pourtant
    testables.
    """
    days = days or sorted(set(store.price_days(symbol))
                          | set(store.snapshot_days(symbol)))
    outcomes: list[dict] = []
    for day in days:
        path = session_path(symbol, day)
        if len(path) < 2:
            continue
        levels = session_levels(symbol, day, spot=float(path[0]))
        if not levels:
            continue
        res = path_resolution(symbol, day)
        for o in evaluate_session(levels, path, day, symbol):
            outcomes.append({**asdict(o), "resolution": res})
    return pd.DataFrame(outcomes)
