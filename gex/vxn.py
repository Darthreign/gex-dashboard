"""VXN (volatilité implicite à 30 jours du Nasdaq-100) : calcul maison en séance,
historique officiel CBOE le soir.

Pourquoi un calcul maison : le flux dxFeed du courtier ne sert pas le VXN
(vérifié le 2026-10-26 : `VXN`, `$VXN`, `VXN.X`, `VXNCLS` et `VOLQ` ne renvoient
rien, alors que VIX et NDX répondent ; la recherche de symboles tastytrade ne
le connaît pas). Le VXN est le VIX appliqué aux options NDX : on le recalcule
avec la méthode publiée par CBOE, sur la chaîne NDX complète collectée toutes
les 15 min (`collect_full_chain`). Ce n'est pas l'indice officiel au centime
près — `refresh_cboe_daily` rapatrie le soir la clôture officielle, gratuite,
qui sert de référence.

Les options NDX ne se négocient qu'en séance cash (9h30-16h15 ET, pas de
séance étendue contrairement au SPX) : le calcul maison n'existe que sur
cette plage.
"""
from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd
import requests

from .metrics import ET

log = logging.getLogger(__name__)

CBOE_DAILY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VXN_History.csv"
PRICES_KEY = "VXN_CALC"      # data/prices/VXN_CALC/<jour>.parquet
DAILY_KEY = "vxn_daily"      # data/history/vxn_daily.parquet

MIN_PER_DAY = 1440
N30 = 30 * MIN_PER_DAY
N365 = 365 * MIN_PER_DAY
# Fenêtre CBOE des deux échéances retenues : plus de 23 et moins de 37 jours.
TERM_MIN_DAYS, TERM_MAX_DAYS = 23, 37
# Cotations d'un contrat collecté : cotation, grecs (complétude, cf.
# futopt._all_have_iv) et open interest (chaîne complète, sans surcoût notable).
EVENTS = ("Quote", "Greeks", "Summary")


def minutes_to_settlement(expiry, settle_am: bool, now_et: datetime) -> float:
    """Minutes jusqu'au règlement : 9h30 ET pour une série AM (NDX mensuel),
    16h00 ET pour une série PM (NDXP)."""
    at = time(9, 30) if settle_am else time(16, 0)
    settle = datetime.combine(expiry, at, tzinfo=ET)
    return (settle - now_et).total_seconds() / 60.0


def _mid(bid: float, ask: float) -> float:
    return (bid + ask) / 2.0


def term_variance(opts: pd.DataFrame, t_years: float, r: float) -> dict | None:
    """Variance d'une échéance, méthode CBOE (livre blanc VIX).

    `opts` : une seule échéance, colonnes strike, type (C/P), bid, ask.
    Forward par parité au strike où |call − put| est minimal ; K0 = premier
    strike sous le forward ; options hors de la monnaie de part et d'autre de
    K0, en s'arrêtant après deux bids nuls consécutifs ; un bid nul isolé est
    simplement ignoré.
    """
    if t_years <= 0:
        return None
    o = opts[["strike", "type", "bid", "ask"]].copy()
    o[["bid", "ask"]] = o[["bid", "ask"]].fillna(0.0)
    calls = o[o["type"] == "C"].groupby("strike")[["bid", "ask"]].last()
    puts = o[o["type"] == "P"].groupby("strike")[["bid", "ask"]].last()
    both = calls.add_suffix("_c").join(puts.add_suffix("_p"), how="inner")
    quoted = both[(both["bid_c"] > 0) & (both["ask_c"] > 0)
                  & (both["bid_p"] > 0) & (both["ask_p"] > 0)]
    if quoted.empty:
        return None
    diff = (_mid(quoted["bid_c"], quoted["ask_c"]) - _mid(quoted["bid_p"], quoted["ask_p"]))
    k_star = diff.abs().idxmin()
    growth = float(np.exp(r * t_years))
    forward = float(k_star + growth * diff.loc[k_star])
    strikes = sorted(set(calls.index) | set(puts.index))
    below = [k for k in strikes if k <= forward]
    if not below:
        return None
    k0 = below[-1]

    def walk(side: pd.DataFrame, ks: list[float]) -> list[tuple[float, float]]:
        out, zeros = [], 0
        for k in ks:
            if k not in side.index:
                continue
            bid, ask = float(side.at[k, "bid"]), float(side.at[k, "ask"])
            if bid <= 0:
                zeros += 1
                if zeros >= 2:
                    break
                continue
            zeros = 0
            out.append((k, _mid(bid, ask)))
        return out

    put_q = walk(puts, [k for k in reversed(strikes) if k < k0])
    call_q = walk(calls, [k for k in strikes if k > k0])
    q0 = []
    if k0 in calls.index and k0 in puts.index:
        q0 = [_mid(*calls.loc[k0, ["bid", "ask"]]), _mid(*puts.loc[k0, ["bid", "ask"]])]
    q0 = [q for q in q0 if q > 0]
    if not q0 or not put_q or not call_q:
        return None
    points = sorted(put_q + [(k0, float(np.mean(q0)))] + call_q)
    ks = np.array([k for k, _ in points])
    qs = np.array([q for _, q in points])
    dk = np.empty_like(ks)
    dk[1:-1] = (ks[2:] - ks[:-2]) / 2.0
    dk[0] = ks[1] - ks[0]
    dk[-1] = ks[-1] - ks[-2]
    var = (2.0 / t_years) * float(np.sum(dk / ks ** 2 * growth * qs)) \
        - (1.0 / t_years) * (forward / k0 - 1.0) ** 2
    return {"variance": var, "forward": forward, "k0": k0, "n_strikes": len(points)}


def _one_root_per_expiry(chain: pd.DataFrame) -> pd.DataFrame:
    """NDX (AM) et NDXP (PM) peuvent échoir le même jour : une seule série par
    date, la plus cotée (le plus de bids non nuls)."""
    quoted = chain.assign(_q=(chain["bid"].fillna(0) > 0).astype(int))
    best = (quoted.groupby(["expiry", "underlying_symbol"])["_q"].sum()
            .reset_index().sort_values("_q").drop_duplicates("expiry", keep="last"))
    keep = set(zip(best["expiry"], best["underlying_symbol"]))
    mask = [(e, u) in keep for e, u in zip(chain["expiry"], chain["underlying_symbol"])]
    return chain[mask]


def compute(chain: pd.DataFrame, now_et: datetime, r: float) -> dict | None:
    """VXN à `now_et` depuis une chaîne NDX cotée (strike, type, expiry,
    settle_am, underlying_symbol, bid, ask). None si les deux échéances qui
    encadrent 30 jours ne sont pas exploitables."""
    if chain.empty:
        return None
    c = _one_root_per_expiry(chain)
    terms = (c.groupby(["expiry", "settle_am"]).size().reset_index()[["expiry", "settle_am"]])
    terms["n_min"] = [minutes_to_settlement(e, bool(am), now_et)
                      for e, am in zip(terms["expiry"], terms["settle_am"])]
    terms = terms[terms["n_min"] > 0].sort_values("n_min")
    near_c = terms[terms["n_min"] <= N30]
    next_c = terms[terms["n_min"] > N30]
    in_win = lambda t: t[(t["n_min"] > TERM_MIN_DAYS * MIN_PER_DAY)  # noqa: E731
                         & (t["n_min"] < TERM_MAX_DAYS * MIN_PER_DAY)]
    near_c = in_win(near_c) if not in_win(near_c).empty else near_c
    next_c = in_win(next_c) if not in_win(next_c).empty else next_c
    if near_c.empty or next_c.empty:
        return None
    near, nxt = near_c.iloc[-1], next_c.iloc[0]
    res = []
    for t in (near, nxt):
        t_years = t["n_min"] / N365
        v = term_variance(c[c["expiry"] == t["expiry"]], t_years, r)
        if v is None or v["variance"] <= 0:
            return None
        res.append((t["n_min"], t_years, v))
    (n1, t1, v1), (n2, t2, v2) = res
    w = (t1 * v1["variance"] * (n2 - N30) / (n2 - n1)
         + t2 * v2["variance"] * (N30 - n1) / (n2 - n1))
    if w <= 0:
        return None
    return {"vxn": 100.0 * float(np.sqrt(w * N365 / N30)),
            "near_expiry": near["expiry"], "next_expiry": nxt["expiry"],
            "near_days": n1 / MIN_PER_DAY, "next_days": n2 / MIN_PER_DAY,
            "near_strikes": v1["n_strikes"], "next_strikes": v2["n_strikes"],
            "forward_near": v1["forward"]}


def collect_full_chain() -> pd.DataFrame | None:
    """Chaîne NDX COMPLÈTE (toutes échéances, tous strikes) avec bid/ask —
    distincte de la collecte toutes les 3 min (`idxopt.build_native_chain`),
    filtrée à 14 jours et ±8 %, trop étroite pour un calcul à 30 jours."""
    from . import idxopt
    from .futopt import _collect
    from .rtquote import quote_token
    _, _, access = quote_token()
    chain = idxopt.fetch_chain_instruments("NDX", access)
    if chain.empty:
        return None
    raw = asyncio.run(_collect(chain["streamer_symbol"].tolist(), events=EVENTS,
                               stop_when_complete=True))
    md = pd.DataFrame.from_dict(raw, orient="index")
    df = chain.merge(md, left_on="streamer_symbol", right_index=True, how="left")
    df = df.rename(columns={"bidPrice": "bid", "askPrice": "ask", "oi": "open_interest"})
    for col in ("bid", "ask"):
        if col not in df.columns:
            df[col] = np.nan
    return df


def update_live() -> dict | None:
    """Collecte + calcul + journalisation d'un point VXN maison."""
    from . import rates, store
    now = datetime.now(ET)
    chain = collect_full_chain()
    if chain is None:
        log.warning("VXN maison : chaîne NDX indisponible")
        return None
    res = compute(chain, now, rates.current_rate())
    if res is None:
        log.warning("VXN maison : échéances à 30 jours inexploitables (%d contrats)", len(chain))
        return None
    ts = now.replace(tzinfo=None, second=0, microsecond=0)
    v = res["vxn"]
    store.append_prices(PRICES_KEY, [{
        "timestamp": ts, "open": v, "high": v, "low": v, "close": v,
        "near_expiry": str(res["near_expiry"]), "next_expiry": str(res["next_expiry"]),
        "near_strikes": res["near_strikes"], "next_strikes": res["next_strikes"],
        "n_contracts": len(chain), "source": "calc_dxfeed"}], ts)
    log.info("VXN maison : %.2f (échéances %s / %s, %d contrats)",
             v, res["near_expiry"], res["next_expiry"], len(chain))
    return res


_LATEST_CACHE: dict = {}
LATEST_CACHE_S = 60          # un point toutes les 15 min : inutile de relire plus souvent


def latest(now_et: datetime | None = None) -> dict:
    """VXN à afficher : le dernier point recalculé s'il date de la séance du
    jour (heure de New York), sinon la dernière clôture officielle CBOE. Les
    deux sont renvoyés, chacun avec sa date et sa provenance, pour que
    l'affichage ne présente jamais le calcul maison comme l'indice officiel."""
    import time as _time
    from . import store
    now_et = now_et or datetime.now(ET)
    hit = _LATEST_CACHE.get("v")
    if hit and _time.monotonic() - hit[0] < LATEST_CACHE_S and hit[2] == now_et.date():
        return hit[1]
    calc = None
    for day in reversed(store.price_days(PRICES_KEY)[-5:]):
        bars = store.load_prices(PRICES_KEY, day)
        if not bars.empty:
            last = bars.sort_values("timestamp").iloc[-1]
            calc = {"value": float(last["close"]), "ts": pd.Timestamp(last["timestamp"]).isoformat()}
            break
    official = None
    from .config import SETTINGS
    path = SETTINGS.data_dir / "history" / f"{DAILY_KEY}.parquet"
    if path.exists():
        d = pd.read_parquet(path)
        if not d.empty:
            official = {"value": float(d["close"].iloc[-1]), "date": str(d["date"].iloc[-1])}
    out: dict = {"available": bool(calc or official), "calc": calc, "officiel": official}
    if calc and calc["ts"][:10] == now_et.date().isoformat():
        out.update(value=calc["value"], source="calc", asof=calc["ts"])
    elif official:
        out.update(value=official["value"], source="officiel", asof=official["date"])
    elif calc:
        out.update(value=calc["value"], source="calc", asof=calc["ts"])
    if out.get("source") == "calc" and official and official["date"] < calc["ts"][:10]:
        out["variation_vs_cloture"] = round(calc["value"] - official["value"], 2)
    _LATEST_CACHE["v"] = (_time.monotonic(), out, now_et.date())
    return out


def parse_cboe_daily(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text))
    return pd.DataFrame({
        "date": pd.to_datetime(df["DATE"], format="%m/%d/%Y").dt.date,
        "open": df["OPEN"], "high": df["HIGH"], "low": df["LOW"], "close": df["CLOSE"],
    }).sort_values("date").reset_index(drop=True)


def refresh_cboe_daily() -> int:
    """Historique quotidien OFFICIEL (gratuit, depuis 2009) : réécrit en entier,
    le fichier CBOE étant lui-même complet à chaque publication."""
    from .config import SETTINGS
    r = requests.get(CBOE_DAILY_URL, timeout=60)
    r.raise_for_status()
    df = parse_cboe_daily(r.text)
    path = SETTINGS.data_dir / "history" / f"{DAILY_KEY}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    log.info("VXN officiel CBOE : %d séances, dernière %s", len(df), df["date"].iloc[-1])
    return len(df)
