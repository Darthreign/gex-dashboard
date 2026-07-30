"""Order flow SIGNÉ sur options — le vrai, pas le proxy.

Ce que ce module apporte face à `metrics.flow_delta` : celui-ci mesure
Δvolume × δ entre deux pulls, sans savoir qui a agressé le carnet. Sa
docstring le dit depuis toujours — « le sens taker n'est pas observable dans
ce feed ». C'était vrai de la source CBOE ; ça ne l'est plus du flux
courtier.

L'événement dxFeed `TimeAndSale` livre chaque transaction individuellement,
avec le bid/ask au moment du print ET un champ `aggressorSide` renseigné par
la source. Mesuré le 2026-07-29 : 937 prints SPX sur 937 avec un côté
explicite (aucun indéterminé), 96,6 % sur ES. Aucune heuristique de
classification (Lee-Ready et consorts) n'est donc nécessaire — on lit le
côté, on ne le devine pas.

Deux pièges que ce module traite explicitement plutôt que de les ignorer :

1. **Les jambes de spread** (`spreadLeg`). 23 % des prints SPX sont des
   morceaux de combos (verticales, condors…). Un « BUY » sur une jambe n'est
   PAS un pari haussier : l'autre jambe part souvent dans l'autre sens. Les
   compter comme du flux directionnel fausserait le signal d'un quart. Ils
   sont donc comptés à part, jamais mélangés au flux net.
2. **Les tailles très inégales** selon le marché. 2,1 contrats en moyenne sur
   SPX contre 10,9 sur ES : compter les prints reviendrait à donner le même
   poids à un lot de 1 et à un bloc de 500. Tout est pondéré par la taille.

Débits mesurés (fenêtre ±1,5 %, 2 échéances) : QQQ 39/s, SPY 32/s, SPX 31/s,
ES 0,6/s, NQ 0,1/s — soit ~2,4 M de prints par séance. D'où l'agrégation en
barres d'une minute EN MÉMOIRE : seules les barres touchent le disque, jamais
les prints bruts.

⚠️ Licence : données courtier, usage personnel, jamais redistribuables. Les
barres sont écrites avec `source="dxfeed"`, ce qui les exclut de l'export
(cf. gex/export.py). Sans identifiants, ce module ne démarre pas.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import requests

from .config import CONTRACT_MULTIPLIER
from .rtquote import (
    BACKOFF_MAX,
    BACKOFF_START,
    QUOTES,
    credentials_present,
    quote_token,
)

log = logging.getLogger(__name__)

# Sous-jacents suivis, et comment construire leur univers de souscription.
# SPY/QQQ sont inclus alors que leurs chaînes GEX restent sur CBOE : ici on ne
# lit pas une structure d'open interest mais un flux de transactions, et ce
# sont parmi les options les plus traitées au monde (39 et 32 prints/s).
TRACKED: dict[str, str] = {
    "SPX": "index", "NDX": "index", "SPY": "index", "QQQ": "index",
    "ES": "future", "NQ": "future",
}

# Fenêtre de souscription. Volontairement serrée : le flux qui compte se
# concentre près de la monnaie et sur les échéances proches, et chaque strike
# supplémentaire coûte du débit sans rien apprendre (cf. les mesures en tête
# de module).
STRIKE_WINDOW = 0.015
MAX_EXPIRIES = 2

# L'univers est reconstruit périodiquement : les échéances roulent et, à
# défaut de dérive marquée, le centrage se rafraîchit quand même. On reconnecte
# plutôt que d'empiler les souscriptions sur un canal déjà ouvert — un ajout
# tardif n'est pas fiable sur dxLink (cf. futopt).
UNIVERSE_REFRESH_S = 1800

# Recentrage anticipé : dès que le spot d'un sous-jacent s'éloigne de son
# centre de plus de cette FRACTION de la demi-fenêtre, l'univers est
# reconstruit sans attendre les 30 min. Sinon, un mouvement rapide fait sortir
# le prix de la bande souscrite : on écouterait alors des strikes que le marché
# a quittés et on raterait ceux vers lesquels il va — précisément les jours qui
# bougent, quand le flux compte le plus. Mesuré le 2026-07-29 : NQ sortait de
# la fenêtre 3 minutes sur 550. À 0,5, on recentre à mi-chemin du bord, ce qui
# laisse toujours une demi-fenêtre de marge du côté où le prix se dirige.
RECENTER_FRACTION = 0.5
# La dérive se vérifie au plus toutes les N secondes : le flux FEED_DATA passe
# des milliers de fois par seconde, inutile de recalculer à chaque print.
DRIFT_CHECK_S = 5.0

INDEX_CHAIN_URL = "https://api.tastyworks.com/option-chains/{symbol}/nested"

# Transactions individuelles gardées PAR sous-jacent pour l'affichage du Tape,
# en plus de l'agrégation en barres. Un tampon par symbole (et non global)
# pour qu'un marché très actif — SPX à ~30 prints/s — n'évince pas les
# transactions d'un marché plus lent comme NQ.
#
# Dimensionné pour que le FILTRE PAR BLOCS reste utile : à 500 prints, SPX ne
# garde que ~16 s d'historique, et un bloc de 50+ contrats (rare sur du 0DTE
# où la taille moyenne est ~2) n'y tombe presque jamais — le filtre ≥50
# affichait alors un tableau vide en permanence. 5000 couvrent ~2-3 min sur
# SPX, bien plus sur les marchés lents. Mémoire volatile, jamais persistée, de
# petits dicts : négligeable même à 6 sous-jacents.
PRINT_BUFFER = 5000


@dataclass
class FlowBar:
    """Agrégat d'une minute pour un sous-jacent.

    Tout est signé du point de vue de l'AGRESSEUR : +1 quand l'acheteur
    traverse le spread, -1 quand c'est le vendeur. Un `net_contracts` positif
    signifie donc que les preneurs de liquidité ont acheté plus qu'ils n'ont
    vendu sur la minute.
    """
    minute: int
    # TOUS les agrégats signés ci-dessous sont en POINT DE VUE DEALER, la même
    # convention que le bandeau et les graphes par strike (cf. ingest_print).
    # Un « + » y veut dire la même chose que sur le GEX/DEX statique.
    net_contracts: float = 0.0      # convention GEX : call acheté +, put acheté −
    net_premium: float = 0.0        # $ encaissés par le dealer (opposé du preneur)
    net_calls: float = 0.0          # contrats, calls (achat preneur -> +)
    net_puts: float = 0.0           # contrats, puts (achat preneur -> −)
    # Delta net des DEALERS, en dollars de sous-jacent — même sens que la tuile
    # « DEX net ». Positif = dealers longs delta (issu d'achats de puts par les
    # preneurs, ou de ventes de calls). C'est l'opposé du delta pris par le
    # preneur, cohérent avec dex = −δ·oi de metrics.enrich.
    net_delta: float = 0.0
    delta_prints: int = 0           # prints ayant un delta connu
    no_delta_prints: int = 0        # delta pas encore reçu : exclu de net_delta
    # Gamma net à l'échelle du GEX ($ par 1 % de move), signe-type GEX (call +,
    # put −). Positif = flux qui ajoute du gamma côté dealers (stabilisant),
    # négatif = qui en retire — même lecture que « GEX net ».
    net_gamma: float = 0.0
    net_gamma_calls: float = 0.0
    net_gamma_puts: float = 0.0
    buy_contracts: float = 0.0      # bruts, pour retrouver le volume total
    sell_contracts: float = 0.0
    prints: int = 0
    spread_contracts: float = 0.0   # jambes de combos, isolées volontairement
    spread_prints: int = 0
    undefined_prints: int = 0       # agresseur non renseigné : ni compté, ni caché

    def as_row(self, symbol: str, timestamp) -> dict:
        return {
            "timestamp": timestamp, "symbol": symbol,
            "net_contracts": self.net_contracts, "net_premium": self.net_premium,
            "net_calls": self.net_calls, "net_puts": self.net_puts,
            "net_delta": self.net_delta,
            "net_gamma": self.net_gamma,
            "net_gamma_calls": self.net_gamma_calls,
            "net_gamma_puts": self.net_gamma_puts,
            "delta_prints": float(self.delta_prints),
            "no_delta_prints": float(self.no_delta_prints),
            "buy_contracts": self.buy_contracts, "sell_contracts": self.sell_contracts,
            "prints": float(self.prints),
            "spread_contracts": self.spread_contracts,
            "spread_prints": float(self.spread_prints),
            "undefined_prints": float(self.undefined_prints),
            "source": "dxfeed",
        }


def option_type_of(streamer_symbol: str) -> str | None:
    """C ou P, lu directement dans le symbole streamer.

    Évite d'avoir à porter une table de correspondance en parallèle du flux :
    les deux conventions rencontrées portent le type juste avant le strike —
    `.SPXW260729C7400` (OPRA) et `./EWN26C7500:XCME` (CME). On lit donc le
    dernier C/P qui précède une suite de chiffres, plutôt que le premier venu
    (la racine peut contenir un C ou un P : QQQ n'en a pas, mais `.SPXWC…`
    n'aurait rien d'impossible sur un autre produit).
    """
    core = streamer_symbol.split(":")[0]
    for i in range(len(core) - 1, -1, -1):
        if core[i] in ("C", "P") and i + 1 < len(core) and core[i + 1].isdigit():
            return core[i]
    return None


def strike_of(streamer_symbol: str) -> float | None:
    """Strike lu dans le symbole streamer, pour l'affichage du Tape.

    Même logique que `option_type_of` : on repère le C/P qui précède les
    chiffres du strike, et on lit le nombre qui suit. Couvre les deux
    conventions — `.SPXW260729C7400` (OPRA) -> 7400 et `./EWN26C7500:XCME`
    (CME) -> 7500. Renvoie None si le format ne s'y prête pas plutôt que de
    risquer un strike faux.
    """
    core = streamer_symbol.split(":")[0]
    for i in range(len(core) - 1, -1, -1):
        if core[i] in ("C", "P") and i + 1 < len(core) and core[i + 1].isdigit():
            reste = core[i + 1:]
            try:
                return float(reste)
            except ValueError:
                return None
    return None


def multiplier_of(symbol: str) -> float:
    """Multiplicateur $/point du contrat d'option.

    100 pour les indices et ETF ; le notionnel du future pour NQ/ES, où un
    point ne vaut pas la même chose (20 $ sur NQ, 50 sur ES). Sans cette
    distinction, les primes ne seraient pas comparables d'un marché à l'autre.
    """
    from .futopt import _multiplier_cache
    return float(_multiplier_cache.get(symbol, CONTRACT_MULTIPLIER))


def build_index_universe(symbol: str, spot: float, access_token: str,
                         window: float = STRIKE_WINDOW,
                         max_expiries: int = MAX_EXPIRIES) -> list[str]:
    """Symboles streamer à suivre pour un indice ou un ETF (SPX, NDX, SPY, QQQ)."""
    r = requests.get(INDEX_CHAIN_URL.format(symbol=symbol),
                     headers={"Authorization": f"Bearer {access_token}"}, timeout=90)
    r.raise_for_status()
    lo, hi = spot * (1 - window), spot * (1 + window)
    out: list[str] = []
    for item in r.json()["data"]["items"]:
        for exp in item.get("expirations", [])[:max_expiries]:
            for st in exp.get("strikes", []):
                if not lo <= float(st["strike-price"]) <= hi:
                    continue
                for key in ("call-streamer-symbol", "put-streamer-symbol"):
                    if st.get(key):
                        out.append(st[key])
    return out


def build_future_universe(code: str, spot: float, access_token: str,
                          window: float = STRIKE_WINDOW,
                          max_days: int = 5) -> list[str]:
    """Symboles streamer à suivre pour une option sur future (NQ, ES)."""
    from .futopt import fetch_chain_instruments, filter_chain
    chain = fetch_chain_instruments(code, access_token)
    chain = filter_chain(chain, spot, window, max_days)
    return chain["streamer_symbol"].tolist() if not chain.empty else []


@dataclass
class FlowTape:
    """Collecteur de prints signés, agrégés en barres d'une minute.

    Une seule connexion dxLink pour tous les sous-jacents suivis : ~500
    souscriptions au total, très en dessous du seuil de rejet, et cela évite
    de multiplier les sessions pour rien.
    """
    bars: dict[str, FlowBar] = field(default_factory=dict)      # symbole -> barre courante
    done: list[tuple[str, FlowBar]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _by_stream: dict[str, str] = field(default_factory=dict)    # streamer -> sous-jacent
    # delta courant par contrat, alimenté par l'événement Greeks du MÊME flux.
    # Pris chez dxFeed plutôt que recalculé : ici on veut le delta au moment
    # exact du print, pas celui d'un snapshot de chaîne vieux de 15 minutes.
    # (Le GEX, lui, reste sur du Black-Scholes maison — cf. futopt : c'est un
    # historique cohérent qu'on y construit, pas une mesure instantanée.)
    _delta: dict[str, float] = field(default_factory=dict)
    _gamma: dict[str, float] = field(default_factory=dict)
    # spot par sous-jacent, figé à la construction de l'univers : il sert à
    # convertir un delta en dollars de sous-jacent. Une dérive intra-session
    # de quelques dixièmes de pour cent ne change pas la lecture d'un flux
    # cumulé, et le rafraîchir à chaque print coûterait un verrou par message.
    _spot: dict[str, float] = field(default_factory=dict)
    # Transactions récentes par sous-jacent (tampon circulaire) pour le Tape.
    # Distinct de l'agrégation en barres : ici on garde le détail par print,
    # là on cumule. Volatil, jamais persisté.
    _prints: dict[str, deque] = field(default_factory=dict)
    # spot autour duquel la fenêtre de souscription courante a été centrée. On
    # y compare le spot LIVE pour décider d'un recentrage : `_spot` peut être
    # figé au spot de construction, `_center` l'est délibérément — c'est le
    # point de référence de la fenêtre, pas une estimation du prix courant.
    _center: dict[str, float] = field(default_factory=dict)
    _started: bool = False
    _state: str = "off"

    def _drifted(self) -> str | None:
        """Nom du premier sous-jacent dont le spot live a trop dérivé de son
        centre, ou None. Compare au spot temps réel de `rtquote.QUOTES`, déjà
        abonné à tous les sous-jacents suivis — aucun appel réseau ajouté.
        """
        seuil = STRIKE_WINDOW * RECENTER_FRACTION
        with self.lock:
            centres = dict(self._center)
        for symbol, centre in centres.items():
            live = QUOTES.price(symbol)
            if live and centre and abs(live - centre) / centre > seuil:
                return symbol
        return None

    def ingest_print(self, item: dict, now: float) -> None:
        """Range un print dans la barre de sa minute.

        Public (et non préfixé) parce que c'est LE point testable du module :
        toute la logique de signe, d'exclusion des combos et de pondération
        par la taille tient ici, sans réseau.
        """
        stream = item.get("eventSymbol")
        symbol = self._by_stream.get(stream)
        if symbol is None:
            return
        size = item.get("size")
        price = item.get("price")
        if not isinstance(size, (int, float)) or size != size or size <= 0:
            return

        minute = int(now // 60) * 60
        with self.lock:
            bar = self.bars.get(symbol)
            if bar is None:
                self.bars[symbol] = bar = FlowBar(minute)
            elif bar.minute != minute:
                self.done.append((symbol, bar))
                self.bars[symbol] = bar = FlowBar(minute)

            bar.prints += 1
            size = float(size)

            # Tape : on garde la transaction TELLE QUELLE avant tout filtrage
            # d'agrégation — combos et côté indéterminé compris, marqués pour
            # que le lecteur les voie plutôt qu'ils disparaissent en silence.
            self._record_print(symbol, stream, item, price, size, now)

            # Jambe de combo : comptée à part. La classer en directionnel
            # fausserait le flux net (cf. docstring du module).
            if item.get("spreadLeg"):
                bar.spread_contracts += size
                bar.spread_prints += 1
                return

            side = item.get("aggressorSide")
            if side == "BUY":
                sign = 1.0
            elif side == "SELL":
                sign = -1.0
            else:
                # Ni compté dans le net, ni passé sous silence : un flux dont
                # une part n'est pas classable doit pouvoir être audité.
                bar.undefined_prints += 1
                return

            # Tout ce qui suit est écrit du POINT DE VUE DEALER, comme le reste
            # du tableau (bandeau, GEX/DEX par strike, cadre de régime). C'est
            # ce qui garde une seule convention partout : un « + » veut dire la
            # même chose sur une courbe de flux et sur un graphe statique. Le
            # dealer prend le côté opposé du preneur, d'où les signes ci-dessous
            # — dérivés de sorte que la CONTRIBUTION PAR TYPE d'un print colle à
            # celle du contrat correspondant dans metrics.enrich (gex/dex).
            typ = option_type_of(stream or "")
            # signe-type du GEX : call +1, put −1 (dealers longs calls / courts
            # puts). Sans lui, calls et puts seraient indiscernables sur le
            # gamma, dont la valeur est toujours positive — exactement la raison
            # d'être du même flip dans metrics.enrich.
            type_sign = 1.0 if typ == "C" else -1.0 if typ == "P" else 0.0

            # Contrats signés en convention GEX : un call acheté monte, un put
            # acheté descend — comme les barres du graphe par strike.
            dealer_contracts = sign * type_sign * size
            bar.net_contracts += dealer_contracts
            if typ == "C":
                bar.net_calls += dealer_contracts
            elif typ == "P":
                bar.net_puts += dealer_contracts
            # bruts, hors convention : servent à retrouver le volume total
            if sign > 0:
                bar.buy_contracts += size
            else:
                bar.sell_contracts += size

            mult = multiplier_of(symbol)
            # Prime encaissée par le dealer : + quand le preneur ACHÈTE (le
            # dealer vend et encaisse), − quand le preneur vend. En valeur
            # c'est la prime que paie le preneur, vue de l'autre côté.
            if isinstance(price, (int, float)) and price == price:
                bar.net_premium += sign * size * float(price) * mult

            # DELTA dealer = opposé du delta pris par le preneur. On vérifie
            # que le signe colle à la tuile DEX (metrics.enrich : dex = −δ·oi) :
            #   preneur achète un CALL (δ>0)  -> dealer court delta -> négatif,
            #   comme la contribution −δ_call d'un call à l'open interest ;
            #   preneur achète un PUT (δ<0)   -> dealer long delta  -> positif.
            # Positif = dealers longs delta, exactement comme « DEX net » > 0.
            delta = self._delta.get(stream)
            spot = self._spot.get(symbol)
            if delta is None or not spot:
                bar.no_delta_prints += 1
            else:
                bar.net_delta += -sign * size * delta * mult * spot
                bar.delta_prints += 1

            # Gamma signé, à l'échelle du GEX ($ par 1 % de move). Même signe-
            # type que metrics.enrich (call +, put −) : un call acheté ajoute du
            # gamma positif, un put acheté du négatif — exactement comme les
            # barres du graphe GEX par strike. Le gamma d'une option étant
            # toujours positif, c'est ce flip qui rend calls et puts lisibles au
            # lieu de les empiler tous du même côté.
            gamma = self._gamma.get(stream)
            if gamma is not None and spot and type_sign:
                g = sign * type_sign * size * gamma * mult * spot ** 2 * 0.01
                bar.net_gamma += g
                if typ == "C":
                    bar.net_gamma_calls += g
                elif typ == "P":
                    bar.net_gamma_puts += g

    def _record_print(self, symbol: str, stream: str, item: dict,
                      price, size: float, now: float) -> None:
        """Ajoute une transaction au tampon du Tape (appelé sous `self.lock`).

        `notional` = prix × taille × multiplicateur : le vrai poids d'un print
        en dollars, qui permet de classer les blocs autrement que par le seul
        nombre de contrats (10 lots à 50 $ pèsent plus que 40 lots à 2 $).
        """
        side = item.get("aggressorSide")
        px = float(price) if isinstance(price, (int, float)) and price == price else None
        notional = (px * size * multiplier_of(symbol)) if px is not None else None
        buf = self._prints.get(symbol)
        if buf is None:
            buf = self._prints[symbol] = deque(maxlen=PRINT_BUFFER)
        buf.append({
            "t": now,
            "symbol": symbol,
            "strike": strike_of(stream or ""),
            "type": option_type_of(stream or ""),
            "price": px,
            "size": size,
            "side": side if side in ("BUY", "SELL") else "?",
            "notional": notional,
            "combo": bool(item.get("spreadLeg")),
        })

    def recent_prints(self, symbol: str, min_size: float = 0.0,
                      include_combos: bool = True, limit: int = 60) -> list[dict]:
        """Dernières transactions d'un sous-jacent, les plus récentes d'abord.

        `min_size` isole les blocs (le firehose est illisible sans filtre) ;
        `include_combos=False` masque les jambes de combos, qui ne sont pas
        directionnelles. Renvoie une COPIE — l'appelant (un callback Dash)
        n'accède jamais au tampon vivant.
        """
        with self.lock:
            buf = list(self._prints.get(symbol, ()))
        out = []
        for rec in reversed(buf):
            if rec["size"] < min_size:
                continue
            if rec["combo"] and not include_combos:
                continue
            out.append(dict(rec))
            if len(out) >= limit:
                break
        return out

    def ingest_greeks(self, item: dict) -> None:
        """Mémorise delta ET gamma courants d'un contrat (événement Greeks).

        Les deux arrivent dans le MÊME message : ne prendre que le delta
        revenait à jeter le gamma pour le recalculer ailleurs, ou à laisser le
        gamma échangé sur le proxy CBOE délayé faute de mieux.
        """
        stream = item.get("eventSymbol")
        if stream not in self._by_stream:
            return
        for champ, cible in (("delta", self._delta), ("gamma", self._gamma)):
            v = item.get(champ)
            if isinstance(v, (int, float)) and v == v:
                cible[stream] = float(v)

    def drain_bars(self, flush: bool = False) -> list[tuple[str, FlowBar]]:
        """Retire et renvoie les barres achevées.

        `flush` force la sortie des barres en cours — réservé à l'arrêt, où
        perdre la minute courante serait dommage.
        """
        with self.lock:
            out = self.done
            self.done = []
            if flush:
                out += list(self.bars.items())
                self.bars = {}
        return out

    # ------------------------------------------------------------------
    # Flux
    # ------------------------------------------------------------------

    def status(self) -> tuple[str, int]:
        """(état, nombre de contrats suivis) — pour l'affichage."""
        return self._state, len(self._by_stream)

    def start(self) -> None:
        if self._started:
            return
        if not credentials_present():
            log.info("Order flow options désactivé (identifiants absents)")
            self._state = "off"
            return
        self._started = True
        self._state = "connecting"
        threading.Thread(target=self._run, name="flowtape", daemon=True).start()

    def _build_universe(self) -> dict[str, str]:
        """streamer -> sous-jacent, pour tous les marchés suivis.

        Un échec sur un sous-jacent ne prive pas les autres de flux : on
        journalise et on continue, plutôt que d'abandonner la session entière
        parce qu'un référentiel a répondu de travers.
        """
        from . import futopt, idxopt

        _, _, access = quote_token()
        out: dict[str, str] = {}
        spots: dict[str, float] = {}
        for symbol, kind in TRACKED.items():
            try:
                if kind == "future":
                    spot = futopt._reference_spot(symbol, access)
                    syms = (build_future_universe(symbol, spot, access)
                            if spot else [])
                else:
                    spot = idxopt.reference_spot(symbol)
                    syms = (build_index_universe(symbol, spot, access)
                            if spot else [])
                if spot:
                    spots[symbol] = float(spot)
                for s in syms:
                    out[s] = symbol
            except Exception:  # noqa: BLE001 — un marché muet n'en condamne pas six
                log.exception("%s : univers de flux indisponible", symbol)
        with self.lock:
            self._spot.update(spots)
            # le centre est REMPLACÉ (pas mis à jour) : c'est le référentiel de
            # la fenêtre qu'on vient de construire, pas un cumul historique
            self._center = dict(spots)
        return out

    def _run(self) -> None:
        backoff = BACKOFF_START
        while True:
            try:
                asyncio.run(self._session())
                backoff = BACKOFF_START
            except Exception as exc:  # noqa: BLE001 — le flux doit survivre à tout
                self._state = "disconnected"
                log.warning("Order flow options interrompu (%s) — reprise dans %.0f s",
                            exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    async def _session(self) -> None:
        """Une session : univers, souscription unique, puis écoute.

        La session se termine d'elle-même au bout de `UNIVERSE_REFRESH_S` pour
        que `_run` la relance avec un univers reconstruit — le spot dérive et
        les échéances roulent. On reconnecte plutôt que d'ajouter des
        souscriptions à un canal ouvert : sur dxLink, un ajout tardif n'est pas
        fiable (cf. futopt._collect_one).
        """
        import websockets

        token, url, _ = quote_token()
        universe = self._build_universe()
        if not universe:
            self._state = "degraded"
            raise RuntimeError("aucun contrat à suivre")
        with self.lock:
            self._by_stream = universe

        async with websockets.connect(url, max_size=2 ** 24, ping_interval=None) as ws:
            async def send(m):
                await ws.send(json.dumps(m))

            await send({"type": "SETUP", "channel": 0, "version": "0.1-flow",
                        "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})
            auth_sent = False
            # cf. rtquote._session : FEED_CONFIG est renvoyé à chaque évolution
            # de la configuration, réexpédier la salve fait rejeter la session
            subscribed = False
            deadline = time.monotonic() + UNIVERSE_REFRESH_S
            next_drift_check = time.monotonic() + DRIFT_CHECK_S

            async for raw in ws:
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
                    # aggregationPeriod 0 : on veut CHAQUE transaction, pas un
                    # échantillon — c'est la matière même du signal
                    await send({"type": "FEED_SETUP", "channel": 1,
                                "acceptAggregationPeriod": 0.0,
                                "acceptDataFormat": "FULL"})
                elif typ == "FEED_CONFIG" and not subscribed:
                    subscribed = True
                    # Greeks en plus des prints : le delta doit être celui du
                    # MOMENT de la transaction, pas celui d'un snapshot de
                    # chaîne vieux de plusieurs minutes.
                    await send({"type": "FEED_SUBSCRIPTION", "channel": 1,
                                "add": [{"type": e, "symbol": s}
                                        for s in universe
                                        for e in ("TimeAndSale", "Greeks")]})
                    self._state = "connected"
                    log.info("Order flow options actif — %d contrats sur %s",
                             len(universe), ", ".join(TRACKED))
                elif typ == "KEEPALIVE":
                    await send({"type": "KEEPALIVE", "channel": 0})
                elif typ == "ERROR":
                    log.warning("dxFeed ERROR (order flow) : %s", str(m)[:200])
                elif typ == "FEED_DATA":
                    now = time.time()
                    for item in m.get("data") or []:
                        if not isinstance(item, dict):
                            continue
                        etype = item.get("eventType")
                        if etype == "Greeks":
                            self.ingest_greeks(item)
                        elif etype == "TimeAndSale":
                            self.ingest_print(item, now)

                now_mono = time.monotonic()
                if now_mono > deadline:
                    log.info("Order flow : renouvellement périodique de l'univers")
                    return
                # Recentrage anticipé : ne se vérifie qu'après souscription (le
                # centre n'a de sens qu'une fois la fenêtre en place) et au plus
                # une fois toutes les DRIFT_CHECK_S.
                if subscribed and now_mono > next_drift_check:
                    next_drift_check = now_mono + DRIFT_CHECK_S
                    derive = self._drifted()
                    if derive is not None:
                        log.info("Order flow : %s a dérivé hors fenêtre — "
                                 "recentrage de l'univers", derive)
                        return


TAPE = FlowTape()
