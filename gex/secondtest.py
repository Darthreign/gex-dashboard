"""Second test d'un excès, détecté EN DIRECT tick par tick, et ce que l'étude
d'un an en dit (avertissement de /scalp).

Mêmes règles, mêmes paramètres que l'étude (gex/excess.py :
`find_excesses` + `find_retest`, scripts/excess_report.py), mais
incrémentales : un excès de 100 pts depuis le dernier swing confirmé à 60 pts,
puis une réaction d'au moins 15 pts, puis un retour à moins de 10 pts de
l'extrême (dans les 120 min) = second test. On mesure alors la durée de la
réaction (extrême -> retour), classée express / normale / lente avec les
bornes de l'étude (terciles de l'apprentissage).

Les chiffres affichés (part des seconds tests qui ont prolongé de 50 / 100
pts, pire mouvement une fois sur dix…) ne sont PAS écrits ici : ils sont
relus dans le fichier produit par le rapport d'excès (`--flux databento
--gamma`), table zone /scalp × durée de la réaction, moitié TEST. Relancer
l'étude met donc l'affichage à jour.
"""
from __future__ import annotations

import json
import threading
from datetime import date, timedelta
from pathlib import Path

from .config import SETTINGS

EXCESS_PTS, SWING_PTS = 100.0, 60.0
REACTION_PTS, RETEST_PTS = 15.0, 10.0
MAX_WAIT_S = 120 * 60
DISPLAY_S = 180.0                # durée d'affichage après le second test
STATS_FILE = "second_test_stats_NQ.json"
DEFAULT_EDGES = (24.0, 103.0)    # bornes de l'étude du 11/10/2026 (repli)


class SecondTestTracker:
    """Version incrémentale de excess.find_excesses + excess.find_retest :
    `feed(ts, prix)` à chaque transaction, renvoie l'événement de second test
    quand il se produit (sinon None)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.leg = 0
        self.ext_hi = self.ext_lo = None
        self.swing, self.fired = None, False
        self.pending: list[dict] = []          # excès en attente de leur second test
        self.last: dict | None = None

    def feed(self, ts: float, price: float) -> dict | None:
        p = float(price)
        if self.ext_hi is None:
            self.ext_hi = self.ext_lo = p
        event = self._watch(ts, p)              # les excès déjà ouverts d'abord
        if self.leg >= 0:
            self.ext_hi = max(self.ext_hi, p)
        if self.leg <= 0:
            self.ext_lo = min(self.ext_lo, p)
        if self.leg >= 0 and self.ext_hi - p >= SWING_PTS:          # sommet confirmé
            self.swing, self.leg, self.fired, self.ext_lo = self.ext_hi, -1, False, p
        elif self.leg <= 0 and p - self.ext_lo >= SWING_PTS:        # creux confirmé
            self.swing, self.leg, self.fired, self.ext_hi = self.ext_lo, 1, False, p
        if self.leg and not self.fired:
            reach = (p - self.swing) if self.leg > 0 else (self.swing - p)
            if reach >= EXCESS_PTS:
                self.fired = True
                self.pending.append({"dir": self.leg, "swing": self.swing,
                                     "ts_excess": ts, "ext": p, "ts_ext": ts,
                                     "reacted": False})
        return event

    def _watch(self, ts: float, p: float) -> dict | None:
        event, keep = None, []
        for e in self.pending:
            if ts - e["ts_excess"] > MAX_WAIT_S:
                continue
            d = e["dir"]
            if not e["reacted"]:
                back = (p - e["ext"]) if d < 0 else (e["ext"] - p)
                if back >= REACTION_PTS:
                    e["reacted"] = True
                elif (p < e["ext"]) if d < 0 else (p > e["ext"]):
                    e["ext"], e["ts_ext"] = p, ts
                keep.append(e)
                continue
            near = (p <= e["ext"] + RETEST_PTS) if d < 0 else (p >= e["ext"] - RETEST_PTS)
            if near:
                event = {"ts": ts, "dir": d, "extreme": e["ext"], "swing": e["swing"],
                         "price": p, "duree_s": ts - e["ts_ext"],
                         "sens": "haussier" if d > 0 else "baissier"}
                self.last = event
                continue
            keep.append(e)
        self.pending = keep
        return event

    def current(self, now: float) -> dict | None:
        """Dernier second test, s'il date de moins de DISPLAY_S secondes."""
        if self.last and 0 <= now - self.last["ts"] <= DISPLAY_S:
            return dict(self.last)
        return None


# --------------------------------------------------------------- statistiques

_STATS: dict = {"mtime": None, "data": None}
_LOCK = threading.Lock()


def stats_path() -> Path:
    return SETTINGS.data_dir / "reports" / STATS_FILE


def load_stats() -> dict | None:
    """Table de l'étude (relue quand le fichier change), ou None."""
    p = stats_path()
    if not p.exists():
        return None
    with _LOCK:
        m = p.stat().st_mtime
        if _STATS["mtime"] != m:
            try:
                _STATS["data"] = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _STATS["data"] = None
            _STATS["mtime"] = m
        return _STATS["data"]


def duree_class(duree_s: float, edges=None) -> str:
    lo, hi = edges or DEFAULT_EDGES
    return "express" if duree_s < lo else ("lente" if duree_s >= hi else "normale")


def expiry_week(day: date) -> bool:
    """Du jeudi de roll (8 jours avant l'échéance) au 3e vendredi des mois
    trimestriels : le régime gamma y est peu fiable (deux contrats en jeu)."""
    from .dbtape import _third_friday
    for m in (3, 6, 9, 12):
        exp = _third_friday(day.year, m)
        if exp - timedelta(days=8) <= day <= exp:
            return True
    return False


def assess(event: dict, zone: str | None, day: date) -> dict:
    """Ce que l'étude dit de CE second test : niveau de risque et chiffres
    mesurés pour sa zone et sa durée de réaction."""
    stats = load_stats() or {}
    cls = duree_class(event["duree_s"], stats.get("duree_edges"))
    out = {"duree_class": cls, "zone": zone or "inconnu", "row": None,
           "risk": "inconnu", "expiry_week": expiry_week(day)}
    if out["expiry_week"] or not zone or zone not in ("frein", "transition", "accélérateur"):
        return out
    out["risk"] = {"accélérateur": "eleve", "transition": "moyen", "frein": "faible"}[zone]
    if zone in ("accélérateur", "transition") and cls == "express":
        out["risk"] = "eleve"
    if zone == "frein" and cls == "express":
        out["risk"] = "moyen"
    out["row"] = (stats.get("table") or {}).get(zone, {}).get(cls)
    return out
