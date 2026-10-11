"""Positionnement gamma des dealers sur les options NQ, reconstruit depuis
l'historique Databento (GLBX.MDP3, schéma `statistics`) — piste 2 du
rapport d'excès.

Par séance CME, UNIQUEMENT avec ce qui était publié AVANT son ouverture
(18h00 ET la veille) :
- open interest de chaque option (`stat_type` OPEN_INTEREST) ;
- volatilité : celle publiée par le CME (`VOLATILITY`) si elle existe et est
  plausible, sinon recalculée depuis le PRIX DE RÈGLEMENT de la veille
  (`SETTLEMENT_PRICE`) et du prix du NQ à 16h00 ET (ticks de l'utilisateur) ;
  à défaut, interpolée par strike dans la même échéance ;
- options sur le contrat suivi par le continu de ticks (dbtape), échéances à
  45 jours au plus.

Au second test (instant t, prix S) : GEX de chaque option à S, convention
dealer du tableau de bord (call +, put −, Γ × OI × 20 $ × S² × 0,01), d'où :
- le régime : GEX net > 0 « frein », < 0 « accélérateur », et le Gamma Flip
  (niveau où le GEX net change de signe, sur une grille ± 4 %) ;
- le mur du côté de l'excès, défini comme au tableau de bord depuis le
  10/10 : Call Wall = strike >= S de plus forte concentration de gamma call,
  Put Support = strike <= S de plus forte concentration de gamma put ;
- sa distance à l'extrême de l'excès (positive : le mur est encore devant,
  négative : l'extrême l'a déjà dépassé) et sa taille (part du GEX brut).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from . import dbtape, greeks
from .metrics import ET, YEAR_SECONDS

OPEN_INTEREST, SETTLEMENT_PRICE, VOLATILITY = 9, 3, 14
STAT_TYPES = (OPEN_INTEREST, SETTLEMENT_PRICE, VOLATILITY)
MAX_DAYS_TO_EXPIRY = 45
STALE_DAYS = 5                   # une statistique plus vieille n'est pas reprise
FLIP_GRID = np.linspace(-0.04, 0.04, 81)
STRIKE_WINDOW = 0.10
MULT = 20.0
SETTLE_ET = time(16, 0)          # règlement des contrats sur indices CME


def stats_table(stats: pd.DataFrame) -> pd.DataFrame:
    """Lignes utiles du schéma `statistics` : open interest (quantité),
    règlement et volatilité (prix), avec leur heure de publication."""
    s = stats.reset_index()
    s = s[s["stat_type"].isin(STAT_TYPES)]
    if "update_action" in s:
        s = s[s["update_action"] == 1]
    value = np.where(s["stat_type"] == OPEN_INTEREST, s["quantity"].astype(float),
                     s["price"].astype(float))
    return pd.DataFrame({"instrument_id": s["instrument_id"].to_numpy(),
                         "stat_type": s["stat_type"].to_numpy(int), "value": value,
                         "ts_recv": pd.to_datetime(s["ts_recv"], utc=True).to_numpy()})


def settle_ts(session: str) -> float:
    """Heure de règlement (16h00 ET) de la séance `session`, en epoch s."""
    return pd.Timestamp(datetime.combine(date.fromisoformat(session), SETTLE_ET),
                        tz=ET).timestamp()


def session_start(session: str) -> pd.Timestamp:
    d = date.fromisoformat(session) - timedelta(days=1)
    return pd.Timestamp(datetime.combine(d, time(18, 0)), tz=ET).tz_convert("UTC")


def _secs(x) -> np.ndarray:
    return dbtape._secs(pd.Series(x))


def load_raw(raw_dir) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(définitions des options, statistiques) depuis les fichiers bruts
    Databento GLBX.MDP3 (*.dbn.zst), lus par morceaux."""
    import databento as db
    defs, stats = [], []
    for f in sorted(raw_dir.glob("*.dbn.zst")) + sorted(raw_dir.glob("*.dbn")):
        st = db.DBNStore.from_file(f)
        schema = str(getattr(st.schema, "value", st.schema))
        if st.dataset != "GLBX.MDP3" or schema not in ("definition", "statistics"):
            continue
        for chunk in st.to_df(map_symbols=False, count=500_000):
            if chunk.empty:
                continue
            if schema == "definition":
                defs.append(dbtape.definitions_table(chunk))
            else:
                stats.append(stats_table(chunk))
    d = pd.concat(defs, ignore_index=True).drop_duplicates("instrument_id", keep="last") \
        if defs else pd.DataFrame()
    s = pd.concat(stats, ignore_index=True) if stats else pd.DataFrame()
    return d, s


def session_chain(stats: pd.DataFrame, defs: pd.DataFrame, session: str,
                  prev_close: float | None, prev_settle_ts: float | None,
                  rate: float) -> pd.DataFrame:
    """Chaîne de la séance : strike, type, expiration, open interest et
    volatilité, connus à l'ouverture. `prev_close` : prix du NQ au règlement
    de la séance précédente, à l'instant `prev_settle_ts` (epoch s) — pour la
    volatilité tirée des prix de règlement."""
    start = session_start(session)
    code, _ = dbtape.continuous_contract(date.fromisoformat(session))
    s = stats[(stats["ts_recv"] < start)
              & (stats["ts_recv"] >= start - pd.Timedelta(days=STALE_DAYS))]
    if s.empty:
        return pd.DataFrame()
    last = s.sort_values("ts_recv", kind="stable").groupby(
        ["instrument_id", "stat_type"])["value"].last().unstack()
    last = last.rename(columns={OPEN_INTEREST: "oi", SETTLEMENT_PRICE: "settle",
                                VOLATILITY: "vol"})
    for c in ("oi", "settle", "vol"):
        if c not in last:
            last[c] = np.nan
    c = defs.merge(last.reset_index(), on="instrument_id", how="inner")
    exp = pd.to_datetime(c["expiration"], utc=True)
    c = c[(exp > start) & (exp <= start + pd.Timedelta(days=MAX_DAYS_TO_EXPIRY))
          & c["underlying"].map(lambda u: dbtape.same_contract(u, code))
          & (c["oi"] > 0)].copy()
    if c.empty:
        return c
    c["iv_source"] = ""
    # 1. volatilité publiée (décimale ou en %, selon la source)
    vol = c["vol"].astype(float)
    if vol.notna().any():
        if vol.median() > 3:
            vol = vol / 100.0
        ok = vol.between(0.03, 3.0)
        c.loc[ok, "iv"] = vol[ok]
        c.loc[ok, "iv_source"] = "publiée"
    # 2. depuis le prix de règlement de la veille
    need = c["iv_source"] == ""
    if need.any() and prev_close and np.isfinite(prev_close) and prev_settle_ts:
        t = np.maximum(_secs(c.loc[need, "expiration"]) - prev_settle_ts, 300.0) \
            / YEAR_SECONDS
        price = c.loc[need, "settle"].to_numpy(float)
        iv = greeks.implied_vol(np.nan_to_num(price, nan=-1.0), prev_close,
                                c.loc[need, "strike"].to_numpy(float), t, rate,
                                (c.loc[need, "type"] == "C").to_numpy(), q=rate)
        got = np.isfinite(iv)
        idx = c.index[need][got]
        c.loc[idx, "iv"] = iv[got]
        c.loc[idx, "iv_source"] = "règlement"
    # 3. interpolation par strike dans l'échéance, sinon médiane de la séance
    if "iv" not in c:
        c["iv"] = np.nan
    for _, g in c.groupby("expiration"):
        known = g[g["iv"].notna()].sort_values("strike")
        miss = g.index[g["iv"].isna()]
        if len(known) >= 2 and len(miss):
            c.loc[miss, "iv"] = np.interp(g.loc[miss, "strike"], known["strike"], known["iv"])
            c.loc[miss, "iv_source"] = "interpolée"
    if c["iv"].notna().any():
        rest = c["iv"].isna()
        c.loc[rest, "iv"] = c["iv"].median()
        c.loc[rest, "iv_source"] = "médiane"
    return c[["strike", "type", "expiration", "oi", "iv", "iv_source"]] \
        .dropna(subset=["iv"]).reset_index(drop=True)


def _gex(chain: pd.DataFrame, spot, t_epoch: float, rate: float) -> np.ndarray:
    """GEX par option (lignes) à un ou plusieurs prix (colonnes)."""
    s = np.atleast_1d(np.asarray(spot, float))[None, :]
    k = chain["strike"].to_numpy(float)[:, None]
    t = (np.maximum(_secs(chain["expiration"]) - t_epoch, 300.0) / YEAR_SECONDS)[:, None]
    iv = chain["iv"].to_numpy(float)[:, None]
    sign = np.where(chain["type"].to_numpy() == "C", 1.0, -1.0)[:, None]
    g = greeks.gamma(s, k, t, rate, iv, rate)
    return sign * g * chain["oi"].to_numpy(float)[:, None] * MULT * s ** 2 * 0.01


def view(chain: pd.DataFrame, t_epoch: float, spot: float, extreme: float,
         excess_dir: int, rate: float) -> dict:
    """Régime, Gamma Flip et mur du côté de l'excès au second test."""
    out = {"regime": "inconnu", "gex_net": np.nan, "dist_flip": np.nan, "mur": np.nan,
           "dist_mur": np.nan, "part_mur": np.nan}
    if chain is None or chain.empty:
        return out
    # strikes à ± 10 % : au-delà, le gamma est négligeable (et le calcul long)
    chain = chain[(chain["strike"] - spot).abs() <= STRIKE_WINDOW * spot]
    if chain.empty:
        return out
    grid = spot * (1 + FLIP_GRID)
    gx = _gex(chain, np.r_[spot, grid], t_epoch, rate)
    net = gx.sum(axis=0)
    out["gex_net"] = float(net[0])
    out["regime"] = "frein" if net[0] > 0 else "accélérateur"
    curve = net[1:]
    flips = np.flatnonzero(np.sign(curve[:-1]) != np.sign(curve[1:]))
    if len(flips):
        x = []
        for j in flips:                          # interpolation linéaire du zéro
            a, b = curve[j], curve[j + 1]
            x.append(grid[j] + (grid[j + 1] - grid[j]) * a / (a - b))
        flip = min(x, key=lambda v: abs(v - spot))
        out["dist_flip"] = float(spot - flip)    # > 0 : au-dessus du flip
    per = pd.DataFrame({"strike": chain["strike"].to_numpy(float),
                        "type": chain["type"].to_numpy(), "gex": gx[:, 0]})
    calls = per[per["type"] == "C"].groupby("strike")["gex"].sum()
    puts = -per[per["type"] == "P"].groupby("strike")["gex"].sum()
    gross = float(np.abs(per["gex"]).sum())
    side = calls[calls.index >= spot] if excess_dir > 0 else puts[puts.index <= spot]
    side = side[side > 0]
    if len(side) and gross > 0:
        wall = float(side.idxmax())
        out["mur"] = wall
        out["dist_mur"] = (wall - extreme) * excess_dir   # > 0 : encore devant
        out["part_mur"] = float(side.max() / gross)
    return out
