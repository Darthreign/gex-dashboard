"""Chaînes d'options d'INDICE (SPX, NDX) lues nativement via dxFeed.

Pourquoi ce module existe : l'endpoint public CBOE est délayé ~15 min à la
source. Sur du 0DTE, c'est énorme — mesuré le 2026-07-29 sur SPX, dxFeed
voyait 3 à 6 fois plus de volume que CBOE aux mêmes strikes (7200P : 1374
contre 317). Le HVL, pondéré par le volume du jour, et le flux de gamma
échangé sont donc structurellement en retard sur la source gratuite.

Ce que la comparaison a établi (même date, 178 contrats 0DTE autour du
spot) : dxFeed livre l'open interest sur 178/178 contrats, avec un écart de
ZÉRO face à CBOE. Ce n'est donc pas une source approximative qu'on
substituerait à une source fiable — c'est la même donnée, sans le retard.

⚠️ Licence. Contrairement à CBOE (public, redistribuable), ces données
viennent du compte courtier : usage personnel, JAMAIS redistribuables. Les
lignes d'historique produites ici portent `source="dxfeed"`, ce qui les
exclut de l'export (cf. gex/export.py, qui n'autorise que "cboe"). Sans
identifiants, ce module ne fait rien et l'outil reste entièrement
fonctionnel sur CBOE — c'est la promesse du README, elle ne bouge pas.

Différences avec `futopt` (options sur FUTURE), qui justifient un module
séparé plutôt qu'un paramètre de plus :
- le référentiel vient d'un autre endpoint, à la structure imbriquée
  (racine -> échéances -> strikes), et non d'une liste plate ;
- le multiplicateur est celui des options d'indice (100), pas le notionnel
  du future (20 $/pt sur NQ, 50 sur ES) ;
- le spot est celui de l'indice cash, pris sur le flux temps réel déjà
  abonné à SPX/NDX (`rtquote.QUOTES`), pas sur un contrat future.

Toute la mécanique de collecte dxLink, elle, est réutilisée telle quelle
depuis `futopt` (`_collect`, `enrich_native`) : une seule implémentation du
protocole, testée à un seul endroit.
"""
from __future__ import annotations

import asyncio
import logging

import pandas as pd
import requests

from . import ingest
from .config import CONTRACT_MULTIPLIER, UNDERLYINGS
from .futopt import DEFAULT_MAX_DAYS, DEFAULT_WINDOW, _collect, enrich_native, filter_chain
from .rtquote import QUOTES, quote_token

log = logging.getLogger(__name__)

CHAIN_URL = "https://api.tastyworks.com/option-chains/{symbol}/nested"

# Indices pour lesquels une chaîne native remplace la chaîne CBOE quand un
# compte courtier est configuré. SPY/QQQ en sont volontairement absents :
# ce sont des ETF versant un dividende, que le calcul traite déjà avec une
# approximation q=0 (cf. limites du README) — le retard de 15 min n'est pas
# leur principal facteur d'imprécision.
NATIVE_INDEX = ("SPX", "NDX")


def fetch_chain_instruments(symbol: str, access_token: str) -> pd.DataFrame:
    """Référentiel des contrats d'indice : strike, type, échéance, symbole dxFeed.

    L'endpoint « nested » renvoie une structure à trois niveaux (racine ->
    échéances -> strikes), avec le symbole streamer déjà résolu pour le call
    ET le put de chaque strike — d'où l'aplatissement fait ici.

    Les DEUX racines sont conservées quand elles existent (SPXW hebdomadaire
    et SPX mensuel, NDXP et NDX) : ce sont des séries distinctes, avec leur
    propre open interest, et la chaîne CBOE les contient toutes les deux
    également. En déduplíquer une fausserait le GEX par strike.
    """
    r = requests.get(CHAIN_URL.format(symbol=symbol),
                     headers={"Authorization": f"Bearer {access_token}"}, timeout=90)
    r.raise_for_status()
    rows = []
    for item in r.json()["data"]["items"]:
        root = item.get("root-symbol")
        for exp in item.get("expirations", []):
            expiry = pd.Timestamp(exp["expiration-date"]).date()
            for st in exp.get("strikes", []):
                strike = float(st["strike-price"])
                for cp, key in (("C", "call-streamer-symbol"),
                                ("P", "put-streamer-symbol")):
                    stream = st.get(key)
                    if stream:
                        rows.append({"strike": strike, "type": cp, "expiry": expiry,
                                     "streamer_symbol": stream,
                                     "underlying_symbol": root})
    return pd.DataFrame(rows)


def reference_spot(symbol: str) -> float | None:
    """Spot de l'indice, temps réel de préférence.

    `rtquote.QUOTES` est déjà abonné à SPX et NDX pour l'affichage : le prix
    y est donc disponible sans un seul appel supplémentaire. Le repli sur le
    spot CBOE (délayé) ne sert qu'au démarrage, avant que le flux n'ait reçu
    sa première cotation — mieux vaut une chaîne évaluée à un spot de 15 min
    que pas de chaîne du tout.
    """
    live = QUOTES.price(symbol)
    if live:
        return float(live)
    u = UNDERLYINGS.get(symbol)
    if u is None:
        return None
    try:
        spot, _ = ingest.fetch_index_spot(u.cboe_symbol)
        log.info("%s : spot temps réel indisponible, repli sur le spot CBOE", symbol)
        return float(spot)
    except Exception:  # noqa: BLE001 — un spot manquant n'est pas fatal
        log.exception("%s : spot indisponible", symbol)
        return None


def build_native_chain(symbol: str, window: float = DEFAULT_WINDOW,
                       max_days: int = DEFAULT_MAX_DAYS) -> pd.DataFrame | None:
    """Chaîne d'indice native complète, prête pour les fonctions de `metrics`.

    Renvoie None plutôt qu'une chaîne partielle si le spot manque : des
    niveaux calculés sur un spot faux sont pires qu'une absence de niveaux.
    """
    _, _, access = quote_token()
    spot = reference_spot(symbol)
    if not spot:
        log.warning("%s : spot indisponible, chaîne native abandonnée", symbol)
        return None

    chain = fetch_chain_instruments(symbol, access)
    chain = filter_chain(chain, spot, window, max_days)
    if chain.empty:
        log.warning("%s : aucun contrat dans la fenêtre", symbol)
        return None

    raw = asyncio.run(_collect(chain["streamer_symbol"].tolist(),
                               stop_when_complete=True))
    # multiplicateur d'options d'INDICE (100), pas le notionnel d'un future
    df = enrich_native(chain, raw, spot, CONTRACT_MULTIPLIER)
    log.info("%s : chaîne d'indice native — %d contrats, spot %.2f",
             symbol, len(df), spot)
    return df
