"""Replay de la heatmap sur la séance du jour (page principale).

Chaque minute, le processus qui calcule les chaînes (la capture en mode
moteur, sinon le dashboard) enregistre pour NQ, ES, SPX et NDX ce que la
heatmap affichait à cet instant : profil de gamma par strike et niveaux
(Gamma Flip, HVL, Call Wall, Put Support, 1D Min/Max, murs GEX), en prix
NATIFS, calculés par la même fonction que l'affichage en direct
(`app.heatmap_levels`). Le replay rejoue ensuite ces images minute par
minute avec les bougies de la séance.

Une ligne JSON par minute dans data/replay/<SYMBOLE>/<séance>.jsonl ; la
séance est la séance CME (18h00 ET la veille -> 17h00 ET, cf.
tickcapture._session_day), ouverture Globex comprise (demande de
l'utilisateur, 2026-10-16). Le profil n'est écrit que lorsqu'il change
(une salve de chaîne toutes les ~3 min) : les lignes intermédiaires ne
portent que les niveaux, le replay reprend le dernier profil connu.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from .config import SETTINGS

log = logging.getLogger(__name__)

SYMBOLS = ("NQ", "ES", "SPX", "NDX")
FRAME_EVERY_S = 60
WINDOW = 0.04                    # fenêtre de strikes par défaut de la heatmap
_LAST_PROFILE: dict[str, list] = {}
_LOCK = threading.Lock()


def _session_day(ts: float) -> str:
    from .tickcapture import _session_day as day
    return day(ts)


def path_for(symbol: str, session: str) -> Path:
    return SETTINGS.data_dir / "replay" / symbol / f"{session}.jsonl"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, 4)


def _profile(lv: dict) -> list:
    # 6 chiffres significatifs : largement assez pour des barres, fichier 3x plus léger
    return [[_num(k), float(f"{o:.6g}"), float(f"{v:.6g}")] for k, o, v in lv["profile"]]


def make_frame(ts: float, lv: dict, last_profile: list | None) -> tuple[dict, list]:
    """Ligne à écrire (profil omis s'il n'a pas changé) et profil courant."""
    prof = _profile(lv)
    frame = {"t": round(ts, 1), "spot": _num(lv["spot"]),
             **{k: _num(lv.get(k)) for k in ("zero_gamma", "hvl", "call_wall",
                                              "put_support", "d1_min", "d1_max")},
             "walls": [[_num(k), str(lab)] for k, lab in lv.get("walls") or ()]}
    if prof != last_profile:
        frame["profile"] = prof
        frame["lo"], frame["hi"] = _num(lv["lo"]), _num(lv["hi"])
    return frame, prof


def record(symbol: str, lv: dict | None, now: float | None = None) -> Path | None:
    """Ajoute l'image de `symbol` à l'instant `now` (None : rien à écrire)."""
    if lv is None:
        return None
    now = time.time() if now is None else now
    p = path_for(symbol, _session_day(now))
    with _LOCK:
        last = _LAST_PROFILE.get(f"{symbol}|{p.name}")
        frame, prof = make_frame(now, lv, last)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(frame, separators=(",", ":")) + "\n")
        _LAST_PROFILE[f"{symbol}|{p.name}"] = prof
    return p


def record_all(now: float | None = None) -> None:
    """Tâche planifiée (chaque minute) : une image par sous-jacent suivi.
    Une chaîne absente ou une erreur sur un symbole n'empêche pas les autres."""
    from . import app
    from .scheduler import STATE
    now = time.time() if now is None else now
    for sym in SYMBOLS:
        try:
            st = app.chain_state(sym)
            with STATE.lock:
                df, snap = st.enriched, st.snapshot
            if df is None or snap is None or not snap.spot:
                continue
            record(sym, app.heatmap_levels(sym, df, float(snap.spot), WINDOW), now)
        except Exception:  # noqa: BLE001 — le replay ne doit jamais gêner le reste
            log.exception("Replay : image %s non enregistrée", sym)


def load(symbol: str, session: str) -> list[dict]:
    """Images de la séance, dans l'ordre, chacune avec son profil (le dernier
    profil connu est reporté sur les lignes qui n'en portent pas). Une ligne
    illisible (écriture interrompue) est ignorée."""
    p = path_for(symbol, session)
    if not p.exists():
        return []
    out, prof, lo, hi = [], None, None, None
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            f = json.loads(line)
        except ValueError:
            continue
        if "profile" in f:
            prof, lo, hi = f["profile"], f.get("lo"), f.get("hi")
        if prof is None:
            continue
        out.append({**f, "profile": prof, "lo": lo, "hi": hi})
    out.sort(key=lambda f: f["t"])
    return out
