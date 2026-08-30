"""Roll de contrat AU VOLUME pour la série continue NQ/ES.

Pourquoi pas le contrat « active-month » du courtier : c'est un critère de
CALENDRIER (il bascule à date fixe avant l'échéance), alors que le jeu de
référence `ticks_full` est un continu Databento `NQ.v.0`, dont le `.v.` signifie
roll AU VOLUME. Les deux diffèrent de quelques jours autour du roll — la série
capturée s'écarterait donc de l'historique précisément là où les prix sautent.

Règle appliquée (celle de Databento) : pour chaque séance, on écrit le contrat
qui a traité le PLUS DE VOLUME lors de la séance PRÉCÉDENTE. La décision est
donc figée pour toute la séance : pas de bascule en cours de route qui
couperait la série en deux.

Cela suppose de connaître le volume des DEUX contrats : la capture s'abonne au
front et au suivant, mais n'écrit sur disque que le dominant (choix
utilisateur). Les volumes des deux sont mémorisés ici, dans un petit fichier
d'état — sans lui, un redémarrage perdrait la référence de la veille.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, timedelta
from pathlib import Path

import requests

from .config import SETTINGS

log = logging.getLogger(__name__)

FUTURES_URL = "https://api.tastyworks.com/instruments/futures"

# Nombre de séances conservées dans le fichier d'état : il n'en faut qu'une
# (la veille), quelques-unes de plus rendent l'historique de roll auditable.
KEEP_SESSIONS = 15

_LOCK = threading.Lock()


def _state_path() -> Path:
    return SETTINGS.data_dir / "ticks" / "_roll_state.json"


def load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — un état illisible ne doit rien bloquer
        log.exception("État de roll illisible — repartir d'un état vide")
        return {}


def save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def resolve_pair(code: str, access: str) -> list[tuple[str, str]]:
    """[(symbole streamer, code contrat)] pour le contrat actif ET le suivant.

    L'API expose directement les drapeaux `active-month` / `next-active-month` ;
    on les lit plutôt que de deviner l'échéance. Le contrat suivant est requis
    pour pouvoir COMPARER les volumes — sans lui, aucun roll au volume n'est
    observable. Renvoie ce qui est disponible (jamais d'exception ici : un
    référentiel muet ne doit pas condamner la capture).
    """
    r = requests.get(FUTURES_URL, params={"product-code": code},
                     headers={"Authorization": f"Bearer {access}"}, timeout=30)
    r.raise_for_status()
    items = r.json()["data"]["items"]
    out: list[tuple[str, str]] = []
    for flag in ("active-month", "next-active-month"):
        for i in items:
            if i.get(flag) and i.get("streamer-symbol"):
                out.append((i["streamer-symbol"], i.get("symbol") or i["streamer-symbol"]))
                break
    return out


def record_volumes(symbol: str, session: str, per_contract: dict[str, float]) -> None:
    """Cumule le volume traité par contrat sur une séance (appelé à chaque
    flush). C'est cette mémoire qui fournira « le volume de la veille »."""
    if not per_contract:
        return
    with _LOCK:
        state = load_state()
        sess = state.setdefault(symbol, {}).setdefault(session, {})
        for contract, vol in per_contract.items():
            sess[contract] = sess.get(contract, 0) + float(vol)
        # bornage : on ne garde que les dernières séances
        keep = dict(sorted(state[symbol].items())[-KEEP_SESSIONS:])
        state[symbol] = keep
        save_state(state)


def dominant(symbol: str, session: str, contracts: list[str],
             state: dict | None = None) -> str | None:
    """Contrat à écrire pour `session` : celui qui a traité le plus de volume
    lors de la séance PRÉCÉDENTE connue.

    Repli explicite quand l'historique manque (premier démarrage, longue coupure)
    : `contracts[0]`, c'est-à-dire le contrat actif du courtier. Mieux vaut la
    convention calendaire — celle d'avant — que pas de capture du tout ; la
    séance suivante disposera d'un vrai volume de référence.
    """
    if not contracts:
        return None
    state = load_state() if state is None else state
    hist = (state.get(symbol) or {})
    # séances strictement antérieures, la plus récente d'abord
    for prev in sorted((s for s in hist if s < session), reverse=True):
        vols = {c: hist[prev].get(c, 0) for c in contracts}
        if any(vols.values()):
            best = max(vols, key=lambda c: vols[c])
            if best != contracts[0]:
                log.info("Roll %s : séance %s écrite sur %s (volume %s = %s vs %s = %s)",
                         symbol, session, best, best, vols[best],
                         contracts[0], vols[contracts[0]])
            return best
    return contracts[0]


def previous_session(session: str) -> str:
    """Séance calendaire précédente (utilitaire de test/inspection)."""
    d = date.fromisoformat(session) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()
