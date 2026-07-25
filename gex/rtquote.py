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

import requests

from .config import UNDERLYINGS

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.tastyworks.com/oauth/token"
QUOTE_TOKEN_URL = "https://api.tastyworks.com/api-quote-tokens"
FUTURES_URL = "https://api.tastyworks.com/instruments/futures"

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
        """(jeton dxFeed, URL dxLink, access token tastytrade)."""
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

    def _resolve_symbols(self, access: str) -> dict[str, str]:
        """Table clé interne -> symbole dxFeed.

        Indices et ETF portent leur ticker. Les futures exigent le contrat
        actif, dont le symbole streamer (`/ESU26:XCME`, année sur DEUX
        chiffres) ne se devine pas : il est lu depuis l'API.
        """
        out: dict[str, str] = {}
        for u in UNDERLYINGS.values():
            if u.enabled:
                out[u.key] = u.key
        h = {"Authorization": f"Bearer {access}"}
        for code in {u.future for u in UNDERLYINGS.values() if u.future and u.enabled}:
            try:
                r = requests.get(FUTURES_URL, params={"product-code": code},
                                 headers=h, timeout=30)
                r.raise_for_status()
                items = [i for i in r.json()["data"]["items"] if i.get("active-month")]
                if items:
                    out[code] = items[0]["streamer-symbol"]
                else:
                    log.warning("Aucun contrat actif pour %s", code)
            except Exception as exc:  # pragma: no cover - dépend du réseau
                log.warning("Symbole future %s non résolu : %s", code, exc)
        return out

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
                    subs = [{"type": "Quote", "symbol": s} for s in symbols.values()]
                    subs += [{"type": "Trade", "symbol": s} for s in symbols.values()]
                    await send({"type": "FEED_SUBSCRIPTION", "channel": 1, "add": subs})
                    self._state = "connected"
                    self._detail = ""
                    log.info("Spot temps réel actif sur %s", ", ".join(symbols))
                elif typ == "FEED_DATA":
                    self._ingest(m.get("data") or [])
                elif typ == "KEEPALIVE":
                    await send({"type": "KEEPALIVE", "channel": 0})
                elif typ == "ERROR":
                    log.warning("dxFeed ERROR : %s", str(m)[:200])

    def _ingest(self, data: list) -> None:
        now = time.time()
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
                t.ts = now


QUOTES = RealtimeQuotes()
