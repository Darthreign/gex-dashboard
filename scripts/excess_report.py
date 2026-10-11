"""Excès depuis le dernier swing : les market makers les soutiennent-ils ?

    python scripts/excess_report.py                    # NQ, 100 / 60 points
    python scripts/excess_report.py --excess 100 --swing 60 --horizon 120
    python scripts/excess_report.py --verifiees-seulement
    python scripts/excess_report.py --flux databento   # tape reconstruit (Databento)
    python scripts/excess_report.py --start 2026-09-28 --end 2026-10-09

Pour chaque séance CME (18h00 la veille -> 17h00 ET) ayant des ticks NQ
(capture live `data/ticks`, sinon `data/import/ticks_full`) et du tape
d'options NQ (le 25/09/2026 est exclu) :

1. chemin de prix TICK PAR TICK (ordre exact des prix, délais en minutes
   réelles) ;
2. excès (gex/excess.py) selon deux variantes :
   A. en points : excès 100 pts, swing 60 pts ;
   B. en EM : excès = a × EM du jour, swing = b × EM, avec a et b choisis
      pour que la séance MÉDIANE retombe sur 100 / 60 pts (EM = straddle du
      premier snapshot de la chaîne NQ du jour) ;
3. couleur du voyant (flux de couverture des 5 min précédentes contre les
   10 d'avant, barres de tape CLOSES uniquement, seuil « franc » 0,35 du
   bandeau, inchangé) ;
4. excursion adverse et retours de 10 / 15 / 20 pts, sur 120 min au plus ;
   un excès qui laisse moins de 60 min de données est écarté ;
5. SECOND TEST (ton entrée, variante en points) : après l'excès, réaction
   d'au moins 15 pts puis retour à moins de 10 pts de l'extrême (dans les
   120 min) ; voyant lu à ce moment, continuation au-delà de l'extrême, et
   suivi d'un ordre à 3 pts de l'extrême s'il est touché ;
6. le gris découpé selon la force du flux (descriptif, seuil inchangé) ;
7. PISTE 1 : soutien des dealers pendant la jambe de l'excès (dernier swing
   -> extrême) et pendant la réaction (extrême -> second test), en terciles
   calés sur la première moitié des séances, résultat lu sur la seconde
   (`--split` pour choisir la coupure). Avec `--flux databento` et le tape à
   la seconde (data/tape_databento_1s), bornes exactes à la seconde, et flux
   des 30 / 60 / 120 dernières secondes avant le second test ;
8. PISTE 2 (`--gamma`) : positionnement gamma des dealers au second test
   (régime frein / accélérateur, mur du côté de l'excès : distance à
   l'extrême et taille), reconstitué depuis l'open interest Databento
   (gex/dbgamma.py), même découpage apprentissage / test.

Source du flux, présentée À PART :
- « vérifié » : barres étiquetées (`mult_source` : correctif ou capture
  postérieure) ;
- « delta moyen » : anciennes barres non vérifiables (pas de prints bruts),
  utilisées seulement si toute la fenêtre de 15 min a la MÊME échelle d'après
  le delta moyen implicite (excess.scale_classes, seuil calé sur les barres
  vérifiées) — le voyant ne lit que des rapports net / brut, justes à échelle
  commune ; sinon « sans flux ».

`--flux databento` : le voyant lit le tape reconstruit depuis l'historique
Databento (data/tape_databento, scripts/databento_tape.py) au lieu du tape
live — à n'utiliser qu'après la vérification de concordance sur les séances
communes (`databento_tape.py --compare`).

Lecture seule ; rapport dans data/reports/exces_<SYM>_<date>.md
(exces_<SYM>_databento_<date>.md avec --flux databento). Peu de
séances : des tendances, pas une preuve (voir les intervalles à 95 %).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gex import dbgamma, excess, rates, store  # noqa: E402
from gex.config import SETTINGS  # noqa: E402
from gex.edge_report import _epoch, _straddle_em  # noqa: E402
from gex.flowtape import FUTURE_POINT_VALUE  # noqa: E402
from gex.metrics import ET  # noqa: E402

EXCLUDED_DAYS = {"2026-09-25"}        # multiplicateur invérifiable ce jour-là
HEDGE = ["hedge_call_buy", "hedge_call_sell", "hedge_put_buy", "hedge_put_sell"]
MIN_AFTER_S = 3600                    # au moins 60 min de données après l'excès
REACTION_PTS, RETEST_PTS, ENTRY_OFFSET = 15.0, 10.0, 3.0
WINDOW_S = 15 * 60                    # fenêtres du voyant : 5 + 10 min
CASH = (9 * 60 + 30, 16 * 60)
SOURCES = ("vérifié", "delta moyen")
TAPE_ROOTS = {"live": "tape", "databento": "tape_databento",
              "databento_1s": "tape_databento_1s"}
FINE_WINDOWS_S = (30, 60, 120)        # flux lu juste avant le second test


def _load_tape(symbol: str, day: str, flux: str) -> pd.DataFrame:
    if flux == "live":
        return store.load_tape(symbol, day)
    p = SETTINGS.data_dir / TAPE_ROOTS[flux] / symbol / f"{day}.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def tape_days(symbol: str, flux: str) -> set[str]:
    if flux == "live":
        return set(store.tape_days(symbol))
    root = SETTINGS.data_dir / TAPE_ROOTS[flux] / symbol
    return {p.stem for p in root.glob("*.parquet")} if root.exists() else set()


def _bounds(day: str) -> tuple[datetime, datetime]:
    d = date.fromisoformat(day)
    return (datetime.combine(d - timedelta(days=1), datetime.min.time()) + timedelta(hours=18),
            datetime.combine(d, datetime.min.time()) + timedelta(hours=17))


def _import_path(symbol: str, day: str) -> Path:
    return SETTINGS.data_dir / "import" / "ticks_full" / symbol / f"{day}.parquet"


def session_path(symbol: str, day: str) -> tuple[np.ndarray, np.ndarray]:
    """Prix de la séance CME `day`, tick par tick (fichiers déjà rangés par
    séance, cf. tickcapture._session_day et nightly_import)."""
    ticks = store.load_ticks(symbol, day)
    if ticks.empty and _import_path(symbol, day).exists():
        ticks = pd.read_parquet(_import_path(symbol, day))
    if ticks.empty:
        return np.array([]), np.array([])
    if "side" in ticks:                                 # transactions seulement
        ticks = ticks[ticks["side"].isin(("BUY", "SELL"))]
    ticks = ticks.sort_values("ts", kind="stable")
    return excess.tick_path(ticks["ts"].to_numpy(float), ticks["price"].to_numpy(float))


def session_flow(symbol: str, day: str, px_ts: np.ndarray, px: np.ndarray,
                 flux: str = "live") -> pd.DataFrame:
    """Barres de tape de la séance : epoch de début de minute, net, brut,
    `labeled` (multiplicateur vérifié), et delta moyen implicite."""
    cols = ["ts", "net", "gross", "labeled", "implied", "usable", "prints"]
    start, end = _bounds(day)
    parts = [_load_tape(symbol, d, flux) for d in
             ((date.fromisoformat(day) - timedelta(days=1)).isoformat(), day)]
    parts = [p for p in parts if not p.empty]
    tape = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if tape.empty or not set(HEDGE) <= set(tape.columns):
        return pd.DataFrame(columns=cols)
    ts = pd.to_datetime(tape["timestamp"])
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert(ET).dt.tz_localize(None)
    keep = ((ts >= start) & (ts < end)).to_numpy()
    tape, ts = tape[keep], ts[keep]
    loc = ts.dt.tz_localize(ET, ambiguous="NaT", nonexistent="NaT")
    tape, loc = tape[loc.notna().to_numpy()], loc[loc.notna()]
    epoch = _epoch(loc) if len(loc) else np.array([])
    order = np.argsort(epoch, kind="stable")
    tape, epoch = tape.iloc[order], epoch[order]
    labeled = tape["mult_source"].notna().to_numpy() if "mult_source" in tape \
        else np.zeros(len(tape), bool)
    gross = tape[HEDGE].abs().sum(axis=1).to_numpy()
    # prix du NQ à la fin de chaque minute
    j = np.searchsorted(px_ts, epoch + 60, side="left") - 1
    spot = np.where(j >= 0, px[np.clip(j, 0, max(len(px) - 1, 0))], np.nan) \
        if len(px) else np.full(len(epoch), np.nan)
    contracts = (tape.get("buy_contracts", 0) + tape.get("sell_contracts", 0))
    contracts = np.asarray(contracts, float) if np.ndim(contracts) else np.zeros(len(tape))
    clean = np.ones(len(tape), bool)
    if "delta_prints" in tape:
        clean &= tape["delta_prints"].to_numpy(float) > 0
    if "no_delta_prints" in tape:
        clean &= tape["no_delta_prints"].to_numpy(float) == 0
    mult = FUTURE_POINT_VALUE.get(symbol, 20.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        implied = gross / (contracts * spot * mult)
    usable = clean & (contracts > 0) & np.isfinite(implied) & (gross > 0)
    prints = tape["delta_prints"].to_numpy(float) if "delta_prints" in tape \
        else np.full(len(tape), np.nan)
    return pd.DataFrame({"ts": epoch, "net": tape[HEDGE].sum(axis=1).to_numpy(),
                         "gross": gross, "labeled": labeled, "implied": implied,
                         "usable": usable, "prints": prints})


def calibrate(sessions: list[dict]) -> float | None:
    """Seuil d'échelle = médiane du delta moyen implicite des barres
    VÉRIFIÉES × √5 (milieu géométrique entre juste et ×5)."""
    vals = np.concatenate([s["flow"].loc[s["flow"]["labeled"] & s["flow"]["usable"],
                                         "implied"].to_numpy(float) for s in sessions]
                          or [np.array([])])
    return float(np.median(vals) * np.sqrt(5)) if len(vals) else None


def apply_scale(flow: pd.DataFrame, threshold: float | None, use_old: bool) -> pd.DataFrame:
    """Colonnes `ok` et `grp` lues par le voyant : barres vérifiées = échelle
    juste ; anciennes barres = échelle estimée, seulement si `use_old`."""
    f = flow.copy()
    grp = np.where(f["labeled"], "x1", "").astype(object)
    old = ~f["labeled"].to_numpy(bool)
    if use_old and threshold and old.any():
        cls = excess.scale_classes(f["implied"].to_numpy(float),
                                   f["usable"].to_numpy(bool) & old, threshold)
        grp[old] = cls[old]
    f["grp"], f["ok"] = grp, grp != ""
    return f


def _segment(ts: float) -> str:
    t = datetime.fromtimestamp(ts, ET)
    m = t.hour * 60 + t.minute
    return "cash" if CASH[0] <= m < CASH[1] else "globex"


def _read(flow: pd.DataFrame, t: float, excess_dir: int) -> tuple[str, str, str]:
    """(couleur, nuance, source du flux) au tick `t` : seules les minutes
    CLOSES avant t comptent (début de la minute en cours)."""
    m = np.floor(t / 60) * 60
    fts = flow["ts"].to_numpy(float)
    color, det = excess.flow_color(fts, flow["net"].to_numpy(float),
                                   flow["gross"].to_numpy(float), flow["ok"].to_numpy(bool),
                                   m, excess_dir, grp=flow["grp"].to_numpy(object))
    win = (fts >= m - WINDOW_S) & (fts < m)
    src = "vérifié" if win.any() and flow["labeled"].to_numpy(bool)[win].all() \
        else "delta moyen"
    nuance = excess.gris_nuance(det.get("support_ratio")) if color == "gris" else color
    return color, nuance, src


def session_events(day: str, px_ts: np.ndarray, px: np.ndarray, flow: pd.DataFrame,
                   excess_pts: float, swing_pts: float, horizon: int,
                   fine: pd.DataFrame | None = None,
                   chain: pd.DataFrame | None = None) -> list[dict]:
    rows = []
    for e in excess.find_excesses(px, px, excess_pts, swing_pts):
        i = e["i"]
        if px_ts[-1] - px_ts[i] < MIN_AFTER_S:
            continue
        color, nuance, src = _read(flow, px_ts[i], e["dir"])
        row = {"day": day, "ts": px_ts[i], "segment": _segment(px_ts[i]),
               "sens": "haussier" if e["dir"] > 0 else "baissier",
               "excess_pts": excess_pts, "couleur": color, "nuance": nuance, "flux": src,
               **excess.outcome(px, px, i, e["level"], -e["dir"], horizon, ts=px_ts)}
        rt = excess.find_retest(px, px, i, e["dir"], REACTION_PTS, RETEST_PTS, horizon,
                                ts=px_ts)
        if rt is not None and px_ts[-1] - px_ts[rt["t"]] >= MIN_AFTER_S:
            t = rt["t"]
            c2, n2, src2 = _read(flow, px_ts[t], e["dir"])
            row["retest"] = {"day": day, "ts": px_ts[t], "segment": _segment(px_ts[t]),
                             "sens": row["sens"], "couleur": c2, "nuance": n2, "flux": src2,
                             "couleur_exces": color,
                             "attente_min": (px_ts[t] - px_ts[i]) / 60,
                             # extrême -> second test, sur le prix seul (tick par tick)
                             "duree_reaction_s": px_ts[t] - px_ts[rt["extreme_i"]],
                             **_phases(flow, px_ts[e["swing_i"]], px_ts[rt["extreme_i"]],
                                       px_ts[t], e["dir"], fine),
                             **_gamma(chain, px_ts[t], px[t], rt["extreme"], e["dir"]),
                             **excess.retest_outcome(px, px, t, rt["extreme"], -e["dir"],
                                                     ENTRY_OFFSET, horizon, ts=px_ts)}
        rows.append(row)
    return rows


def _gamma(chain: pd.DataFrame | None, t: float, spot: float, extreme: float,
           excess_dir: int) -> dict:
    """Piste 2 : positionnement gamma au second test (colonnes g_*)."""
    if chain is None:
        return {}
    v = dbgamma.view(chain, t, spot, extreme, excess_dir, rates.current_rate())
    return {f"g_{k}": val for k, val in v.items()}


def _phases(flow: pd.DataFrame, t_swing: float, t_ext: float, t_test: float,
            excess_dir: int, fine: pd.DataFrame | None = None) -> dict:
    """Soutien des dealers pendant la jambe de l'excès (dernier swing ->
    extrême), pendant la réaction (extrême -> second test) et, avec le tape à
    la seconde, sur les dernières secondes avant le second test. Bornes
    exactes à la seconde quand `fine` est fourni, à la minute sinon."""
    use_fine = fine is not None and not fine.empty
    src, b_s = (fine, 1.0) if use_fine else (flow, 60.0)
    args = (src["ts"].to_numpy(float), src["net"].to_numpy(float),
            src["gross"].to_numpy(float), src["ok"].to_numpy(bool))
    kw = {"grp": src["grp"].to_numpy(object), "bucket_s": b_s,
          "prints": src["prints"].to_numpy(float) if "prints" in src else None}
    a = excess.phase_support(*args, t_swing, t_ext, excess_dir, **kw)
    b = excess.phase_support(*args, t_ext, t_test, excess_dir, **kw)
    ga, gb = a["gross_per_min"], b["gross_per_min"]
    out = {"pas_flux_s": b_s, "prints_exces": a["prints"], "prints_reaction": b["prints"]}
    for w in FINE_WINDOWS_S:
        c = excess.phase_support(*args, t_test - w, t_test, excess_dir, **kw) if use_fine \
            else {"support": np.nan, "prints": np.nan}
        out[f"sup_{w}s"], out[f"prints_{w}s"] = c["support"], c["prints"]
    return {**out, "sup_exces": a["support"], "sup_reaction": b["support"],
            # flux brut par minute pendant la réaction / pendant l'excès
            "intensite": gb / ga if ga and ga > 0 and np.isfinite(gb) else np.nan,
            "brut_min_exces": a["gross_per_min"], "brut_min_reaction": b["gross_per_min"],
            "min_exces": a["minutes"], "min_reaction": b["minutes"]}


def support_analysis(rt: pd.DataFrame, split_day: str) -> list[str]:
    """Piste 1 : le soutien de l'excès a-t-il faibli pendant la réaction ?
    Terciles calés sur l'APPRENTISSAGE (séances < split_day), résultat lu sur
    le TEST (séances >= split_day), sans retouche."""
    if rt.empty or "sup_exces" not in rt:
        return ["_(aucun second test)_", ""]
    train, test = rt[rt["day"] < split_day], rt[rt["day"] >= split_day]
    if train["day"].nunique() < 20 or test["day"].nunique() < 20:
        return [f"_Trop peu de séances de part et d'autre du {split_day} "
                f"({train['day'].nunique()} / {test['day'].nunique()}) : il en faut au "
                "moins 20 de chaque côté._", ""]
    ea, eb = excess.tercile_edges(train["sup_exces"]), excess.tercile_edges(train["sup_reaction"])

    def label(df):
        te = df["sup_exces"].map(lambda x: excess.tercile(x, ea))
        tr = df["sup_reaction"].map(lambda x: excess.tercile(x, eb))
        return df.assign(t_exces=te, t_reaction=tr,
                         situation=[excess.support_situation(a, b) for a, b in zip(te, tr)])
    train, test = label(train), label(test)
    lines = [f"Apprentissage : {train['day'].min()} → {train['day'].max()} "
             f"({train['day'].nunique()} séances) · test : {test['day'].min()} → "
             f"{test['day'].max()} ({test['day'].nunique()} séances).", "",
             "Soutien = flux net des dealers dans le sens de l'excès / flux brut (-1 contre, "
             "+1 pour). Phase excès : dernier swing -> extrême ; phase réaction : extrême -> "
             "second test. Terciles calés sur l'apprentissage : excès "
             f"[{ea[0]:.2f} ; {ea[1]:.2f}], réaction [{eb[0]:.2f} ; {eb[1]:.2f}]. "
             "`soutien maintenu` = excès et réaction dans le tercile haut ; `soutien qui "
             "faiblit` = excès haut, réaction milieu ou bas ; `jamais de soutien` = excès "
             "bas.", ""]
    for name, df in (("APPRENTISSAGE (sert à caler, ne prouve rien)", train),
                     ("TEST (le seul qui compte)", test)):
        lines += [f"#### {name}", "",
                  _table(excess.summarize_retests(df, key="situation",
                                                  order=excess.SITUATIONS)), ""]
    for sens in ("haussier", "baissier"):
        lines += [f"#### TEST — excès {sens} "
                  f"(fade = {'vente' if sens == 'haussier' else 'achat'})", "",
                  _table(excess.summarize_retests(test[test["sens"] == sens], key="situation",
                                                  order=excess.SITUATIONS)), ""]
    if "duree_reaction_s" in train:
        ed = excess.tercile_edges(train["duree_reaction_s"])
        dlab = {"bas": "réaction express", "milieu": "réaction normale",
                "haut": "réaction lente", "": "inconnue"}
        test = test.assign(duree_t=test["duree_reaction_s"].map(
            lambda x: dlab[excess.tercile(x, ed)]))
        tr_d = train.assign(duree_t=train["duree_reaction_s"].map(
            lambda x: dlab[excess.tercile(x, ed)]))
        lines += ["#### Durée de la réaction (prix seul : extrême -> second test)", "",
                  f"Terciles calés sur l'apprentissage : [{ed[0]:.0f} s ; {ed[1]:.0f} s].", "",
                  "APPRENTISSAGE :", "",
                  _table(excess.summarize_retests(tr_d, key="duree_t",
                                                  order=tuple(dlab.values()))), "",
                  "TEST :", "",
                  _table(excess.summarize_retests(test, key="duree_t",
                                                  order=tuple(dlab.values()))), ""]
    ei = excess.tercile_edges(train["intensite"]) if "intensite" in train else (np.nan, np.nan)
    if not np.isnan(ei[0]):
        test = test.assign(intensite_t=test["intensite"].map(
            lambda x: excess.INTENSITY_LABELS[excess.tercile(x, ei)]))
        lines += ["#### TEST — intensité du flux (information)", "",
                  "Flux brut par minute pendant la réaction ÷ pendant l'excès ; terciles calés "
                  f"sur l'apprentissage [{ei[0]:.2f} ; {ei[1]:.2f}]. `pire25_n` / `rappel25_%` : "
                  "ordres remplis partis d'au moins 25 pts contre, et part de ceux qui sont "
                  "ensuite REVENUS au prix d'entrée (de quoi sortir sans perte) ; idem 50.", "",
                  _table(excess.summarize_retests(
                      test, key="intensite_t",
                      order=tuple(excess.INTENSITY_LABELS.values()))), ""]
    if "pas_flux_s" in test:
        fine_share = 100 * float((test["pas_flux_s"] == 1.0).mean())
        lines += [f"Phases mesurées sur le flux à la seconde : {fine_share:.0f} % des seconds "
                  "tests (le reste à la minute). Transactions par phase (médiane) : excès "
                  f"{test['prints_exces'].median():.0f}, réaction "
                  f"{test['prints_reaction'].median():.0f}.", ""]
    for w in FINE_WINDOWS_S:
        col = f"sup_{w}s"
        if col not in train or train[col].notna().sum() < 30:
            continue
        ew = excess.tercile_edges(train[col])
        lab = {"bas": "contre l'excès", "milieu": "neutre", "haut": "pour l'excès",
               "": "sans flux"}
        tw = test.assign(**{f"t_{w}s": test[col].map(lambda x: lab[excess.tercile(x, ew)])})
        lines += [f"#### TEST — flux des {w} dernières secondes avant le second test", "",
                  f"Soutien sur [second test − {w} s ; second test), terciles calés sur "
                  f"l'apprentissage [{ew[0]:.2f} ; {ew[1]:.2f}] ; transactions dans la fenêtre "
                  f"(médiane) : {test[f'prints_{w}s'].median():.0f}.", "",
                  _table(excess.summarize_retests(tw, key=f"t_{w}s",
                                                  order=tuple(lab.values()))), ""]
    grid = test[(test["t_exces"] != "") & (test["t_reaction"] != "")]
    if not grid.empty:
        order = ["bas", "milieu", "haut"]
        pct = grid.assign(c50=100 * (grid["continuation"] >= 50)).pivot_table(
            index="t_exces", columns="t_reaction", values="c50", aggfunc="mean")
        cnt = grid.pivot_table(index="t_exces", columns="t_reaction", values="continuation",
                               aggfunc="size")
        lines += ["#### TEST — continuation d'au moins 50 pts (%), soutien pendant l'excès "
                  "(lignes) × pendant la réaction (colonnes)", "",
                  _table(pct.reindex(index=order, columns=order)), "", "Nombre de cas :", "",
                  _table(cnt.reindex(index=order, columns=order)), ""]
    return lines


def gamma_analysis(rt: pd.DataFrame, split_day: str, sessions: list[dict]) -> list[str]:
    """Piste 2 : régime (frein / accélérateur), mur du côté de l'excès (distance
    à l'extrême, taille). Bornes calées sur l'apprentissage, lecture sur le
    test, comme la piste 1."""
    chains = [s["chain"] for s in sessions if s.get("chain") is not None
              and not s["chain"].empty]
    if rt.empty or "g_regime" not in rt or not chains:
        return ["_(aucune chaîne reconstituée : open interest ou définitions absents)_", ""]
    src = pd.concat(chains)["iv_source"].value_counts(normalize=True).mul(100).round(1)
    known = rt[rt["g_regime"] != "inconnu"]
    lines = [f"Chaînes reconstituées : {len(chains)} séances sur {len(sessions)} ; seconds "
             f"tests avec un positionnement : {len(known)} sur {len(rt)}. Origine de la "
             "volatilité (% des options) : "
             + ", ".join(f"{k} {v}" for k, v in src.items()) + ".", "",
             "Régime : GEX net des dealers au prix du second test (> 0 frein, < 0 "
             "accélérateur). Mur : Call Wall (excès haussier) ou Put Support (excès baissier) "
             "au sens du tableau de bord ; distance = mur − extrême dans le sens de l'excès "
             "(négative : l'extrême a déjà dépassé le mur) ; taille = part du GEX brut.", ""]
    train, test = known[known["day"] < split_day], known[known["day"] >= split_day]
    if train["day"].nunique() < 20 or test["day"].nunique() < 20:
        return lines + [f"_Trop peu de séances de part et d'autre du {split_day}._", ""]
    pos = train.loc[train["g_dist_mur"] >= 0, "g_dist_mur"]
    ed, es = excess.tercile_edges(pos), excess.tercile_edges(train["g_part_mur"])

    def mur(x):
        if x is None or np.isnan(x):
            return "pas de mur"
        if x < 0:
            return "mur déjà dépassé"
        return {"bas": "mur tout proche", "milieu": "mur proche",
                "haut": "mur loin"}[excess.tercile(x, ed)]
    size = {"bas": "petit mur", "milieu": "mur moyen", "haut": "gros mur", "": "pas de mur"}
    test = test.assign(
        regime_fade=test["g_regime"] + " / fade " + np.where(test["sens"] == "haussier",
                                                             "vendeur", "acheteur"),
        position_mur=test["g_dist_mur"].map(mur),
        taille_mur=test["g_part_mur"].map(lambda x: size[excess.tercile(x, es)]))
    lines += [f"Bornes (apprentissage) : distance au mur [{ed[0]:.0f} ; {ed[1]:.0f}] pts, "
              f"taille [{es[0]:.3f} ; {es[1]:.3f}] du GEX brut.", ""]
    lines += ["#### APPRENTISSAGE — régime (pour vérifier que l'effet existe des deux côtés)",
              "", _table(excess.summarize_retests(train, key="g_regime",
                                                  order=("frein", "accélérateur"))), ""]
    st = stability(known, key="g_regime", order=("frein", "accélérateur"))
    lines += ["#### Régime — stabilité par trimestre (toutes séances)", "", _table(st), ""]
    if "duree_reaction_s" in train:
        ed = excess.tercile_edges(train["duree_reaction_s"])
        dl = {"bas": "express", "milieu": "normale", "haut": "lente", "": "inconnue"}
        def rd(df):
            return df.assign(regime_duree=df["g_regime"] + " / réaction " + df[
                "duree_reaction_s"].map(lambda x: dl[excess.tercile(x, ed)]))
        test = rd(test)
        lines += ["#### APPRENTISSAGE — régime et durée de la réaction (contrôle)", "",
                  _table(excess.summarize_retests(rd(train), key="regime_duree", order=())),
                  "", "#### TEST — régime et durée de la réaction", "",
                  _table(excess.summarize_retests(test, key="regime_duree", order=())), ""]
    for key, title, order in (
            ("g_regime", "régime", ("frein", "accélérateur")),
            ("regime_fade", "régime et sens du fade", ()),
            ("position_mur", "position du mur par rapport à l'extrême",
             ("mur déjà dépassé", "mur tout proche", "mur proche", "mur loin", "pas de mur")),
            ("taille_mur", "taille du mur", tuple(size.values()))):
        lines += [f"#### TEST — {title}", "",
                  _table(excess.summarize_retests(test, key=key, order=order)), ""]
    return lines


def retests(ev: pd.DataFrame) -> pd.DataFrame:
    if ev.empty or "retest" not in ev:
        return pd.DataFrame()
    return pd.DataFrame([r for r in ev["retest"] if isinstance(r, dict)])


def _tick_days(symbol: str) -> set[str]:
    root = SETTINGS.data_dir / "import" / "ticks_full" / symbol
    hist = {p.stem for p in root.glob("*.parquet")} if root.exists() else set()
    return set(store.tick_days(symbol)) | hist


def _prev_close(symbol: str, day: str, all_days: list[str], cache: dict):
    """(prix du NQ au règlement 16h00 ET de la séance précédente, heure)."""
    prev = [d for d in all_days if d < day]
    if not prev:
        return None, None
    p = prev[-1]
    if p not in cache:
        cache.clear()
        cache[p] = session_path(symbol, p)
    ts, px = cache[p]
    st = dbgamma.settle_ts(p)
    j = int(np.searchsorted(ts, st, side="right")) - 1 if len(ts) else -1
    return (float(px[j]), st) if j >= 0 and st - ts[j] <= 900 else (None, None)


def gamma_context(raw: Path | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(définitions, statistiques) Databento pour la piste 2. La première
    lecture des fichiers bruts (plusieurs Go) est longue : seules les colonnes
    utiles sont gardées dans data/import/databento_derived/, relues tant
    qu'aucun fichier brut n'est plus récent."""
    raw = raw or SETTINGS.data_dir / "import" / "databento_raw"
    cache = SETTINGS.data_dir / "import" / "databento_derived"
    files = list(raw.glob("*.dbn.zst")) + list(raw.glob("*.dbn")) if raw.exists() else []
    newest = max((f.stat().st_mtime for f in files), default=0.0)
    cd, cs = cache / "nq_option_definitions.parquet", cache / "nq_option_statistics.parquet"
    if cd.exists() and cs.exists() and min(cd.stat().st_mtime, cs.stat().st_mtime) >= newest:
        return pd.read_parquet(cd), pd.read_parquet(cs)
    print("Piste 2 : lecture des fichiers bruts Databento (une seule fois, plusieurs minutes)…")
    defs, stats = dbgamma.load_raw(raw)
    if not defs.empty and not stats.empty:
        cache.mkdir(parents=True, exist_ok=True)
        defs.to_parquet(cd, index=False)
        stats.to_parquet(cs, index=False)
    return defs, stats


def load_sessions(symbol: str, use_old: bool = True, flux: str = "live",
                  start: str | None = None, end: str | None = None,
                  gamma: bool = False) -> tuple[list[dict], float | None]:
    if flux != "live":
        # tape reconstruit : une séance non étiquetée l'est pour un sous-jacent
        # incertain (roll), pas pour une question d'échelle — jamais utilisée
        use_old = False
    tdays = tape_days(symbol, flux)
    # une séance lit le tape de son jour ET de la veille (soirée Globex)
    wanted = {d for d in _tick_days(symbol)
              if d in tdays
              or (date.fromisoformat(d) - timedelta(days=1)).isoformat() in tdays}
    excluded = EXCLUDED_DAYS if flux == "live" else set()
    defs = stats = None
    if gamma:
        defs, stats = gamma_context()
        if defs.empty or stats.empty:
            raise SystemExit("Piste 2 : il faut les fichiers Databento `definition` ET "
                             "`statistics` dans data/import/databento_raw/")
    all_days = sorted(d for d in _tick_days(symbol)
                      if date.fromisoformat(d).weekday() < 5)
    prev_cache: dict = {}
    out = []
    for day in sorted(wanted - excluded):
        if date.fromisoformat(day).weekday() >= 5 or (start and day < start) \
                or (end and day > end):
            continue
        px_ts, px = session_path(symbol, day)
        if len(px) < 1000:
            continue
        flow = session_flow(symbol, day, px_ts, px, flux)
        if flow.empty or (not use_old and not flow["labeled"].any()):
            continue
        fine = None
        if flux == "databento":
            fine = session_flow(symbol, day, px_ts, px, "databento_1s")
            if not fine.empty:
                fine = fine[fine["labeled"].astype(bool)].assign(ok=True, grp="x1")
        chain = None
        if gamma:
            close, close_ts = _prev_close(symbol, day, all_days, prev_cache)
            chain = dbgamma.session_chain(stats, defs, day, close, close_ts,
                                          rates.current_rate())
        out.append({"day": day, "px_ts": px_ts, "px": px, "flow": flow, "fine": fine,
                    "chain": chain, "em": _straddle_em(symbol, day)})
    threshold = calibrate(out)
    for s in out:
        s["flow"] = apply_scale(s["flow"], threshold, use_old)
    if not use_old:
        out = [s for s in out if s["flow"]["labeled"].any()]
    return out, threshold


def run(sessions: list[dict], variant: str, excess_pts: float, swing_pts: float,
        horizon: int) -> tuple[pd.DataFrame, dict]:
    rows, info = [], {}
    if variant == "A":
        for s in sessions:
            rows += session_events(s["day"], s["px_ts"], s["px"], s["flow"], excess_pts,
                                   swing_pts, horizon, s.get("fine"), s.get("chain"))
        return pd.DataFrame(rows), info
    ems = [s["em"] for s in sessions if s["em"]]
    if not ems:
        return pd.DataFrame(), {"erreur": "aucun EM disponible"}
    med = float(np.median(ems))
    a, b = excess_pts / med, swing_pts / med
    info = {"em_median": med, "a": a, "b": b, "seances_sans_em": len(sessions) - len(ems)}
    for s in sessions:
        if s["em"]:
            rows += session_events(s["day"], s["px_ts"], s["px"], s["flow"], a * s["em"],
                                   b * s["em"], horizon, s.get("fine"))
    return pd.DataFrame(rows), info


def _table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_(aucun excès)_"
    return "```\n" + df.round(1).to_string() + "\n```"


def scale_table(sessions: list[dict]) -> pd.DataFrame:
    rows = []
    for s in sessions:
        f = s["flow"]
        lab, old = f["labeled"].astype(bool), ~f["labeled"].astype(bool)
        u = f["usable"].astype(bool)
        rows.append({"seance": s["day"], "barres": len(f), "verifiees": int(lab.sum()),
                     "delta_moy_verif": f.loc[lab & u, "implied"].median(),
                     "delta_moy_anciennes": f.loc[old & u, "implied"].median(),
                     "anciennes_x1": int((old & (f["grp"] == "x1")).sum()),
                     "anciennes_x5": int((old & (f["grp"] == "x5")).sum()),
                     "anciennes_inconnues": int((old & (f["grp"] == "")).sum())})
    return pd.DataFrame(rows)


def _block(ev: pd.DataFrame) -> list[str]:
    """Tables au franchissement puis au second test, pour un jeu d'excès."""
    if ev.empty:
        return ["_(aucun excès)_", ""]
    lines = []
    for seg in ("tout", "cash", "globex"):
        sub = ev if seg == "tout" else ev[ev["segment"] == seg]
        lines += [f"#### Au franchissement — {seg}", "", _table(excess.summarize(sub)), ""]
    lines += ["#### Au franchissement — gris détaillé", "",
              _table(excess.summarize(ev[ev["couleur"] == "gris"], key="nuance", order=())),
              ""]
    for sens in ("baissier", "haussier"):
        lines += [f"#### Au franchissement — excès {sens} "
                  f"(fade = {'achat' if sens == 'baissier' else 'vente'})", "",
                  _table(excess.summarize(ev[ev["sens"] == sens])), ""]
    return lines


def stability(rt: pd.DataFrame, key: str = "couleur", order=excess.COLORS) -> pd.DataFrame:
    """Au second test, par trimestre : nombre de cas et part des
    continuations d'au moins 50 pts, par couleur — un effet réel doit se
    retrouver d'un trimestre à l'autre, pas seulement en moyenne."""
    if rt.empty:
        return pd.DataFrame()
    q = pd.to_datetime(rt["day"]).dt.to_period("Q").astype(str)
    g = rt.assign(trimestre=q, c50=100 * (rt["continuation"] >= 50))
    n = g.pivot_table(index="trimestre", columns=key, values="c50", aggfunc="size")
    pct = g.pivot_table(index="trimestre", columns=key, values="c50", aggfunc="mean")
    cols = [c for c in order if c in n.columns] or list(n.columns)
    return pd.concat({"n": n[cols], "cont>=50_%": pct[cols]}, axis=1)


def _retest_block(rt: pd.DataFrame) -> list[str]:
    if rt.empty:
        return ["_(aucun second test)_", ""]
    lines = []
    for seg in ("tout", "cash", "globex"):
        sub = rt if seg == "tout" else rt[rt["segment"] == seg]
        lines += [f"#### Second test — {seg}", "", _table(excess.summarize_retests(sub)), ""]
    lines += ["#### Second test — gris détaillé", "",
              _table(excess.summarize_retests(rt[rt["couleur"] == "gris"], key="nuance",
                                              order=())), ""]
    for sens in ("baissier", "haussier"):
        lines += [f"#### Second test — excès {sens} "
                  f"(fade = {'achat' if sens == 'baissier' else 'vente'})", "",
                  _table(excess.summarize_retests(rt[rt["sens"] == sens])), ""]
    if rt["day"].nunique() >= 40:
        lines += ["#### Second test — stabilité par trimestre", "", _table(stability(rt)), "",
                  "Fade vendeur seulement (excès haussier) :", "",
                  _table(stability(rt[rt["sens"] == "haussier"])), ""]
    return lines


def report(symbol: str, excess_pts: float, swing_pts: float, horizon: int,
           use_old: bool = True, flux: str = "live", start: str | None = None,
           end: str | None = None, split_day: str | None = None,
           gamma: bool = False) -> str:
    sessions, threshold = load_sessions(symbol, use_old, flux, start, end, gamma)
    lines = [f"# Excès et soutien des market makers — {symbol}"
             + (" (tape reconstruit Databento)" if flux == "databento" else ""),
             f"_{datetime.now(ET):%Y-%m-%d %H:%M} — prix tick par tick ; excès {excess_pts:g} "
             f"pts depuis le dernier swing (swing confirmé à {swing_pts:g} pts), horizon "
             f"{horizon} min. Points NQ ; MAE = excursion adverse maximale ; retour_k = le prix "
             "revient de k pts en faveur du fade ; délais en minutes réelles._", "",
             f"Séances : {len(sessions)}"
             + (f" ({sessions[0]['day']} → {sessions[-1]['day']})" if sessions else ""), ""]
    if not sessions:
        return "\n".join(lines + ["_(aucune séance avec ticks et tape)_"])
    if flux == "live":
        lines += ["## Contrôle d'échelle du tape", "",
                  "Delta moyen implicite = brut / (contrats × prix NQ × 20 $) : juste, il reste "
                  "sous 1 ; valorisé ×5, il est 5 fois plus grand. Seuil calé sur les barres "
                  "vérifiées : "
                  + (f"**{threshold:.2f}**" if threshold else "_aucune barre vérifiée_")
                  + ". Les anciennes barres ne servent que dans une fenêtre de 15 min "
                  "d'échelle commune.", "",
                  _table(scale_table(sessions).set_index("seance")), ""]
    lines += ["## Équivalence de tes points en EM", "",
              "```\n" + pd.DataFrame(
                  [{"jour": s["day"], "EM_pts": s["em"],
                    f"{excess_pts:g}pts_en_EM": excess_pts / s["em"] if s["em"] else np.nan}
                   for s in sessions]).round(2).to_string(index=False) + "\n```", ""]
    ev, _ = run(sessions, "A", excess_pts, swing_pts, horizon)
    rt = retests(ev)
    lines += ["## Variante A — en points", "",
              f"Excès : {len(ev)} · suivis d'un second test (réaction d'au moins "
              f"{REACTION_PTS:g} pts puis retour à moins de {RETEST_PTS:g} pts de l'extrême) : "
              f"{len(rt)}. Au second test : `cont_*` = dépassement de l'extrême précédent ; "
              f"`rempli_%` = ordre à {ENTRY_OFFSET:g} pts de l'extrême touché ; retours et MAE "
              "depuis ce prix d'entrée, ordres remplis seulement.", ""]
    if flux != "live":                 # une seule source : le tape reconstruit
        lines += ["### Flux reconstruit (Databento)", ""] + _block(ev) + _retest_block(rt)
    for src in (SOURCES + ("ensemble",)) if flux == "live" else ():
        e_sub = ev if src == "ensemble" or ev.empty else ev[ev["flux"] == src]
        r_sub = rt if src == "ensemble" or rt.empty else rt[rt["flux"] == src]
        title = {"vérifié": "Flux vérifié (référence)",
                 "delta moyen": "Flux des anciennes séances (échelle par le delta moyen)",
                 "ensemble": "Ensemble (à n'additionner que si les deux blocs concordent)"}[src]
        lines += [f"### {title}", ""] + _block(e_sub) + _retest_block(r_sub)
    split = split_day or sessions[len(sessions) // 2]["day"]
    lines += ["## Piste 1 — le soutien a-t-il faibli entre l'excès et le second test ?", ""]
    lines += support_analysis(rt if flux != "live" or rt.empty
                              else rt[rt["flux"] == "vérifié"], split)
    if gamma:
        lines += ["## Piste 2 — positionnement gamma au second test", ""]
        lines += gamma_analysis(rt, split, sessions)
    evb, info = run(sessions, "B", excess_pts, swing_pts, horizon)
    lines += ["## Variante B — en EM (ensemble, au franchissement)", ""]
    if info.get("erreur"):
        lines += [f"_{info['erreur']}_", ""]
    else:
        lines += [f"EM médian {info['em_median']:.1f} pts : excès = {info['a']:.2f} EM, "
                  f"swing = {info['b']:.2f} EM ; séances sans EM (écartées) : "
                  f"{info['seances_sans_em']}", "", _table(excess.summarize(evb)), ""]
    lines += ["Lecture : au SECOND TEST, le voyant n'est utile que si le ROUGE montre une "
              "continuation nettement plus longue (cont_p90, cont>=50_%) que le VERT. "
              + ("Comparer d'abord le bloc « vérifié » et le bloc « anciennes séances » : s'ils "
                 "divergent, le bloc « ensemble » ne vaut rien. " if flux == "live" else "")
              + "Regarder n et les intervalles à 95 % : avec quelques dizaines de cas, seuls "
              "de gros écarts comptent."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbols", nargs="*", default=["NQ"])
    ap.add_argument("--excess", type=float, default=100.0, help="excès en points")
    ap.add_argument("--swing", type=float, default=60.0, help="retournement qui confirme un swing")
    ap.add_argument("--horizon", type=int, default=120, help="minutes suivies après l'excès")
    ap.add_argument("--verifiees-seulement", action="store_true",
                    help="ignorer les anciennes barres de tape non vérifiées")
    ap.add_argument("--flux", choices=sorted(TAPE_ROOTS), default="live",
                    help="tape lu par le voyant : live (défaut) ou databento (reconstruit)")
    ap.add_argument("--start", help="première séance (AAAA-MM-JJ)")
    ap.add_argument("--end", help="dernière séance (AAAA-MM-JJ)")
    ap.add_argument("--split", help="première séance de TEST pour les pistes 1 et 2 "
                                    "(défaut : séance du milieu)")
    ap.add_argument("--gamma", action="store_true",
                    help="piste 2 : positionnement gamma (fichiers Databento `statistics`)")
    args = ap.parse_args(argv)
    outdir = SETTINGS.data_dir / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    for sym in args.symbols:
        md = report(sym.upper(), args.excess, args.swing, args.horizon,
                    use_old=not args.verifiees_seulement, flux=args.flux,
                    start=args.start, end=args.end, split_day=args.split,
                    gamma=args.gamma)
        tag = ("_databento" if args.flux == "databento" else "") \
            + (f"_{args.start or 'debut'}_{args.end or 'fin'}" if args.start or args.end else "")
        path = outdir / f"exces_{sym.upper()}{tag}_{datetime.now(ET):%Y-%m-%d}.md"
        path.write_text(md, encoding="utf-8")
        print(md)
        print(f"\n→ {path}\n")


if __name__ == "__main__":
    main()
