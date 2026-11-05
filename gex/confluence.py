"""Zones de confluence et réaction du prix — NQ et ES uniquement.

Deux idées distinctes, volontairement séparées :

1. **La confluence** (`cluster_levels`) est une lecture MÉCANIQUE, vraie dès
   le premier jour, sans aucun historique : quand plusieurs niveaux issus de
   mécanismes de couverture DIFFÉRENTS (mur de gamma, HVL pondéré volume,
   Zero Gamma, bornes de move attendu) tombent dans une fourchette étroite,
   plusieurs flux de dealers convergent au même endroit. C'est structurel,
   ça ne se déduit d'aucune statistique.

2. **Le taux de réaction** (`reaction_at_zone` puis l'agrégation historique)
   est une mesure EMPIRIQUE qui vient s'ajouter à la première quand assez de
   séances ont été accumulées — jamais la remplacer. Sans ce découpage,
   l'outil serait muet ses premières semaines et raterait précisément les
   confluences les plus utiles à ce moment-là.

⚠️ Ce que ce module mesure et ne mesure pas. « Réaction » décrit un
COMPORTEMENT DE PRIX (le prix est-il reparti dans l'autre sens, et de
combien), observable et rejouable sur l'historique. Ce n'est en aucun cas
une probabilité de gain, une qualité d'entrée, ni un quelconque avis sur
l'opportunité de prendre une position : aucune notion d'entrée, de stop ou
d'objectif n'entre dans le calcul, et aucun champ produit ici ne doit être
relu comme tel. Un agent qui restitue ces chiffres décrit la structure du
marché ; il ne prescrit pas d'action.

Périmètre NQ/ES : les seuils viennent de la lecture du carnet (NQ « léger »,
ES « lourd »), qui n'a pas d'équivalent sur les indices cash SPX/NDX/SPY/QQQ.
Ceux-ci fournissent les NIVEAUX, mais la réaction ne se mesure que sur les
futures effectivement tradés.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import metrics

# Écart maximal entre deux niveaux voisins pour qu'ils comptent comme une
# seule zone. En POINTS, pas en pourcentage : toute la méthodologie de
# réaction est déjà exprimée en points, mélanger les deux unités rendrait le
# backtest incohérent à relire. À réviser si NQ/ES dérivent fortement.
CLUSTER_POINTS: dict[str, float] = {"NQ": 50.0, "ES": 15.0}

# Retour minimal, depuis le niveau de référence et dans le sens opposé à la
# transpercion, pour qu'on parle de réaction. Pas de maximum : une réaction
# de 10 points et une de 200 comptent toutes deux, leur amplitude étant
# conservée à part (`retracement`) pour l'analyse.
REACTION_POINTS: dict[str, float] = {"NQ": 10.0, "ES": 5.0}

# Le prix n'a pas besoin de croiser le niveau : s'en approcher à cette
# distance suffit à considérer la zone comme touchée.
TOUCH_TOLERANCE = 5.0

# Fenêtre d'observation après le toucher. Bornée volontairement : sans
# limite, un retournement finit toujours par arriver et la mesure ne dirait
# plus rien de la zone.
WINDOW_MINUTES = 2


def supported(symbol: str) -> bool:
    """NQ/ES seulement — cf. docstring du module."""
    return symbol in CLUSTER_POINTS


@dataclass
class Zone:
    """Groupe de niveaux assez proches pour se lire comme une seule zone."""
    levels: dict[str, float] = field(default_factory=dict)

    @property
    def low(self) -> float:
        return min(self.levels.values())

    @property
    def high(self) -> float:
        return max(self.levels.values())

    @property
    def width(self) -> float:
        return self.high - self.low

    def __len__(self) -> int:
        return len(self.levels)

    def reference(self, going_up: bool) -> float:
        """Niveau de référence pour mesurer le retour : le PREMIER que le prix
        rencontre, donc le plus bas s'il monte, le plus haut s'il descend.

        On ne sait pas lequel des niveaux d'une confluence a réellement fait
        réagir le prix — prendre celui qu'il touche en premier est le seul
        choix qui ne suppose rien (le centre de la zone, lui, serait un point
        que le marché n'a peut-être jamais atteint).
        """
        return self.low if going_up else self.high


def collect_levels(df: pd.DataFrame, spot: float) -> dict[str, float]:
    """Tous les niveaux d'une chaîne, sous forme nom -> prix.

    Réutilise `metrics` sans rien recalculer : Zero Gamma (open interest),
    HVL (pondéré par le volume du jour), Call Wall / Put Support, bornes de
    move attendu, et les murs GEX1-5.
    """
    out: dict[str, float] = {}
    if df is None or df.empty or not spot:
        return out

    zg = metrics.zero_gamma(df, spot)
    if zg is not None:
        out["zero_gamma"] = float(zg)
    hvl = metrics.zero_gamma(df, spot, weight_col="volume")
    if hvl is not None:
        out["hvl"] = float(hvl)

    for key, value in metrics.key_levels(df, spot).items():
        if value is not None:
            out[key] = float(value)

    walls = metrics.top_gex_levels(df, ref_spot=spot)
    if not walls.empty:
        for lv in walls.itertuples():
            out[f"gex{int(lv.rank)}"] = float(lv.strike)
    return out


def cluster_levels(levels: dict[str, float], symbol: str) -> list[Zone]:
    """Regroupe les niveaux distants de moins de `CLUSTER_POINTS[symbol]`.

    Balayage simple par prix croissant : un niveau rejoint la zone courante
    tant qu'il reste à portée du PRÉCÉDENT (chaînage), ce qui laisse une zone
    s'étendre au-delà du seuil si ses niveaux se suivent de proche en proche
    — c'est voulu, une grappe dense de six murs espacés de 20 points est bien
    une seule zone même si elle couvre 100 points au total. `Zone.width`
    conserve l'étendue réelle pour qui veut filtrer là-dessus.
    """
    if not levels:
        return []
    threshold = CLUSTER_POINTS[symbol]
    ordered = sorted(levels.items(), key=lambda kv: kv[1])

    zones: list[Zone] = []
    current = Zone(levels={ordered[0][0]: ordered[0][1]})
    previous = ordered[0][1]
    for name, price in ordered[1:]:
        if price - previous <= threshold:
            current.levels[name] = price
        else:
            zones.append(current)
            current = Zone(levels={name: price})
        previous = price
    zones.append(current)
    return zones


def _touch_index(bars: pd.DataFrame, zone: Zone) -> int | None:
    """Indice de la première bougie qui entre dans la zone (croisement ou
    approche à `TOUCH_TOLERANCE`)."""
    lo = zone.low - TOUCH_TOLERANCE
    hi = zone.high + TOUCH_TOLERANCE
    hit = bars.index[(bars["low"] <= hi) & (bars["high"] >= lo)]
    return int(hit[0]) if len(hit) else None


def reaction_at_zone(bars: pd.DataFrame, zone: Zone, symbol: str) -> dict | None:
    """Le prix a-t-il réagi à cette zone, et de combien ?

    `bars` : bougies 1 min d'une séance (colonnes timestamp, open, high, low,
    close), telles que les écrit `store.append_prices`.

    Renvoie None si la zone n'a jamais été touchée ce jour-là — cas à
    distinguer d'une absence de réaction, qui est une information, alors
    qu'un niveau jamais atteint n'en est pas une.

    Le sens de la transpercion est donné par la bougie du toucher elle-même
    (haussière = le prix venait d'en dessous), et le retour est mesuré dans
    le sens OPPOSÉ, depuis le premier niveau rencontré.
    """
    if bars is None or bars.empty:
        return None
    bars = bars.sort_values("timestamp").reset_index(drop=True)
    idx = _touch_index(bars, zone)
    if idx is None:
        return None

    touch = bars.loc[idx]
    going_up = float(touch["close"]) >= float(touch["open"])
    ref = zone.reference(going_up)

    end = pd.Timestamp(touch["timestamp"]) + pd.Timedelta(minutes=WINDOW_MINUTES)
    window = bars[(bars["timestamp"] >= touch["timestamp"])
                  & (bars["timestamp"] <= end)]

    # Le prix montait : une réaction le fait redescendre sous la référence
    # (et inversement). Un retour négatif signifie qu'il a continué tout
    # droit — pas de réaction, sans ambiguïté.
    if going_up:
        retracement = float(ref - window["low"].min())
    else:
        retracement = float(window["high"].max() - ref)

    threshold = REACTION_POINTS[symbol]
    return {
        "touched_at": pd.Timestamp(touch["timestamp"]),
        "direction": "up" if going_up else "down",
        "reference": float(ref),
        "retracement": retracement,
        "threshold": threshold,
        "reaction": bool(retracement >= threshold),
        "levels": dict(zone.levels),
    }
