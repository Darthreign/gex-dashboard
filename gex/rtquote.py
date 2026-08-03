"""Spot temps réel via dxFeed (dxLink), optionnel.

Le dashboard fonctionne sans : les chaînes d'options viennent de CBOE, délayées
15 minutes, et le spot en est extrait. Cette couche ne remplace pas les chaînes
— elle ne fournit QUE le prix courant des sous-jacents, ce qui suffit à savoir
en temps réel de quel côté du Gamma Flip on se trouve et quand un niveau est
franchi. Les niveaux eux-mêmes reposent sur l'open interest, publié une fois
par jour : les recalculer plus vite n'apporterait rien.

Activation : renseigner TT_REFRESH, TASTYTRADE_CLIENT_ID et
TASTYTRADE_CLIENT_SECRET (cf. gex/tt_auth.py). Sans ces variables, le module
reste inerte et `status()` renvoie "off".

⚠️ Données courtier : NON redistribuables. Elles servent à l'affichage local
et ne sont pas persistées dans les Parquet partageables (cf. gex/export.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import requests

from .config import UNDERLYINGS
from .tickrec import TICKREC

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.tastyworks.com/oauth/token"
QUOTE_TOKEN_URL = "https://api.tastyworks.com/api-quote-tokens"
FUTURES_URL = "https://api.tastyworks.com/instruments/futures"

# Flux public dxFeed (aucun compte, aucun jeton) — confirmé le 2026-07-28 :
# AUTH_STATE renvoie directement AUTHORIZED, sans jamais demander de jeton.
# Délayé (~15-20 min, vérifié par l'écart entre bidTime et l'heure réelle),
# donc pas un remplacement du flux temps réel — un repli pour un poste sans
# identifiants courtier plutôt que rien du tout sur NQ/ES.
PUBLIC_DEMO_URL = "wss://demo.dxfeed.com/market-data/dxlink-ws"

_QUARTERLY_MONTHS = (3, 6, 9, 12)
_QUARTERLY_CODE = {3: "H", 6: "M", 9: "U", 12: "Z"}


def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    first_friday = 1 + (4 - d.weekday()) % 7  # weekday() : lundi=0 … vendredi=4
    return date(year, month, first_friday + 14)


def front_quarterly_code(today: date | None = None) -> str:
    """Code mois+année (ex. "U26") du contrat trimestriel actif pour un future
    indiciel (NQ, ES) — calculé sans appel réseau, contrairement à
    `resolve_symbols` qui interroge l'API tastytrade authentifiée pour la
    même info. Roule au contrat suivant ~1 semaine avant le 3e vendredi
    (approximation suffisante pour un spot d'affichage, pas pour trader le
    roll lui-même)."""
    today = today or date.today()
    for month in _QUARTERLY_MONTHS:
        expiry = _third_friday(today.year, month)
        if today <= expiry - timedelta(days=7):
            return f"{_QUARTERLY_CODE[month]}{today.year % 100:02d}"
    year = today.year + 1
    return f"{_QUARTERLY_CODE[3]}{year % 100:02d}"

# Au-delà de ce silence (secondes) on considère le flux dégradé : la connexion
# tient mais plus rien n'arrive. Hors séance, l'absence de tick est normale —
# l'état "dégradé" n'a donc de sens que marché ouvert (cf. status()).
STALE_S = 30.0
# Reconnexion : temporisation croissante, plafonnée.
BACKOFF_START, BACKOFF_MAX = 2.0, 60.0


def _env(name: str) -> str | None:
    """Variable d'environnement, avec repli sur le registre utilisateur Windows
    (une session ouverte avant `setx` ne voit pas la nouvelle valeur)."""
    val = os.environ.get(name)
    if not val and sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                val = winreg.QueryValueEx(k, name)[0]
        except OSError:
            pass
    return val


def credentials_present() -> bool:
    return all(_env(n) for n in
               ("TT_REFRESH", "TASTYTRADE_CLIENT_ID", "TASTYTRADE_CLIENT_SECRET"))


# Champs demandés à FEED_SETUP, DANS CET ORDRE — cf. quote-streamer.ts du SDK
# officiel tastytrade (JS), qui configure { acceptAggregationPeriod: 10,
# acceptDataFormat: COMPACT } avant de souscrire. Le format FULL (reçu par
# défaut sans ce message) est annoncé par la doc dxLink comme voué à
# disparaître. On ne demande que les champs réellement utilisés en aval
# (rtquote._ingest, futopt.enrich_native) plutôt que la liste complète de
# l'exemple officiel (bidSize/askSize, delta/gamma/theta/rho/vega, etc.),
# absents de nos besoins.
# ⚠️ Le volume du jour vit sur `Trade` (`dayVolume`), PAS sur `Summary`.
# Vérifié le 2026-07-29 en interrogeant le flux en format FULL, sur options
# d'indice (OPRA) comme sur options sur future (CME) : `Summary` ne porte que
# `openInterest` et `prevDayVolume` (celui de la VEILLE). Le réclamer sur
# `Summary` comme on le faisait ne produisait aucune erreur — le champ était
# simplement toujours absent, donc le volume restait à zéro sur toute la
# chaîne native, `pc_volume` à NaN et le HVL (pondéré volume) incalculable
# pour NQ/ES.
COMPACT_FIELDS: dict[str, list[str]] = {
    "Quote": ["eventType", "eventSymbol", "bidPrice", "askPrice"],
    "Trade": ["eventType", "eventSymbol", "price", "dayVolume"],
    "Greeks": ["eventType", "eventSymbol", "volatility"],
    "Summary": ["eventType", "eventSymbol", "openInterest"],
}


def decode_compact_feed_data(data: list) -> list[dict]:
    """Décode le format COMPACT de FEED_DATA en la même forme (liste de
    dicts) que produisait l'ancien format FULL implicite — pour ne rien
    changer au code qui consomme ces événements en aval.

    En COMPACT, `data` alterne [typeTag, valeurs_à_plat, typeTag, ...] : un
    bloc `valeurs_à_plat` répète un groupe de N valeurs par événement (N =
    len(COMPACT_FIELDS[typeTag])), positionnellement dans l'ordre déclaré à
    FEED_SETUP — pas de clés, l'ordre EST le contrat. Un typeTag inconnu (un
    champ qu'on n'a pas déclaré dans COMPACT_FIELDS) est ignoré plutôt que de
    faire échouer tout le décodage.
    """
    out: list[dict] = []
    i = 0
    while i + 1 < len(data):
        type_tag, flat = data[i], data[i + 1]
        i += 2
        fields = COMPACT_FIELDS.get(type_tag)
        if not fields or not isinstance(flat, list):
            continue
        n = len(fields)
        for j in range(0, len(flat) - n + 1, n):
            out.append(dict(zip(fields, flat[j:j + n])))
    return out


def feed_setup_message(channel: int) -> dict:
    """Trame FEED_SETUP commune aux deux collecteurs (rtquote, futopt) — même
    configuration que le SDK officiel tastytrade, cf. COMPACT_FIELDS."""
    return {"type": "FEED_SETUP", "channel": channel,
            "acceptAggregationPeriod": 10, "acceptDataFormat": "COMPACT",
            "acceptEventFields": COMPACT_FIELDS}


def quote_token() -> tuple[str, str, str]:
    """(jeton dxFeed, URL dxLink, access token tastytrade).

    Fonction de module : le backfill historique s'en sert aussi, sans avoir à
    instancier un client de streaming.
    """
    r = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": _env("TT_REFRESH"),
        "client_id": _env("TASTYTRADE_CLIENT_ID"),
        "client_secret": _env("TASTYTRADE_CLIENT_SECRET"),
    }, timeout=30)
    r.raise_for_status()
    access = r.json()["access_token"]
    q = requests.get(QUOTE_TOKEN_URL,
                     headers={"Authorization": f"Bearer {access}"}, timeout=30)
    q.raise_for_status()
    d = q.json()["data"]
    return d["token"], d["dxlink-url"], access


def _is_future_key(key: str) -> bool:
    """Un sous-jacent qui EST un future (NQ, ES), et non un indice ou une action.

    Ces clés ne doivent jamais servir de symbole dxFeed telles quelles : elles
    coïncident avec des tickers d'actions sans rapport (cf. resolve_symbols).
    """
    u = UNDERLYINGS.get(key)
    return u is not None and u.source == "futopt"


# Symbole streamer du contrat actif, mémorisé une fois résolu. L'API
# tastytrade renvoie des 429 quand plusieurs composants l'interrogent coup sur
# coup (resolve_symbols, futopt._reference_spot, flowtape._build_universe se
# suivent à chaque démarrage) : ce cache supprime l'essentiel de ces appels.
_FUTURE_STREAM_CACHE: dict[str, str] = {}


def resolve_symbols(access: str) -> dict[str, str]:
    """Table clé interne -> symbole dxFeed.

    Indices, ETF et actions portent leur ticker. Les futures exigent le contrat
    actif, dont le symbole streamer (`/ESU26:XCME`, année sur DEUX chiffres) ne
    se devine pas : il est lu depuis l'API.

    ⚠️ Un future NON résolu est OMIS, jamais rabattu sur son code brut. Ce
    repli existait et il était dangereux : « ES » et « NQ » sont aussi des
    tickers d'ACTIONS (Eversource Energy cote autour de 75 $). Sur un 429 de
    l'API — provoqué le 2026-07-30 par des redémarrages rapprochés — le flux
    souscrivait donc à Eversource et enregistrait 74,75 comme prix du future
    ES, dans les bougies servant à la Heatmap. Mieux vaut aucun spot qu'un
    spot d'un autre instrument : c'est le même principe qu'ailleurs dans le
    projet, ne rien produire plutôt que du faux.
    """
    out = {u.key: u.key for u in UNDERLYINGS.values()
           if u.enabled and not _is_future_key(u.key)}
    h = {"Authorization": f"Bearer {access}"}
    for code in {u.future for u in UNDERLYINGS.values() if u.future and u.enabled}:
        cached = _FUTURE_STREAM_CACHE.get(code)
        if cached:
            out[code] = cached
            continue
        try:
            r = requests.get(FUTURES_URL, params={"product-code": code},
                             headers=h, timeout=30)
            r.raise_for_status()
            items = [i for i in r.json()["data"]["items"] if i.get("active-month")]
            if items:
                out[code] = _FUTURE_STREAM_CACHE[code] = items[0]["streamer-symbol"]
            else:
                log.warning("Aucun contrat actif pour %s — %s exclu du flux", code, code)
        except Exception as exc:  # pragma: no cover - dépend du réseau
            log.warning("Symbole future %s non résolu (%s) — exclu du flux "
                        "plutôt que rabattu sur le ticker action homonyme",
                        code, exc)
    return out


@dataclass
class Tick:
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    ts: float = 0.0

    @property
    def price(self) -> float | None:
        """Milieu de fourchette, à défaut le dernier échangé.

        Le mid est préférable au last : il ne saute pas d'un côté à l'autre du
        spread selon le sens de la dernière transaction.
        """
        if self.bid and self.ask and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last or None


@dataclass
class Bar:
    """Bougie en cours de construction pour une minute donnée."""
    minute: int          # epoch de la minute (secondes, tronquées)
    open: float
    high: float
    low: float
    close: float
    ticks: int = 1

    def update(self, px: float) -> None:
        self.high = max(self.high, px)
        self.low = min(self.low, px)
        self.close = px
        self.ticks += 1


@dataclass
class RealtimeQuotes:
    """Client dxLink : maintient le dernier prix connu de chaque sous-jacent.

    Tourne dans un thread démon avec sa propre boucle asyncio. Toute erreur est
    journalisée et suivie d'une reconnexion : le dashboard ne doit jamais
    tomber parce que le flux courtier est indisponible.
    """
    ticks: dict[str, Tick] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _state: str = "off"          # off | connecting | connected | disconnected
    _detail: str = ""
    _started: bool = False
    # symbole dxFeed -> clé interne ("SPX", "ES"…)
    _by_stream: dict[str, str] = field(default_factory=dict)
    # Bougies 1 min construites à la volée. Agréger ici plutôt que d'échantillonner
    # le dernier prix donne des extrêmes exacts : on voit passer chaque tick,
    # donc les mèches ne sont pas perdues — ce qui est précisément ce qui
    # manquait au backtest de niveaux.
    _bar: dict[str, Bar] = field(default_factory=dict)
    _done: list[tuple[str, Bar]] = field(default_factory=list)

    # ------------------------------------------------------------- démarrage
    def start(self) -> None:
        if self._started:
            return
        if not credentials_present():
            log.info("Spot temps réel désactivé (identifiants tastytrade absents)")
            self._state = "off"
            return
        self._started = True
        self._state = "connecting"
        threading.Thread(target=self._run, name="rtquote", daemon=True).start()
        log.info("Spot temps réel : démarrage du flux dxFeed")

    # ---------------------------------------------------------------- lecture
    def price(self, key: str) -> float | None:
        """Dernier prix connu pour une clé interne ("SPX", "ES", "NQ"…)."""
        with self.lock:
            t = self.ticks.get(key)
            return t.price if t else None

    def status(self, market_open: bool = True) -> tuple[str, str]:
        """(état, détail) — état ∈ off | connected | degraded | disconnected.

        "degraded" = connecté mais plus aucun tick depuis STALE_S. Hors séance
        ce silence est normal, l'état reste donc "connected".
        """
        if self._state == "off":
            return "off", ""
        if self._state != "connected":
            return "disconnected", self._detail
        with self.lock:
            newest = max((t.ts for t in self.ticks.values()), default=0.0)
        age = time.time() - newest if newest else None
        if age is None:
            return "degraded", "aucune cotation reçue"
        if market_open and age > STALE_S:
            return "degraded", f"aucun tick depuis {int(age)} s"
        return "connected", ""

    # -------------------------------------------------------------- interne
    def _quote_token(self) -> tuple[str, str, str]:
        return quote_token()

    def _resolve_symbols(self, access: str) -> dict[str, str]:
        return resolve_symbols(access)

    def _run(self) -> None:
        backoff = BACKOFF_START
        while True:
            try:
                asyncio.run(self._session())
                backoff = BACKOFF_START      # session propre : on repart à zéro
            except Exception as exc:
                self._state = "disconnected"
                self._detail = str(exc)[:120]
                log.warning("Flux dxFeed interrompu (%s) — reprise dans %.0f s",
                            exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    async def _session(self) -> None:
        import websockets

        token, url, access = self._quote_token()
        symbols = self._resolve_symbols(access)
        self._by_stream = {v: k for k, v in symbols.items()}

        async with websockets.connect(url, max_size=2 ** 22) as ws:
            async def send(m):
                await ws.send(json.dumps(m))

            await send({"type": "SETUP", "channel": 0, "version": "0.1-gex",
                        "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})
            auth_sent = False
            # FEED_CONFIG n'est PAS un événement unique : le serveur le renvoie
            # chaque fois que la configuration du feed évolue, y compris APRÈS
            # notre propre souscription. Sans ce verrou, chaque renvoi
            # réexpédiait la salve entière — observé le 2026-07-29, trois
            # salves en 100 ms (cf. les trois "Spot temps réel actif" d'affilée
            # dans les logs), rejetées par dxFeed en "BAD_ACTION: Your
            # subscription rate is too high". Régression introduite avec le
            # passage au format COMPACT : la souscription partait auparavant
            # sur CHANNEL_OPENED, qui lui n'arrive qu'une fois.
            subscribed = False

            async for raw in ws:
                m = json.loads(raw)
                typ = m.get("type")

                if typ == "AUTH_STATE":
                    state = m.get("state")
                    # Un premier UNAUTHORIZED précède TOUJOURS l'authentification :
                    # c'est l'invitation à envoyer le jeton, pas un refus.
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
                    await send(feed_setup_message(1))
                elif typ == "FEED_CONFIG" and not subscribed:
                    subscribed = True
                    subs = [{"type": "Quote", "symbol": s} for s in symbols.values()]
                    subs += [{"type": "Trade", "symbol": s} for s in symbols.values()]
                    await send({"type": "FEED_SUBSCRIPTION", "channel": 1, "add": subs})
                    self._state = "connected"
                    self._detail = ""
                    log.info("Spot temps réel actif sur %s", ", ".join(symbols))
                elif typ == "FEED_DATA":
                    self._ingest(decode_compact_feed_data(m.get("data") or []))
                elif typ == "KEEPALIVE":
                    await send({"type": "KEEPALIVE", "channel": 0})
                elif typ == "ERROR":
                    log.warning("dxFeed ERROR : %s", str(m)[:200])

    def _ingest(self, data: list) -> None:
        now = time.time()
        minute = int(now // 60) * 60
        with self.lock:
            for item in data:
                if not isinstance(item, dict):
                    continue
                key = self._by_stream.get(item.get("eventSymbol"))
                if not key:
                    continue
                t = self.ticks.setdefault(key, Tick())
                etype = item.get("eventType")
                if etype == "Quote":
                    bid, ask = item.get("bidPrice"), item.get("askPrice")
                    # NaN pour un indice sans carnet (NDX) : on garde le last
                    if isinstance(bid, (int, float)) and bid == bid:
                        t.bid = float(bid)
                    if isinstance(ask, (int, float)) and ask == ask:
                        t.ask = float(ask)
                elif etype == "Trade":
                    px = item.get("price")
                    if isinstance(px, (int, float)) and px == px:
                        t.last = float(px)
                        # Capture tick de clôture — STRICTEMENT best-effort :
                        # négligeable hors fenêtre (record sort aussitôt si
                        # inactif), et enveloppée pour qu'un pépin de capture ne
                        # puisse JAMAIS perturber le flux temps réel (le dashboard
                        # doit rester en direct). Seul le flux temps réel arme la
                        # fenêtre (cf. scheduler) ; le repli public reste inerte.
                        try:
                            TICKREC.record(key, now, float(px), t.bid, t.ask)
                        except Exception:  # noqa: BLE001 — capture jamais bloquante
                            pass
                t.ts = now
                self._accumulate(key, t.price, minute)

    def _accumulate(self, key: str, px: float | None, minute: int) -> None:
        """Alimente la bougie de la minute courante ; clôture la précédente.

        Appelé sous `self.lock` depuis `_ingest`.
        """
        if px is None:
            return
        cur = self._bar.get(key)
        if cur is None:
            self._bar[key] = Bar(minute, px, px, px, px)
        elif cur.minute == minute:
            cur.update(px)
        else:
            self._done.append((key, cur))
            self._bar[key] = Bar(minute, px, px, px, px)

    def drain_bars(self, flush: bool = False, now: float | None = None
                   ) -> list[tuple[str, Bar]]:
        """Retire et renvoie les bougies achevées.

        Une bougie dont la minute est passée est close même si aucun tick n'est
        arrivé depuis : sans cela, un symbole qui cesse de coter — marché fermé,
        titre peu liquide, dernière minute de la séance — ne livrerait jamais sa
        dernière bougie, puisque la clôture n'interviendrait qu'au tick suivant.

        `flush` force en plus la clôture de la minute en cours, pour l'arrêt.
        """
        current = int((now if now is not None else time.time()) // 60) * 60
        with self.lock:
            out, self._done = self._done, []
            for key in list(self._bar):
                if flush or self._bar[key].minute < current:
                    out.append((key, self._bar.pop(key)))
        return out


QUOTES = RealtimeQuotes()


@dataclass
class PublicDelayedQuotes(RealtimeQuotes):
    """Repli gratuit sans compte : spot NQ/ES délayé (~15-20 min) via le flux
    public dxFeed. Ne tourne QUE si aucun identifiant courtier n'est
    configuré — un vrai compte donne le temps réel via QUOTES, ce repli
    n'a alors plus de raison d'être.

    Réutilise tout `RealtimeQuotes._session()` tel quel (SETUP/AUTH_STATE/
    CHANNEL_REQUEST/FEED_SETUP/FEED_SUBSCRIPTION/KEEPALIVE) : seules les deux
    méthodes qui, dans la version courtier, appelaient l'API tastytrade
    authentifiée (jeton + résolution du contrat actif) sont remplacées par
    un calcul local — c'est la seule vraie différence entre les deux flux.
    """

    def start(self) -> None:
        if self._started:
            return
        if credentials_present():
            return  # un compte réel est configuré : pas de repli à lancer
        self._started = True
        self._state = "connecting"
        threading.Thread(target=self._run, name="rtquote-public", daemon=True).start()
        log.info("Spot NQ/ES délayé (public, sans compte) : démarrage")

    def _quote_token(self) -> tuple[str, str, str]:
        # "demo" : aucun jeton réel requis (AUTH_STATE renvoie AUTHORIZED
        # directement), mais une chaîne non vide au cas où une session
        # demanderait quand même un AUTH.
        return "demo", PUBLIC_DEMO_URL, ""

    def _resolve_symbols(self, access: str) -> dict[str, str]:
        code = front_quarterly_code()
        return {"NQ": f"/NQ{code}:XCME", "ES": f"/ES{code}:XCME"}


PUBLIC_QUOTES = PublicDelayedQuotes()
