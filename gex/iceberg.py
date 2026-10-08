"""Détection d'ABSORPTION (candidat iceberg) sur les futures NQ/ES, à partir des
ticks capturés (cf. gex/tickcapture.py) — logique pure, aucune E/S.

⚠️ Ce qu'on peut voir et ce qu'on ne peut pas. dxFeed/tastytrade ne donne QUE le
top-of-book (meilleur bid/ask et leur taille), jamais le carnet complet ni le
détail par ordre (MBO) — vérifié le 2026-09-28, cf. mémoire du projet. On ne
peut donc PAS confirmer un iceberg au sens strict (un ordre cause connue qui se
reconstitue). Ce qu'on MESURE est un proxy observable : un niveau de prix qui
absorbe beaucoup plus de volume agressif que ce qui était affiché, en se
RECHARGEANT au lieu de céder. C'est compatible avec un iceberg, mais aussi avec
un teneur de marché qui replace ses ordres à la main. D'où « absorption »,
jamais « iceberg confirmé », dans tout le code et l'affichage.

Principe : regrouper les prints agressifs CONSÉCUTIFS au même prix et au même
sens (une « salve ») ; comparer le volume total de la salve à la taille
affichée avant qu'elle ne commence. Un ratio élevé, avec un niveau qui n'a pas
cédé (taille encore présente après la salve), est le signal.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

THRESHOLDS_VERSION = "v2-2026-09-29"
# v1 (posée sur une nuit peu liquide) donnait ~41 salves/h sur NQ et ~137/h sur ES —
# beaucoup trop pour un « candidat » à regarder, et le déséquilibre venait du volume de
# ticks bien plus élevé sur ES (2,2x celui de NQ en RTH) plutôt que d'une vraie
# différence de qualité de signal. v2, calibrée sur la séance RTH du 2026-09-28
# (458k ticks NQ / 1,0M ticks ES, 15h30-22h Paris) : seuils relevés et MIN_TOTAL
# distinct par instrument pour ramener les deux à un rythme comparable, ~8-9/h.
# Contrôlé à la main : les salves retenues ont presque toutes plusieurs prints
# (rechargement répété), pas un seul gros bloc isolé.

MAX_GAP_S = 2.0            # écart max entre deux prints d'une même salve
MIN_TOTAL = {"NQ": 40.0, "ES": 100.0}         # volume minimum de la salve pour compter
DEFAULT_MIN_TOTAL = 40.0
MIN_RATIO = 4.0             # salve >= 4x la taille affichée avant qu'elle ne commence
MIN_REFILL_FRACTION = 0.5   # le niveau doit garder au moins 50 % de sa taille d'avant


@dataclass(frozen=True)
class Sweep:
    """Série de prints agressifs consécutifs au même prix et au même sens."""
    side: str            # "SELL" (teste le bid) ou "BUY" (teste l'ask)
    price: float
    start_ts: float
    end_ts: float
    n_prints: int
    total_size: float
    size_before: float | None   # taille affichée avant le premier print (prev_*_size)
    size_after: float | None    # taille affichée après le dernier print (*_size)

    @property
    def ratio(self) -> float | None:
        return None if not self.size_before else self.total_size / self.size_before

    @property
    def refilled(self) -> bool:
        """Le niveau garde une taille significative malgré la salve — signe de
        rechargement plutôt que de retrait."""
        if self.size_before is None or self.size_after is None or self.size_before <= 0:
            return False
        return self.size_after >= MIN_REFILL_FRACTION * self.size_before


def build_sweeps(ticks: pd.DataFrame, max_gap_s: float = MAX_GAP_S) -> list[Sweep]:
    """Regroupe les prints agressifs consécutifs (mêmes prix, sens, écart <=
    `max_gap_s`) en salves. `ticks` : colonnes ts, price, volume, side, bid_size,
    ask_size, prev_bid_size, prev_ask_size (triées par ts). Les côtés indéterminés
    et les tailles absentes sont ignorés au niveau de la case, pas de la salve."""
    out: list[Sweep] = []
    cur: dict | None = None

    def flush():
        if cur is not None:
            out.append(Sweep(cur["side"], cur["price"], cur["start"], cur["end"],
                             cur["n"], cur["total"], cur["before"], cur["after"]))

    for r in ticks.itertuples():
        side = r.side
        if side not in ("BUY", "SELL"):
            flush()
            cur = None
            continue
        before_col, after_col = (("prev_bid_size", "bid_size") if side == "SELL"
                                 else ("prev_ask_size", "ask_size"))
        before = getattr(r, before_col, None)
        after = getattr(r, after_col, None)
        same = (cur is not None and cur["side"] == side and cur["price"] == r.price
               and r.ts - cur["end"] <= max_gap_s)
        if same:
            cur["end"] = r.ts
            cur["n"] += 1
            cur["total"] += r.volume
            cur["after"] = after                  # dernière taille observée
        else:
            flush()
            cur = {"side": side, "price": r.price, "start": r.ts, "end": r.ts,
                   "n": 1, "total": float(r.volume), "before": before, "after": after}
    flush()
    return out


def _min_total(symbol: str) -> float:
    return MIN_TOTAL.get(symbol.upper(), DEFAULT_MIN_TOTAL)


def flag_absorption(sweeps: list[Sweep], symbol: str, min_ratio: float = MIN_RATIO,
                    min_refill_fraction: float = MIN_REFILL_FRACTION) -> list[Sweep]:
    """Salves qui ressemblent à de l'absorption : volume >= seuil, ratio au
    volume affiché >= `min_ratio`, et niveau rechargé (pas cédé)."""
    thr = _min_total(symbol)
    out = []
    for s in sweeps:
        if s.total_size < thr or s.ratio is None or s.ratio < min_ratio:
            continue
        if s.size_before is None or s.size_after is None:
            continue
        if s.size_after < min_refill_fraction * s.size_before:
            continue
        out.append(s)
    return out


def detect_absorptions(ticks: pd.DataFrame, symbol: str, max_gap_s: float = MAX_GAP_S,
                       min_ratio: float = MIN_RATIO,
                       min_refill_fraction: float = MIN_REFILL_FRACTION) -> pd.DataFrame:
    """`build_sweeps` + `flag_absorption` VECTORISÉS : mêmes salves (même prix,
    même sens, écart <= `max_gap_s`, un côté indéterminé coupe la salve), mêmes
    seuils, en numpy au lieu d'une boucle Python par tick — utilisable sur la
    fenêtre glissante en direct comme sur une séance entière (graphique).

    Une taille affichée absente (NaN) écarte la salve : la version itérative
    la laissait passer avec un ratio NaN (NaN < seuil est faux).

    Colonnes : side, price, start_ts, end_ts, n_prints, total_size,
    size_before, size_after, ratio — une ligne par absorption, ordre chronologique.
    """
    cols = ["side", "price", "start_ts", "end_ts", "n_prints", "total_size",
            "size_before", "size_after", "ratio"]
    if ticks is None or ticks.empty:
        return pd.DataFrame(columns=cols)
    n = len(ticks)
    side = ticks["side"].to_numpy()
    price = ticks["price"].to_numpy(dtype=float)
    ts = ticks["ts"].to_numpy(dtype=float)
    vol = ticks["volume"].to_numpy(dtype=float)

    def col(name):
        return (ticks[name].to_numpy(dtype=float) if name in ticks
                else np.full(n, np.nan))
    is_sell = side == "SELL"
    valid = is_sell | (side == "BUY")
    code = np.where(is_sell, 2, np.where(valid, 1, 0))   # comparaisons entières, pas chaînes
    before = np.where(is_sell, col("prev_bid_size"), col("prev_ask_size"))
    after = np.where(is_sell, col("bid_size"), col("ask_size"))

    prev_valid = np.concatenate([[False], valid[:-1]])
    same = np.concatenate([[False], (code[1:] == code[:-1]) & (price[1:] == price[:-1])
                           & (ts[1:] - ts[:-1] <= max_gap_s)])
    starts = np.flatnonzero(valid & ~(prev_valid & same))
    if len(starts) == 0:
        return pd.DataFrame(columns=cols)
    # fin d'une salve : dernier tick valide avant la salve suivante (ou un
    # tick indéterminé, qui ne peut jamais en faire partie)
    nxt = np.concatenate([starts[1:], [n]])
    invalid_idx = np.append(np.flatnonzero(~valid), n)   # sentinelle : fin des données
    first_invalid = invalid_idx[np.searchsorted(invalid_idx, starts, side="right")]
    ends = np.minimum(nxt, first_invalid) - 1

    cum = np.concatenate([[0.0], np.cumsum(vol)])
    total = cum[ends + 1] - cum[starts]
    b, a = before[starts], after[ends]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(b > 0, total / b, np.nan)
    keep = ((total >= _min_total(symbol)) & (b > 0) & (ratio >= min_ratio)
            & np.isfinite(a) & (a >= min_refill_fraction * b))
    starts, ends, total, b, a, ratio = (x[keep] for x in (starts, ends, total, b, a, ratio))
    return pd.DataFrame({
        "side": side[starts], "price": price[starts], "start_ts": ts[starts],
        "end_ts": ts[ends], "n_prints": ends - starts + 1, "total_size": total,
        "size_before": b, "size_after": a, "ratio": ratio,
    })


# --- Volume profile de séance (HVL — High Volume Level) --------------------
# Différent de `build_sweeps`/`flag_absorption`, qui n'ont que quelques minutes
# de mémoire : ceci accumule TOUTE la séance (cf. TickCapture._vp, remis à zéro
# à chaque nouvelle séance CME). La question posée n'est plus « il y a eu une
# absorption il y a peu » mais « ce prix concentre-t-il, depuis l'ouverture,
# beaucoup plus de volume que ses voisins ET un déséquilibre acheteur/vendeur
# marqué ? ». Un niveau qui coche les deux réagit souvent (tape reading
# classique) — sert de CONFIRMATION à une salve détectée au même prix, jamais
# de détecteur à lui seul. Seuils posés au jugé (pas encore calibrés sur des
# données réelles, cf. THRESHOLDS_VERSION pour les seuils de salve) : à revoir
# après une séance RTH si trop/pas assez de niveaux ressortent.
VP_BUCKET = {"NQ": 5.0, "ES": 5.0}          # points par palier de regroupement
DEFAULT_VP_BUCKET = 5.0
HVL_MIN_VOL_RATIO = 3.0          # palier retenu si volume >= 3x la médiane des paliers actifs
HVL_MIN_DELTA_FRACTION = 0.35    # et delta net >= 35 % du volume du palier


def _vp_bucket(symbol: str) -> float:
    return VP_BUCKET.get(symbol.upper(), DEFAULT_VP_BUCKET)


def bucket_price(price: float, symbol: str) -> float:
    """Palier de regroupement du volume profile — un HVL par prix exact
    n'aurait aucun sens (bruit), on regroupe par tranches de `_vp_bucket`."""
    size = _vp_bucket(symbol)
    return round(price / size) * size


def update_profile(levels: dict[float, dict], price: float, side: str,
                   volume: float, symbol: str) -> None:
    """Ajoute un print au volume profile de séance, EN PLACE. `levels` : dict
    palier -> {vol, bid_vol, ask_vol}, remis à zéro par l'appelant (TickCapture)
    au changement de séance. Un côté indéterminé est ignoré (compte quand même
    dans aucun agrégat plutôt que de fausser un delta)."""
    if side not in ("BUY", "SELL"):
        return
    key = bucket_price(price, symbol)
    lvl = levels.setdefault(key, {"vol": 0.0, "bid_vol": 0.0, "ask_vol": 0.0})
    lvl["vol"] += volume
    if side == "BUY":
        lvl["ask_vol"] += volume
    else:
        lvl["bid_vol"] += volume


def hvl_levels(levels: dict[float, dict], min_vol_ratio: float = HVL_MIN_VOL_RATIO,
              min_delta_fraction: float = HVL_MIN_DELTA_FRACTION) -> list[dict]:
    """Paliers retenus comme HVL : volume nettement au-dessus de la médiane des
    paliers actifs de la séance ET delta net marqué, triés par volume
    décroissant. Pure — ne lit ni n'écrit l'état de `TickCapture`."""
    actifs = sorted(l["vol"] for l in levels.values() if l["vol"] > 0)
    if len(actifs) < 3:
        return []
    mediane = actifs[len(actifs) // 2]
    if mediane <= 0:
        return []
    out = []
    for price, lvl in levels.items():
        vol = lvl["vol"]
        if vol < mediane * min_vol_ratio:
            continue
        delta = lvl["ask_vol"] - lvl["bid_vol"]
        if abs(delta) / vol < min_delta_fraction:
            continue
        out.append({"price": price, "vol": vol, "delta": delta,
                    "side": "BUY" if delta > 0 else "SELL"})
    return sorted(out, key=lambda d: -d["vol"])


def hvl_near(levels: dict[float, dict], price: float, symbol: str,
            tol_buckets: int = 1, **kwargs) -> dict | None:
    """Le HVL le plus proche de `price` (à `tol_buckets` paliers près), ou None
    — sert à confirmer une salve d'absorption détectée au même niveau."""
    target = bucket_price(price, symbol)
    size = _vp_bucket(symbol)
    for hv in hvl_levels(levels, **kwargs):
        if abs(hv["price"] - target) <= tol_buckets * size:
            return hv
    return None


def analyze(ticks: pd.DataFrame, symbol: str) -> dict:
    """Résumé : nombre de salves, nombre retenues comme absorption, détail des
    plus fortes (par ratio). Pratique pour un rapport ou un test d'ensemble."""
    sw = build_sweeps(ticks)
    flags = flag_absorption(sw, symbol)
    top = sorted(flags, key=lambda s: -(s.ratio or 0))[:20]
    return {"n_sweeps": len(sw), "n_flags": len(flags),
           "top": [{"side": s.side, "price": s.price, "total": s.total_size,
                    "ratio": round(s.ratio, 1) if s.ratio else None,
                    "n_prints": s.n_prints} for s in top]}
