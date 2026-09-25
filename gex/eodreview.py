"""Bilan de fin de séance : ce que la journée a RÉELLEMENT fait, comparé à ce que
les briefs annonçaient.

Logique pure (aucune E/S) : `scripts/eod_review.py` lit les bougies et les briefs,
appelle ces fonctions, puis écrit dans le journal (`daily_metrics`).

Définitions posées avec l'utilisateur le 2026-09-24 (voir THRESHOLDS_VERSION) :

- **mouvement propre** : un déplacement d'au moins PHASE_MIN_PTS points dont le
  retracement interne (recul depuis le sommet courant du mouvement) ne dépasse
  jamais RETRACE_MAX de sa taille finale. Mesuré sur les clôtures 1 min de la
  séance US (9h30-16h ET). Étalonné sur 47 séances ; le seuil de retracement
  (25-35 %) ne change rien aux résultats, seule compte la taille.
- **phase directionnelle** : au moins un mouvement propre >= PHASE_MIN_PTS.
- **journée directionnelle** : le plus grand mouvement propre >= JOUR_MIN_PTS.
- **fenêtre contrarienne** : avant 16h15 Paris (= 10h15 ET). Le MOMENT de départ
  compte autant que la taille : un mouvement qui démarre à 12h26 ET (18/09) laisse
  la matinée jouable pour un contrarien, un mouvement dès 9h30 ET (23/09) non.

⚠️ Mesure descriptive, pas un signal de trading.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time

import numpy as np
import pandas as pd

THRESHOLDS_VERSION = "v1-2026-09-24"
PHASE_MIN_PTS = 150.0
JOUR_MIN_PTS = 250.0
RETRACE_MAX = 0.30
RTH_START = time(9, 30)
RTH_END = time(16, 0)
CONTRARIAN_CUT = time(10, 15)        # 16h15 Paris
MIN_BARS = 200                       # séance trop lacunaire en dessous : on n'évalue pas

# Emoji de la ligne « PRÉCAUTION TRADING » des briefs -> code stable.
QUALIF_EMOJI = {
    "🔴": "EXPANSION_CONFIRME",
    "🟠": "EXPANSION_POSSIBLE",
    "🟡": "MIXTE",
    "🟢": "MEAN_REVERSION",
    "⚪": "INSUFFISANT",
}
BRIEF_NOMS = ("matin", "prebrief", "ajustement", "ajustement2")


@dataclass(frozen=True)
class Leg:
    start: int      # indice de la bougie de départ
    end: int        # indice de la bougie d'arrivée
    pts: float      # signé : + hausse, - baisse


def clean_legs(closes, retr_max: float = RETRACE_MAX,
               min_move: float = PHASE_MIN_PTS) -> list[Leg]:
    """Mouvements propres (taille >= min_move, retracement interne <= retr_max x
    taille), un par point d'ARRIVÉE et par sens.

    Pour une même arrivée, on garde le départ qui donne le PLUS GRAND mouvement,
    c'est-à-dire l'extrême réel (le plus bas d'une hausse). Sans cela, la
    tolérance de 30 % laisserait un mouvement « démarrer » n'importe où dans le
    calme qui le précède, et l'heure de départ serait fausse. À égalité (plateau
    parfaitement plat), on prend le départ le plus TARDIF, collé au mouvement."""
    c = np.asarray(closes, dtype=float)
    legs: list[Leg] = []
    for sign in (1.0, -1.0):
        x = sign * c
        by_end: dict[int, Leg] = {}
        for i in range(len(x) - 1):
            seg = x[i:]
            m = seg - seg[0]
            peak = np.maximum.accumulate(seg)
            dd = np.maximum.accumulate(peak - seg)
            ok = (m >= min_move) & (dd <= retr_max * m)
            for k in np.flatnonzero(ok):
                j = i + int(k)
                prev = by_end.get(j)
                if prev is None or abs(m[k]) >= abs(prev.pts):
                    by_end[j] = Leg(start=i, end=j, pts=float(sign * m[k]))
        legs.extend(by_end.values())
    return legs


def _rth(bars: pd.DataFrame) -> pd.DataFrame:
    t = bars["timestamp"].dt.time
    return bars[(t >= RTH_START) & (t < RTH_END)].reset_index(drop=True)


def summarize_session(bars: pd.DataFrame) -> dict | None:
    """Faits de la séance US à partir des bougies 1 min (timestamps naïfs en ET).
    None si la séance est trop lacunaire pour être évaluée."""
    s = _rth(bars)
    if len(s) < MIN_BARS:
        return None
    legs = clean_legs(s["close"].to_numpy())
    ts = s["timestamp"]
    out = {
        "range_pts": float(s["high"].max() - s["low"].min()),
        "open_to_close_pts": float(s["close"].iloc[-1] - s["open"].iloc[0]),
        "rth_bars": len(s),
        "dir_pts": 0.0, "dir_debut": None, "dir_debut_avant_1615": None,
        "dir_first_debut": None, "dir_early": 0, "dir_tier": "aucune",
    }
    if not legs:
        return out
    big = max(legs, key=lambda g: abs(g.pts))
    first = min(legs, key=lambda g: g.start)
    out["dir_pts"] = big.pts
    out["dir_debut"] = f"{ts.iloc[big.start]:%H:%M}"
    out["dir_debut_avant_1615"] = int(ts.iloc[big.start].time() < CONTRARIAN_CUT)
    out["dir_first_debut"] = f"{ts.iloc[first.start]:%H:%M}"
    out["dir_early"] = int(ts.iloc[first.start].time() < CONTRARIAN_CUT)
    out["dir_tier"] = ("journee" if abs(big.pts) >= JOUR_MIN_PTS else "phase")
    return out


def parse_qualification(text: str | None) -> str | None:
    """Code de qualification (EXPANSION_CONFIRME, ...) lu dans la section
    « PRÉCAUTION TRADING » d'un brief ; None si absente."""
    if not text:
        return None
    m = re.search(r"PR[ÉE]CAUTION TRADING", text, flags=re.IGNORECASE)
    zone = text[m.end():m.end() + 800] if m else text[-800:]
    hits = [(zone.find(e), code) for e, code in QUALIF_EMOJI.items() if e in zone]
    return min(hits)[1] if hits else None


def score_brief(qualif: str | None, facts: dict) -> int | None:
    """1 = l'annonce du brief s'est vérifiée, 0 = non, None = non noté.

    - EXPANSION_* : vérifiée s'il y a eu au moins une phase directionnelle ;
    - MEAN_REVERSION : vérifiée s'il n'y a PAS eu de mouvement propre démarrant
      avant 16h15 (le contrarien avait sa fenêtre, même si ça part ensuite) ;
    - MIXTE / INSUFFISANT / absent : non noté (pas de prise de position à juger).
    """
    if qualif in ("EXPANSION_CONFIRME", "EXPANSION_POSSIBLE"):
        return int(facts.get("dir_tier") != "aucune")
    if qualif == "MEAN_REVERSION":
        return int(not facts.get("dir_early"))
    return None


def build_metrics(bars: pd.DataFrame, briefs: dict[str, str] | None = None,
                  prev_atr: float | None = None) -> dict | None:
    """{nom_de_métrique: (value_num, value_txt)} pour `journal.set_metric`,
    ou None si la séance n'est pas évaluable."""
    f = summarize_session(bars)
    if f is None:
        return None
    m: dict[str, tuple] = {
        "dir_pts": (f["dir_pts"], None),
        "dir_tier": (None, f["dir_tier"]),
        "dir_early": (float(f["dir_early"]), None),
        "range_pts": (f["range_pts"], None),
        "open_to_close_pts": (f["open_to_close_pts"], None),
        "eod_thresholds_version": (None, THRESHOLDS_VERSION),
    }
    if f["dir_debut"] is not None:
        m["dir_debut"] = (None, f["dir_debut"])
        m["dir_debut_avant_1615"] = (float(f["dir_debut_avant_1615"]), None)
        m["dir_first_debut"] = (None, f["dir_first_debut"])
    if prev_atr:
        m["range_over_prev_atr"] = (round(f["range_pts"] / prev_atr, 3), None)
    for nom, texte in (briefs or {}).items():
        q = parse_qualification(texte)
        if q is None:
            continue
        m[f"brief_{nom}_qualif"] = (None, q)
        sc = score_brief(q, f)
        if sc is not None:
            m[f"brief_{nom}_score"] = (float(sc), None)
    return m


def review_markdown(date: str, metrics: dict) -> str:
    """Bilan court, lisible en 20 secondes (fichier data/reviews/AAAA-MM-JJ.md)."""
    g = lambda k: (metrics.get(k) or (None, None))
    pts, tier = g("dir_pts")[0], g("dir_tier")[1]
    lignes = [f"# Bilan de séance — {date}", ""]
    if tier == "aucune":
        lignes.append("- **Aucun mouvement directionnel propre** "
                      f"(>= {PHASE_MIN_PTS:.0f} pts, retracement <= {RETRACE_MAX:.0%}).")
    else:
        lab = "journée directionnelle" if tier == "journee" else "phase directionnelle"
        avant = g("dir_debut_avant_1615")[0]
        lignes.append(f"- **{lab.capitalize()}** : {pts:+.0f} pts, départ "
                      f"{g('dir_debut')[1]} ET ("
                      f"{'avant' if avant else 'après'} 16h15 Paris).")
        lignes.append("- Fenêtre contrarienne (avant 16h15) : "
                      + ("**mouvement propre déjà en cours**" if g("dir_early")[0]
                         else "**calme**, pas de mouvement propre avant 16h15")
                      + f" (premier départ {g('dir_first_debut')[1]} ET).")
    lignes.append(f"- Range {g('range_pts')[0]:.0f} pts"
                  + (f" ({g('range_over_prev_atr')[0]:.2f} x ATR de la veille)"
                     if "range_over_prev_atr" in metrics else "")
                  + f", ouverture->clôture {g('open_to_close_pts')[0]:+.0f} pts.")
    briefs = [(n, metrics[f"brief_{n}_qualif"][1], metrics.get(f"brief_{n}_score"))
              for n in BRIEF_NOMS if f"brief_{n}_qualif" in metrics]
    if briefs:
        lignes += ["", "## Briefs vs réalité", ""]
        for n, q, sc in briefs:
            verdict = ("vérifié" if sc and sc[0] == 1 else
                       "non vérifié" if sc else "non noté")
            lignes.append(f"- {n} : {q} -> **{verdict}**")
    lignes += ["", f"_Seuils {THRESHOLDS_VERSION} — mesure descriptive, pas un signal._"]
    return "\n".join(lignes) + "\n"
