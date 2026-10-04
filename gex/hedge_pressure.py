"""Hedge Pressure (2026-10-05, prototype) : d(Hedge)/dt = Γ·dS + Vanna·dσ + Charm·dt.

Mesure du flux de couverture MÉCANIQUE que la structure d'options en place
impose aux dealers — par opposition au flux de PRIMES des trades
(`net_musd`/`gross_musd`, cf. `scalp.py`/`flowtape.py`), qui mesure le
comportement des acheteurs, pas l'obligation de couverture qui en résulte.

Pourquoi les deux peuvent diverger : un achat de calls peut être du
market-making réactif (le dealer vend le call et se couvre dans l'AUTRE
sens), donc "flux acheteur net" ne dit pas forcément "dealers achètent le
sous-jacent". Hedge Pressure répond à une question différente et purement
structurelle : "combien de delta la position GAMMA/VANNA/CHARM déjà en place
oblige-t-elle mécaniquement à rehedger pour CE mouvement de spot/vol/temps ?"
— indépendant de ce qui se traite en ce moment sur les options.

Demande explicite de l'utilisateur (2026-10-05) : diagnostiquer les cas où
"une fenêtre verte [flux de primes positif] même sur une montée violente
indique que les dealers ne se couvrent pas dans le sens du mouvement" —
Hedge Pressure est le candidat pour détecter cette divergence.

Chaque composante réutilise EXACTEMENT les formules déjà en place dans
`metrics.py`/`greeks.py` (GEX, VEX, CEX), jamais une formule dupliquée :
- Γ (gamma) : même recalcul "spot recalculé, IV/OI/maturités figées" que
  `metrics.net_gex_at`/`gamma_profile` — mais exprimé en $ PAR POINT de
  spot (pas $ par 1% comme la convention GEX d'affichage), pour pouvoir le
  multiplier directement par un dS en points.
- Vanna : même formule que `metrics.add_second_order` (colonne "vex", $ par
  1 point de vol), mais recalculée à un spot arbitraire plutôt qu'au spot du
  snapshot — même principe que Γ ci-dessus.
- Charm : idem, colonne "cex" ($ par jour écoulé).

Convention de signe IDENTIQUE au GEX (cf. metrics.py) : dealers longs calls,
courts puts. Un flux positif = dealers ACHETEURS du sous-jacent pour se
couvrir ; négatif = VENDEURS.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import greeks, rates
from .config import CONTRACT_MULTIPLIER


def _signed(df: pd.DataFrame) -> np.ndarray:
    return np.where((df["type"] == "C").to_numpy(), 1.0, -1.0)


def _usable(df: pd.DataFrame, weight_col: str) -> pd.DataFrame:
    return df[(df["iv"] > 1e-4) & (df[weight_col] > 0) & (df["t_years"] > 0)]


def net_gamma_per_point(df: pd.DataFrame, spot: float,
                        weight_col: str = "open_interest") -> float:
    """$ de delta net créé par 1 POINT de mouvement du spot, gamma recalculé
    À CE spot (IV/OI/maturités figées) — même recalcul que
    `metrics.net_gex_at`, mais sans le facteur `spot * 0.01` de la
    convention GEX "par 1%" : ici on veut directement multiplier par un dS
    en points pour obtenir un flux en dollars, pas en pourcentage de spot."""
    d = _usable(df, weight_col)
    if d.empty:
        return 0.0
    g = greeks.gamma(spot, d["strike"].to_numpy(), d["t_years"].to_numpy(),
                     rates.current_rate(), d["iv"].to_numpy())
    sign = _signed(d)
    return float((sign * g * d[weight_col].to_numpy() * CONTRACT_MULTIPLIER * spot).sum())


def net_vanna_per_vol_point(df: pd.DataFrame, spot: float,
                            weight_col: str = "open_interest") -> float:
    """$ de delta net créé par 1 POINT de vol implicite (1 %), vanna
    recalculée à CE spot — même formule que la colonne "vex" de
    `metrics.add_second_order`, recalculée au lieu de relue (le spot
    courant diverge souvent de celui du dernier pull de chaîne)."""
    d = _usable(df, weight_col)
    if d.empty:
        return 0.0
    v = greeks.vanna(spot, d["strike"].to_numpy(), d["t_years"].to_numpy(),
                     rates.current_rate(), d["iv"].to_numpy())
    sign = _signed(d)
    return float((sign * v * 0.01 * d[weight_col].to_numpy() * CONTRACT_MULTIPLIER * spot).sum())


def net_charm_per_day(df: pd.DataFrame, spot: float,
                      weight_col: str = "open_interest") -> float:
    """$ de delta net créé par 1 JOUR écoulé, charm recalculé à CE spot —
    même formule que la colonne "cex" de `metrics.add_second_order`."""
    d = _usable(df, weight_col)
    if d.empty:
        return 0.0
    c = greeks.charm_per_day(spot, d["strike"].to_numpy(), d["t_years"].to_numpy(),
                             rates.current_rate(), d["iv"].to_numpy())
    sign = _signed(d)
    return float((sign * c * d[weight_col].to_numpy() * CONTRACT_MULTIPLIER * spot).sum())


def atm_iv(df: pd.DataFrame, spot: float) -> float | None:
    """IV représentative (moyenne call+put au strike le plus proche du
    spot, échéance la plus proche — 0DTE si dispo) : sert de dσ pour la
    composante vanna. Pas pondérée par l'OI à dessein (l'IV at-the-money
    est la mieux formée, l'OI loin de la monnaie souvent illiquide/périmé)."""
    d = df[(df["iv"] > 1e-4) & (df["t_years"] > 0)]
    if d.empty:
        return None
    nearest_expiry = d["t_years"].min()
    front = d[np.isclose(d["t_years"], nearest_expiry)]
    if front.empty:
        return None
    idx = (front["strike"] - spot).abs().idxmin()
    atm_strike = front.loc[idx, "strike"]
    at_strike = front[front["strike"] == atm_strike]
    return float(at_strike["iv"].mean())


@dataclass
class HedgePressure:
    """Attention au sens : gamma_flow/vanna_flow/charm_flow/total décrivent
    la DÉRIVE DE L'EXPOSITION DELTA DE LA POSITION d'options elle-même (sous
    la convention GEX "dealers longs calls, courts puts", identique à
    `metrics.enrich`/`regime_read`) — PAS le sens dans lequel les dealers
    tradent pour se re-hedger. Les deux sont de signe OPPOSÉ : si la
    position gagne du delta long (total > 0) quand le spot monte, les
    dealers doivent VENDRE pour rester neutres (cf. `regime_read` :
    "GEX positif = les dealers vendent les hausses"). `dealer_hedge_flow`
    ci-dessous fait cette conversion — c'est LUI qu'il faut comparer à
    `net_musd` (un flux de TRADE, positif = achat), jamais `total`
    directement, sous peine d'inverser le diagnostic."""
    gamma_flow: float    # $ de delta DE LA POSITION, imposé par dS
    vanna_flow: float    # $ de delta DE LA POSITION, imposé par dsigma
    charm_flow: float     # $ de delta DE LA POSITION, imposé par dt

    @property
    def total(self) -> float:
        """Dérive de delta de la POSITION (pas le flux de hedge — cf.
        docstring de la classe)."""
        return self.gamma_flow + self.vanna_flow + self.charm_flow

    @property
    def dealer_hedge_flow(self) -> float:
        """$ que les dealers doivent TRADER sur le sous-jacent pour rester
        neutres — l'opposé de `total`. Positif = ACHAT net imposé,
        négatif = VENTE nette imposée. C'est cette valeur, pas `total`,
        qui se compare à `net_musd` (flux de primes des trades options,
        cf. scalp.py) pour diagnostiquer une divergence flux-de-primes vs
        obligation-de-couverture réelle."""
        return -self.total


def hedge_pressure(df: pd.DataFrame, spot0: float, spot1: float,
                   iv0: float | None, iv1: float | None, dt_days: float,
                   weight_col: str = "open_interest") -> HedgePressure:
    """Décompose le flux de couverture mécanique entre deux instants (t0,t1)
    d'une MÊME structure d'options (un seul snapshot : OI/maturités figées,
    seuls spot/IV/temps bougent — cohérent avec l'hypothèse déjà faite
    partout ailleurs dans ce fichier pour le GEX temps réel).

    Chaque brique est évaluée à spot0 (approximation au premier ordre,
    exactement la formule demandée : Γ·dS + Vanna·dσ + Charm·dt) — pas une
    intégrale le long du chemin, qui demanderait de ré-évaluer à chaque
    tick. Acceptable pour des `dS` de l'ordre de quelques dizaines de points
    sur NQ/ES (la zone où ce diagnostic est utile) ; à revisiter si testé
    sur des mouvements beaucoup plus larges.

    `iv0`/`iv1` en décimal (0.20, pas 20) — None si indisponible (dsigma
    traité comme 0, la composante vanna est alors omise plutôt que faussée)."""
    d_spot = spot1 - spot0
    gamma_flow = net_gamma_per_point(df, spot0, weight_col) * d_spot
    vanna_flow = 0.0
    if iv0 is not None and iv1 is not None:
        d_vol_points = (iv1 - iv0) * 100.0  # décimal -> points de vol
        vanna_flow = net_vanna_per_vol_point(df, spot0, weight_col) * d_vol_points
    charm_flow = net_charm_per_day(df, spot0, weight_col) * dt_days
    return HedgePressure(gamma_flow=gamma_flow, vanna_flow=vanna_flow, charm_flow=charm_flow)
