"""Un niveau GEX fait-il réagir le prix, selon le SIGNE de son GEX ?

Question de l'utilisateur (2026-11-04) : « seuls les niveaux GEX à gamma
positif provoquent des réactions, les négatifs sont des tremplins ». Ce module
la mesure sur l'historique, sans rien caler sur les données :

- niveaux : à l'ouverture de la fenêtre, les `n` strikes de plus fort |GEX
  net| (convention dealer du tableau de bord : call +, put −, évalué à la
  clôture de la veille), chacun avec le signe de son GEX net ;
- ZONE : un niveau GEX n'est pas une ligne. Autour de chaque niveau, une zone
  de ± `ZONE_PTS` points (5 pts NQ par défaut, comme la tolérance de contact
  de gex/confluence.py) ;
- contact : premier instant où le prix ENTRE dans la zone, par le côté d'où
  la fenêtre l'a trouvé (une zone jamais atteinte n'est pas un contact ;
  démarrer dans la zone n'en est pas un non plus) ;
- issue, en `r` points et `w` secondes, mesurée depuis le point d'ENTRÉE (le
  bord de la zone) : RÉACTION si le prix recule de `r` points avant d'avancer
  de `r` points de plus ; TRAVERSÉE dans le cas contraire ; INDÉCIS si aucun
  des deux dans le délai. Les deux seuils sont à la même distance du point
  d'entrée : une marche aléatoire donne 50 / 50, ce qui donne un repère
  naturel. Avec une zone de 0, c'est le contact avec la ligne exacte ;
- TÉMOINS : des prix sans rien de particulier (les niveaux décalés de ±30 /
  ±40 pts, à l'écart de tout vrai niveau), mesurés pareil. Sans eux, « 60 % de
  réaction » ne dit pas si c'est le niveau ou le marché ;
- comparaison positif / négatif / témoin par RÉÉCHANTILLONNAGE DES SÉANCES
  (gex/edgestats.py : les niveaux d'une même séance ne sont pas
  indépendants) et correction de Holm sur toutes les comparaisons.

Pur : tableaux numpy et DataFrames en entrée (cf. tests/test_levelreact.py) ;
lecture des fichiers dans scripts/level_reaction_report.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import dbgamma, edgestats as es

# (points, secondes, libellé) — fixés AVANT de regarder les données ; le
# premier est l'échelle du micro-scalping de l'utilisateur
SCALES = ((10.0, 120.0, "micro : 10 pts en 2 min"),
          (20.0, 300.0, "court : 20 pts en 5 min"),
          (40.0, 900.0, "moyen : 40 pts en 15 min"))
ZONE_PTS = 5.0                 # demi-largeur de la zone d'un niveau (NQ)
PLACEBO_SHIFTS = (-40.0, -30.0, 30.0, 40.0)
MIN_PLACEBO_GAP = 15.0
GROUPS = ("positif", "négatif", "témoin")
DECIDED = ("réaction", "traversée")


def session_levels(chain: pd.DataFrame, t_epoch: float, ref_spot: float, rate: float,
                   mode: str = "tdb", n: int = 5) -> pd.DataFrame:
    """Les `n` strikes de plus fort |GEX net| de la chaîne reconstruite
    (`dbgamma.session_chain`) à l'instant `t_epoch`, évaluée au prix `ref_spot`
    (clôture de la veille, comme les murs du tableau de bord). Colonnes :
    strike, gex (net, signé), gross, rang, signe."""
    cols = ["strike", "gex", "gross", "rang", "signe"]
    if chain is None or chain.empty:
        return pd.DataFrame(columns=cols)
    sub = dbgamma.select(chain, t_epoch, ref_spot, mode)
    if sub.empty:
        return pd.DataFrame(columns=cols)
    per = pd.DataFrame({"strike": np.round(dbgamma._equiv_strike(sub) * 4) / 4,
                        "gex": dbgamma._gex(sub, ref_spot, t_epoch, rate)[:, 0]})
    per["abs"] = per["gex"].abs()
    agg = per.groupby("strike").agg(gex=("gex", "sum"), gross=("abs", "sum")).reset_index()
    agg = agg[agg["gex"] != 0]
    top = agg.loc[agg["gex"].abs().nlargest(n).index].sort_values(
        "gex", key=abs, ascending=False).reset_index(drop=True)
    top["rang"] = top.index + 1
    top["signe"] = np.where(top["gex"] > 0, "positif", "négatif")
    return top[cols]


def touch_outcome(ts: np.ndarray, px: np.ndarray, level: float, r: float,
                  window_s: float, zone: float = 0.0) -> dict | None:
    """Premier contact avec la zone [level − zone, level + zone] et ce qui suit,
    mesuré depuis le bord d'entrée. None si le prix démarre dans la zone ou ne
    l'atteint jamais. `ts`, `px` : chemin de prix trié (cf. excess.tick_path)."""
    if len(px) < 2:
        return None
    lo, hi = level - zone, level + zone
    if lo <= px[0] <= hi:
        return None
    side = 1 if px[0] < lo else -1           # +1 : le prix arrive PAR LE BAS
    edge = lo if side > 0 else hi            # bord d'entrée de la zone
    hit = np.flatnonzero(px >= edge) if side > 0 else np.flatnonzero(px <= edge)
    if not len(hit):
        return None
    i = int(hit[0])
    end = int(np.searchsorted(ts, ts[i] + window_s, side="right"))
    seg = px[i:end]
    through = np.flatnonzero(seg >= edge + r) if side > 0 else np.flatnonzero(seg <= edge - r)
    back = np.flatnonzero(seg <= edge - r) if side > 0 else np.flatnonzero(seg >= edge + r)
    first_t = int(through[0]) if len(through) else None
    first_b = int(back[0]) if len(back) else None
    if first_t is None and first_b is None:
        issue, k = "indécis", None
    elif first_t is None or (first_b is not None and first_b < first_t):
        issue, k = "réaction", first_b
    else:
        issue, k = "traversée", first_t
    return {"cote": "par le bas" if side > 0 else "par le haut", "issue": issue,
            "delai_s": float(ts[i + k] - ts[i]) if k is not None else np.nan}


def placebo_levels(levels, shifts=PLACEBO_SHIFTS, min_gap: float = MIN_PLACEBO_GAP) -> np.ndarray:
    """Niveaux témoins : chaque vrai niveau décalé, en écartant ceux qui
    tombent près d'un VRAI niveau de la séance."""
    real = np.asarray(levels, float)
    out = [q for q in (real[:, None] + np.asarray(shifts)[None, :]).ravel()
           if np.all(np.abs(real - q) >= min_gap)]
    return np.unique(np.round(out, 2))


def session_events(day: str, ts: np.ndarray, px: np.ndarray, levels: pd.DataFrame,
                   zone: float = ZONE_PTS) -> list[dict]:
    """Une ligne par (niveau, échelle) : vrais niveaux (positif / négatif) et
    témoins. `touche` = la zone du niveau a été atteinte ; `issue` sinon
    « non touché ». Les zones témoins ne chevauchent jamais celles des vrais
    niveaux (écart d'au moins 2 × zone + 5 pts)."""
    rows = []
    real = [(g.strike, g.signe, g.rang, g.gex) for g in levels.itertuples()]
    gap = max(MIN_PLACEBO_GAP, 2 * zone + 5.0)
    # décalages d'au moins ±30 / ±40 pts, davantage si la zone est large
    near, far = max(30.0, gap + 10.0), max(40.0, gap + 20.0)
    for p in placebo_levels(levels["strike"].to_numpy(), (-far, -near, near, far), gap):
        real.append((float(p), "témoin", 0, np.nan))
    for strike, groupe, rang, gex in real:
        for k, (r, w, _) in enumerate(SCALES):
            o = touch_outcome(ts, px, strike, r, w, zone)
            rows.append({"day": day, "groupe": groupe, "strike": strike, "rang": rang,
                         "gex": gex, "zone": zone, "echelle": k, "touche": o is not None,
                         "cote": o["cote"] if o else "",
                         "issue": o["issue"] if o else "non touché",
                         "delai_s": o["delai_s"] if o else np.nan})
    return rows


# (groupe A, groupe B, libellé, côté d'arrivée ou None) — comparaisons
# prévues d'avance ; la correction de Holm porte sur TOUTES celles-ci
CONTRASTS = (
    ("positif", "négatif", "positif − négatif", None),
    ("positif", "témoin", "positif − témoin", None),
    ("négatif", "témoin", "négatif − témoin", None),
    ("positif", "négatif", "positif − négatif, prix arrivant par le bas (niveau au-dessus)",
     "par le bas"),
    ("positif", "négatif", "positif − négatif, prix arrivant par le haut (niveau en dessous)",
     "par le haut"),
)


def analyse(ev: pd.DataFrame, n_boot: int = es.N_BOOT) -> dict:
    """Tableaux par échelle et comparaisons corrigées. `ev` : lignes de
    `session_events`. Renvoie {"tables": [DataFrame], "tests": DataFrame}."""
    tables, rows = [], []
    for k, (r, w, name) in enumerate(SCALES):
        e = ev[ev["echelle"] == k]
        dec = e[e["issue"].isin(DECIDED)].copy()
        dec["react100"] = np.where(dec["issue"] == "réaction", 100.0, 0.0)
        dec = dec.reset_index(drop=True)
        t = []
        for g in GROUPS:
            a, d = e[e["groupe"] == g], dec[dec["groupe"] == g]
            touched = a[a["touche"]]
            lo, hi = es.cluster_ci(d, "react100", es.share_at_least(50), n_boot) \
                if len(d) else (np.nan, np.nan)
            t.append({"groupe": g, "niveaux": len(a),
                      "touchés_%": 100 * float(a["touche"].mean()) if len(a) else np.nan,
                      "décidés": len(d), "séances": int(d["day"].nunique()),
                      "réaction_%": float(d["react100"].mean()) if len(d) else np.nan,
                      "ic95_séances": f"{lo:.0f}-{hi:.0f}",
                      "indécis_%": 100 * float((touched["issue"] == "indécis").mean())
                      if len(touched) else np.nan})
        tables.append(pd.DataFrame(t).set_index("groupe"))
        for a_, b_, label, cote in CONTRASTS:
            d2 = dec if cote is None else dec[dec["cote"] == cote].reset_index(drop=True)
            res = es.diff_test(d2, d2["groupe"], a_, b_, "react100", es.share_at_least(50),
                               n_boot)
            if res["n_a"] < 10 or res["n_b"] < 10:
                continue
            rows.append({"échelle": name, "comparaison": label, "n_A": res["n_a"],
                         "n_B": res["n_b"], "écart_pts_%": res["diff"],
                         "ic95": f"{res['ic_lo']:+.0f} à {res['ic_hi']:+.0f}", "p": res["p"]})
    tests = pd.DataFrame(rows)
    if not tests.empty:
        tests["p_holm"] = es.holm(tests["p"])
        tests["verdict"] = np.where(tests["p_holm"] < 0.05, "tient",
                                    np.where(tests["p"] < 0.05, "fragile (tests multiples)",
                                             "pas démontré"))
    return {"tables": tables, "tests": tests}
