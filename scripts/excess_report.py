"""Excès depuis le dernier swing : les market makers les soutiennent-ils ?

    python scripts/excess_report.py                    # NQ, 100 / 60 points
    python scripts/excess_report.py --excess 100 --swing 60 --horizon 120
    python scripts/excess_report.py --verifiees-seulement

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
6. le gris découpé selon la force du flux (descriptif, seuil inchangé).

Source du flux, présentée À PART :
- « vérifié » : barres étiquetées (`mult_source` : correctif ou capture
  postérieure) ;
- « delta moyen » : anciennes barres non vérifiables (pas de prints bruts),
  utilisées seulement si toute la fenêtre de 15 min a la MÊME échelle d'après
  le delta moyen implicite (excess.scale_classes, seuil calé sur les barres
  vérifiées) — le voyant ne lit que des rapports net / brut, justes à échelle
  commune ; sinon « sans flux ».

Lecture seule ; rapport dans data/reports/exces_<SYM>_<date>.md. Peu de
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

from gex import excess, store  # noqa: E402
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


def session_flow(symbol: str, day: str, px_ts: np.ndarray, px: np.ndarray) -> pd.DataFrame:
    """Barres de tape de la séance : epoch de début de minute, net, brut,
    `labeled` (multiplicateur vérifié), et delta moyen implicite."""
    cols = ["ts", "net", "gross", "labeled", "implied", "usable"]
    start, end = _bounds(day)
    parts = [store.load_tape(symbol, d) for d in
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
    return pd.DataFrame({"ts": epoch, "net": tape[HEDGE].sum(axis=1).to_numpy(),
                         "gross": gross, "labeled": labeled, "implied": implied,
                         "usable": usable})


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
                   excess_pts: float, swing_pts: float, horizon: int) -> list[dict]:
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
                             **excess.retest_outcome(px, px, t, rt["extreme"], -e["dir"],
                                                     ENTRY_OFFSET, horizon, ts=px_ts)}
        rows.append(row)
    return rows


def retests(ev: pd.DataFrame) -> pd.DataFrame:
    if ev.empty or "retest" not in ev:
        return pd.DataFrame()
    return pd.DataFrame([r for r in ev["retest"] if isinstance(r, dict)])


def _tick_days(symbol: str) -> set[str]:
    root = SETTINGS.data_dir / "import" / "ticks_full" / symbol
    hist = {p.stem for p in root.glob("*.parquet")} if root.exists() else set()
    return set(store.tick_days(symbol)) | hist


def load_sessions(symbol: str, use_old: bool = True) -> tuple[list[dict], float | None]:
    tape_days = set(store.tape_days(symbol))
    # une séance lit le tape de son jour ET de la veille (soirée Globex)
    wanted = {d for d in _tick_days(symbol)
              if d in tape_days
              or (date.fromisoformat(d) - timedelta(days=1)).isoformat() in tape_days}
    out = []
    for day in sorted(wanted - EXCLUDED_DAYS):
        if date.fromisoformat(day).weekday() >= 5:
            continue
        px_ts, px = session_path(symbol, day)
        if len(px) < 1000:
            continue
        flow = session_flow(symbol, day, px_ts, px)
        if flow.empty or (not use_old and not flow["labeled"].any()):
            continue
        out.append({"day": day, "px_ts": px_ts, "px": px, "flow": flow,
                    "em": _straddle_em(symbol, day)})
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
                                   swing_pts, horizon)
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
                                   b * s["em"], horizon)
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
    return lines


def report(symbol: str, excess_pts: float, swing_pts: float, horizon: int,
           use_old: bool = True) -> str:
    sessions, threshold = load_sessions(symbol, use_old)
    lines = [f"# Excès et soutien des market makers — {symbol}",
             f"_{datetime.now(ET):%Y-%m-%d %H:%M} — prix tick par tick ; excès {excess_pts:g} "
             f"pts depuis le dernier swing (swing confirmé à {swing_pts:g} pts), horizon "
             f"{horizon} min. Points NQ ; MAE = excursion adverse maximale ; retour_k = le prix "
             "revient de k pts en faveur du fade ; délais en minutes réelles._", "",
             f"Séances : {len(sessions)}"
             + (f" ({sessions[0]['day']} → {sessions[-1]['day']})" if sessions else ""), ""]
    if not sessions:
        return "\n".join(lines + ["_(aucune séance avec ticks et tape)_"])
    lines += ["## Contrôle d'échelle du tape", "",
              "Delta moyen implicite = brut / (contrats × prix NQ × 20 $) : juste, il reste sous "
              "1 ; valorisé ×5, il est 5 fois plus grand. Seuil calé sur les barres vérifiées : "
              + (f"**{threshold:.2f}**" if threshold else "_aucune barre vérifiée_")
              + ". Les anciennes barres ne servent que dans une fenêtre de 15 min d'échelle "
              "commune.", "", _table(scale_table(sessions).set_index("seance")), ""]
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
    for src in SOURCES + ("ensemble",):
        e_sub = ev if src == "ensemble" or ev.empty else ev[ev["flux"] == src]
        r_sub = rt if src == "ensemble" or rt.empty else rt[rt["flux"] == src]
        title = {"vérifié": "Flux vérifié (référence)",
                 "delta moyen": "Flux des anciennes séances (échelle par le delta moyen)",
                 "ensemble": "Ensemble (à n'additionner que si les deux blocs concordent)"}[src]
        lines += [f"### {title}", ""] + _block(e_sub) + _retest_block(r_sub)
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
              "Comparer d'abord le bloc « vérifié » et le bloc « anciennes séances » : s'ils "
              "divergent, le bloc « ensemble » ne vaut rien. Regarder n et les intervalles à "
              "95 % : avec quelques dizaines de cas, seuls de gros écarts comptent."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbols", nargs="*", default=["NQ"])
    ap.add_argument("--excess", type=float, default=100.0, help="excès en points")
    ap.add_argument("--swing", type=float, default=60.0, help="retournement qui confirme un swing")
    ap.add_argument("--horizon", type=int, default=120, help="minutes suivies après l'excès")
    ap.add_argument("--verifiees-seulement", action="store_true",
                    help="ignorer les anciennes barres de tape non vérifiées")
    args = ap.parse_args(argv)
    outdir = SETTINGS.data_dir / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    for sym in args.symbols:
        md = report(sym.upper(), args.excess, args.swing, args.horizon,
                    use_old=not args.verifiees_seulement)
        path = outdir / f"exces_{sym.upper()}_{datetime.now(ET):%Y-%m-%d}.md"
        path.write_text(md, encoding="utf-8")
        print(md)
        print(f"\n→ {path}\n")


if __name__ == "__main__":
    main()
