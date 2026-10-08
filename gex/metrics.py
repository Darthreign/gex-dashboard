"""Métriques de structure de marché : GEX/DEX par strike, zero gamma,
put/call ratios, proxy de flux delta.

Convention GEX (SpotGamma "naive") :
    GEX($ par 1% de move) = gamma × OI × multiplicateur × spot² × 0.01
    calls comptés positifs, puts négatifs (hypothèse dealers longs calls /
    courts puts vendus par le marché).
"""
from __future__ import annotations

import logging
import threading
import time as time_mod
import weakref
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import greeks, rates
from .config import CONTRACT_MULTIPLIER, SETTINGS
from .ingest import ChainSnapshot

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
YEAR_SECONDS = 365.0 * 24 * 3600

EXPIRY_BUCKETS = ["0DTE", "Semaine", "Mois", "Tout", "Pondéré"]


# Racines réglées sur la cotation d'ouverture (SOQ) du jour d'échéance : leur
# gamma disparaît à 9:30 ET, pas à 16:00. Les séries PM ont leur propre racine
# (SPXW, NDXP, RUTW).
AM_SETTLED_ROOTS = frozenset({"SPX", "NDX", "RUT"})
AM_SETTLE = pd.Timedelta(hours=9, minutes=30)
PM_SETTLE = pd.Timedelta(hours=16)


def am_settled(df: pd.DataFrame) -> np.ndarray:
    """Contrats réglés à l'ouverture : colonne `settle_am` fournie par la source
    si elle existe, sinon déduite de la racine (`root` ou `underlying_symbol`)."""
    if "settle_am" in df.columns:
        return df["settle_am"].fillna(False).astype(bool).to_numpy()
    for col in ("root", "underlying_symbol"):
        if col in df.columns:
            return df[col].isin(AM_SETTLED_ROOTS).to_numpy()
    return np.zeros(len(df), dtype=bool)


def expiry_datetimes(expiries: pd.Series, am: np.ndarray | None = None) -> pd.Series:
    """Instant de règlement en ET : 9:30 pour les séries AM, 16:00 sinon."""
    base = pd.to_datetime(pd.Series(expiries).reset_index(drop=True)).dt.tz_localize(ET)
    if am is None:
        return base + PM_SETTLE
    minutes = np.where(am, AM_SETTLE.total_seconds(), PM_SETTLE.total_seconds()) / 60
    return base + pd.to_timedelta(pd.Series(minutes), unit="min")


def seconds_to_expiry(expiries: pd.Series, now_et: datetime,
                      am: np.ndarray | None = None) -> np.ndarray:
    """Secondes jusqu'au règlement (cf. `expiry_datetimes`).

    Négatif = contrat expiré (0DTE après la cloche, ou série AM après
    l'ouverture le jour de l'opex) — à exclure.
    """
    return (expiry_datetimes(expiries, am) - now_et).dt.total_seconds().to_numpy()


# Horloge de variance : la variance ne s'écoule pas au rythme du calendrier.
# Une journée de bourse (16:00 → 16:00) vaut 1 unité : la séance en porte
# RTH_SHARE, en U (ouverture et clôture plus actives), la nuit le reste ;
# un jour de week-end vaut WEEKEND_FACTOR d'une heure de nuit. 252 unités par
# an. Jours fériés non modélisés (traités comme des jours ouvrés).
TRADING_DAYS = 252
RTH_SHARE = 0.80
WEEKEND_FACTOR = 0.25
U_SHAPE = 1.0
VAR_CLOCK_HORIZON_DAYS = 60
_RTH_OPEN_MIN, _RTH_LEN_MIN = 9 * 60 + 30, 390
_OVN_RATE = (1 - RTH_SHARE) / ((24 * 60) - _RTH_LEN_MIN)
_RTH_BASE = RTH_SHARE / (_RTH_LEN_MIN * (1 + U_SHAPE / 3))


def _variance_per_minute(start: pd.Timestamp, n: int) -> np.ndarray:
    m = start + pd.to_timedelta(np.arange(n), unit="min")
    mins = (m.hour * 60 + m.minute).to_numpy()
    weekday = (m.weekday < 5)
    x = (mins - _RTH_OPEN_MIN) / _RTH_LEN_MIN
    rth = weekday & (x >= 0) & (x < 1)
    return np.where(rth, _RTH_BASE * (1 + U_SHAPE * (2 * x - 1) ** 2),
                    np.where(weekday, _OVN_RATE, _OVN_RATE * WEEKEND_FACTOR))


def variance_time_years(now_et: datetime, settle: pd.Series) -> np.ndarray:
    """Temps restant mesuré en variance (années de 252 séances), pas en
    calendrier : la dernière heure de séance pèse bien plus qu'une nuit ou
    qu'un week-end. Au-delà de VAR_CLOCK_HORIZON_DAYS, le reliquat est
    converti au prorata 252/365."""
    if len(settle) == 0:
        return np.zeros(0)
    secs = (settle - now_et).dt.total_seconds().to_numpy()
    horizon = VAR_CLOCK_HORIZON_DAYS * 86400.0
    n = int(np.ceil(min(max(secs.max(initial=0.0), 0.0), horizon) / 60.0)) + 1
    cum = np.concatenate([[0.0], np.cumsum(_variance_per_minute(pd.Timestamp(now_et), n))])
    units = np.interp(np.clip(secs, 0.0, horizon) / 60.0, np.arange(n + 1), cum)
    units += np.maximum(secs - horizon, 0.0) / 86400.0 * TRADING_DAYS / 365.0
    return units / TRADING_DAYS


def t_var_years(now_et: datetime, expiries: pd.Series, am: np.ndarray | None) -> np.ndarray:
    """`variance_time_years` jusqu'au règlement, plancher de 5 min de séance
    (même rôle que le plancher calendaire de 300 s sur t_years)."""
    tv = variance_time_years(now_et, expiry_datetimes(expiries, am))
    return np.maximum(tv, 5 * _RTH_BASE / TRADING_DAYS)


def variance_clock(df: pd.DataFrame) -> tuple:
    """(t_var, sigma, r, q) équivalents sur l'horloge de variance, avec
    σ_eff²·t_var = σ²·t et les mêmes facteurs d'actualisation : prix, delta et
    gamma au spot sont inchangés, seules les dérivées par rapport au temps
    (charm) changent d'horloge. None si la chaîne n'a pas de `t_var`."""
    if "t_var" not in df:
        return None
    t = df["t_years"].to_numpy(dtype=float)
    tv = df["t_var"].to_numpy(dtype=float)
    ratio = t / tv
    iv = df["iv"].to_numpy(dtype=float)
    return tv, iv * np.sqrt(ratio), rates.current_rate() * ratio, np.asarray(carry(df)) * ratio


def quote_price(df: pd.DataFrame) -> np.ndarray:
    """Prix de marché exploitable : milieu de fourchette si bid ET ask sont
    cotés, à défaut le close (chaînes Databento, sans bid/ask). NaN sinon."""
    n = len(df)
    bid = df["bid"].to_numpy(dtype=float) if "bid" in df else np.full(n, np.nan)
    ask = df["ask"].to_numpy(dtype=float) if "ask" in df else np.full(n, np.nan)
    mid = np.where((bid > 0) & (ask >= bid), (bid + ask) / 2, np.nan)
    if "close" in df:
        close = df["close"].to_numpy(dtype=float)
        mid = np.where(np.isnan(mid) & (close > 0), close, mid)
    return mid


MAX_DIV_YIELD = 0.02   # rendement de dividende annuel toléré au-delà du portage


def implied_forwards(df: pd.DataFrame, spot: float, r: float,
                     window: float = 0.05, min_pairs: int = 3,
                     max_basis: float = 0.03) -> dict:
    """Forward implicite de chaque échéance par parité call-put :
    F = K + e^(rT)·(C − P), médiane sur les strikes à ±`window` du spot.

    Le forward contient tout ce que le marché price entre spot et échéance :
    dividendes (SPX, SPY, QQQ), portage, ou l'écart au future de livraison
    (options sur future). Une échéance sans assez de paires cotées, ou dont
    le forward s'écarte à la fois du spot (`max_basis`) et du forward de
    portage S·e^(rT) (tolérance élargie avec l'échéance) — quotes aberrantes
    ou spot figé —, est laissée de côté plutôt que devinée.
    """
    if df.empty:
        return {}
    d = df.assign(_px=quote_price(df))
    d = d[d["_px"] > 0]
    if "volume" in d:
        # racines multiples (SPX/SPXW) sur une même échéance : la plus traitée
        d = d.sort_values("volume")
    d = d.drop_duplicates(["expiry", "type", "strike"], keep="last")
    out = {}
    for exp, e in d.groupby("expiry"):
        calls = e[e["type"] == "C"].set_index("strike")
        puts = e[e["type"] == "P"].set_index("strike")
        common = [k for k in calls.index.intersection(puts.index)
                  if abs(k - spot) / spot < window]
        if len(common) < min_pairs:
            continue
        t = calls.loc[common, "t_years"].to_numpy()
        k = np.asarray(common, dtype=float)
        f = float(np.median(k + np.exp(r * t) * (calls.loc[common, "_px"].to_numpy()
                                                  - puts.loc[common, "_px"].to_numpy())))
        # Référence = le spot (options sur future) OU le forward de portage
        # S·e^(rT) (indices, ETF, actions) : sur 1 à 4 ans, le portage écarte
        # légitimement le forward du spot de 5 à 20 %. La tolérance s'élargit
        # avec l'échéance (dividendes jusqu'à ~2 %/an) ; un écart à court
        # terme reste suspect (spot figé ou quotes aberrantes).
        t_med = float(np.median(t))
        carry_fwd = spot * np.exp(r * t_med)
        if (abs(f / spot - 1) > max_basis
                and abs(f / carry_fwd - 1) > max_basis + MAX_DIV_YIELD * t_med):
            log.warning("Forward %s aberrant ignoré : %.2f pour spot %.2f (portage %.2f)",
                        exp, f, spot, carry_fwd)
            continue
        out[exp] = f
    return out


def carry_rates(df: pd.DataFrame, spot: float, r: float,
                forwards: dict) -> tuple[np.ndarray, np.ndarray]:
    """(forward, q) par contrat, avec F = S·e^((r−q)t).

    Une échéance sans forward mesuré reprend le q médian des échéances d'au
    moins 2 jours (un q de 0DTE, très bruité ramené à l'année, ne se
    transpose pas), ou 0 s'il n'y en a aucune.
    """
    t = df["t_years"].to_numpy(dtype=float)
    f = df["expiry"].map(forwards).to_numpy(dtype=float)
    q = r - np.log(f / spot) / t
    ref = q[np.isfinite(q) & (t >= 2 / 365)]
    fallback = float(np.median(ref)) if len(ref) else 0.0
    q = np.where(np.isfinite(q), q, fallback)
    return spot * np.exp((r - q) * t), q


def calibrate_chain(df: pd.DataFrame, spot: float, r: float,
                    max_rel_spread: float = 0.5) -> pd.DataFrame:
    """Forward, portage q et IV propres à l'outil, à partir des prix de la chaîne.

    L'IV n'est plus reprise telle quelle du fournisseur : elle est inversée
    depuis le milieu de fourchette avec le MÊME forward (donc les mêmes
    dividendes et le même taux) que celui qui sert ensuite au gamma. Mélanger
    l'IV d'un modèle tiers avec nos propres r et q faussait le gamma.

    À chaque strike, c'est l'option HORS de la monnaie qui fixe l'IV, appliquée
    au call comme au put (la parité garantit la même vol aux deux) : l'option
    dans la monnaie a un prix dominé par sa valeur intrinsèque, son IV inversée
    est bruitée. Repli sur l'IV du flux (`iv_feed`, conservée pour comparaison)
    quand aucun prix exploitable n'existe. Colonnes ajoutées : forward,
    carry_q, iv_feed, iv (recalibrée), iv_source.
    """
    d = df.copy()
    feed = d["iv"].to_numpy(dtype=float) if "iv" in d else np.zeros(len(d))
    d["iv_feed"] = feed
    fwd, q = carry_rates(d, spot, r, implied_forwards(d, spot, r))
    d["forward"], d["carry_q"] = fwd, q

    px = quote_price(d)
    if "bid" in d and "ask" in d:
        spread = (d["ask"] - d["bid"]).to_numpy(dtype=float)
        px = np.where(spread <= max_rel_spread * px, px, np.nan)
    k = d["strike"].to_numpy(dtype=float)
    t = d["t_years"].to_numpy(dtype=float)
    is_call = (d["type"] == "C").to_numpy()
    inv = np.full(len(d), np.nan)
    ok = np.isfinite(px)
    if ok.any():
        inv[ok] = greeks.implied_vol(px[ok], spot, k[ok], t[ok], r, is_call[ok], q=q[ok])

    otm = np.where(is_call, k >= fwd, k < fwd) & np.isfinite(inv)
    keys = ["expiry", "strike", "t_years"]
    otm_iv = d.loc[otm, keys].assign(_v=inv[otm]).drop_duplicates(keys)
    by_strike = d[keys].merge(otm_iv, on=keys, how="left")["_v"].to_numpy()

    iv = np.where(np.isfinite(by_strike), by_strike,
                  np.where(np.isfinite(inv), inv, np.where(feed > 1e-4, feed, 0.0)))
    d["iv"] = iv
    d["iv_source"] = np.where(np.isfinite(by_strike), "otm",
                              np.where(np.isfinite(inv), "quote",
                                       np.where(feed > 1e-4, "feed", "none")))
    return d


def multiplier(df: pd.DataFrame):
    """Multiplicateur $/point par contrat : 100 pour les options d'indice et
    d'ETF, le notionnel du future pour les options sur future (20 NQ, 50 ES).
    Les recalculs (profils, murs, vanna/charm) le relisent sur la chaîne au
    lieu de supposer 100, qui gonflait les montants NQ ×5 et ES ×2."""
    if "multiplier" in df:
        return df["multiplier"].to_numpy(dtype=float)
    return float(CONTRACT_MULTIPLIER)


def carry(df: pd.DataFrame):
    """q par contrat pour les recalculs de greeks (0 sur les snapshots
    antérieurs à la calibration)."""
    return df["carry_q"].to_numpy(dtype=float) if "carry_q" in df else 0.0


def enrich(snapshot: ChainSnapshot, now_et: datetime | None = None) -> pd.DataFrame:
    """Ajoute t, forward/IV calibrés (`calibrate_chain`), greeks et GEX/DEX.

    Quand aucune IV n'est disponible (deep ITM sans quote ni IV feed), on
    retombe sur les Greeks CBOE — leur gamma est ~0 sur ces contrats.
    """
    now_et = now_et or datetime.now(ET)
    df = snapshot.options.copy()
    # exclut les contrats expirés (dont les 0DTE du jour après 16:00 ET,
    # dont les quotes résiduelles polluent GEX 0DTE et skew IV)
    secs = seconds_to_expiry(df["expiry"], now_et, am_settled(df))
    df = df[secs > 0].reset_index(drop=True)
    s = snapshot.spot
    # plancher 5 min pour éviter les gammas explosifs à la cloche
    t = np.maximum(secs[secs > 0], 300.0) / YEAR_SECONDS
    df["t_years"] = t
    df["t_var"] = t_var_years(now_et, df["expiry"], am_settled(df))
    r = rates.current_rate()
    df = calibrate_chain(df, s, r)
    iv = df["iv"].to_numpy()
    valid = iv > 1e-4
    q = carry(df)

    g = np.where(valid, greeks.gamma(s, df["strike"], t, r, np.where(valid, iv, 1.0), q),
                 df["gamma_cboe"])
    is_call = (df["type"] == "C").to_numpy()
    d_call = greeks.call_delta(s, df["strike"], t, r, np.where(valid, iv, 1.0), q)
    d_put = greeks.put_delta(s, df["strike"], t, r, np.where(valid, iv, 1.0), q)
    d = np.where(valid, np.where(is_call, d_call, d_put), df["delta_cboe"])

    df["gamma_bs"] = g
    df["delta_bs"] = d

    oi = df["open_interest"].to_numpy()
    sign = np.where(is_call, 1.0, -1.0)
    df["multiplier"] = float(CONTRACT_MULTIPLIER)
    df["gex"] = greeks.gex_dollars(sign, g, oi, CONTRACT_MULTIPLIER, s)
    # Convention DIFFÉRENTE de celle du GEX, et c'est voulu. Le gamma d'une
    # option est TOUJOURS positif (call comme put) : sans un signe artificiel,
    # calls et puts seraient indiscernables — d'où le flip `sign` (dealers
    # longs calls / courts puts) qui fait tout le travail pour le GEX.
    # Le delta, lui, a DÉJÀ un signe naturel opposé entre call (positif) et
    # put (négatif) : réappliquer le MÊME flip différentiel par-dessus (essayé
    # le 2026-07-27, corrigé le 2026-07-28) rend CHAQUE contrat positif sans
    # exception — un call devient +δ_call, un put devient -1×δ_put = +|δ_put| :
    # les deux positifs, plus aucun strike ne peut jamais ressortir négatif.
    # Ce n'était pas visible sur les tests d'agrégat (qui ne regardent que le
    # NET), mais sautait aux yeux sur le graphique par strike — barres toutes
    # bleues, plus aucune rouge.
    #
    # La convention correcte pour le DEX (cf. MenthorQ, FlashAlpha) suppose
    # les dealers COURTS des deux côtés (calls ET puts — hypothèse que les
    # clients achètent des calls pour l'upside ET des puts en protection),
    # donc une négation UNIFORME du delta brut, pas un flip différentiel :
    # court un call -> -δ_call (négatif, cohérent) ; court un put ->
    # -δ_put = +|δ_put| (positif, cohérent) — les deux types redeviennent
    # discernables. Le NET reste cohérent avec le récit du GEX : plus de puts
    # -> dealers plus courts puts -> plus longs delta -> DEX net positif,
    # exactement comme avant, mais construit sans casser le signe par strike.
    df["dex"] = -1.0 * d * oi * CONTRACT_MULTIPLIER * s
    # Spot répété sur chaque ligne : un snapshot persisté devient ainsi
    # auto-suffisant, et le backtest peut en recalculer les niveaux sans aller
    # chercher le prix ailleurs. Une constante ne coûte rien en Parquet.
    df["spot"] = float(s)
    return df


def add_second_order(df: pd.DataFrame, spot: float) -> pd.DataFrame:
    """Ajoute vanna/charm et leurs expositions en $.

    Conventions (mêmes hypothèses de signe que le GEX : dealers longs calls,
    courts puts) :
    - vex   : $ de delta par POINT DE VOL (1 %) — l'ampleur du re-hedging
              quand l'IV bouge d'un point.
    - cex   : $ de delta par JOUR écoulé — le flux mécanique que les dealers
              doivent absorber par simple passage du temps.
    """
    d = df.copy()
    valid = d["iv"] > 1e-4
    iv = np.where(valid, d["iv"].to_numpy(), 1.0)
    t = d["t_years"].to_numpy()
    k = d["strike"].to_numpy()
    r = rates.current_rate()
    q = carry(d)
    is_call = (d["type"] == "C").to_numpy()
    v = greeks.vanna(spot, k, t, r, iv, q)
    clock = variance_clock(d)
    if clock is None:
        c = greeks.charm_per_day(spot, k, t, r, iv, q, is_call)
    else:
        # par SÉANCE de variance (1/252 d'année sur l'horloge de variance) :
        # le delta qui fond d'ici la prochaine clôture, nuit et week-end compris
        tv, sv, rv, qv = clock
        c = greeks.charm(spot, k, tv, rv, np.where(valid, sv, 1.0), qv, is_call) / TRADING_DAYS
    v = np.where(valid, v, 0.0)
    c = np.where(valid, c, 0.0)
    sign = np.where((d["type"] == "C").to_numpy(), 1.0, -1.0)
    oi = d["open_interest"].to_numpy()
    d["vanna"] = v
    d["charm"] = c
    mult = multiplier(d)
    d["vex"] = sign * v * 0.01 * oi * mult * spot
    d["cex"] = sign * c * oi * mult * spot
    return d


def bucket_mask(df: pd.DataFrame, bucket: str, today: date) -> pd.Series:
    if bucket == "0DTE":
        # échéance la plus proche : le vrai 0DTE en séance (elle == today),
        # la prochaine séance hors séance/week-end (cohérent avec top_gex_levels
        # et le bandeau de niveaux, qui utilisent aussi l'échéance min).
        if df.empty:
            return pd.Series(False, index=df.index)
        return df["expiry"] == df["expiry"].min()
    if bucket == "Semaine":
        return df["expiry"] <= today + timedelta(days=7)
    if bucket == "Mois":
        return df["expiry"] <= today + timedelta(days=35)
    return pd.Series(True, index=df.index)


def expiry_weights(df: pd.DataFrame) -> np.ndarray:
    """Poids continu par échéance : exp(−séances restantes / τ).

    Alternative aux paliers 0DTE/Semaine/Mois, qui comptent à égalité un
    contrat à 1 jour et un à 6 jours. τ (SETTINGS.weighted_tau_sessions, 5 par
    défaut) fixe l'horizon : à 1 séance un contrat pèse ~82 %, à 5 ~37 %, à
    un mois ~1,5 %. Séances mesurées sur l'horloge de variance si disponible."""
    if "t_var" in df:
        sessions = df["t_var"].to_numpy(dtype=float) * TRADING_DAYS
    else:
        sessions = df["t_years"].to_numpy(dtype=float) * TRADING_DAYS
    return np.exp(-sessions / SETTINGS.weighted_tau_sessions)


def select_bucket(df: pd.DataFrame, bucket: str, today: date) -> pd.DataFrame:
    """Périmètre d'échéances affiché. Pour "Pondéré", toutes les échéances
    restent mais leur open interest et volume (les poids de tous les calculs
    en aval : murs, profils, flip) sont multipliés par `expiry_weights`."""
    if bucket != "Pondéré" or df.empty:
        return df[bucket_mask(df, bucket, today)]
    w = expiry_weights(df)
    out = df.copy()
    for col in ("open_interest", "volume", "gex", "dex"):
        if col in out:
            out[col] = out[col].to_numpy(dtype=float) * w
    return out


def exposure_by_strike(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Agrège gex/dex par strike, calls et puts séparés + net."""
    pivot = df.pivot_table(index="strike", columns="type", values=col, aggfunc="sum").fillna(0.0)
    for side in ("C", "P"):
        if side not in pivot:
            pivot[side] = 0.0
    pivot["net"] = pivot["C"] + pivot["P"]
    return pivot.reset_index()


def _sticky_moneyness_iv(d: pd.DataFrame, spot: float, grid: np.ndarray) -> np.ndarray:
    """IV (contrats × grille) si le smile suit le spot : au spot S', le strike K
    prend l'IV que le smile actuel donne à K·S/S' (même moneyness). Une
    interpolation par échéance ; extrapolation plate aux bords."""
    k = d["strike"].to_numpy(dtype=float)
    out = np.empty((len(d), len(grid)))
    pos = np.arange(len(d))
    for _, idx in d.groupby("t_years").indices.items():
        e = d.iloc[idx]
        smile = e.groupby("strike")["iv"].mean()
        ks, ivs = smile.index.to_numpy(dtype=float), smile.to_numpy(dtype=float)
        rows = pos[idx]
        out[rows] = np.interp(k[rows, None] * spot / grid[None, :], ks, ivs)
    return out


def gamma_profile(df: pd.DataFrame, spot: float, weight_col: str = "open_interest",
                  range_pct: float | None = None, steps: int | None = None,
                  sticky: str = "strike") -> tuple[np.ndarray, np.ndarray] | None:
    """Profil de GEX net recalculé sur une grille de spots hypothétiques.

    `sticky="strike"` (défaut) : IV et maturités figées, on ne simule que le
    déplacement du spot, ce qui isole l'effet de position. La pente au niveau
    du spot dit à quelle vitesse le régime se dégrade ; les creux signalent
    les zones d'accélération.

    `sticky="moneyness"` : le smile se déplace avec le spot (cf.
    `_sticky_moneyness_iv`) — plus réaliste en marché baissier, où le skew
    suit le prix. L'écart entre les deux flips borne l'incertitude due à la
    dynamique de vol.

    Retourne (grille de spots, GEX net en $ par 1 %), ou None si rien d'exploitable.
    """
    d = df[(df["iv"] > 1e-4) & (df[weight_col] > 0)]
    if d.empty:
        return None
    rng = SETTINGS.zg_range if range_pct is None else range_pct
    n = SETTINGS.zg_steps if steps is None else steps
    grid = np.linspace(spot * (1 - rng), spot * (1 + rng), n)
    k = d["strike"].to_numpy()[:, None]
    t = d["t_years"].to_numpy()[:, None]
    iv = (_sticky_moneyness_iv(d, spot, grid) if sticky == "moneyness"
          else d["iv"].to_numpy()[:, None])
    oi = d[weight_col].to_numpy()[:, None]
    sign = np.where((d["type"] == "C").to_numpy()[:, None], 1.0, -1.0)
    q = np.asarray(carry(d))
    q = q[:, None] if q.ndim else q
    g = greeks.gamma(grid[None, :], k, t, rates.current_rate(), iv, q)
    mult = np.asarray(multiplier(d))
    mult = mult[:, None] if mult.ndim else mult
    profile = greeks.gex_dollars(sign, g, oi, mult, grid[None, :]).sum(axis=0)
    return grid, profile


def gex_at_spot(df: pd.DataFrame, ref_spot: float,
                weight_col: str = "open_interest") -> pd.Series:
    """GEX par strike, gamma RECALCULÉ à un spot de référence donné.

    Distinct de `gex_by_strike_weighted`, qui réutilise le gamma déjà stocké —
    donc celui du spot au moment du pull.

    Pourquoi c'est nécessaire : le gamma culmine à la monnaie. Évalué au spot
    courant, le strike au plus fort |GEX| migre avec le prix, et le « mur »
    finit par désigner l'endroit où se trouve le marché plutôt qu'une zone de
    couverture. Mesuré sur une chaîne SPX réelle, faire varier la référence de
    7350 à 7500 déplace les cinq murs de bout en bout.

    Un mur est une propriété de la distribution d'open interest, qui ne change
    qu'une fois par jour. L'évaluer à un spot figé — la clôture de la veille,
    quand cet open interest a été arrêté — le rend stable en séance, ce qu'un
    plan de trading exige.
    """
    d = df[(df["iv"] > 1e-4) & (df[weight_col] > 0)]
    if d.empty:
        return pd.Series(dtype=float)
    g = greeks.gamma(ref_spot, d["strike"].to_numpy(), d["t_years"].to_numpy(),
                     rates.current_rate(), d["iv"].to_numpy(), carry(d))
    sign = np.where((d["type"] == "C").to_numpy(), 1.0, -1.0)
    gex = greeks.gex_dollars(sign, g, d[weight_col].to_numpy(),
                             multiplier(d), ref_spot)
    return pd.Series(gex, index=d["strike"].to_numpy()).groupby(level=0).sum()


def gex_by_strike_weighted(df: pd.DataFrame, spot: float,
                           weight_col: str = "open_interest") -> pd.Series:
    """GEX par strike, pondéré par l'open interest ou par le volume du jour.

    Les deux racontent des choses différentes : l'open interest décrit le
    positionnement installé, le volume ce qui se traite aujourd'hui et donc se
    couvre maintenant. Superposés, l'écart entre les deux signale un strike qui
    prend de l'importance en séance sans figurer dans la structure de la veille.
    """
    if df.empty or weight_col not in df.columns:
        return pd.Series(dtype=float)
    sign = np.where((df["type"] == "C").to_numpy(), 1.0, -1.0)
    gex = greeks.gex_dollars(sign, df["gamma_bs"].to_numpy(), df[weight_col].to_numpy(),
                             multiplier(df), spot)
    return pd.Series(gex, index=df["strike"].to_numpy()).groupby(level=0).sum()


def net_gex_at(df: pd.DataFrame, spot: float,
               weight_col: str = "open_interest") -> float | None:
    """GEX net recalculé à un spot donné, IV et maturités figées.

    Sert à rafraîchir le GEX net au spot temps réel sans chaîne d'options
    fraîche : l'open interest ne change qu'une fois par jour et l'IV bouge
    lentement, alors que le gamma de chaque contrat suit le spot en continu.
    C'est donc le spot qui rend la mesure périmée, pas la chaîne.

    Le calcul est celui de `gamma_profile` évalué en un point, donc sur le même
    sous-ensemble de contrats que le Gamma Flip : GEX net et distance au flip
    restent cohérents entre eux, ce qui est ce qui compte pour lire le régime.
    """
    res = gamma_profile(df, spot, weight_col, range_pct=0.0, steps=1)
    return None if res is None else float(res[1][0])


def zero_gamma(df: pd.DataFrame, spot: float, weight_col: str = "open_interest") -> float | None:
    """Niveau de spot où le GEX net (recalculé à ce spot) change de signe.

    Recalcule le gamma BS sur une grille de spots ±zg_range en gardant IV et
    t figés, puis interpole le passage par zéro le plus proche du spot.

    weight_col="open_interest" : le flip structurel (zero gamma classique).
    weight_col="volume"        : le HVL façon volatility trigger — bascule du
    profil pondéré par ce qui se traite (et donc se hedge) aujourd'hui.

    Détail du résultat (statut, pente, fenêtre) : `zero_gamma_info`.
    """
    return zero_gamma_info(df, spot, weight_col)["level"]


def zero_gamma_band(df: pd.DataFrame, spot: float) -> tuple[float | None, float | None]:
    """(flip sticky strike, flip sticky moneyness) : la fourchette du Gamma
    Flip selon que le smile reste accroché aux strikes ou suit le spot."""
    return (zero_gamma_info(df, spot)["level"],
            zero_gamma_info(df, spot, sticky="moneyness")["level"])


# Fenêtres successives de recherche du flip : la première est celle du
# réglage (±8 % par défaut) ; un marché qui a dérivé loin de son flip le
# retrouve dans les suivantes au lieu de renvoyer « rien » sans explication.
ZG_FALLBACK_RANGES = (0.15, 0.25)


def _nearest_crossing(grid: np.ndarray, profile: np.ndarray,
                      spot: float) -> tuple[float, float] | None:
    """(niveau interpolé, pente $/pt) du passage par zéro le plus proche du spot."""
    crossings = np.where(np.diff(np.sign(profile)) != 0)[0]
    if len(crossings) == 0:
        return None
    idx = crossings[np.argmin(np.abs(grid[crossings] - spot))]
    x0, x1 = grid[idx], grid[idx + 1]
    y0, y1 = profile[idx], profile[idx + 1]
    return float(x0 - y0 * (x1 - x0) / (y1 - y0)), float((y1 - y0) / (x1 - x0))


ZG_LIVE_REFRESH_S = 30
# chaîne réévaluée au spot live -> (réf. à elle-même, réf. à la salve, spot
# de la salve), cf. register_live_alias
_LIVE_ALIAS: dict[int, tuple] = {}


def register_live_alias(live: pd.DataFrame, base: pd.DataFrame, base_spot: float) -> None:
    """Déclare `live` comme la salve `base` réévaluée à un autre spot (mêmes
    contrats, même OI, même IV) : son Gamma Flip est alors celui de la salve.
    Un sous-ensemble de `live` (autre objet) n'est jamais concerné."""
    with _ZG_LOCK:
        for k in [k for k, v in _LIVE_ALIAS.items() if v[0]() is None]:
            del _LIVE_ALIAS[k]
        _LIVE_ALIAS[id(live)] = (weakref.ref(live), weakref.ref(base), float(base_spot))


_ZG_MEMO: dict[tuple, tuple] = {}
_ZG_MEMO_MAX = 64
_ZG_LOCK = threading.Lock()


def zero_gamma_info(df: pd.DataFrame, spot: float,
                    weight_col: str = "open_interest", sticky: str = "strike") -> dict:
    """Mémoïsé par chaîne (même objet DataFrame) et paramètres : le résumé,
    les tuiles et l'API demandent le même flip pour la même version de chaîne
    (réévaluée toutes les ~2 s en OPRA, cf. gex/livechain.py)."""
    alias = _LIVE_ALIAS.get(id(df))
    if alias is not None and alias[0]() is df and alias[1]() is not None:
        base, base_spot = alias[1](), alias[2]
        if weight_col == "open_interest" and sticky == "strike":
            # IV figée : le profil ne dépend pas du spot courant (il ne fait que
            # centrer la grille) — le flip de la salve est le bon, calculé une fois
            return zero_gamma_info(base, base_spot, weight_col, sticky)
        # HVL (volume live) et flip sticky moneyness (suit le spot) : recalculés
        # sur la chaîne réévaluée, au plus toutes les ZG_LIVE_REFRESH_S
        key = (id(base), int(time_mod.time() // ZG_LIVE_REFRESH_S), weight_col, sticky)
    else:
        key = (id(df), float(spot), weight_col, sticky, SETTINGS.zg_range, SETTINGS.zg_steps)
    with _ZG_LOCK:
        hit = _ZG_MEMO.get(key)
    if alias is not None and hit is not None and hit[0]() is alias[1]():
        return dict(hit[1])
    if hit is not None and hit[0]() is df:
        return dict(hit[1])
    out = _zero_gamma_info(df, spot, weight_col, sticky)
    try:
        ref = weakref.ref(alias[1]() if alias is not None else df)
    except TypeError:
        return out
    with _ZG_LOCK:
        if len(_ZG_MEMO) >= _ZG_MEMO_MAX:
            for k in [k for k, v in _ZG_MEMO.items() if v[0]() is None] or list(_ZG_MEMO)[:16]:
                _ZG_MEMO.pop(k, None)
        _ZG_MEMO[key] = (ref, dict(out))
    return out


def _zero_gamma_info(df: pd.DataFrame, spot: float,
                     weight_col: str = "open_interest", sticky: str = "strike") -> dict:
    """Zero gamma avec son contexte :

    - `status` : "ok", "no_flip" (profil d'un seul signe jusqu'à ±25 % — le
      régime est franc, ce n'est pas une panne) ou "no_data" (aucun contrat
      exploitable) ;
    - `slope`  : pente du GEX net au flip, en $ par point. Une pente faible
      veut dire un flip mal défini, qu'un petit changement d'OI déplace loin ;
    - `range`  : demi-largeur de la fenêtre (fraction du spot) où il a été trouvé.
    """
    out = {"level": None, "status": "no_data", "slope": None, "range": None}
    base = SETTINGS.zg_range
    for rng in (base, *[r for r in ZG_FALLBACK_RANGES if r > base]):
        # pas de grille constant quelle que soit la fenêtre
        steps = max(SETTINGS.zg_steps, int(round(SETTINGS.zg_steps * rng / base)))
        res = gamma_profile(df, spot, weight_col, range_pct=rng, steps=steps, sticky=sticky)
        if res is None:
            return out
        out["status"], out["range"] = "no_flip", rng
        hit = _nearest_crossing(*res, spot)
        if hit is not None:
            out["level"], out["slope"] = hit
            out["status"] = "ok"
            return out
    log.info("Zero gamma (%s) : aucun flip à ±%.0f %% du spot %.2f",
             weight_col, out["range"] * 100, spot)
    return out


def vanna_profile(df: pd.DataFrame, spot: float, weight_col: str = "open_interest",
                  range_pct: float | None = None, steps: int | None = None
                  ) -> tuple[np.ndarray, np.ndarray] | None:
    """Même principe que `gamma_profile`, pour la vanna : profil de VEX net
    recalculé sur une grille de spots hypothétiques, IV et maturités figées.

    Sert à chercher où l'exposition vanna nette change de signe (cf.
    `zero_vanna`) — un niveau DIFFÉRENT du Gamma Flip (`zero_gamma`), constaté
    le 2026-09-30 : un « VFlip » partagé par un tiers (~30 817 sur NQ) ne
    correspondait à aucun de nos deux zero_gamma (NDX transposé ni natif NQ),
    écart de 500 à 1100 pts — trop grand pour être du bruit. Hypothèse (non
    confirmée formellement) : ce tiers désignait un flip de VANNA, pas de
    gamma — deux mécaniques distinctes (sensibilité au spot vs à la vol)."""
    d = df[(df["iv"] > 1e-4) & (df[weight_col] > 0)]
    if d.empty:
        return None
    rng = SETTINGS.zg_range if range_pct is None else range_pct
    n = SETTINGS.zg_steps if steps is None else steps
    grid = np.linspace(spot * (1 - rng), spot * (1 + rng), n)
    k = d["strike"].to_numpy()[:, None]
    t = d["t_years"].to_numpy()[:, None]
    iv = d["iv"].to_numpy()[:, None]
    oi = d[weight_col].to_numpy()[:, None]
    sign = np.where((d["type"] == "C").to_numpy()[:, None], 1.0, -1.0)
    q = np.asarray(carry(d))
    q = q[:, None] if q.ndim else q
    v = greeks.vanna(grid[None, :], k, t, rates.current_rate(), iv, q)
    mult = np.asarray(multiplier(d))
    mult = mult[:, None] if mult.ndim else mult
    profile = (sign * v * 0.01 * oi * mult * grid[None, :]).sum(axis=0)
    return grid, profile


def zero_vanna(df: pd.DataFrame, spot: float, weight_col: str = "open_interest") -> float | None:
    """Niveau de spot où le VEX net (recalculé à ce spot) change de signe —
    même mécanique que `zero_gamma`, cf. `vanna_profile` pour le pourquoi."""
    res = vanna_profile(df, spot, weight_col)
    if res is None:
        return None
    hit = _nearest_crossing(*res, spot)
    return None if hit is None else hit[0]


def third_friday(year: int, month: int) -> date:
    """3e vendredi du mois — échéance des futures index CME."""
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def front_futures_expiry(today: date) -> date:
    """Échéance du future front month (trimestriel : mars/juin/sept/déc)."""
    for y in (today.year, today.year + 1):
        for m in (3, 6, 9, 12):
            e = third_friday(y, m)
            if e >= today:
                return e
    raise ValueError("échéance introuvable")


def futures_basis(df: pd.DataFrame, spot: float, today: date | None = None) -> float | None:
    """Basis future - spot, déduit de la parité call-put : F = (C-P)·e^(rT) + K.

    Utilise l'échéance d'options la plus proche de celle du future front month,
    et la médiane sur les strikes proches de la monnaie (robuste aux quotes
    aberrantes). Retourne None si aucune paire exploitable.

    Le basis décroît vers 0 à l'approche de l'échéance : il est recalculé à
    chaque pull, jamais figé.
    """
    if df.empty:
        return None
    today = today or datetime.now(ET).date()
    target_exp = front_futures_expiry(today)
    exps = df["expiry"].unique()
    if len(exps) == 0:
        return None
    target = min(exps, key=lambda e: abs((e - target_exp).days))

    fwd = implied_forwards(df[df["expiry"] == target], spot, rates.current_rate(),
                           window=0.05, min_pairs=5, max_basis=0.02)
    # garde-fou max_basis : le basis d'un future index reste sous ~2 % du spot
    # (portage taux - dividendes sur < 1 an). Au-delà, les quotes sont
    # aberrantes et une conversion silencieuse fausserait tous les niveaux.
    return fwd[target] - spot if target in fwd else None


def top_gex_levels(df: pd.DataFrame, n: int = 5,
                   ref_spot: float | None = None,
                   all_expiries: bool = False) -> pd.DataFrame:
    """Les n strikes au |GEX| le plus fort.

    Par défaut sur l'échéance la plus proche (le 0DTE en séance ; la prochaine
    séance après la cloche). `all_expiries=True` agrège TOUTES les échéances du
    df fourni — c'est le point d'entrée `compute_levels` qui fixe alors le
    périmètre en amont (par bucket).

    `ref_spot` fige le spot auquel le gamma est évalué — la clôture de la
    veille, quand l'open interest a été arrêté. Sans lui, le gamma est repris
    du dernier pull et les murs se déplacent avec le prix (cf. `gex_at_spot`).

    Retourne strike, gex net, rang (1 = mur le plus fort) et l'expiration utilisée.
    """
    if df.empty:
        return pd.DataFrame()
    nearest = df["expiry"].min()
    sub = df if all_expiries else df[df["expiry"] == nearest]
    if ref_spot:
        agg = gex_at_spot(sub, ref_spot).rename("gex").reset_index()
        agg = agg.rename(columns={"index": "strike"})
    else:
        agg = sub.groupby("strike")["gex"].sum().reset_index()
    agg = agg.loc[agg["gex"].abs().nlargest(n).index]
    agg = agg.sort_values("gex", key=abs, ascending=False).reset_index(drop=True)
    agg["rank"] = agg.index + 1
    agg["expiry"] = nearest
    return agg


def expected_move(df: pd.DataFrame, spot: float) -> float | None:
    """Move attendu sur l'échéance la plus proche, via le straddle ATM.

    Le prix du straddle à la monnaie EST l'estimation de move du marché, sans
    hypothèse de modèle. Sert de bornes 1D Min / 1D Max (façon MenthorQ).
    """
    if df.empty:
        return None
    nearest = df["expiry"].min()
    e = df[df["expiry"] == nearest]
    e = e.sort_values("volume").drop_duplicates(["type", "strike"], keep="last")
    calls = e[e["type"] == "C"].set_index("strike")
    puts = e[e["type"] == "P"].set_index("strike")
    common = calls.index.intersection(puts.index)
    if len(common) == 0:
        return None
    k_atm = min(common, key=lambda k: abs(k - spot))

    def _price(side: pd.DataFrame) -> float | None:
        """Milieu de fourchette, à défaut le close.

        Les chaînes reconstruites depuis Databento ne portent pas de bid/ask :
        ce sont des photos de clôture, où le prix de règlement tient lieu de
        valorisation. Le straddle y reste calculable.
        """
        row = side.loc[k_atm]
        if "bid" in side.columns and "ask" in side.columns:
            mid = (row["bid"] + row["ask"]) / 2
            if mid > 0:
                return float(mid)
        close = row.get("close")
        return float(close) if close is not None and close > 0 else None

    cmid, pmid = _price(calls), _price(puts)
    if not cmid or not pmid:
        return None
    move = float(cmid + pmid)
    # garde-fou : un move attendu > 10 % du spot sur l'échéance front est
    # incohérent hors krach — quotes probablement aberrantes.
    return move if 0 < move < 0.10 * spot else None


def key_levels(df: pd.DataFrame, spot: float,
               ref_spot: float | None = None,
               all_expiries: bool = False) -> dict[str, float | None]:
    """Niveaux directionnels (esprit MenthorQ) :

    - call_wall  : plus forte concentration de gamma call AU-DESSUS du spot
                   (résistance)
    - put_support: plus forte concentration de gamma put SOUS le spot (support)
    - d1_max/d1_min : bornes de move attendu (straddle ATM)

    Contrairement au classement GEX1-5 (non directionnel), ces niveaux ne sont
    cherchés que du côté où ils font sens comme support/résistance.

    Par défaut sur l'échéance la plus proche ; `all_expiries=True` agrège tout
    le df fourni (périmètre fixé en amont par `compute_levels`).
    """
    out: dict[str, float | None] = {
        "call_wall": None, "put_support": None, "d1_min": None, "d1_max": None,
    }
    if df.empty:
        return out
    nearest = df["expiry"].min()
    sub = df if all_expiries else df[df["expiry"] == nearest]
    # Le CLASSEMENT des murs se fait au spot de référence (structure figée) ;
    # le côté où on les cherche dépend en revanche du spot COURANT, une
    # résistance n'ayant de sens qu'au-dessus du marché du moment.
    agg = gex_at_spot(sub, ref_spot) if ref_spot else sub.groupby("strike")["gex"].sum()

    above = agg[(agg.index >= spot) & (agg > 0)]
    if len(above):
        out["call_wall"] = float(above.idxmax())
    below = agg[(agg.index <= spot) & (agg < 0)]
    if len(below):
        out["put_support"] = float(below.idxmin())

    move = expected_move(df, spot)
    if move is not None:
        out["d1_min"] = spot - move
        out["d1_max"] = spot + move
    return out


def max_pain(df: pd.DataFrame) -> float | None:
    """Prix de règlement de l'échéance la plus proche qui minimise la valeur
    totale versée aux détenteurs : Σ OI_call·(X−K)⁺ + Σ OI_put·(K−X)⁺.

    ⚠ Heuristique faible : elle suppose que les vendeurs d'options pilotent
    le règlement à leur avantage, ce qui n'est pas un modèle de couverture.
    Utile comme repère d'aimantation le jour de l'échéance, sans plus.
    """
    if df.empty:
        return None
    e = df[df["expiry"] == df["expiry"].min()]
    e = e[e["open_interest"] > 0]
    if e.empty:
        return None
    k = e["strike"].to_numpy(dtype=float)
    oi = e["open_interest"].to_numpy(dtype=float)
    is_call = (e["type"] == "C").to_numpy()
    x = np.unique(k)[:, None]
    pay = np.where(is_call, np.maximum(x - k, 0.0), np.maximum(k - x, 0.0)) * oi
    return float(x[np.argmin(pay.sum(axis=1)), 0])


def compute_levels(chain: pd.DataFrame, structural_spot: float, live_spot: float,
                   bucket: str = "0DTE", today: date | None = None,
                   n: int = 5) -> dict:
    """POINT D'ENTRÉE UNIQUE des niveaux affichés (murs GEX1-5 + call/put wall).

    Dashboard, API et bot l'appellent tous, pour ne PLUS JAMAIS diverger sur le
    spot de référence ou le périmètre d'échéances (le vrai bug identifié).

    - `structural_spot` : spot figé (clôture veille) → **magnitude** des murs
      (l'OI est une photo, on ne veut pas que le prix live déplace les murs) ;
    - `live_spot` : spot courant → **côté** (au-dessus/en dessous = résistance /
      support) ;
    - `bucket` : périmètre d'échéances (0DTE / Semaine / Mois / Tout), plus de
      filtre `expiry.min()` caché — les murs suivent ce qu'affiche l'interface.

    Renvoie {"levels": DataFrame GEX1-n, "keys": dict call_wall/put_support/1D}.
    """
    today = today or datetime.now(ET).date()
    sub = select_bucket(chain, bucket, today)
    keys = key_levels(sub, live_spot, ref_spot=structural_spot, all_expiries=True)
    keys["max_pain"] = max_pain(sub)
    return {
        "levels": top_gex_levels(sub, n=n, ref_spot=structural_spot, all_expiries=True),
        "keys": keys,
    }


def put_call_ratios(df: pd.DataFrame) -> dict[str, float]:
    calls = df[df["type"] == "C"]
    puts = df[df["type"] == "P"]
    oi_c, oi_p = calls["open_interest"].sum(), puts["open_interest"].sum()
    v_c, v_p = calls["volume"].sum(), puts["volume"].sum()
    return {
        "pc_oi": float(oi_p / oi_c) if oi_c > 0 else float("nan"),
        "pc_volume": float(v_p / v_c) if v_c > 0 else float("nan"),
    }


@dataclass
class SummaryMetrics:
    timestamp: datetime
    symbol: str
    spot: float
    net_gex: float
    zero_gamma: float | None
    pc_oi: float
    pc_volume: float
    net_gex_0dte: float = 0.0
    basis: float | None = None   # future front month - spot, suivi dans le temps
    net_dex: float = 0.0
    # Provenance de la ligne. Détermine ce qui peut être partagé : "cboe" =
    # source publique gratuite, redistribuable ; "databento" = source payante
    # sous licence d'usage personnel, NON redistribuable.
    source: str = "cboe"

    def as_row(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "spot": self.spot,
            "net_gex": self.net_gex,
            "zero_gamma": self.zero_gamma,
            "pc_oi": self.pc_oi,
            "pc_volume": self.pc_volume,
            "net_gex_0dte": self.net_gex_0dte,
            "basis": self.basis,
            "net_dex": self.net_dex,
            "source": self.source,
        }


def summarize(snapshot: ChainSnapshot, df: pd.DataFrame,
              with_basis: bool = True) -> SummaryMetrics:
    """with_basis=False pour les sous-jacents sans future associé (ETF) :
    la parité call-put y mesurerait un simple report de dividendes, qu'il
    serait trompeur de stocker sous le nom de « basis »."""
    today = datetime.now(ET).date()
    ratios = put_call_ratios(df)
    return SummaryMetrics(
        timestamp=snapshot.feed_timestamp,
        symbol=snapshot.symbol,
        spot=snapshot.spot,
        net_gex=float(df["gex"].sum()),
        zero_gamma=zero_gamma(df, snapshot.spot),
        pc_oi=ratios["pc_oi"],
        pc_volume=ratios["pc_volume"],
        net_gex_0dte=float(df.loc[bucket_mask(df, "0DTE", today), "gex"].sum()),
        basis=futures_basis(df, snapshot.spot, today) if with_basis else None,
        net_dex=float(df["dex"].sum()),
    )


def regime_read(net_gex: float, net_dex: float,
                 dex_history: pd.Series | None = None) -> dict:
    """Lecture croisée Gamma/Delta : mécanique de couverture des dealers, pas
    un signal d'entrée. Deux axes indépendants :

    - GEX (comment un mouvement se comporte une fois lancé) : positif = les
      dealers vendent les hausses et achètent les baisses, donc freinent ;
      négatif = l'inverse, donc amplifient.
    - DEX (le sens de l'obligation de couverture latente des dealers, sous
      l'hypothèse longs calls / courts puts, cf. `enrich`) : positif = ils
      sont structurellement LONGS delta (côté puts vendus qui s'enfoncent
      dans la monnaie) → pression de couverture VENDEUSE latente, biais
      baissier si un mouvement démarre. Négatif = l'inverse (short delta),
      pression ACHETEUSE latente, biais haussier.

    Ni l'un ni l'autre ne dit SI un mouvement démarre, ni dans quel sens il
    démarre — seulement sa nature probable s'il se produit. `dex_history`
    (série de |net_dex| passés du même sous-jacent) sert uniquement à situer
    l'ampleur actuelle par rang percentile ; sans historique suffisant (< 20
    points), la magnitude est laissée à None plutôt que devinée.
    """
    gex_frein = net_gex >= 0
    dex_long = net_dex >= 0   # dealers structurellement longs delta
    sens_delta = "long" if dex_long else "short"
    # codes neutres (pas de mot figé dans une langue) : gex/i18n.py les
    # traduit en "vendeuse"/"acheteuse" (fr) ou "selling"/"buying" (en)
    pression_code = "sell" if dex_long else "buy"
    biais_code = "down" if dex_long else "up"

    magnitude = None
    if dex_history is not None:
        ref = dex_history.dropna().abs()
        if len(ref) >= 20:
            rank = (ref < abs(net_dex)).mean()
            magnitude = "fort" if rank >= 0.67 else ("faible" if rank <= 0.33 else None)

    params = {"sens_delta": sens_delta, "pression_code": pression_code,
              "biais_code": biais_code}
    if gex_frein:
        key, severity = "regime_frein", "info"
    elif magnitude == "fort":
        key, severity = "regime_accel_fort", "danger"
    else:
        key, severity = "regime_accel_modere", "warning"

    return {
        "gex_frein": gex_frein,
        "dex_sign": sens_delta,
        "magnitude": magnitude,
        "severity": severity,
        "i18n_key": key,
        "params": params,
    }


def oi_change(prev: pd.DataFrame, cur: pd.DataFrame) -> pd.DataFrame:
    """Variation d'open interest par strike entre deux séances.

    L'OI n'est publié qu'une fois par jour (matin, par l'OCC) : la différence
    entre deux séances mesure le positionnement NET réellement ouvert ou
    fermé, à distinguer du gamma résiduel hérité de positions anciennes.

    Retourne un DataFrame strike / d_call / d_put / d_net / oi_call / oi_put.
    """
    if prev is None or prev.empty or cur.empty:
        return pd.DataFrame()
    keys = ["strike", "type"]
    a = cur.groupby(keys)["open_interest"].sum().rename("cur")
    b = prev.groupby(keys)["open_interest"].sum().rename("prev")
    m = pd.concat([a, b], axis=1).fillna(0.0).reset_index()
    m["delta"] = m["cur"] - m["prev"]
    piv = m.pivot_table(index="strike", columns="type",
                        values=["delta", "cur"], aggfunc="sum").fillna(0.0)
    out = pd.DataFrame({"strike": piv.index})
    for side, name in (("C", "call"), ("P", "put")):
        out[f"d_{name}"] = piv["delta"][side].to_numpy() if side in piv["delta"] else 0.0
        out[f"oi_{name}"] = piv["cur"][side].to_numpy() if side in piv["cur"] else 0.0
    out["d_net"] = out["d_call"] - out["d_put"]
    return out.reset_index(drop=True)


def flow_delta(prev: pd.DataFrame, cur: pd.DataFrame, spot: float) -> dict[str, float]:
    """Proxy de flux delta entre deux pulls : Δvolume × delta × mult × spot.

    Le sens taker (achat/vente) n'est pas observable dans ce feed : c'est un
    proxy de pression delta-pondérée, pas un vrai order-flow signé.
    """
    m = cur.merge(
        prev[["contract", "volume"]].rename(columns={"volume": "volume_prev"}),
        on="contract",
        how="left",
    )
    dvol = (m["volume"] - m["volume_prev"].fillna(0.0)).clip(lower=0.0)
    signed = dvol * m["delta_bs"] * multiplier(m) * spot
    is_call = m["type"] == "C"
    today = datetime.now(ET).date()
    is_0dte = bucket_mask(m, "0DTE", today)

    # Gamma échangé sur l'intervalle : même formule que le GEX, mais pondérée
    # par le volume du pas de temps au lieu de l'open interest. Cumulé sur la
    # séance, cela montre si ce qui se traite ajoute du gamma stabilisant
    # (calls) ou déstabilisant (puts) — un « CVD » du gamma.
    gsign = np.where(is_call, 1.0, -1.0)
    gsigned = greeks.gex_dollars(gsign, m["gamma_bs"], dvol, multiplier(m), spot)
    return {
        "flow_total": float(signed.sum()),
        "flow_calls": float(signed[is_call].sum()),
        "flow_puts": float(signed[~is_call].sum()),
        "flow_0dte": float(signed[is_0dte].sum()),
        # gflow_calls est positif, gflow_puts négatif : leur somme est le net
        "gflow_total": float(gsigned.sum()),
        "gflow_calls": float(gsigned[is_call].sum()),
        "gflow_puts": float(gsigned[~is_call].sum()),
        "gflow_0dte": float(gsigned[is_0dte].sum()),
        "contracts_traded": float(dvol.sum()),
        "source": "cboe",   # collecté en direct sur la source publique
    }
