"""État du gamma condensé, pour diffusion partageable (bot Discord, page, API).

Transforme les métriques par sous-jacent en un VERDICT qualitatif — la
conclusion, pas la donnée brute. C'est ce qui permet de le diffuser sans
redistribuer les chaînes dxFeed : « Gamma négatif sur SPX » est une analyse
dérivée, pas le feed. Pour SPX/NDX/SPY/QQQ la source sous-jacente est de toute
façon CBOE (publique) ; pour NQ/ES c'est un agrégat de signe, très loin de la
chaîne.

Format calqué sur la demande (4 exemples du 2026-07-30) :
- une ligne par état, regroupant les symboles qui le partagent :
  « {Gamma sign} - {Delta sign} ({gloss dealer}) sur SPX, SPY… » ;
- « Fort Gamma Négatif » quand le gamma net est dans la queue forte de son
  propre historique (même logique de percentile que metrics.regime_read) ;
- ligne VIX si au-dessus du seuil ;
- couleur (vert / orange / rouge) + verdict de trading contrarien.

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
RED_FORT_COUNT = 2        # nb de « Fort Gamma Négatif » qui fait passer au rouge

# Couleurs Discord (barre d'embed) — vert / orange / rouge.
COLORS = {"green": 0x2ECC71, "orange": 0xE67E22, "red": 0xE74C3C}


@dataclass
class Digest:
    header: str
    lines: list[str]                 # lignes d'état groupées
    vix_line: str | None
    verdict: str
    color: str                       # "green" | "orange" | "red"
    signature: tuple = field(default_factory=tuple)   # pour détecter un changement

    def to_text(self) -> str:
        parts = [self.header, ""] + self.lines
        if self.vix_line:
            parts.append(self.vix_line)
        parts += ["", self.verdict]
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

    color, verdict = _verdict(etats, vix, vix_seuil)
    signature = tuple(sorted((s, e["gamma"], e["delta"]) for s, e in etats.items()))
    return Digest(_header(now), lines, vix_line, verdict, color, signature)


def _liste(syms: list[str]) -> str:
    """« SPX, SPY et NDX » — virgules puis « et » avant le dernier."""
    if len(syms) == 1:
        return syms[0]
    return ", ".join(syms[:-1]) + " et " + syms[-1]


def _verdict(etats: dict[str, dict], vix: float | None,
             vix_seuil: float) -> tuple[str, str]:
    """Couleur + phrase de verdict, calquées sur les 4 exemples :

    - rouge  : présence marquée de « Fort Gamma Négatif » → déconseillé ;
    - orange : majorité en Gamma Négatif, OU VIX au-dessus du seuil ;
    - vert   : majorité en Gamma Positif et VIX calme.
    """
    n = len(etats) or 1
    n_fort = sum(1 for e in etats.values() if e["fort"])
    n_neg = sum(1 for e in etats.values() if e["neg"])
    vix_haut = vix is not None and vix > vix_seuil

    if n_fort >= RED_FORT_COUNT:
        return "red", "Trading contrarient déconseillé sur session US."
    if n_neg > n / 2:
        return "orange", "Trading contrarient risqué sur session US."
    if vix_haut:
        return ("orange",
                "Trading contrarient risqué sur session US — forte amplitude attendue.")
    return "green", "Trading contrarient avec peu de risque sur session US."


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
