"""Book dealer estimé : open interest + flux signé du jour.

Le GEX « naïf » (metrics.enrich) suppose un positionnement fixe — dealers
longs calls, courts puts — et le DEX une autre hypothèse (dealers courts des
deux côtés). Deux lectures d'un même book qui ne peuvent pas être vraies
ensemble, faute de savoir qui détient l'open interest.

Ici, une seule hypothèse, corrigée par ce qui s'observe :

    position dealer = inventaire de début de séance − flux preneur du jour

- inventaire de début de séance : la convention GEX historique (dealers
  longs l'OI des calls, courts celui des puts) — l'a priori standard des
  options d'indice, où les clients vendent des calls couverts et achètent des
  puts de protection ;
- flux preneur du jour : Σ taille × (+1 achat agresseur, −1 vente) par
  contrat, lu dans les prints bruts persistés (`store.load_optprints`). Le
  dealer prend l'autre côté. Jambes de combos exclues (leur côté ne dit rien
  de la position, cf. flowtape), annulations et corrections aussi.

GEX et DEX sont alors calculés sur CE book avec le vrai signe des greeks
(un dealer long d'une option est long gamma, quel que soit son type) : plus
de signe artificiel ni de convention différente entre les deux mesures.
Sans prints (source CBOE seule, contrats hors de l'univers suivi), le book
retombe sur l'inventaire de début de séance, donc sur le GEX naïf.
"""
from __future__ import annotations

import time as _time
from datetime import date

import numpy as np
import pandas as pd

from . import store
from .greeks import gex_dollars
from .metrics import multiplier

# Recalcul du flux du jour au plus toutes les N secondes : une journée de SPX
# pèse ~1 M de prints, inutile de la relire à chaque rafraîchissement.
CACHE_TTL_S = 300.0
_cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}


def occ_to_streamer(contract: str) -> str | None:
    """Symbole OCC (CBOE) -> symbole streamer dxFeed : SPXW260729C07400000 ->
    .SPXW260729C7400 (strike sans zéros superflus)."""
    from .ingest import OCC_RE
    m = OCC_RE.match(contract or "")
    if not m:
        return None
    k = int(m.group("strike")) / 1000.0
    ks = f"{k:f}".rstrip("0").rstrip(".")
    return f".{m.group('root')}{m.group('exp')}{m.group('cp')}{ks}"


def chain_keys(df: pd.DataFrame) -> pd.Series:
    """Identifiant streamer de chaque contrat de la chaîne."""
    if "streamer_symbol" in df:
        return df["streamer_symbol"]
    if "contract" in df:
        return df["contract"].map(occ_to_streamer)
    return pd.Series([None] * len(df), index=df.index)


def taker_flow(prints: pd.DataFrame) -> pd.Series:
    """Contrats nets achetés par les preneurs, par contrat streamer."""
    if prints is None or prints.empty:
        return pd.Series(dtype=float)
    p = prints
    if "spread" in p:
        p = p[~p["spread"].fillna(False).astype(bool)]
    if "ttype" in p:
        p = p[~p["ttype"].isin(["CANCEL", "CORRECTION"])]
    side = p["side"].map({"BUY": 1.0, "SELL": -1.0})
    p = p.assign(_s=side * p["size"].astype(float)).dropna(subset=["_s"])
    return p.groupby("contract")["_s"].sum()


def daily_taker_flow(symbol: str, day: date) -> pd.Series:
    key = (symbol, day.isoformat())
    hit = _cache.get(key)
    now = _time.monotonic()
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    flow = taker_flow(store.load_optprints(
        symbol, day.isoformat(), columns=["contract", "side", "size", "spread", "ttype"]))
    _cache[key] = (now, flow)
    return flow


def dealer_book(df: pd.DataFrame, flow: pd.Series, spot: float) -> pd.DataFrame:
    """Ajoute dealer_pos (contrats, + = dealer long), gex_book et dex_book."""
    d = df.copy()
    is_call = (d["type"] == "C").to_numpy()
    oi = d["open_interest"].to_numpy(dtype=float)
    start = np.where(is_call, oi, -oi)
    taken = chain_keys(d).map(flow).fillna(0.0).to_numpy(dtype=float)
    pos = start - taken
    mult = multiplier(d)
    d["taker_flow"] = taken
    d["dealer_pos"] = pos
    d["gex_book"] = gex_dollars(1.0, d["gamma_bs"].to_numpy(), pos, mult, spot)
    d["dex_book"] = d["delta_bs"].to_numpy() * pos * mult * spot
    return d


def book_summary(df: pd.DataFrame, flow: pd.Series, spot: float) -> dict:
    """GEX/DEX nets naïfs et sur book estimé, plus la couverture du flux :
    part du |GEX| naïf portée par des contrats effectivement traités."""
    b = dealer_book(df, flow, spot)
    traded = b["taker_flow"] != 0
    total = b["gex"].abs().sum()
    return {
        "net_gex_naive": float(b["gex"].sum()),
        "net_dex_naive": float(b["dex"].sum()),
        "net_gex_book": float(b["gex_book"].sum()),
        "net_dex_book": float(b["dex_book"].sum()),
        "flow_contracts": float(b["taker_flow"].abs().sum()),
        "flow_coverage": float(b.loc[traded, "gex"].abs().sum() / total) if total else 0.0,
    }
