"""Page « Scalp » : logique pure (aucune E/S) de l'échelle de niveaux et de l'état
de séance. L'affichage vit dans app.py, les données dans metrics / rtquote.

Pensée pour un scalping CONTRARIEN sur rejet : on joue la correction des excès
autour des niveaux (freins des teneurs de marché). Ce qui compte à l'écran, c'est
donc où est le spot PAR RAPPORT à chaque niveau, et si un niveau est au contact.

⚠️ Affichage de lecture, pas un signal de trading.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

# Distance (en points du sous-jacent) sous laquelle un niveau est « au contact ».
NEAR_PTS = {"NQ": 15.0, "ES": 4.0, "NDX": 15.0, "SPX": 4.0, "SPY": 0.4, "QQQ": 0.4}
DEFAULT_NEAR = 10.0

OPEN_ET = time(9, 30)
CLOSE_ET = time(16, 0)
CONTRARIAN_CUT_ET = time(10, 15)      # 16h15 Paris : le contrarien devient risqué


@dataclass(frozen=True)
class Rung:
    name: str            # « Call Wall », « Gamma Flip », « GEX2 »…
    price: float
    kind: str            # cw | ps | zg | hvl | d1 | gex
    gex: float | None    # $ de gamma du mur (signé), None pour les autres niveaux
    dist: float          # niveau - spot, en points (positif = au-dessus du spot)
    near: bool           # au contact du spot


def near_threshold(symbol: str) -> float:
    return NEAR_PTS.get(symbol.upper(), DEFAULT_NEAR)


def build_ladder(symbol: str, spot: float, zg: float | None, hvl: float | None,
                 keys: dict | None, walls: list[tuple[str, float, float]] | None
                 ) -> list[Rung]:
    """Échelle des niveaux, du plus haut au plus bas.

    `keys` : call_wall / put_support / d1_min / d1_max (metrics.compute_levels) ;
    `walls` : [(nom, strike, gex)] des murs GEX classés. Les niveaux absents sont
    omis, jamais inventés. Deux niveaux au même prix restent deux lignes (leurs
    noms comptent : Call Wall et GEX1 se confondent souvent)."""
    thr = near_threshold(symbol)
    raw: list[tuple[str, float, str, float | None]] = []
    if zg is not None:
        raw.append(("Gamma Flip", zg, "zg", None))
    if hvl is not None:
        raw.append(("HVL", hvl, "hvl", None))
    for key, name, kind in (("call_wall", "Call Wall", "cw"),
                            ("put_support", "Put Support", "ps"),
                            ("d1_max", "1D Max", "d1"), ("d1_min", "1D Min", "d1")):
        v = (keys or {}).get(key)
        if v is not None:
            raw.append((name, float(v), kind, None))
    for name, strike, gex in walls or []:
        raw.append((name, float(strike), "gex", float(gex)))
    rungs = [Rung(n, float(p), k, g, float(p) - spot, abs(float(p) - spot) <= thr)
             for n, p, k, g in raw]
    return sorted(rungs, key=lambda r: (-r.price, r.name))


def nearest(rungs: list[Rung]) -> tuple[Rung | None, Rung | None]:
    """(niveau le plus proche AU-DESSUS, le plus proche EN DESSOUS) du spot."""
    above = [r for r in rungs if r.dist >= 0]
    below = [r for r in rungs if r.dist < 0]
    return (min(above, key=lambda r: r.dist) if above else None,
            max(below, key=lambda r: r.dist) if below else None)


def session_state(now_et: datetime) -> tuple[str, str]:
    """(code, libellé court) de l'état de séance US en heure de New York :
    `closed`, `pre`, `open`, `late` (après la coupure contrarienne de 16h15 Paris)."""
    if now_et.weekday() >= 5:
        return "closed", "Week-end"
    t = now_et.time()
    if t < OPEN_ET:
        return "pre", "Avant l'open US"
    if t >= CLOSE_ET:
        return "closed", "Séance US terminée"
    minutes = (now_et.hour * 60 + now_et.minute) - (OPEN_ET.hour * 60 + OPEN_ET.minute)
    if t < CONTRARIAN_CUT_ET:
        return "open", f"Séance US · {minutes} min · fenêtre contrarienne"
    return "late", f"Séance US · {minutes} min · après 16h15 (contrarien plus risqué)"


def extension_pts(spot: float | None, open_: float | None) -> float | None:
    """Écart du spot à l'ouverture de la séance, en points."""
    if spot is None or open_ is None:
        return None
    return float(spot) - float(open_)
