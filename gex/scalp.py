"""Page « Scalp » : logique pure (aucune E/S) de l'échelle de niveaux et de l'état
de séance. L'affichage vit dans app.py, les données dans metrics / rtquote.

Pensée pour un scalping CONTRARIEN sur rejet : on joue la correction des excès
autour des niveaux (freins des teneurs de marché). Ce qui compte à l'écran, c'est
donc où est le spot PAR RAPPORT à chaque niveau, et si un niveau est au contact.

⚠️ Affichage de lecture, pas un signal de trading.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from . import i18n

# Distance (en points du sous-jacent) sous laquelle un niveau est « au contact ».
NEAR_PTS = {"NQ": 15.0, "ES": 4.0, "NDX": 15.0, "SPX": 4.0, "SPY": 0.4, "QQQ": 0.4}
DEFAULT_NEAR = 10.0

# Pas de cotation (tick) du future — un prix affiché qui n'en est pas multiple
# est impossible sur le marché (ex. le milieu bid/ask d'une fourchette de 0.25
# tombe souvent sur un quart de pas). Sert de garde-fou même quand la source
# est déjà censée être tick-accurate.
TICK_SIZE = {"NQ": 0.25, "ES": 0.25}


def round_to_tick(symbol: str, price: float) -> float:
    """Arrondit au pas de cotation du symbole ; renvoie `price` telle quelle si
    le symbole n'a pas de pas connu (options, indices cash…)."""
    tick = TICK_SIZE.get(symbol.upper())
    if not tick:
        return price
    return round(round(price / tick) * tick, 10)      # round(...,10) : purge le bruit flottant

OPEN_ET = time(9, 30)
CLOSE_ET = time(16, 0)
CONTRARIAN_CUT_ET = time(10, 15)      # 16h15 Paris : le contrarien devient risqué


@dataclass(frozen=True)
class Rung:
    name: str            # « Call Wall », « Gamma Flip », « GEX2 »…
    price: float
    kind: str            # cw | ps | zg | hvl | d1 | gex
    gex: float | None    # $ de gamma du mur (signé), None pour les autres niveaux
    dist: float          # niveau - spot, en points (positif = au-dessus du spot)
    near: bool           # au contact du spot


def near_threshold(symbol: str) -> float:
    return NEAR_PTS.get(symbol.upper(), DEFAULT_NEAR)


def build_ladder(symbol: str, spot: float, zg: float | None, hvl: float | None,
                 keys: dict | None, walls: list[tuple[str, float, float]] | None
                 ) -> list[Rung]:
    """Échelle des niveaux, du plus haut au plus bas.

    `keys` : call_wall / put_support / d1_min / d1_max (metrics.compute_levels) ;
    `walls` : [(nom, strike, gex)] des murs GEX classés. Les niveaux absents sont
    omis, jamais inventés. Deux niveaux au même prix restent deux lignes (leurs
    noms comptent : Call Wall et GEX1 se confondent souvent)."""
    thr = near_threshold(symbol)
    raw: list[tuple[str, float, str, float | None]] = []
    if zg is not None:
        raw.append(("Gamma Flip", zg, "zg", None))
    if hvl is not None:
        raw.append(("HVL", hvl, "hvl", None))
    for key, name, kind in (("call_wall", "Call Wall", "cw"),
                            ("put_support", "Put Support", "ps"),
                            ("d1_max", "1D Max", "d1"), ("d1_min", "1D Min", "d1")):
        v = (keys or {}).get(key)
        if v is not None:
            raw.append((name, float(v), kind, None))
    for name, strike, gex in walls or []:
        raw.append((name, float(strike), "gex", float(gex)))
    rungs = [Rung(n, float(p), k, g, float(p) - spot, abs(float(p) - spot) <= thr)
             for n, p, k, g in raw]
    return sorted(rungs, key=lambda r: (-r.price, r.name))


def nearest(rungs: list[Rung]) -> tuple[Rung | None, Rung | None]:
    """(niveau le plus proche AU-DESSUS, le plus proche EN DESSOUS) du spot."""
    above = [r for r in rungs if r.dist >= 0]
    below = [r for r in rungs if r.dist < 0]
    return (min(above, key=lambda r: r.dist) if above else None,
            max(below, key=lambda r: r.dist) if below else None)


def session_state(now_et: datetime, lang: str = "fr") -> tuple[str, str]:
    """(code, libellé court) de l'état de séance US en heure de New York :
    `closed`, `pre`, `open`, `late` (après la coupure contrarienne de 16h15 Paris)."""
    if now_et.weekday() >= 5:
        return "closed", i18n.t(lang, "sc_weekend")
    t = now_et.time()
    if t < OPEN_ET:
        return "pre", i18n.t(lang, "sc_pre_open")
    if t >= CLOSE_ET:
        return "closed", i18n.t(lang, "sc_closed")
    minutes = (now_et.hour * 60 + now_et.minute) - (OPEN_ET.hour * 60 + OPEN_ET.minute)
    if t < CONTRARIAN_CUT_ET:
        return "open", i18n.t(lang, "sc_session_open", minutes=minutes)
    return "late", i18n.t(lang, "sc_session_late", minutes=minutes)


def extension_pts(spot: float | None, open_: float | None) -> float | None:
    """Écart du spot à l'ouverture de la séance, en points."""
    if spot is None or open_ is None:
        return None
    return float(spot) - float(open_)


# --- Détection d'amplification (bandeau) ------------------------------------
# ⚠️ SEUILS PROVISOIRES : posés sans historique de séances réelles avec le tape
# signé, à recalibrer sur les premières séances. Mesure descriptive, pas un signal.
WINDOW_S = 300                                   # fenêtre d'analyse : 5 min
MOVE_MIN_PTS = {"NQ": 25.0, "ES": 6.0, "NDX": 25.0, "SPX": 6.0}
DEFAULT_MOVE_MIN = 10.0
GROSS_MIN_MUSD = 100.0       # flux de couverture brut minimum sur la fenêtre (M$)
RATIO_MIN = 0.35             # part nette du flux (|net| / brut) pour parler de sens unique
FLIP_NEAR_FACTOR = 3.0       # « fonce vers le Flip » : à moins de 3 x le seuil de contact


def move_threshold(symbol: str) -> float:
    return MOVE_MIN_PTS.get(symbol.upper(), DEFAULT_MOVE_MIN)


def assess(symbol: str, move_pts: float | None, net_musd: float, gross_musd: float,
           gamma_negative: bool, dist_to_flip: float | None, lang: str = "fr",
           *, swing: bool = False) -> dict:
    """État d'amplification sur la fenêtre de 5 min.

    move_pts   : variation du prix sur la fenêtre (None si données insuffisantes)
    net_musd   : pression de couverture nette (M$ ; + = les dealers doivent ACHETER)
    gross_musd : somme des |pressions| (M$), pour juger si le flux est significatif
    dist_to_flip : niveau du Gamma Flip - spot, en points (None si inconnu)

    États (`state`) : insufficient | calm | amplification | unsupported | brake.
    `tone` : alert (amplification) ; ok (favorable au contrarien — seulement
    `brake`, le seul état qui mesure un flux OPPOSÉ au mouvement) ; neutral
    (`unsupported` inclus — absence de lecture de flux, pas un signal)."""
    if move_pts is None:
        return {"state": "insufficient", "tone": "neutral", "direction": 0,
                "title": i18n.t(lang, "sc_insufficient_title"),
                "detail": i18n.t(lang, "sc_insufficient_detail"),
                "lights": {}}
    thr = move_threshold(symbol)
    direction = 0 if abs(move_pts) < thr else (1 if move_pts > 0 else -1)
    ratio = abs(net_musd) / gross_musd if gross_musd > 0 else 0.0
    flow_sig = gross_musd >= GROSS_MIN_MUSD and ratio >= RATIO_MIN
    flow_dir = (1 if net_musd > 0 else -1) if flow_sig else 0
    toward_flip = (dist_to_flip is not None and direction != 0
                   and dist_to_flip * direction > 0
                   and abs(dist_to_flip) <= FLIP_NEAR_FACTOR * near_threshold(symbol))
    gamma_light = bool(gamma_negative or toward_flip)
    lights = {"mouvement": direction != 0,
              "flux": direction != 0 and flow_dir == direction,
              "gamma": gamma_light}
    side = i18n._SIDE_WORD[lang][direction] if direction else ""     # accorde avec « amplification »
    side_m = i18n._SIDE_WORD_M[lang][direction] if direction else "" # accorde avec « mouvement »
    flux_txt = (i18n.t(lang, "sc_flux_net", net=net_musd, ratio=ratio) if gross_musd > 0
                else i18n.t(lang, "sc_flux_none"))
    gamma_txt = i18n.t(lang, "sc_gamma_negatif" if gamma_negative else "sc_gamma_positif")
    toward_flip_txt = i18n.t(lang, "sc_toward_flip") if toward_flip else ""
    # V2 (swing) : pas de fenêtre fixe de 5 min, "sc_detail_swing" omet la
    # mention — cf. commentaire i18n.
    detail = i18n.t(lang, "sc_detail_swing" if swing else "sc_detail",
                    move_pts=move_pts, flux_txt=flux_txt,
                    gamma_txt=gamma_txt, toward_flip_txt=toward_flip_txt)
    if direction == 0:
        return {"state": "calm", "tone": "neutral", "direction": 0,
                "title": i18n.t(lang, "sc_calm_title"), "detail": detail, "lights": lights}
    if flow_dir == direction:
        renforce = i18n.t(lang, "sc_renforce_defavorable" if gamma_light
                          else "sc_renforce_favorable")
        return {"state": "amplification", "tone": "alert", "direction": direction,
                "title": i18n.t(lang, "sc_amplification_title", side=side, renforce=renforce),
                "detail": detail, "lights": lights}
    if flow_dir == -direction:
        return {"state": "brake", "tone": "ok", "direction": direction,
                "title": i18n.t(lang, "sc_brake_title", side_m=side_m),
                "detail": detail, "lights": lights}
    # tone="neutral", pas "ok" : vérifié le 2026-10-03 sur 321 signaux réels
    # (cf. mémoire du projet roadmap-scalp-v2) — ce branchement n'est atteint
    # QUE quand flow_dir==0 (flux insignifiant, sous GROSS_MIN_MUSD/RATIO_MIN),
    # jamais quand le flux s'oppose activement au mouvement (ce cas renvoie
    # "brake" juste au-dessus). "unsupported" ne mesure donc jamais un vrai
    # signal contrarien, seulement l'ABSENCE de lecture de flux — lui donner
    # tone="ok" (vert, "favorable au contrarien") était trompeur : aucun edge
    # mesuré (44% continued sur les 4 jours audités, = taux de base).
    return {"state": "unsupported", "tone": "neutral", "direction": direction,
            "title": i18n.t(lang, "sc_unsupported_title", side_m=side_m),
            "detail": detail, "lights": lights}


# --- Journal du bandeau (calibration a posteriori) --------------------------
# Ce que le bandeau ANNONCE (`assess`) ne dit rien de ce qui s'est réellement
# passé — le journaliser à chaque déclenchement, puis vérifier après coup si le
# mouvement s'est confirmé ou retourné, est ce qui permet de calibrer les
# seuils sur des faits plutôt qu'au jugé (cf. mémoire du projet). Purement
# descriptif : ce n'est toujours pas un signal de trading.
SIGNAL_STATES = frozenset({"amplification", "unsupported", "brake"})
OUTCOME_DELAY_MIN = 15.0        # attendre ça avant de juger un signal résolu
OUTCOME_MIN_FACTOR = 0.5        # fraction de move_threshold(symbol) pour trancher
SIGNAL_COOLDOWN_S = 120.0       # même alerte qui re-déclenche vite -> pas une nouvelle ligne


def should_log_signal(prev: tuple[str, int] | None, state: str, direction: int,
                      last_logged: tuple[str, int, float] | None = None,
                      now: float = 0.0, cooldown_s: float = SIGNAL_COOLDOWN_S) -> bool:
    """True si (state, direction) mérite une NOUVELLE ligne de journal.

    Deux gardes : (1) un état-signal (cf. SIGNAL_STATES) qui diffère du dernier
    CONNU (`prev`) — jamais à chaque cycle où rien n'a changé (l'appelant relit
    `assess` toutes les 2 s) ; (2) même après un aller-retour par un état calme,
    la MÊME alerte (même state, même direction) qui reviendrait moins de
    `cooldown_s` après son dernier enregistrement (`last_logged`) n'en recrée
    pas une — sans ça, un ratio qui oscille juste autour d'un seuil en pleine
    liquidité fine spammerait le journal (observé le 2026-09-28 : 3 lignes en
    70 s pour la même alerte, faute de cette garde)."""
    if state not in SIGNAL_STATES or prev == (state, direction):
        return False
    if last_logged and last_logged[:2] == (state, direction) and now - last_logged[2] < cooldown_s:
        return False
    return True


def classify_outcome(symbol: str, direction: int, move_pts: float) -> str:
    """« continued » (le signal s'est confirmé), « reversed » (l'inverse),
    « flat » (rien de net) — seuil = la moitié du seuil de mouvement de
    l'instrument. Description factuelle, pas une note de performance."""
    thr = move_threshold(symbol) * OUTCOME_MIN_FACTOR
    if abs(move_pts) < thr:
        return "flat"
    return "continued" if (move_pts > 0) == (direction > 0) else "reversed"
