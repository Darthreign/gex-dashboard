"""Tape d'options NQ reconstruit depuis l'historique Databento (GLBX.MDP3).

Même format de barres 1 min que le tape live (flowtape.FlowBar), pour que le
voyant du rapport d'excès lise les deux de la même façon, mais rangé À PART
(`data/tape_databento/`, `mult_source = "databento"`) : jamais mélangé au
flux live.

Par transaction d'option (schéma `trades`) :
- côté agresseur fourni par le CME (`side` Databento : "B" = acheteur
  agresseur, "A" = vendeur agresseur, "N" = non attribué -> compté à part,
  comme les prints non classables du live) ;
- strike, échéance, call/put et future sous-jacent lus dans `definition` ;
- prix du future : ticks NQ de l'utilisateur (continu au volume, cf. roll.py)
  au moment de la transaction ;
- delta Black-76 (portage q = r, comme les chaînes natives) à la volatilité
  implicite tirée du PRIX DE LA TRANSACTION ; temps restant jusqu'à
  l'expiration donnée par la `definition` (plancher 5 min, comme le live) ;
- pression de couverture = côté × taille × delta × 20 $ × prix du future,
  rangée par type et par sens comme `hedge_*` dans flowtape.

Le continu de ticks (`NQ.v.0`, roll au volume) ne suit qu'UN contrat :
seules les options dont le sous-jacent est ce contrat sont valorisées ; les
autres sont comptées (`other_und_contracts`). Le contrat du continu est
déduit du calendrier CME (roll vers le trimestre suivant 8 jours avant
l'échéance, le jeudi) ; les séances à 2 jours ou moins de cette date, et
celles où plus de la moitié des contrats échangés portent sur un autre
sous-jacent, ne sont PAS étiquetées (`mult_source` vide) : le prix du
continu pourrait y appartenir à l'autre contrat (écart de plusieurs dizaines
de points), donc le delta serait faux.

Ce n'est PAS le delta du flux dxFeed : la concordance avec le tape live se
vérifie sur les séances communes avant tout usage (scripts/databento_tape.py
--compare).
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import greeks
from .metrics import ET, YEAR_SECONDS

HEDGE = ("hedge_call_buy", "hedge_call_sell", "hedge_put_buy", "hedge_put_sell")
SUM_COLS = HEDGE + ("net_delta", "buy_contracts", "sell_contracts", "delta_prints",
                    "no_delta_prints", "undefined_prints", "prints")
MIN_T_SECONDS = 300.0
QUARTER_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}
ROLL_DAYS_BEFORE_EXPIRY = 8
ROLL_UNCERTAIN_DAYS = 2
MAX_OTHER_SHARE = 0.5


def _secs(ts: pd.Series) -> np.ndarray:
    """Secondes Unix d'une série de dates (n'importe quelle résolution)."""
    return ((pd.to_datetime(ts, utc=True) - pd.Timestamp(0, tz="UTC"))
            / pd.Timedelta(seconds=1)).to_numpy(dtype=float)


def _third_friday(y: int, m: int) -> date:
    d = date(y, m, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7 + 14)


def continuous_contract(day: date) -> tuple[str, bool]:
    """(code du contrat suivi par le continu, séance incertaine). Chaque
    trimestriel est suivi jusqu'à son jour de roll inclus (8 jours avant
    l'échéance du 3e vendredi), puis le suivant ; « incertaine » = à 2 jours
    ou moins d'un roll. Code au format Databento court, ex. « NQZ6 »."""
    y = day.year
    quarters = [(y - 1, 12)] + [(y, q) for q in (3, 6, 9, 12)] + [(y + 1, 3)]
    rolls = [(q, _third_friday(*q) - timedelta(days=ROLL_DAYS_BEFORE_EXPIRY))
             for q in quarters]
    qy, qm = next(q for q, r in rolls if day <= r)
    uncertain = any(abs((day - r).days) <= ROLL_UNCERTAIN_DAYS for _, r in rolls)
    return f"NQ{QUARTER_CODES[qm]}{qy % 10}", uncertain


def same_contract(underlying: str, code: str) -> bool:
    """Compare deux codes de future NQ, quel que soit le format de l'année
    (« NQZ6 », « NQZ26 »)."""
    u = str(underlying).strip().upper()
    return u[:3] == code[:3] and u[-1:] == code[-1:] and (u[3:-1] == "" or u[3:-1].isdigit())


def session_of(ts_utc: pd.Series) -> pd.Series:
    """Séance CME (18h00 ET -> 16h59 ET le lendemain) : (heure ET + 6 h).date."""
    return (ts_utc.dt.tz_convert(ET) + pd.Timedelta(hours=6)).dt.strftime("%Y-%m-%d")


def definitions_table(defs: pd.DataFrame) -> pd.DataFrame:
    """Options seulement (call / put), dernière définition par instrument."""
    d = defs.reset_index()
    d = d[d["instrument_class"].isin(("C", "P"))]
    if "ts_recv" in d:
        d = d.sort_values("ts_recv", kind="stable")
    d = d.drop_duplicates("instrument_id", keep="last")
    return pd.DataFrame({"instrument_id": d["instrument_id"].to_numpy(),
                         "type": d["instrument_class"].to_numpy(),
                         "strike": d["strike_price"].astype(float).to_numpy(),
                         "expiration": pd.to_datetime(d["expiration"], utc=True).to_numpy(),
                         "underlying": d["underlying"].astype(str).to_numpy()})


def trades_to_bars(trades: pd.DataFrame, defs: pd.DataFrame, spot_at, rate: float,
                   mult: float = 20.0) -> pd.DataFrame:
    """Agrégats PARTIELS par (séance, minute, sous-jacent) — additifs, donc
    calculables morceau par morceau. `trades` : index ts_recv (UTC), colonnes
    instrument_id, price, size, side. `defs` : sortie de `definitions_table`.
    `spot_at(session, epoch_s) -> prix du future` (NaN si inconnu)."""
    t = trades.reset_index()
    tcol = "ts_recv" if "ts_recv" in t else t.columns[0]
    t = t.merge(defs, on="instrument_id", how="inner")
    ts = pd.to_datetime(t[tcol], utc=True)
    t = t[(ts < t["expiration"]).to_numpy()]
    ts = pd.to_datetime(t[tcol], utc=True)
    if t.empty:
        return pd.DataFrame()
    session = session_of(ts).to_numpy()
    epoch = _secs(ts)
    minute_et = ts.dt.tz_convert(ET).dt.floor("min").dt.tz_localize(None)
    side = t["side"].astype(str).to_numpy()
    sign = np.where(side == "B", 1.0, np.where(side == "A", -1.0, 0.0))
    size = t["size"].astype(float).to_numpy()
    is_call = (t["type"] == "C").to_numpy()
    spot = np.full(len(t), np.nan)
    for s in np.unique(session):
        m = session == s
        spot[m] = spot_at(s, epoch[m])
    exp_s = _secs(t["expiration"])
    tyears = np.maximum(exp_s - epoch, MIN_T_SECONDS) / YEAR_SECONDS
    k = t["strike"].to_numpy(float)
    price = t["price"].astype(float).to_numpy()
    ok_in = np.isfinite(spot) & (price > 0) & (sign != 0)
    iv = np.full(len(t), np.nan)
    if ok_in.any():
        iv[ok_in] = greeks.implied_vol(price[ok_in], spot[ok_in], k[ok_in], tyears[ok_in],
                                       rate, is_call[ok_in], q=rate)
    good = ok_in & np.isfinite(iv)
    delta = np.zeros(len(t))
    if good.any():
        g = good
        delta[g] = np.where(is_call[g],
                            greeks.call_delta(spot[g], k[g], tyears[g], rate, iv[g], rate),
                            greeks.put_delta(spot[g], k[g], tyears[g], rate, iv[g], rate))
    hedge = np.where(good, sign * size * delta * mult * np.nan_to_num(spot), 0.0)
    out = pd.DataFrame({
        "session": session, "timestamp": minute_et.to_numpy(),
        "underlying": t["underlying"].to_numpy(),
        "hedge_call_buy": np.where(is_call & (sign > 0), hedge, 0.0),
        "hedge_call_sell": np.where(is_call & (sign < 0), hedge, 0.0),
        "hedge_put_buy": np.where(~is_call & (sign > 0), hedge, 0.0),
        "hedge_put_sell": np.where(~is_call & (sign < 0), hedge, 0.0),
        "net_delta": -hedge,
        "buy_contracts": np.where(sign > 0, size, 0.0),
        "sell_contracts": np.where(sign < 0, size, 0.0),
        "delta_prints": good.astype(float),
        "no_delta_prints": ((sign != 0) & ~good).astype(float),
        "undefined_prints": (sign == 0).astype(float),
        "prints": np.ones(len(t)),
    })
    return out.groupby(["session", "timestamp", "underlying"], as_index=False)[list(SUM_COLS)] \
        .sum()


def finalize(partials: list[pd.DataFrame], mult: float = 20.0) -> dict[str, pd.DataFrame]:
    """Barres finales par séance : options sur le contrat du continu
    seulement, autres sous-jacents comptés ; séances incertaines non
    étiquetées."""
    parts = [p for p in partials if p is not None and not p.empty]
    if not parts:
        return {}
    allp = pd.concat(parts, ignore_index=True).groupby(
        ["session", "timestamp", "underlying"], as_index=False)[list(SUM_COLS)].sum()
    out = {}
    for s, d in allp.groupby("session"):
        code, uncertain = continuous_contract(date.fromisoformat(s))
        match = d["underlying"].map(lambda u: same_contract(u, code)).to_numpy(bool)
        contracts = d["buy_contracts"] + d["sell_contracts"]
        other_share = float(contracts[~match].sum() / contracts.sum()) if contracts.sum() else 0.0
        main = d[match].drop(columns=["session", "underlying"]) \
            .groupby("timestamp", as_index=False).sum()
        other = d[~match].assign(other_und_contracts=contracts[~match]) \
            .groupby("timestamp")["other_und_contracts"].sum()
        bars = main.set_index("timestamp").join(other, how="outer").fillna(0.0) \
            .reset_index().sort_values("timestamp")
        label = not uncertain and other_share <= MAX_OTHER_SHARE
        bars["underlying"] = code
        bars["mult"] = mult
        bars["mult_source"] = "databento" if label else None
        bars["roll_session"] = uncertain
        bars["other_share"] = other_share
        bars["source"] = "databento"
        bars["symbol"] = "NQ"
        out[s] = bars.reset_index(drop=True)
    return out
