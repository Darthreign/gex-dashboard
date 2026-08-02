"""État du gamma condensé, pour diffusion partageable (bot Discord, page, API).

Transforme les métriques par sous-jacent en un VERDICT qualitatif — la
conclusion, pas la donnée brute. Les données viennent du flux dxFeed temps
réel (compte courtier), sur TOUS les sous-jacents : c'est l'intérêt du projet.
« Gamma négatif sur SPX » est une analyse que nous produisons, pas le feed —
ce qui permet de la partager sans rediffuser les chaînes dxFeed (usage
personnel du flux à respecter côté diffusion).

Format calqué sur la demande (4 exemples du 2026-07-30) :
- une ligne par état, regroupant les symboles qui le partagent :
  « {Gamma sign} - {Delta sign} ({gloss dealer}) sur SPX, SPY… » ;
- « Fort Gamma Négatif » quand le gamma net est dans la queue forte de son
  propre historique (même logique de percentile que metrics.regime_read) ;
- ligne VIX si au-dessus du seuil ;
- couleur (vert / orange / rouge) + verdict de trading contrarien ;
- ligne de confiance (forte / moyenne / faible) selon la couverture des données.

Le VERDICT ne compte pas les symboles à égalité : il raisonne par FAMILLE
indépendante (S&P : SPX/SPY/ES — Nasdaq : NDX/QQQ/NQ), car ce sont deux vues
d'un même sous-jacent chacune. Chaque famille agrège l'intensité de ses
symboles (poids indice cash > ETF > future) en un score, puis les deux familles
+ le VIX donnent la couleur (cf. _verdict). L'indice cash (SPX/NDX) est l'indice
principal : s'il passe en fort négatif, sa famille l'est.

Décodage du format utilisateur, vérifié cohérent sur les 8 lignes des
exemples : le glose « (Dealers long/short gamma) » suit le signe du DELTA
(Delta+ → « long gamma », Delta− → « short gamma »), pas du gamma. Reproduit
tel quel — c'est le texte public de l'utilisateur.

⚠️ Pas un conseil : décrit la mécanique de couverture des dealers, jamais une
prise de position. La ligne de verdict qualifie le RISQUE du contrarien, pas
une direction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

PARIS = ZoneInfo("Europe/Paris")

# Ordre d'affichage et périmètre (les 6 des exemples).
SYMBOLS = ("SPX", "SPY", "NDX", "QQQ", "ES", "NQ")

# Seuils — configurables, valeurs par défaut calées sur les exemples.
VIX_SEUIL = 17.0          # au-dessus : bascule au moins en orange + amplitude
FORT_PERCENTILE = 0.67    # |net_gex| dans le tiers supérieur de son historique
FORT_MIN_HISTORY = 20     # sans assez d'historique, pas de « Fort » deviné

# Couleurs Discord (barre d'embed) — vert / orange / rouge.
COLORS = {"green": 0x2ECC71, "orange": 0xE67E22, "red": 0xE74C3C}

# Familles indépendantes. Le régime réel tient à DEUX classes d'actifs, pas à
# six marchés : SPX/SPY/ES sont trois vues du même S&P 500 ; NDX/QQQ/NQ du même
# Nasdaq. On les agrège par famille pour ne pas compter trois fois le même
# sous-jacent. Poids = importance du marché d'options : indice cash > ETF >
# future. L'indice cash est aussi l'« indice principal » (le vrai marché des
# dealers) : s'il passe en fort négatif, toute la famille l'est.
FAMILLES = {
    "S&P":    {"principal": "SPX", "poids": {"SPX": 3, "SPY": 2, "ES": 1}},
    "Nasdaq": {"principal": "NDX", "poids": {"NDX": 3, "QQQ": 2, "NQ": 1}},
}
# Repli quand l'indice principal est absent : score de famille sous ce seuil =
# fort négative (échelle d'intensité -2..+1, cf. _intensite).
FAMILLE_FORT_SEUIL = -1.5
_CONF_RANG = {"faible": 0, "moyenne": 1, "forte": 2}


@dataclass
class Digest:
    header: str
    lines: list[str]                 # lignes d'état groupées
    vix_line: str | None
    verdict: str
    color: str                       # "green" | "orange" | "red"
    confidence: str | None = None    # "forte" | "moyenne" | "faible"
    signature: tuple = field(default_factory=tuple)   # pour détecter un changement

    def to_text(self) -> str:
        parts = [self.header, ""] + self.lines
        if self.vix_line:
            parts.append(self.vix_line)
        parts += ["", self.verdict]
        if self.confidence:
            parts.append(f"Confiance : {self.confidence.capitalize()}")
        return "\n".join(parts)

    @property
    def discord_color(self) -> int:
        return COLORS[self.color]


def _header(now: datetime) -> str:
    p = now.astimezone(PARIS)
    off = int((p.utcoffset() or pd.Timedelta(0)).total_seconds() // 3600)
    return f"État du gamma à {p.hour}h{p.minute:02d} GMT{off:+d} (Paris)"


def _is_fort(net_gex: float, hist) -> bool:
    """Gamma négatif ET dans la queue forte de son propre historique.

    Symétrique de la magnitude DEX de metrics.regime_read : on ne qualifie de
    « Fort » que si assez d'historique existe, jamais au jugé.
    """
    if net_gex >= 0 or hist is None:
        return False
    ref = pd.Series(list(hist), dtype="float64").dropna().abs()
    if len(ref) < FORT_MIN_HISTORY:
        return False
    return bool((ref < abs(net_gex)).mean() >= FORT_PERCENTILE)


def classify(net_gex: float, net_dex: float, hist=None) -> dict:
    """État d'un sous-jacent : libellés Gamma/Delta + glose dealer.

    `hist` : série/liste des net_gex passés du même symbole, pour le « Fort ».
    """
    fort = _is_fort(net_gex, hist)
    if fort:
        gamma = "Fort Gamma Négatif"
    elif net_gex < 0:
        gamma = "Gamma Négatif"
    else:
        gamma = "Gamma Positif"
    delta_pos = net_dex >= 0
    delta = "Delta Positif" if delta_pos else "Delta Négatif"
    # glose calquée sur le texte utilisateur : suit le DELTA, pas le gamma
    gloss = "Dealers long gamma" if delta_pos else "Dealers short gamma"
    return {"gamma": gamma, "delta": delta, "gloss": gloss,
            "neg": net_gex < 0, "fort": fort}


def build_digest(rows: list[dict], vix: float | None = None,
                 now: datetime | None = None, vix_seuil: float = VIX_SEUIL) -> Digest:
    """Construit le digest à partir des états par symbole.

    `rows` : liste de dicts {symbol, net_gex, net_dex, hist?}. L'ordre de
    sortie suit `SYMBOLS`, pas l'ordre d'entrée.
    """
    now = now or datetime.now(PARIS)
    by_symbol = {r["symbol"]: r for r in rows if r.get("symbol") in SYMBOLS
                 and r.get("net_gex") is not None}

    # regroupe les symboles partageant exactement le même état, dans l'ordre
    # d'affichage ; une clé = (gamma, delta, gloss)
    groupes: dict[tuple, list[str]] = {}
    etats: dict[str, dict] = {}
    for sym in SYMBOLS:
        r = by_symbol.get(sym)
        if r is None:
            continue
        c = classify(float(r["net_gex"]), float(r.get("net_dex") or 0.0),
                     r.get("hist"))
        etats[sym] = c
        groupes.setdefault((c["gamma"], c["delta"], c["gloss"]), []).append(sym)

    lines = []
    for (gamma, delta, gloss), syms in groupes.items():
        lines.append(f"{gamma} - {delta} ({gloss}) sur {_liste(syms)}")

    vix_line = (f"VIX supérieur à {int(vix_seuil)} ! (actuellement {vix:.1f})"
                if vix is not None and vix > vix_seuil else None)

    color, verdict, familles = _verdict(etats, vix, vix_seuil)
    confidence = _confiance_globale(familles)
    # Signature = régime réel (statut par famille + couleur) : on ne re-poste
    # que sur un vrai changement de verdict, pas au moindre frémissement d'un
    # petit frère (SPY/ES/QQQ/NQ) qui ne fait pas basculer sa famille.
    signature = tuple(sorted((nom, f["statut"]) for nom, f in familles.items()))
    signature += (("couleur", color),)
    return Digest(_header(now), lines, vix_line, verdict, color, confidence, signature)


def _liste(syms: list[str]) -> str:
    """« SPX, SPY et NDX » — virgules puis « et » avant le dernier."""
    if len(syms) == 1:
        return syms[0]
    return ", ".join(syms[:-1]) + " et " + syms[-1]


def _intensite(c: dict) -> int:
    """Intensité signée d'un symbole, depuis son classify().

    Fort négatif -2 · Négatif -1 · Positif +1. Volontairement ASYMÉTRIQUE :
    pas de « fort positif » (+2). En intraday, un fort gamma négatif change le
    comportement du marché (accélérations, cassures) ; un gamma positif plus
    élevé ne fait que renforcer une stabilité déjà connue — la nuance +1/+2
    n'est pas exploitable, la nuance -1/-2 l'est.
    """
    if c["fort"]:
        return -2
    return -1 if c["neg"] else 1


def _famille(etats: dict[str, dict], poids: dict[str, int],
             principal: str) -> dict | None:
    """État d'une famille : score pondéré normalisé, statut, confiance.

    - `score` : moyenne pondérée des intensités PRÉSENTES, normalisée par les
      poids présents → échelle stable [-2, +1] même si une source manque ;
    - `statut` : 'fort_neg' | 'neg' | 'pos'. `fort_neg` dès que l'indice
      principal (SPX/NDX) est en fort négatif — règle explicite « le cash index
      commande » — ou, à défaut d'indice principal, si le score plonge sous le
      seuil ;
    - `confiance` : 'forte' (indice principal + les 3 symboles, signes
      concordants), 'faible' (indice principal absent, ou signes qui se
      contredisent), 'moyenne' sinon.
    """
    presents = {s: etats[s] for s in poids if s in etats}
    if not presents:
        return None
    w = sum(poids[s] for s in presents)
    score = sum(poids[s] * _intensite(presents[s]) for s in presents) / w

    principal_present = principal in presents
    principal_fort = principal_present and presents[principal]["fort"]
    if principal_fort or (not principal_present and score <= FAMILLE_FORT_SEUIL):
        statut = "fort_neg"
    elif score < 0:
        statut = "neg"
    else:
        statut = "pos"

    signes = {1 if _intensite(c) > 0 else -1 for c in presents.values()}
    contradiction = len(signes) > 1
    complet = w == sum(poids.values())
    if not principal_present or contradiction:
        confiance = "faible"
    elif complet:
        confiance = "forte"
    else:
        confiance = "moyenne"
    return {"score": score, "statut": statut, "confiance": confiance}


def _confiance_globale(familles: dict[str, dict]) -> str | None:
    """La plus faible des confiances de famille (le maillon faible commande)."""
    if not familles:
        return None
    return min((f["confiance"] for f in familles.values()),
               key=lambda c: _CONF_RANG[c])


def _verdict(etats: dict[str, dict], vix: float | None,
             vix_seuil: float) -> tuple[str, str, dict[str, dict]]:
    """Couleur + phrase de verdict, décidés par les DEUX familles (pas les 6
    symboles) plus le VIX :

    - rouge  : les 2 familles négatives, OU une famille en fort négatif ;
    - orange : 1 famille négative, OU VIX au-dessus du seuil ;
    - vert   : sinon.

    Retourne aussi le détail par famille (pour la confiance et la signature).
    """
    familles = {}
    for nom, spec in FAMILLES.items():
        r = _famille(etats, spec["poids"], spec["principal"])
        if r is not None:
            familles[nom] = r

    n_neg = sum(1 for f in familles.values() if f["statut"] in ("neg", "fort_neg"))
    fort = any(f["statut"] == "fort_neg" for f in familles.values())
    vix_haut = vix is not None and vix > vix_seuil

    if fort or n_neg >= 2:
        return "red", "Trading contrarient déconseillé sur session US.", familles
    if n_neg == 1:
        return "orange", "Trading contrarient risqué sur session US.", familles
    if vix_haut:
        return ("orange",
                "Trading contrarient risqué sur session US — forte amplitude attendue.",
                familles)
    return "green", "Trading contrarient avec peu de risque sur session US.", familles


# --------------------------------------------------------------------------
# Lecture de l'état courant (branche sur le moteur ; utilisé par l'API et le
# job planifié). Isolé de la logique pure ci-dessus pour rester testable.
# --------------------------------------------------------------------------

def _preferred_key(symbol: str) -> str:
    """Native _RT si elle a un état frais, sinon le symbole de base — même
    règle que l'interface (app.chain_state), répliquée ici pour ne pas importer
    tout le dashboard."""
    from .rtquote import credentials_present
    from .scheduler import STATE
    if symbol in ("SPX", "NDX", "SPY", "QQQ") and credentials_present():
        rt = STATE.get(f"{symbol}_RT")
        with STATE.lock:
            if rt.summary is not None:
                return f"{symbol}_RT"
    return symbol


def _current_vix() -> float | None:
    from .rtquote import QUOTES, credentials_present
    from . import store
    live = QUOTES.price("VIX") if credentials_present() else None
    if live:
        return float(live)
    hist = store.load_index_spot("vix")
    if not hist.empty:
        return float(hist.sort_values("timestamp")["vix"].iloc[-1])
    return None


def current_digest(now: datetime | None = None) -> Digest:
    """Digest de l'état courant, lu depuis STATE + historique + VIX."""
    from . import store
    from .scheduler import STATE
    rows = []
    for sym in SYMBOLS:
        key = _preferred_key(sym)
        st = STATE.get(key)
        with STATE.lock:
            s = st.summary
        if s is None:
            continue
        hist = store.load_history(key)
        rows.append({
            "symbol": sym,
            "net_gex": s.net_gex,
            "net_dex": s.net_dex,
            "hist": hist["net_gex"] if not hist.empty and "net_gex" in hist else None,
        })
    return build_digest(rows, vix=_current_vix(), now=now)
