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


def seconds_to_expiry(expiries: pd.Series, now_et: datetime) -> np.ndarray:
    """Secondes jusqu'à l'expiration, échéance posée à 16:00 ET.

    Négatif = contrat expiré (0DTE après la cloche) — à exclure.
    """
    expiry_dt = pd.to_datetime(expiries).dt.tz_localize(ET) + pd.Timedelta(hours=16)
    return (expiry_dt - now_et).dt.total_seconds().to_numpy()


def enrich(snapshot: ChainSnapshot, now_et: datetime | None = None) -> pd.DataFrame:
    """Ajoute t, greeks calculés (BS sur l'IV du feed) et les colonnes GEX/DEX.

    Quand l'IV du feed est nulle/absente (deep ITM sans quote), on retombe
    sur les Greeks CBOE — leur gamma est ~0 sur ces contrats de toute façon.
    """
    now_et = now_et or datetime.now(ET)
    df = snapshot.options.copy()
    # exclut les contrats expirés (dont les 0DTE du jour après 16:00 ET,
    # dont les quotes résiduelles polluent GEX 0DTE et skew IV)
    secs = seconds_to_expiry(df["expiry"], now_et)
    df = df[secs > 0].reset_index(drop=True)
    s = snapshot.spot
    # plancher 5 min pour éviter les gammas explosifs à la cloche
    t = np.maximum(secs[secs > 0], 300.0) / YEAR_SECONDS
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
    # Même hypothèse que GEX (dealers longs calls, courts puts) : la gamma est
    # toujours positive, donc `sign` seul suffisait à distinguer calls/puts.
    # Le delta, lui, a DÉJÀ un signe naturel opposé entre call et put — sans
    # cette multiplication par `sign`, le code prenait le delta brut, ce qui
    # calculait le delta de l'OPEN INTEREST détenu long des deux côtés, pas
    # celui du dealer supposé COURT les puts.
    # Être court un put donne une exposition delta POSITIVE (delta du put
    # négatif × position courte = positif) : c'est la mécanique par laquelle
    # les dealers courts puts sont structurellement longs delta et vendent
    # dans la baisse pour rester couverts, à mesure que les puts s'enfoncent
    # dans la monnaie. Sans le flip, cette exposition ressortait négative —
    # signe opposé à ce que la même convention que GEX implique.
    df["dex"] = sign * d * oi * CONTRACT_MULTIPLIER * s
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
    v = greeks.vanna(spot, k, t, RISK_FREE_RATE, iv)
    c = greeks.charm_per_day(spot, k, t, RISK_FREE_RATE, iv)
    v = np.where(valid, v, 0.0)
    c = np.where(valid, c, 0.0)
    sign = np.where((d["type"] == "C").to_numpy(), 1.0, -1.0)
    oi = d["open_interest"].to_numpy()
    d["vanna"] = v
    d["charm"] = c
    d["vex"] = sign * v * 0.01 * oi * CONTRACT_MULTIPLIER * spot
    d["cex"] = sign * c * oi * CONTRACT_MULTIPLIER * spot
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


def exposure_by_strike(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """Agrège gex/dex par strike, calls et puts séparés + net."""
    pivot = df.pivot_table(index="strike", columns="type", values=col, aggfunc="sum").fillna(0.0)
    for side in ("C", "P"):
        if side not in pivot:
            pivot[side] = 0.0
    pivot["net"] = pivot["C"] + pivot["P"]
    return pivot.reset_index()


def gamma_profile(df: pd.DataFrame, spot: float, weight_col: str = "open_interest",
                  range_pct: float | None = None, steps: int | None = None
                  ) -> tuple[np.ndarray, np.ndarray] | None:
    """Profil de GEX net recalculé sur une grille de spots hypothétiques.

    IV et maturités sont figées : on ne simule que le déplacement du spot, ce
    qui isole l'effet de position. La pente au niveau du spot dit à quelle
    vitesse le régime se dégrade ; les creux signalent les zones
    d'accélération.

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
    iv = d["iv"].to_numpy()[:, None]
    oi = d[weight_col].to_numpy()[:, None]
    sign = np.where((d["type"] == "C").to_numpy()[:, None], 1.0, -1.0)
    g = greeks.gamma(grid[None, :], k, t, RISK_FREE_RATE, iv)
    profile = (sign * g * oi * CONTRACT_MULTIPLIER * grid[None, :] ** 2 * 0.01).sum(axis=0)
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
                     RISK_FREE_RATE, d["iv"].to_numpy())
    sign = np.where((d["type"] == "C").to_numpy(), 1.0, -1.0)
    gex = (sign * g * d[weight_col].to_numpy()
           * CONTRACT_MULTIPLIER * ref_spot ** 2 * 0.01)
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
    gex = (sign * df["gamma_bs"].to_numpy() * df[weight_col].to_numpy()
           * CONTRACT_MULTIPLIER * spot ** 2 * 0.01)
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
    """
    res = gamma_profile(df, spot, weight_col)
    if res is None:
        return None
    grid, profile = res
    crossings = np.where(np.diff(np.sign(profile)) != 0)[0]
    if len(crossings) == 0:
        return None
    # passage par zéro le plus proche du spot
    idx = crossings[np.argmin(np.abs(grid[crossings] - spot))]
    x0, x1 = grid[idx], grid[idx + 1]
    y0, y1 = profile[idx], profile[idx + 1]
    return float(x0 - y0 * (x1 - x0) / (y1 - y0))


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

    e = df[df["expiry"] == target]
    # racines multiples (SPX/SPXW) sur une même échéance : garder le plus traité
    e = e.sort_values("volume").drop_duplicates(["type", "strike"], keep="last")
    calls = e[e["type"] == "C"].set_index("strike")
    puts = e[e["type"] == "P"].set_index("strike")
    common = [k for k in calls.index.intersection(puts.index)
              if abs(k - spot) / spot < 0.05]
    fwds = []
    for k in common:
        cmid = (calls.loc[k, "bid"] + calls.loc[k, "ask"]) / 2
        pmid = (puts.loc[k, "bid"] + puts.loc[k, "ask"]) / 2
        if cmid <= 0 or pmid <= 0:
            continue
        t = calls.loc[k, "t_years"]
        fwds.append((cmid - pmid) * np.exp(RISK_FREE_RATE * t) + k)
    if len(fwds) < 5:
        return None
    basis = float(np.median(fwds)) - spot
    # garde-fou : le basis d'un future index reste sous ~2 % du spot (portage
    # taux - dividendes sur < 1 an). Au-delà, les quotes sont aberrantes et une
    # conversion silencieuse fausserait tous les niveaux.
    if abs(basis) > 0.02 * spot:
        log.warning("Basis aberrant ignoré : %+.1f pts sur spot %.0f (%d paires)",
                    basis, spot, len(fwds))
        return None
    return basis


def top_gex_levels(df: pd.DataFrame, n: int = 5,
                   ref_spot: float | None = None) -> pd.DataFrame:
    """Les n strikes au |GEX| le plus fort sur l'échéance la plus proche
    (le 0DTE en séance ; la prochaine séance après la cloche).

    `ref_spot` fige le spot auquel le gamma est évalué — la clôture de la
    veille, quand l'open interest a été arrêté. Sans lui, le gamma est repris
    du dernier pull et les murs se déplacent avec le prix (cf. `gex_at_spot`).

    Retourne strike, gex net, rang (1 = mur le plus fort) et l'expiration utilisée.
    """
    if df.empty:
        return pd.DataFrame()
    nearest = df["expiry"].min()
    sub = df[df["expiry"] == nearest]
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
               ref_spot: float | None = None) -> dict[str, float | None]:
    """Niveaux directionnels de l'échéance la plus proche (esprit MenthorQ) :

    - call_wall  : plus forte concentration de gamma call AU-DESSUS du spot
                   (résistance)
    - put_support: plus forte concentration de gamma put SOUS le spot (support)
    - d1_max/d1_min : bornes de move attendu (straddle ATM)

    Contrairement au classement GEX1-5 (non directionnel), ces niveaux ne sont
    cherchés que du côté où ils font sens comme support/résistance.
    """
    out: dict[str, float | None] = {
        "call_wall": None, "put_support": None, "d1_min": None, "d1_max": None,
    }
    if df.empty:
        return out
    nearest = df["expiry"].min()
    sub = df[df["expiry"] == nearest]
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
    signed = dvol * m["delta_bs"] * CONTRACT_MULTIPLIER * spot
    is_call = m["type"] == "C"
    today = datetime.now(ET).date()

    # Gamma échangé sur l'intervalle : même formule que le GEX, mais pondérée
    # par le volume du pas de temps au lieu de l'open interest. Cumulé sur la
    # séance, cela montre si ce qui se traite ajoute du gamma stabilisant
    # (calls) ou déstabilisant (puts) — un « CVD » du gamma.
    gsign = np.where(is_call, 1.0, -1.0)
    gsigned = (dvol * m["gamma_bs"] * gsign
               * CONTRACT_MULTIPLIER * spot ** 2 * 0.01)
    return {
        "flow_total": float(signed.sum()),
        "flow_calls": float(signed[is_call].sum()),
        "flow_puts": float(signed[~is_call].sum()),
        "flow_0dte": float(signed[m["expiry"] == today].sum()),
        # gflow_calls est positif, gflow_puts négatif : leur somme est le net
        "gflow_total": float(gsigned.sum()),
        "gflow_calls": float(gsigned[is_call].sum()),
        "gflow_puts": float(gsigned[~is_call].sum()),
        "gflow_0dte": float(gsigned[m["expiry"] == today].sum()),
        "contracts_traded": float(dvol.sum()),
        "source": "cboe",   # collecté en direct sur la source publique
    }
