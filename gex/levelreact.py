"""Un niveau GEX est-il traversé, jusqu'où va le prix, et se retrace-t-il ?
Selon le SIGNE du GEX du niveau.

Question de l'utilisateur (2026-11-04) : « seuls les niveaux GEX à gamma
positif provoquent des réactions, les négatifs sont des tremplins ». Ce module
la mesure sur l'historique, sans rien caler sur les données :

- niveaux : à l'ouverture de la fenêtre, les `n` strikes de plus fort |GEX
  net| (convention dealer du tableau de bord : call +, put −, évalué à la
  clôture de la veille), chacun avec le signe de son GEX net ;
- ZONE : un niveau GEX n'est pas une ligne. Autour de chaque niveau, une zone
  de ± `ZONE_PTS` points (5 pts NQ, comme la tolérance de contact de
  gex/confluence.py) ;
- contact : premier instant où le prix ENTRE dans la zone, par le côté d'où
  la fenêtre l'a trouvé (démarrer dans la zone, ou ne jamais l'atteindre, n'est
  pas un contact ; les contacts des 30 dernières minutes sont écartés : trop
  peu de temps pour se développer) ;
- issue, SANS limite de temps (jusqu'à la fin de la fenêtre) :
    TRAVERSÉE : le prix atteint le bord opposé de la zone ;
    REJET : le prix recule de `RETRACE_PTS` (20) points depuis son point le
      plus profond dans la zone, sans l'avoir traversée ;
- EXPÉDITION : après la traversée, distance maximale parcourue au-delà du bord
  de sortie, jusqu'au premier retracement d'au moins `RETRACE_PTS` points ;
  après un rejet, la même chose dans l'autre sens, depuis le bord d'entrée ;
- RETRACEMENT : ce premier recul d'au moins 20 points depuis le plus haut de
  l'expédition (profondeur maximale avant que le prix ne dépasse de nouveau
  ce sommet), et s'il est retourné jusqu'au bord de la zone. Sans recul de 20
  points avant la fin de la fenêtre : « sans retracement » ;
- repère : une marche aléatoire traverse une zone de largeur L avant un recul
  de M points avec la probabilité exp(−L / M) (≈ 61 % pour ±5 pts et 20 pts) ;
- TÉMOINS : des prix sans rien de particulier (les niveaux décalés de ±30 /
  ±40 pts au moins, zones de même largeur, jamais chevauchantes), mesurés
  pareil. Sans eux, « 70 % de traversées » ne dit pas si c'est le niveau ou
  le marché ;
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

ZONE_PTS = 5.0                 # demi-largeur de la zone d'un niveau (NQ)
RETRACE_PTS = 20.0             # recul minimal pour parler de retracement / rejet
CONTACT_CUTOFF_S = 1800.0      # contacts écartés dans les 30 dernières minutes
PLACEBO_SHIFTS = (-40.0, -30.0, 30.0, 40.0)
MIN_PLACEBO_GAP = 15.0
GROUPS = ("positif", "négatif", "témoin")


def baseline_crossing(zone: float = ZONE_PTS, retrace: float = RETRACE_PTS) -> float:
    """Part de traversées d'une marche aléatoire : exp(−largeur / recul)."""
    return float(np.exp(-2 * zone / retrace))


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


def _leg(y: np.ndarray, start: int, origin: float, m: float) -> dict:
    """Expédition puis retracement, dans le sens où `y` MONTE, à partir de
    l'indice `start`. `origin` : prix depuis lequel l'expédition se mesure (bord
    de la zone). L'expédition s'arrête au premier recul d'au moins `m` depuis
    le plus haut courant ; le retracement est la profondeur maximale de ce
    recul avant que `y` ne dépasse de nouveau ce sommet."""
    seg = y[start:]
    run = np.maximum.accumulate(seg)
    cut = np.flatnonzero(run - seg >= m)
    if not len(cut):                                   # jamais de recul de m points
        return {"exp": float(run[-1] - origin), "retr": np.nan, "retr_oui": False,
                "retour": False}
    q = int(cut[0])
    peak = run[q]
    after = seg[q:]
    again = np.flatnonzero(after > peak)
    low = float(after[: int(again[0]) if len(again) else len(after)].min())
    return {"exp": float(peak - origin), "retr": float(peak - low), "retr_oui": True,
            "retour": bool(low <= origin)}


def touch_outcome(ts: np.ndarray, px: np.ndarray, level: float, zone: float = ZONE_PTS,
                  retrace: float = RETRACE_PTS, t_end: float | None = None,
                  cutoff_s: float = CONTACT_CUTOFF_S) -> dict | None:
    """Premier contact avec la zone [level − zone, level + zone] et ce qui suit,
    jusqu'à la fin du chemin (`ts`, `px` : chemin de prix trié, cf.
    excess.tick_path). None si le prix démarre dans la zone ou ne l'atteint
    jamais. Issues : traversée, rejet, non résolu (reste dans la zone), trop
    tard (contact dans les `cutoff_s` dernières secondes avant `t_end`)."""
    if len(px) < 2:
        return None
    lo, hi = level - zone, level + zone
    if lo <= px[0] <= hi:
        return None
    side = 1 if px[0] < lo else -1                  # +1 : le prix arrive PAR LE BAS
    y = px if side > 0 else -px                     # ramené à « arrive par le bas »
    ylo, yhi = (lo, hi) if side > 0 else (-hi, -lo)
    hit = np.flatnonzero(y >= ylo)
    if not len(hit):
        return None
    i = int(hit[0])
    out = {"cote": "par le bas" if side > 0 else "par le haut", "exp": np.nan,
           "retr": np.nan, "retr_oui": False, "retour": False}
    end = ts[-1] if t_end is None else t_end
    if ts[i] > end - cutoff_s:
        return {**out, "issue": "trop tard"}
    seg = y[i:]
    run = np.maximum.accumulate(seg)
    crossed = np.flatnonzero(seg >= yhi)
    rejected = np.flatnonzero(run - seg >= retrace)
    a = int(crossed[0]) if len(crossed) else None
    b = int(rejected[0]) if len(rejected) else None
    if a is None and b is None:
        return {**out, "issue": "non résolu"}
    if b is None or (a is not None and a < b):
        leg = _leg(y, i + a, yhi, retrace)             # traversée : au-delà du bord opposé
        issue = "traversée"
    else:
        leg = _leg(-y, i + b, -ylo, retrace)           # rejet : loin du bord d'entrée
        issue = "rejet"
    return {**out, "issue": issue, "exp": leg["exp"], "retr": leg["retr"],
            "retr_oui": leg["retr_oui"], "retour": leg["retour"]}


def placebo_levels(levels, shifts=PLACEBO_SHIFTS, min_gap: float = MIN_PLACEBO_GAP) -> np.ndarray:
    """Niveaux témoins : chaque vrai niveau décalé, en écartant ceux qui
    tombent près d'un VRAI niveau de la séance."""
    real = np.asarray(levels, float)
    out = [q for q in (real[:, None] + np.asarray(shifts)[None, :]).ravel()
           if np.all(np.abs(real - q) >= min_gap)]
    return np.unique(np.round(out, 2))


def session_events(day: str, ts: np.ndarray, px: np.ndarray, levels: pd.DataFrame,
                   zone: float = ZONE_PTS, retrace: float = RETRACE_PTS,
                   t_end: float | None = None) -> list[dict]:
    """Une ligne par niveau : vrais niveaux (positif / négatif) et témoins.
    `touche` = la zone du niveau a été atteinte (avant l'heure limite) ;
    `issue` sinon « non touché ». Les zones témoins ne chevauchent jamais
    celles des vrais niveaux (écart d'au moins 2 × zone + 5 pts)."""
    rows = []
    real = [(g.strike, g.signe, g.rang, g.gex) for g in levels.itertuples()]
    gap = max(MIN_PLACEBO_GAP, 2 * zone + 5.0)
    # décalages d'au moins ±30 / ±40 pts, davantage si la zone est large
    near, far = max(30.0, gap + 10.0), max(40.0, gap + 20.0)
    for p in placebo_levels(levels["strike"].to_numpy(), (-far, -near, near, far), gap):
        real.append((float(p), "témoin", 0, np.nan))
    for strike, groupe, rang, gex in real:
        o = touch_outcome(ts, px, strike, zone, retrace, t_end)
        rows.append({"day": day, "groupe": groupe, "strike": strike, "rang": rang,
                     "gex": gex, "zone": zone, "retrace": retrace,
                     "touche": o is not None and o["issue"] != "trop tard",
                     "cote": o["cote"] if o else "",
                     "issue": o["issue"] if o else "non touché",
                     "exp_pts": o["exp"] if o else np.nan,
                     "retr_pts": o["retr"] if o else np.nan,
                     "retr_oui": bool(o["retr_oui"]) if o else False,
                     "retour_zone": bool(o["retour"]) if o else False})
    return rows


def _ci(d: pd.DataFrame, col: str, stat, n_boot: int) -> str:
    if d.empty:
        return "—"
    lo, hi = es.cluster_ci(d, col, stat, n_boot)
    return f"{lo:.0f}-{hi:.0f}" if np.isfinite(lo) else "—"


def _after_table(d: pd.DataFrame, n_boot: int) -> pd.DataFrame:
    rows = []
    for g in GROUPS:
        x = d[d["groupe"] == g].reset_index(drop=True)
        w = x[x["retr_oui"]]
        rows.append({"groupe": g, "cas": len(x), "séances": int(x["day"].nunique()),
                     "exp_méd": float(x["exp_pts"].median()) if len(x) else np.nan,
                     "ic95_méd": _ci(x, "exp_pts", es.quantile_of(0.5), n_boot),
                     "exp_p75": float(x["exp_pts"].quantile(0.75)) if len(x) else np.nan,
                     "exp_p90": float(x["exp_pts"].quantile(0.90)) if len(x) else np.nan,
                     "retracement_%": 100 * float(x["retr_oui"].mean()) if len(x) else np.nan,
                     "retr_méd": float(w["retr_pts"].median()) if len(w) else np.nan,
                     "retour_zone_%": 100 * float(w["retour_zone"].mean()) if len(w) else np.nan})
    return pd.DataFrame(rows).set_index("groupe")


# (groupe A, groupe B, libellé) — comparaisons prévues d'avance ; la correction
# de Holm porte sur TOUTES celles-ci
PAIRS = (("positif", "négatif", "positif − négatif"),
         ("positif", "témoin", "positif − témoin"),
         ("négatif", "témoin", "négatif − témoin"))


def analyse(ev: pd.DataFrame, n_boot: int = es.N_BOOT) -> dict:
    """Tableaux et comparaisons corrigées. `ev` : lignes de `session_events`.
    Renvoie {"resume": DataFrame, "apres_traversee": DataFrame, "apres_rejet":
    DataFrame, "tests": DataFrame}."""
    d = ev[ev["issue"] != "trop tard"]
    res = d[d["issue"].isin(("traversée", "rejet"))].copy()
    res["trav100"] = np.where(res["issue"] == "traversée", 100.0, 0.0)
    res = res.reset_index(drop=True)
    trav = res[res["issue"] == "traversée"].reset_index(drop=True)
    rej = res[res["issue"] == "rejet"].reset_index(drop=True)
    summary = []
    for g in GROUPS:
        a, r = d[d["groupe"] == g], res[res["groupe"] == g]
        summary.append({"groupe": g, "niveaux": len(a),
                        "touchés_%": 100 * float(a["touche"].mean()) if len(a) else np.nan,
                        "résolus": len(r), "séances": int(r["day"].nunique()),
                        "traversée_%": float(r["trav100"].mean()) if len(r) else np.nan,
                        "ic95_séances": _ci(r.reset_index(drop=True), "trav100",
                                            es.share_at_least(50), n_boot),
                        "non_résolus_%": 100 * float((a["issue"] == "non résolu").sum()
                                                     / max(int(a["touche"].sum()), 1))})
    rows = []

    def run(df, col, stat, mesure, unite, cote=None):
        for a_, b_, label in PAIRS:
            if cote is not None and (a_, b_) != ("positif", "négatif"):
                continue
            x = df if cote is None else df[df["cote"] == cote].reset_index(drop=True)
            r = es.diff_test(x, x["groupe"], a_, b_, col, stat, n_boot)
            if r["n_a"] < 10 or r["n_b"] < 10:
                continue
            rows.append({"mesure": mesure, "comparaison": label + (f", {cote}" if cote else ""),
                         "unité": unite, "n_A": r["n_a"], "n_B": r["n_b"],
                         "écart": r["diff"], "ic95": f"{r['ic_lo']:+.0f} à {r['ic_hi']:+.0f}",
                         "p": r["p"]})
    run(res, "trav100", es.share_at_least(50), "taux de traversée", "pts de %")
    run(res, "trav100", es.share_at_least(50), "taux de traversée", "pts de %", "par le bas")
    run(res, "trav100", es.share_at_least(50), "taux de traversée", "pts de %", "par le haut")
    run(trav, "exp_pts", es.quantile_of(0.5), "expédition médiane après traversée", "pts")
    run(trav, "exp_pts", es.quantile_of(0.9), "expédition p90 après traversée", "pts")
    run(rej, "exp_pts", es.quantile_of(0.5), "expédition médiane après rejet", "pts")
    tests = pd.DataFrame(rows)
    if not tests.empty:
        tests["p_holm"] = es.holm(tests["p"])
        tests["verdict"] = np.where(tests["p_holm"] < 0.05, "tient",
                                    np.where(tests["p"] < 0.05, "fragile (tests multiples)",
                                             "pas démontré"))
    return {"resume": pd.DataFrame(summary).set_index("groupe"),
            "apres_traversee": _after_table(trav, n_boot),
            "apres_rejet": _after_table(rej, n_boot), "tests": tests}
