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

import pandas as pd

THRESHOLDS_VERSION = "v1-2026-09-28"          # provisoire, à recalibrer sur des séances RTH

MAX_GAP_S = 2.0            # écart max entre deux prints d'une même salve
MIN_TOTAL = {"NQ": 20.0, "ES": 20.0}          # volume minimum de la salve pour compter
DEFAULT_MIN_TOTAL = 20.0
MIN_RATIO = 3.0             # salve >= 3x la taille affichée avant qu'elle ne commence
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
