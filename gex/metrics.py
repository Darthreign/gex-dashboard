"""Métriques de structure de marché : GEX/DEX par strike, zero gamma,
put/call ratios, proxy de flux delta.

Convention GEX (SpotGamma "naive") :
    GEX($ par 1% de move) = gamma × OI × multiplicateur × spot² × 0.01
    calls comptés positifs, puts négatifs (hypothèse dealers longs calls /
    courts puts vendus par le marché).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import greeks
from .config import CONTRACT_MULTIPLIER, RISK_FREE_RATE, SETTINGS
from .ingest import ChainSnapshot

ET = ZoneInfo("America/New_York")
YEAR_SECONDS = 365.0 * 24 * 3600

EXPIRY_BUCKETS = ["0DTE", "Semaine", "Mois", "Tout"]


def time_to_expiry_years(expiries: pd.Series, now_et: datetime) -> np.ndarray:
    """Années jusqu'à l'expiration, échéance posée à 16:00 ET.

    Pour les 0DTE en séance, la fraction de journée restante est conservée
    (plancher 5 minutes pour éviter les gammas explosifs à la cloche).
    """
    expiry_dt = pd.to_datetime(expiries).dt.tz_localize(ET) + pd.Timedelta(hours=16)
    secs = (expiry_dt - now_et).dt.total_seconds().to_numpy()
    return np.maximum(secs, 300.0) / YEAR_SECONDS


def enrich(snapshot: ChainSnapshot, now_et: datetime | None = None) -> pd.DataFrame:
    """Ajoute t, greeks calculés (BS sur l'IV du feed) et les colonnes GEX/DEX.

    Quand l'IV du feed est nulle/absente (deep ITM sans quote), on retombe
    sur les Greeks CBOE — leur gamma est ~0 sur ces contrats de toute façon.
    """
    now_et = now_et or datetime.now(ET)
    df = snapshot.options.copy()
    df = df[df["expiry"] >= now_et.date()].reset_index(drop=True)
    s = snapshot.spot
    t = time_to_expiry_years(df["expiry"], now_et)
    iv = df["iv"].to_numpy()
    valid = iv > 1e-4

    g = np.where(valid, greeks.gamma(s, df["strike"], t, RISK_FREE_RATE, np.where(valid, iv, 1.0)), df["gamma_cboe"])
    d_call = greeks.call_delta(s, df["strike"], t, RISK_FREE_RATE, np.where(valid, iv, 1.0))
    is_call = (df["type"] == "C").to_numpy()
    d = np.where(valid, np.where(is_call, d_call, d_call - 1.0), df["delta_cboe"])

    df["t_years"] = t
    df["gamma_bs"] = g
    df["delta_bs"] = d

    oi = df["open_interest"].to_numpy()
    sign = np.where(is_call, 1.0, -1.0)
    df["gex"] = sign * g * oi * CONTRACT_MULTIPLIER * s**2 * 0.01
    df["dex"] = d * oi * CONTRACT_MULTIPLIER * s
    return df


def bucket_mask(df: pd.DataFrame, bucket: str, today: date) -> pd.Series:
    if bucket == "0DTE":
        return df["expiry"] == today
    if bucket == "Semaine":
        return df["expiry"] <= today + timedelta(days=7)
    if bucket == "Mois":
        return df["expiry"] <= today + timedelta(days=35)
    return pd.Series(True, index=df.index)


def exposure_by_strike(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Agrège gex/dex par strike, calls et puts séparés + net."""
    pivot = df.pivot_table(index="strike", columns="type", values=col, aggfunc="sum").fillna(0.0)
    for side in ("C", "P"):
        if side not in pivot:
            pivot[side] = 0.0
    pivot["net"] = pivot["C"] + pivot["P"]
    return pivot.reset_index()


def zero_gamma(df: pd.DataFrame, spot: float) -> float | None:
    """Niveau de spot où le GEX net (recalculé à ce spot) change de signe.

    Recalcule le gamma BS sur une grille de spots ±zg_range en gardant IV et
    t figés, puis interpole le passage par zéro le plus proche du spot.
    """
    d = df[(df["iv"] > 1e-4) & (df["open_interest"] > 0)]
    if d.empty:
        return None
    grid = np.linspace(spot * (1 - SETTINGS.zg_range), spot * (1 + SETTINGS.zg_range), SETTINGS.zg_steps)
    k = d["strike"].to_numpy()[:, None]
    t = d["t_years"].to_numpy()[:, None]
    iv = d["iv"].to_numpy()[:, None]
    oi = d["open_interest"].to_numpy()[:, None]
    sign = np.where((d["type"] == "C").to_numpy()[:, None], 1.0, -1.0)
    g = greeks.gamma(grid[None, :], k, t, RISK_FREE_RATE, iv)
    profile = (sign * g * oi * CONTRACT_MULTIPLIER * grid[None, :] ** 2 * 0.01).sum(axis=0)
    crossings = np.where(np.diff(np.sign(profile)) != 0)[0]
    if len(crossings) == 0:
        return None
    # passage par zéro le plus proche du spot
    idx = crossings[np.argmin(np.abs(grid[crossings] - spot))]
    x0, x1 = grid[idx], grid[idx + 1]
    y0, y1 = profile[idx], profile[idx + 1]
    return float(x0 - y0 * (x1 - x0) / (y1 - y0))


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
        }


def summarize(snapshot: ChainSnapshot, df: pd.DataFrame) -> SummaryMetrics:
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
    )


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
    signed = dvol * m["delta_bs"] * CONTRACT_MULTIPLIER * spot
    is_call = m["type"] == "C"
    today = datetime.now(ET).date()
    return {
        "flow_total": float(signed.sum()),
        "flow_calls": float(signed[is_call].sum()),
        "flow_puts": float(signed[~is_call].sum()),
        "flow_0dte": float(signed[m["expiry"] == today].sum()),
        "contracts_traded": float(dvol.sum()),
    }
