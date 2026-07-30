"""Taux sans risque r du modèle Black-Scholes, chargé automatiquement.

Un r figé devient un biais PERMANENT quand les taux bougent : le 2026-07-30,
le SOFR effectif était 3,65 % contre les 4,5 % codés en dur — déjà 0,85 point
d'écart. On charge donc le SOFR publié chaque jour ouvré par la NY Fed
(overnight, endpoint public sans clé), mis en cache une fois par jour.

Repli sur la constante `config.RISK_FREE_RATE` si le réseau échoue : l'outil
tourne alors exactement comme avant, hors ligne compris. `current_rate()` ne
fait JAMAIS d'appel réseau — seul `refresh()`, piloté par le scheduler au
démarrage puis quotidiennement, en fait un.

Portée : le taux courant sert aux calculs LIVE (metrics, futopt). La
reconstruction historique (backfill) garde volontairement la constante — lui
appliquer le SOFR d'aujourd'hui à une séance de l'an dernier serait un
anachronisme. r pèse de toute façon peu sur le court terme (rT ≈ 0 en 0DTE) ;
son effet se voit surtout sur le DEX et les échéances mensuelles.
"""
from __future__ import annotations

import logging
import threading
from datetime import date

import requests

from .config import RISK_FREE_RATE

log = logging.getLogger(__name__)

# Overnight SOFR, publié chaque jour ouvré pour la veille (~8h ET). Le taux au
# jour le jour est une approximation du sans-risque court terme, cohérente avec
# des échéances d'options majoritairement < 45 jours.
SOFR_URL = "https://markets.newyorkfed.org/api/rates/secured/sofr/last/1.json"

_lock = threading.Lock()
_rate: float = RISK_FREE_RATE     # repli tant qu'aucun refresh n'a abouti
_day: date | None = None


def current_rate() -> float:
    """Taux courant, SANS appel réseau (valeur en cache, ou repli constant)."""
    return _rate


def refresh(force: bool = False) -> float:
    """Récupère le SOFR du jour et le met en cache.

    À appeler au démarrage puis une fois par jour (cf. scheduler). Idempotent
    dans la journée. En cas d'échec (réseau, JSON, valeur aberrante), conserve
    la valeur courante — jamais d'exception propagée, le calcul ne doit pas
    dépendre de la disponibilité d'une API externe.
    """
    global _rate, _day
    today = date.today()
    if not force and _day == today:
        return _rate
    try:
        resp = requests.get(SOFR_URL, timeout=15)
        resp.raise_for_status()
        item = resp.json()["refRates"][0]
        rate = float(item["percentRate"]) / 100.0
        # garde-fou : un taux hors [0, 20 %] est une réponse aberrante — on
        # garde le repli plutôt que d'empoisonner tous les niveaux calculés
        if not 0.0 <= rate <= 0.20:
            raise ValueError(f"SOFR hors plage : {item.get('percentRate')}")
        with _lock:
            _rate, _day = rate, today
        log.info("Taux sans risque r = %.4f (SOFR effectif %s)",
                 rate, item.get("effectiveDate"))
    except Exception:  # noqa: BLE001 — réseau/JSON/plage : repli silencieux
        log.warning("SOFR indisponible, repli sur r = %.4f", _rate)
    return _rate
