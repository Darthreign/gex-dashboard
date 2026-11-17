"""Pression mécanique estimée sur la clôture (MOC) — NQ et ES.

Le déséquilibre officiel (NOII Nasdaq / imbalance NYSE, publié à partir de
15h50) n'est pas dans le flux dxFeed. On estime ici ce qui est MÉCANIQUE et
calculable avec nos données : les flux que des acteurs contraints devront
exécuter à la clôture, quel que soit leur avis sur le marché.

1. Couverture des options par les dealers, sur toute la famille d'indice
   (NDX + QQQ + NQ, ou SPX + SPY + ES), à partir du book estimé du jour
   (OI de la veille − flux preneur du jour, cf. positioning) :
   - échéances du jour réglées à 16h00 : le delta converge vers son
     intrinsèque (1/0) ; en règlement cash (SPXW, NDXP) il disparaît, et la
     couverture des options finies dans la monnaie est en plus débouclée.
     Ce débouclage cash est montré À PART, hors du total : il est dominé par
     les contrats profondément ITM, dont l'inventaire de début de séance
     (dealers longs de tout l'OI des calls) décrit le moins bien le détenteur ;
   - autres échéances : le temps qui passe jusqu'à 16h (charm, sur l'horloge
     de variance quand la chaîne la porte) fait glisser leur delta.
   Flux = −Σ position dealer × (δ à la clôture − δ maintenant) × mult × S.
   Positif = les dealers doivent ACHETER.
2. Rééquilibrage des ETF à levier : AUM × (L² − L) × rendement du jour,
   toujours dans le sens du jour (achat si hausse, vente si baisse), pour les
   ETF longs comme inverses. AUM indicatifs, à tenir à jour dans
   `data/moc_letf.json`.

Le total est converti en contrats NQ (20 $/pt) ou ES (50 $/pt).

Hypothèses à garder en tête : le book dealer est une estimation (cf.
positioning) ; on suppose les dealers couverts en delta en continu, si bien
que seul l'écart restant à la cloche passe en MOC ; le rééquilibrage des ETF
peut être en partie anticipé ou étalé par leurs contreparties de swaps.
L'edge, s'il existe, se mesure avec `scripts/moc_report.py`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time

import numpy as np
import pandas as pd

from . import greeks, rates
from .config import SETTINGS
from .metrics import ET, YEAR_SECONDS, carry, multiplier, variance_time_years
from .metrics import am_settled as metrics_am_settled

log = logging.getLogger(__name__)

CLOSE = time(16, 0)
WINDOW_START = time(15, 0)        # la page se concentre sur la dernière heure
NOII_START = time(15, 50)         # premières publications officielles

# Chaînes qui composent la famille de chaque future, avec leur indice de
# référence pour le rendement du jour (rééquilibrage des ETF à levier).
FAMILIES = {
    "NQ": {"chains": ("NDX", "QQQ", "NQ"), "index": "NDX", "fut_mult": 20.0},
    "ES": {"chains": ("SPX", "SPY", "ES"), "index": "SPX", "fut_mult": 50.0},
}
# Règlement en cash : à l'échéance l'option disparaît, la couverture aussi.
CASH_SETTLED = frozenset({"SPX", "NDX"})

# ETF à levier : (levier, AUM indicatif en $). Ordres de grandeur, à METTRE À
# JOUR dans data/moc_letf.json — {"NQ": {"TQQQ": [3, 2.5e10], ...}, ...}.
DEFAULT_LETF = {
    "NQ": {"TQQQ": (3.0, 25e9), "SQQQ": (-3.0, 3e9), "QLD": (2.0, 9e9),
           "QID": (-2.0, 0.3e9), "PSQ": (-1.0, 0.5e9)},
    "ES": {"SPXL": (3.0, 5e9), "UPRO": (3.0, 4e9), "SSO": (2.0, 6e9),
           "SPXS": (-3.0, 0.5e9), "SPXU": (-3.0, 0.5e9), "SDS": (-2.0, 0.4e9),
           "SH": (-1.0, 0.6e9)},
}


def letf_config(symbol: str) -> tuple[dict[str, tuple[float, float]], bool]:
    """(ETF -> (levier, AUM), personnalisé ?). Le fichier utilisateur, s'il
    existe et se lit, remplace les valeurs par défaut pour ce symbole."""
    path = SETTINGS.data_dir / "moc_letf.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8")).get(symbol)
            if raw:
                return {k: (float(v[0]), float(v[1])) for k, v in raw.items()}, True
        except Exception as e:  # noqa: BLE001 — un fichier mal formé ne casse pas la page
            log.warning("moc_letf.json illisible : %s", e)
    return dict(DEFAULT_LETF.get(symbol, {})), False


def letf_rebalance(day_return: float | None, cfg: dict[str, tuple[float, float]]) -> dict:
    """$ à exécuter à la clôture par ETF : AUM × (L² − L) × r."""
    if day_return is None or not np.isfinite(day_return):
        return {"total": None, "by_etf": {}}
    by = {k: aum * (lev * lev - lev) * day_return for k, (lev, aum) in cfg.items()}
    return {"total": float(sum(by.values())), "by_etf": by}


def live_contracts(df: pd.DataFrame, now_et: datetime) -> pd.DataFrame:
    """Contrats encore vivants à `now_et` : sans les échéances passées ni
    celles réglées à l'ouverture du jour (mensuels SPX/NDX, trimestriels
    ES/NQ). Sans ce filtre, un snapshot de l'après-midi d'un 3e vendredi
    comptait ces séries comme expirant à la clôture : le 18/09/2026 (quatre
    sorcières), −4 millions de contrats ES estimés."""
    if df is None or df.empty or "expiry" not in df:
        return df
    exp = pd.to_datetime(df["expiry"]).dt.date.to_numpy()
    today = now_et.date()
    settled = (exp < today) | ((exp == today) & metrics_am_settled(df)
                                & (now_et.time() >= time(9, 30)))
    return df[~settled] if settled.any() else df


def _close_dt(now_et: datetime) -> datetime:
    return datetime.combine(now_et.date(), CLOSE, ET)


def _deltas(spot, k, t, r, sigma, q, is_call):
    c = greeks.call_delta(spot, k, t, r, sigma, q)
    return np.where(is_call, c, c - np.exp(-q * t))


def close_deltas(df: pd.DataFrame, spot: float,
                 now_et: datetime) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(δ maintenant, δ à 16h00, masque échéance du jour) au spot `spot`, en
    règlement PHYSIQUE : l'échéance du jour converge vers son intrinsèque.

    Le temps restant jusqu'à la cloche est retiré de chaque échéance sur
    l'horloge de variance quand la chaîne la porte (`t_var`) : la dernière
    heure de séance pèse bien plus que sa durée calendaire.
    """
    n = len(df)
    if n == 0:
        z = np.zeros(0)
        return z, z, z.astype(bool)
    close = _close_dt(now_et)
    secs = max((close - now_et).total_seconds(), 0.0)
    k = df["strike"].to_numpy(dtype=float)
    is_call = (df["type"] == "C").to_numpy()
    iv = df["iv"].to_numpy(dtype=float) if "iv" in df else np.zeros(n)
    valid = iv > 1e-4
    sig = np.where(valid, iv, 1.0)
    q = np.broadcast_to(np.asarray(carry(df), dtype=float), (n,))
    r = rates.current_rate()
    t = df["t_years"].to_numpy(dtype=float)
    exp_day = pd.to_datetime(df["expiry"]).dt.date.to_numpy() == now_et.date()
    expiring = exp_day & ~metrics_am_settled(df)

    # ½ pile sur le strike : l'issue y est indécise, et 0 créerait un saut
    # artificiel dans le profil (calls ET puts à zéro au même point)
    itm_call = 0.5 * (np.sign(spot - k) + 1.0)
    intrinsic = np.where(is_call, itm_call, itm_call - 1.0)
    # sans IV exploitable : le delta du feed s'il existe, sinon l'intrinsèque
    fallback = df["delta_bs"].to_numpy(dtype=float) if "delta_bs" in df else intrinsic
    d_now = np.where(valid, _deltas(spot, k, t, r, sig, q, is_call), fallback)

    if "t_var" in df:
        tv = df["t_var"].to_numpy(dtype=float)
        left = variance_time_years(now_et, pd.Series([pd.Timestamp(close)]))[0] if secs else 0.0
        ratio = t / np.maximum(tv, 1e-12)
        tv_close = np.maximum(tv - left, 1e-9)
        d_later = _deltas(spot, k, tv_close, r * ratio, sig * np.sqrt(ratio), q * ratio, is_call)
    else:
        d_later = _deltas(spot, k, np.maximum(t - secs / YEAR_SECONDS, 1e-9), r, sig, q, is_call)

    d_close = np.where(expiring, intrinsic, np.where(valid, d_later, d_now))
    return d_now, d_close, expiring


def hedge_flow(book: pd.DataFrame, spot: float, now_et: datetime,
               cash_settled: bool) -> dict:
    """$ que les dealers devront traiter à la clôture sur une chaîne (positif =
    achat) :

    - `expiring` : convergence des échéances du jour vers leur intrinsèque —
      la partie qui ne dépend pas du mode de règlement ;
    - `cash_unwind` : en règlement cash seulement, débouclage de la
      couverture des options qui finissent DANS la monnaie (+pos × intrinsèque).
      Elle est dominée par les contrats profondément ITM, dont le détenteur
      réel est le plus incertain (l'inventaire de début de séance suppose les
      dealers longs de TOUT l'OI des calls) : montrée à part, hors du total ;
    - `charm` : glissement du delta des autres échéances jusqu'à la cloche.
    """
    if book is None or book.empty:
        return {"expiring": 0.0, "cash_unwind": 0.0, "charm": 0.0, "total": 0.0}
    d_now, d_close, expiring = close_deltas(book, spot, now_et)
    pos = book["dealer_pos"].to_numpy(dtype=float)
    notional = pos * multiplier(book) * spot
    flow = -notional * (d_close - d_now)
    e, c = float(flow[expiring].sum()), float(flow[~expiring].sum())
    cash = float((notional * d_close)[expiring].sum()) if cash_settled else 0.0
    return {"expiring": e, "cash_unwind": cash, "charm": c, "total": e + c}


def flow_profile(book: pd.DataFrame, spot: float, now_et: datetime,
                 cash_settled: bool, moves=None) -> pd.DataFrame:
    """Flux de clôture si le spot était à spot×(1+m) à l'approche de la cloche :
    où la pression change de signe, et à quelle vitesse."""
    moves = np.linspace(-0.01, 0.01, 41) if moves is None else np.asarray(moves)
    rows = [{"move": float(m), **hedge_flow(book, spot * (1 + m), now_et, cash_settled)}
            for m in moves]
    return pd.DataFrame(rows)


def expiring_magnets(book: pd.DataFrame, spot: float, today: date,
                     n: int = 3) -> list[dict]:
    """Strikes du jour au plus gros gamma de book (aimants de clôture)."""
    if book is None or book.empty or "gex_book" not in book:
        return []
    d = book[pd.to_datetime(book["expiry"]).dt.date == today]
    if d.empty:
        return []
    by = d.groupby("strike")["gex_book"].sum()
    top = by.abs().sort_values(ascending=False).index[:n]
    return [{"strike": float(k), "gex": float(by[k]), "dist": float(k - spot)} for k in top]


@dataclass
class ChainInput:
    symbol: str
    df: pd.DataFrame          # chaîne enrichie (metrics.enrich)
    spot: float


def estimate(symbol: str, chains: list[ChainInput], flows: dict[str, pd.Series],
             fut_price: float | None, day_return: float | None,
             now_et: datetime | None = None, letf: dict | None = None,
             with_profile: bool = True) -> dict:
    """Pression de clôture de la famille de `symbol` (NQ ou ES)."""
    from . import positioning
    now_et = now_et or datetime.now(ET)
    fam = FAMILIES[symbol]
    letf = letf if letf is not None else letf_config(symbol)[0]
    parts, profile, magnets = {}, None, []
    for c in chains:
        if c.df is None or c.df.empty or not c.spot:
            continue
        book = positioning.dealer_book(live_contracts(c.df, now_et),
                                       flows.get(c.symbol, pd.Series(dtype=float)), c.spot)
        cash = c.symbol in CASH_SETTLED
        parts[c.symbol] = hedge_flow(book, c.spot, now_et, cash)
        if with_profile:
            p = flow_profile(book, c.spot, now_et, cash).set_index("move")["total"]
            profile = p if profile is None else profile.add(p, fill_value=0.0)
        if c.symbol == symbol or (not magnets and c.symbol == fam["index"]):
            m = expiring_magnets(book, c.spot, now_et.date())
            if m:
                magnets = [{**x, "chain": c.symbol} for x in m]
    re = letf_rebalance(day_return, letf)
    opt = sum(p["total"] for p in parts.values())
    total = opt + (re["total"] or 0.0)
    per_contract = fam["fut_mult"] * fut_price if fut_price else None
    return {
        "symbol": symbol, "asof": now_et.isoformat(timespec="seconds"),
        "minutes_to_close": max((_close_dt(now_et) - now_et).total_seconds() / 60, 0.0),
        "options": parts,
        "options_total": float(opt),
        "expiring_total": float(sum(p["expiring"] for p in parts.values())),
        "charm_total": float(sum(p["charm"] for p in parts.values())),
        "cash_unwind_total": float(sum(p["cash_unwind"] for p in parts.values())),
        "letf": re, "day_return": day_return,
        "total": float(total),
        "contracts": float(total / per_contract) if per_contract else None,
        "fut_price": fut_price,
        "profile": None if profile is None else
        pd.DataFrame({"move": profile.index, "total": profile.to_numpy()}),
        "magnets": magnets,
        "missing": [s for s in fam["chains"] if s not in parts],
    }


def day_return(index_spot: float | None, prev_close: float | None) -> float | None:
    if not index_spot or not prev_close:
        return None
    return index_spot / prev_close - 1.0


def session_phase(now_et: datetime) -> str:
    """'avant' (avant 15h), 'fenetre' (15h-15h50), 'noii' (15h50-16h),
    'clos' (après la cloche ou week-end)."""
    if now_et.weekday() >= 5:
        return "clos"
    t = now_et.time()
    if t < WINDOW_START:
        return "avant"
    if t < NOII_START:
        return "fenetre"
    if t < CLOSE:
        return "noii"
    return "clos"


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5
