"""Ingestion native des options sur futures (NQ, ES) via dxFeed.

Pourquoi ce module existe : les options sur future CME ont leur propre
structure de gamma, DISTINCTE de la chaîne d'indice transposée (SPX→ES,
NDX→NQ). Confirmé le 2026-07-27 en comparant à une plateforme tierce — son
Zero Gamma pour NQ colle à 25 points près au calcul fait ici, contre 81 à
160 points d'écart pour la version transposée depuis NDX. La transposition
convertit correctement les PRIX ; elle ne dit rien de la structure propre au
marché des options sur future, dont les teneurs ne se couvrent pas aux mêmes
strikes que sur l'indice cash.

Différence de contrat à ne pas oublier : le multiplicateur est celui du
FUTURE sous-jacent (20 $/point pour NQ, 50 $/point pour ES), pas 100 comme
les indices CBOE. Les options hebdomadaires livrent dans le contrat actif
(`future-price-ratio` = 1.0, vérifié via l'API) : le spot de référence est
donc celui du future, pas un cash inexistant.

Le calcul lui-même réutilise `metrics.top_gex_levels`, `key_levels`,
`zero_gamma`, `gex_at_spot` : la chaîne native est construite avec exactement
les colonnes qu'ils attendent (strike, type, expiry, iv, t_years, gamma_bs,
delta_bs, gex, dex, open_interest, volume, bid, ask, spot), donc aucune
logique de niveau n'est dupliquée.

Comme pour rtquote, on prend l'IV que dxFeed calcule mais PAS son gamma : le
gamma est recalculé ici en Black-Scholes maison, pour rester cohérent avec le
reste de l'outil plutôt que de mélanger deux modèles dans le même historique.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import numpy as np
import pandas as pd
import requests

from . import greeks, store
from .config import RISK_FREE_RATE
from .metrics import ET, YEAR_SECONDS, seconds_to_expiry
from .rtquote import QUOTES, quote_token

log = logging.getLogger(__name__)

FUTURES_URL = "https://api.tastyworks.com/instruments/futures"
CHAIN_URL = "https://api.tastyworks.com/futures-option-chains/{code}"

# Silence après lequel on considère que dxFeed a fini d'envoyer l'état courant
# de la souscription (pas un flux d'historique : ceci retourne l'existant puis
# se tait, contrairement à pricehist qui attend une clôture de connexion).
# Plus large que sur un flux simple : avec le fractionnement des souscriptions
# (cf. SUBSCRIBE_CHUNK), les derniers lots peuvent commencer à répondre
# plusieurs secondes après les premiers.
IDLE_TIMEOUT_S = 20.0

# Fenêtre par défaut resserrée sur le 0DTE et l'hebdomadaire — c'est ce qui
# motive ce module (cf. mémoire dxfeed-streaming-acquis.md). Une collecte
# native coûte du temps (une salve dxFeed de ~2000 contrats prend ~90 s, cf.
# MAX_BURST), donc élargir la fenêtre au-delà de ce qui est réellement utilisé
# ne fait qu'allonger la collecte pour des strikes jamais affichés.
DEFAULT_WINDOW = 0.08
DEFAULT_MAX_DAYS = 14

# dxFeed livre fiablement une salve de souscriptions envoyée D'UN SEUL COUP à
# la connexion (constaté : jusqu'à 9 000 passent, 15 000 sont rejetées avec
# "BAD_ACTION: Your subscription rate is too high"). Fractionner en plusieurs
# messages sur la MÊME connexion a été tenté et rejeté d'une autre façon : les
# lots sont acceptés (FEED_CONFIG en retour) mais aucune donnée n'arrive
# jamais — le mécanisme de snapshot initial de dxLink ne semble répondre qu'à
# la toute première demande d'un canal. La solution qui marche à coup sûr :
# une salve unique par connexion, et plusieurs connexions SÉQUENTIELLES si le
# nombre de contrats dépasse le seuil sûr.
MAX_BURST = 6000  # marge sous le seuil de rejet observé (9000)
# Filet de sécurité : quoi qu'il arrive, une collecte planifiée ne doit jamais
# tourner indéfiniment. Découvert le 2026-07-27 : un calcul de silence basé sur
# TOUT message (KEEPALIVE compris) ne s'arrête jamais, puisqu'un serveur en vit
# indéfiniment — d'où ce plafond, en plus du calcul corrigé sur le seul FEED_DATA.
MAX_DURATION_S = 90.0

_multiplier_cache: dict[str, float] = {}


def get_multiplier(product_code: str, access_token: str) -> float | None:
    """Multiplicateur $/point du future — PAS celui des options d'indice.

    Résolu une fois via l'API (`notional-multiplier`) et mis en cache : il ne
    change pas en cours de session.
    """
    if product_code in _multiplier_cache:
        return _multiplier_cache[product_code]
    r = requests.get(FUTURES_URL, params={"product-code": product_code},
                     headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    r.raise_for_status()
    items = [i for i in r.json()["data"]["items"] if i.get("active-month")]
    if not items:
        return None
    mult = float(items[0]["notional-multiplier"])
    _multiplier_cache[product_code] = mult
    return mult


def fetch_chain_instruments(product_code: str, access_token: str) -> pd.DataFrame:
    """Référentiel des contrats : strike, type, échéance, symbole dxFeed.

    Une ligne par contrat, sans donnée de marché — celle-ci vient ensuite du
    flux dxLink (`_collect`).
    """
    r = requests.get(CHAIN_URL.format(code=product_code),
                     headers={"Authorization": f"Bearer {access_token}"}, timeout=90)
    r.raise_for_status()
    items = r.json()["data"]["items"]
    rows = [{
        "strike": float(i["strike-price"]),
        "type": i["option-type"],
        "expiry": pd.Timestamp(i["expiration-date"]).date(),
        "streamer_symbol": i["streamer-symbol"],
        "underlying_symbol": i.get("underlying-symbol"),
    } for i in items if i.get("streamer-symbol")]
    return pd.DataFrame(rows)


def filter_chain(chain: pd.DataFrame, spot: float, window: float = DEFAULT_WINDOW,
                 max_days: int = DEFAULT_MAX_DAYS) -> pd.DataFrame:
    """Restreint aux strikes et échéances qui comptent réellement.

    Contrôle le volume de souscriptions : la chaîne NQ complète avoisine les
    7 000 contrats, alors que les niveaux affichés ne portent jamais sur des
    strikes à plus de 15 % du spot ni des échéances à plus de 45 jours.
    """
    if chain.empty:
        return chain
    lo, hi = spot * (1 - window), spot * (1 + window)
    horizon = (pd.Timestamp.now(tz=ET).date()
               + pd.Timedelta(days=max_days))
    return chain[chain["strike"].between(lo, hi)
                & (chain["expiry"] <= horizon)].reset_index(drop=True)


async def _collect_one(streamer_symbols: list[str],
                       events: tuple[str, ...],
                       timeout: float) -> dict[str, dict]:
    """Une connexion, une salve unique de souscription, jusqu'à `MAX_BURST`.

    Envoyer la salve complète EN UN SEUL message est essentiel : fractionner
    en plusieurs `FEED_SUBSCRIPTION` sur la même connexion a été tenté et
    échoue silencieusement — chaque lot est accusé réception (`FEED_CONFIG`)
    mais aucune donnée n'arrive jamais. Le mécanisme de snapshot initial de
    dxLink ne semble répondre qu'à la toute première demande d'un canal.
    """
    import time as _time

    import websockets

    token, url, _ = quote_token()
    out: dict[str, dict] = {}

    # ping_interval=None : dxLink a son PROPRE keepalive applicatif (le type
    # "KEEPALIVE" géré plus bas), redondant avec le ping WebSocket automatique
    # de la bibliothèque.
    async with websockets.connect(url, max_size=2 ** 24, ping_interval=None) as ws:
        async def send(m):
            await ws.send(json.dumps(m))

        await send({"type": "SETUP", "channel": 0, "version": "0.1-futopt",
                    "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})
        auth_sent = False
        # Le silence qui compte est celui du FLUX DE DONNÉES, pas celui de la
        # connexion : un KEEPALIVE arrive indéfiniment sur une connexion
        # normale et rouvrirait la fenêtre à chaque fois si on l'y incluait —
        # la collecte n'aurait alors plus jamais de raison de s'arrêter, même
        # une fois toute la donnée reçue. Seul un FEED_DATA repousse
        # `last_data`, un KEEPALIVE est traité mais ignoré pour ce calcul.
        started = last_data = _time.monotonic()
        while True:
            now = _time.monotonic()
            remaining = min(timeout - (now - last_data),
                            MAX_DURATION_S - (now - started))
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            except websockets.exceptions.ConnectionClosed:
                # dxFeed clôt parfois la connexion une fois livré (cf. pricehist)
                break
            m = json.loads(raw)
            typ = m.get("type")
            if typ == "AUTH_STATE":
                state = m.get("state")
                if state == "UNAUTHORIZED" and not auth_sent:
                    auth_sent = True
                    await send({"type": "AUTH", "channel": 0, "token": token})
                elif state == "UNAUTHORIZED":
                    raise RuntimeError("jeton dxFeed refusé")
                elif state == "AUTHORIZED":
                    await send({"type": "CHANNEL_REQUEST", "channel": 1,
                                "service": "FEED",
                                "parameters": {"contract": "AUTO"}})
            elif typ == "CHANNEL_OPENED":
                subs = [{"type": e, "symbol": s}
                       for s in streamer_symbols for e in events]
                await send({"type": "FEED_SUBSCRIPTION", "channel": 1, "add": subs})
            elif typ == "KEEPALIVE":
                await send({"type": "KEEPALIVE", "channel": 0})
            elif typ == "ERROR":
                log.warning("dxFeed ERROR pendant la collecte : %s", str(m)[:300])
            elif typ == "FEED_DATA":
                last_data = _time.monotonic()
                for item in (m.get("data") or []):
                    if not isinstance(item, dict):
                        continue
                    sym = item.get("eventSymbol")
                    if sym not in streamer_symbols:
                        continue
                    d = out.setdefault(sym, {})
                    etype = item.get("eventType")
                    if etype == "Quote":
                        for k in ("bidPrice", "askPrice"):
                            v = item.get(k)
                            if isinstance(v, (int, float)) and v == v:
                                d[k] = float(v)
                    elif etype == "Greeks":
                        v = item.get("volatility")
                        if isinstance(v, (int, float)) and v == v:
                            d["iv"] = float(v)
                    elif etype == "Summary":
                        for k, out_k in (("openInterest", "oi"), ("dayVolume", "volume")):
                            v = item.get(k)
                            if isinstance(v, (int, float)) and v == v:
                                d[out_k] = float(v)
    return out


async def _collect(streamer_symbols: list[str],
                   events: tuple[str, ...] = ("Quote", "Greeks", "Summary"),
                   timeout: float = IDLE_TIMEOUT_S) -> dict[str, dict]:
    """Souscrit et fusionne les événements reçus, un dict par symbole.

    Fractionne en plusieurs connexions séquentielles si le nombre de
    souscriptions dépasse `MAX_BURST` — chacune envoie sa salve en un seul
    message (cf. `_collect_one`). Contrairement au flux temps réel
    (`rtquote`), ce sont des connexions à usage unique : demander l'état
    courant, l'accumuler jusqu'au silence, puis fermer.
    """
    per_symbol = max(len(events), 1)
    batch_size = max(MAX_BURST // per_symbol, 1)
    out: dict[str, dict] = {}
    for i in range(0, len(streamer_symbols), batch_size):
        batch = streamer_symbols[i:i + batch_size]
        out.update(await _collect_one(batch, events, timeout))
    return out


def enrich_native(chain: pd.DataFrame, raw: dict[str, dict], spot: float,
                  multiplier: float, now_et: datetime | None = None) -> pd.DataFrame:
    """Assemble référentiel + données de marché, calcule gamma/delta/GEX/DEX.

    Colonnes en sortie : celles qu'attendent `metrics.top_gex_levels`,
    `key_levels`, `zero_gamma`, `gex_at_spot` — aucune logique de niveau n'est
    dupliquée ici, seule la construction de la chaîne diffère de `metrics.enrich`
    (multiplicateur paramétrable, IV prise de dxFeed plutôt qu'inversée du prix).
    """
    now_et = now_et or datetime.now(ET)
    df = chain.copy()
    md = pd.DataFrame.from_dict(raw, orient="index")
    df = df.merge(md, left_on="streamer_symbol", right_index=True, how="left")
    for col in ("iv", "oi", "volume", "bidPrice", "askPrice"):
        if col not in df.columns:
            df[col] = np.nan
    df = df.rename(columns={"oi": "open_interest", "bidPrice": "bid", "askPrice": "ask"})
    df["open_interest"] = df["open_interest"].fillna(0.0)
    df["volume"] = df["volume"].fillna(0.0)
    df["bid"] = df["bid"].fillna(0.0)
    df["ask"] = df["ask"].fillna(0.0)

    secs = seconds_to_expiry(pd.Series(df["expiry"]), now_et)
    df = df[secs > 0].reset_index(drop=True)
    secs = secs[secs > 0]
    t = np.maximum(secs, 300.0) / YEAR_SECONDS
    df["t_years"] = t

    valid = df["iv"].to_numpy() > 1e-4
    iv = np.where(valid, df["iv"].to_numpy(), 1.0)
    is_call = (df["type"] == "C").to_numpy()
    g = np.where(valid, greeks.gamma(spot, df["strike"].to_numpy(), t, RISK_FREE_RATE, iv), 0.0)
    dcall = greeks.call_delta(spot, df["strike"].to_numpy(), t, RISK_FREE_RATE, iv)
    d = np.where(valid, np.where(is_call, dcall, dcall - 1.0), 0.0)

    df["gamma_bs"] = g
    df["delta_bs"] = d
    sign = np.where(is_call, 1.0, -1.0)
    oi = df["open_interest"].to_numpy()
    df["gex"] = sign * g * oi * multiplier * spot ** 2 * 0.01
    df["dex"] = d * oi * multiplier * spot
    df["spot"] = float(spot)
    return df


def _reference_spot(product_code: str, access_token: str) -> float | None:
    """Spot du future actif : celui du flux temps réel si disponible, sinon un
    Quote ponctuel sur le contrat actif lui-même."""
    live = QUOTES.price(product_code)
    if live:
        return live
    r = requests.get(FUTURES_URL, params={"product-code": product_code},
                     headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    r.raise_for_status()
    items = [i for i in r.json()["data"]["items"] if i.get("active-month")]
    if not items:
        return None
    stream_sym = items[0]["streamer-symbol"]
    data = asyncio.run(_collect([stream_sym], events=("Quote",)))
    d = data.get(stream_sym, {})
    bid, ask = d.get("bidPrice"), d.get("askPrice")
    if bid and ask:
        return (bid + ask) / 2
    return None


def build_native_chain(product_code: str, window: float = DEFAULT_WINDOW,
                       max_days: int = DEFAULT_MAX_DAYS) -> pd.DataFrame | None:
    """Chaîne d'options natives complète, prête pour les fonctions de `metrics`.

    Renvoie None si le spot ou le multiplicateur sont indisponibles — mieux
    vaut ne rien produire que des niveaux faux.
    """
    _, _, access = quote_token()
    spot = _reference_spot(product_code, access)
    if not spot:
        log.warning("%s : spot indisponible, chaîne native abandonnée", product_code)
        return None
    multiplier = get_multiplier(product_code, access)
    if not multiplier:
        log.warning("%s : multiplicateur indisponible", product_code)
        return None

    chain = fetch_chain_instruments(product_code, access)
    chain = filter_chain(chain, spot, window, max_days)
    if chain.empty:
        log.warning("%s : aucun contrat dans la fenêtre", product_code)
        return None

    raw = asyncio.run(_collect(chain["streamer_symbol"].tolist()))
    df = enrich_native(chain, raw, spot, multiplier)
    log.info("%s : chaîne native — %d contrats, spot %.2f, multiplicateur %.0f",
             product_code, len(df), spot, multiplier)
    return df


def pull_native(product_code: str, persist: bool = True) -> pd.DataFrame | None:
    """Point d'entrée planifiable : construit la chaîne et la persiste.

    N'élève jamais — appelée depuis le planificateur, un échec ne doit pas
    interrompre le reste de la collecte.
    """
    try:
        df = build_native_chain(product_code)
    except Exception:  # noqa: BLE001
        log.exception("%s : échec de la chaîne native", product_code)
        return None
    if df is None or df.empty:
        return None
    if persist:
        # symbole distinct des clés d'échelle "NQ"/"ES" : celles-ci désignent
        # un PRIX transposé, "_OPT" une chaîne d'options natives — les deux ne
        # doivent jamais se confondre dans le stockage.
        try:
            store.save_snapshot(f"{product_code}_OPT", df, datetime.now(ET))
        except Exception:  # noqa: BLE001
            log.exception("%s : échec d'écriture du snapshot natif", product_code)
    return df
