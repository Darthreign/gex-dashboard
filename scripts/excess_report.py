"""Excès depuis le dernier swing : les market makers les soutiennent-ils ?

    python scripts/excess_report.py                 # NQ, 100 / 60 points
    python scripts/excess_report.py --excess 100 --swing 60 --horizon 120

Pour chaque séance CME (18h00 la veille -> 17h00 ET) ayant des ticks NQ et
des barres de tape VÉRIFIÉES (colonne `mult_source` renseignée : correctif du
multiplicateur ou barre écrite après lui ; le 25/09/2026 est exclu) :

1. bougies 1 min depuis les ticks ;
2. excès (gex/excess.py) selon deux variantes :
   A. en points : excès 100 pts, swing 60 pts ;
   B. en EM : excès = a × EM du jour, swing = b × EM, avec a et b choisis
      pour que la séance MÉDIANE retombe sur 100 / 60 pts (EM = straddle du
      premier snapshot de la chaîne NQ du jour) ;
3. couleur du voyant (flux de couverture des 5 min précédentes contre les
   10 d'avant, seuil « franc » 0,35 du bandeau, inchangé) ;
4. excursion adverse et retours de 10 / 15 / 20 pts, sur 120 min au plus ;
   un excès qui laisse moins de 60 min de données est écarté ;
5. SECOND TEST (ton entrée, variante en points) : après l'excès, réaction
   d'au moins 15 pts puis retour à moins de 10 pts de l'extrême (dans les
   120 min) ; voyant lu à ce moment, continuation au-delà de l'extrême, et
   suivi d'un ordre à 3 pts de l'extrême s'il est touché ;
6. le gris découpé selon la force du flux (descriptif, seuil inchangé).

Séparé cash (9h30-16h00 ET) / Globex. Lecture seule ; rapport dans
data/reports/exces_<SYM>_<date>.md. Peu de séances : des tendances, pas une
preuve (voir les intervalles à 95 %).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gex import edge, excess, store  # noqa: E402
from gex.config import SETTINGS  # noqa: E402
from gex.edge_report import _epoch, _straddle_em  # noqa: E402
from gex.metrics import ET  # noqa: E402

EXCLUDED_DAYS = {"2026-09-25"}        # multiplicateur invérifiable ce jour-là
HEDGE = ["hedge_call_buy", "hedge_call_sell", "hedge_put_buy", "hedge_put_sell"]
MIN_BARS_AFTER = 60
REACTION_PTS, RETEST_PTS, ENTRY_OFFSET = 15.0, 10.0, 3.0
CASH = (9 * 60 + 30, 16 * 60)


def _bounds(day: str) -> tuple[datetime, datetime]:
    d = date.fromisoformat(day)
    return (datetime.combine(d - timedelta(days=1), datetime.min.time()) + timedelta(hours=18),
            datetime.combine(d, datetime.min.time()) + timedelta(hours=17))


def session_bars(symbol: str, day: str) -> pd.DataFrame:
    """Bougies 1 min de la séance CME `day` (le fichier de ticks est déjà
    rangé par séance, cf. tickcapture._session_day)."""
    ticks = store.load_ticks(symbol, day)
    if ticks.empty:
        return pd.DataFrame()
    return edge.minute_bars(ticks.sort_values("ts", kind="stable"))


def session_flow(symbol: str, day: str) -> pd.DataFrame:
    """Barres de tape de la séance : epoch de début de minute, net, brut, et
    `ok` (multiplicateur vérifié)."""
    start, end = _bounds(day)
    parts = [store.load_tape(symbol, d) for d in
             ((date.fromisoformat(day) - timedelta(days=1)).isoformat(), day)]
    tape = pd.concat([p for p in parts if not p.empty], ignore_index=True) \
        if any(not p.empty for p in parts) else pd.DataFrame()
    if tape.empty or not set(HEDGE) <= set(tape.columns):
        return pd.DataFrame(columns=["ts", "net", "gross", "ok"])
    ts = pd.to_datetime(tape["timestamp"])
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert(ET).dt.tz_localize(None)
    keep = ((ts >= start) & (ts < end)).to_numpy()
    tape, ts = tape[keep], ts[keep]
    loc = ts.dt.tz_localize(ET, ambiguous="NaT", nonexistent="NaT")
    tape, loc = tape[loc.notna().to_numpy()], loc[loc.notna()]
    epoch = _epoch(loc) if len(loc) else np.array([])
    ok = tape["mult_source"].notna().to_numpy() if "mult_source" in tape \
        else np.zeros(len(tape), bool)
    return pd.DataFrame({"ts": epoch, "net": tape[HEDGE].sum(axis=1).to_numpy(),
                         "gross": tape[HEDGE].abs().sum(axis=1).to_numpy(), "ok": ok})


def _segment(ts: float) -> str:
    t = datetime.fromtimestamp(ts, ET)
    m = t.hour * 60 + t.minute
    return "cash" if CASH[0] <= m < CASH[1] else "globex"


def session_events(day: str, bars: pd.DataFrame, flow: pd.DataFrame, excess_pts: float,
                   swing_pts: float, horizon: int) -> list[dict]:
    hi, lo = bars["high"].to_numpy(float), bars["low"].to_numpy(float)
    ts = bars["ts"].to_numpy(float)
    fts, fnet = flow["ts"].to_numpy(float), flow["net"].to_numpy(float)
    fgross, fok = flow["gross"].to_numpy(float), flow["ok"].to_numpy(bool)
    rows = []
    for e in excess.find_excesses(hi, lo, excess_pts, swing_pts):
        i = e["i"]
        if len(hi) - i - 1 < MIN_BARS_AFTER:
            continue
        color, det = excess.flow_color(fts, fnet, fgross, fok, ts[i], e["dir"])
        row = {"day": day, "ts": ts[i], "segment": _segment(ts[i]),
               "sens": "haussier" if e["dir"] > 0 else "baissier",
               "excess_pts": excess_pts, "couleur": color,
               "nuance": _nuance(color, det),
               **excess.outcome(hi, lo, i, e["level"], -e["dir"], horizon)}
        rt = excess.find_retest(hi, lo, i, e["dir"], REACTION_PTS, RETEST_PTS, horizon)
        if rt is not None and len(hi) - rt["t"] - 1 >= MIN_BARS_AFTER:
            t = rt["t"]
            c2, det2 = excess.flow_color(fts, fnet, fgross, fok, ts[t], e["dir"])
            row["retest"] = {"day": day, "ts": ts[t], "segment": _segment(ts[t]),
                             "sens": row["sens"], "couleur": c2, "nuance": _nuance(c2, det2),
                             "couleur_exces": color, "attente_min": t - i,
                             **excess.retest_outcome(hi, lo, t, rt["extreme"], -e["dir"],
                                                     ENTRY_OFFSET, horizon)}
        rows.append(row)
    return rows


def _nuance(color: str, detail: dict) -> str:
    return excess.gris_nuance(detail.get("support_ratio")) if color == "gris" else color


def retests(ev: pd.DataFrame) -> pd.DataFrame:
    if ev.empty or "retest" not in ev:
        return pd.DataFrame()
    return pd.DataFrame([r for r in ev["retest"] if isinstance(r, dict)])


def load_sessions(symbol: str) -> list[dict]:
    days = sorted(set(store.tick_days(symbol)) - EXCLUDED_DAYS)
    out = []
    for day in days:
        if date.fromisoformat(day).weekday() >= 5:
            continue
        flow = session_flow(symbol, day)
        if not flow["ok"].any():
            continue                       # aucune barre de tape vérifiée
        bars = session_bars(symbol, day)
        if len(bars) < 120:
            continue
        out.append({"day": day, "bars": bars, "flow": flow, "em": _straddle_em(symbol, day)})
    return out


def run(sessions: list[dict], variant: str, excess_pts: float, swing_pts: float,
        horizon: int) -> tuple[pd.DataFrame, dict]:
    rows, info = [], {}
    if variant == "A":
        for s in sessions:
            rows += session_events(s["day"], s["bars"], s["flow"], excess_pts, swing_pts,
                                   horizon)
        return pd.DataFrame(rows), info
    ems = [s["em"] for s in sessions if s["em"]]
    if not ems:
        return pd.DataFrame(), {"erreur": "aucun EM disponible"}
    med = float(np.median(ems))
    a, b = excess_pts / med, swing_pts / med
    info = {"em_median": med, "a": a, "b": b, "seances_sans_em": len(sessions) - len(ems)}
    for s in sessions:
        if s["em"]:
            rows += session_events(s["day"], s["bars"], s["flow"], a * s["em"],
                                   b * s["em"], horizon)
    return pd.DataFrame(rows), info


def _table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_(aucun excès)_"
    return "```\n" + df.round(1).to_string() + "\n```"


def report(symbol: str, excess_pts: float, swing_pts: float, horizon: int) -> str:
    sessions = load_sessions(symbol)
    lines = [f"# Excès et soutien des market makers — {symbol}",
             f"_{datetime.now(ET):%Y-%m-%d %H:%M} — excès {excess_pts:g} pts depuis le dernier "
             f"swing (swing confirmé à {swing_pts:g} pts), horizon {horizon} min. "
             "Points NQ ; MAE = excursion adverse maximale ; retour_k = le prix revient de "
             "k pts en faveur du fade._", "",
             f"Séances : {len(sessions)}"
             + (f" ({sessions[0]['day']} → {sessions[-1]['day']})" if sessions else ""), ""]
    if not sessions:
        return "\n".join(lines + ["_(aucune séance avec ticks et tape vérifié)_"])
    lines += ["## Équivalence de tes points en EM", "",
              "```\n" + pd.DataFrame(
                  [{"jour": s["day"], "EM_pts": s["em"],
                    f"{excess_pts:g}pts_en_EM": excess_pts / s["em"] if s["em"] else np.nan}
                   for s in sessions]).round(2).to_string(index=False) + "\n```", ""]
    for variant, title in (("A", "Variante A — en points"), ("B", "Variante B — en EM")):
        ev, info = run(sessions, variant, excess_pts, swing_pts, horizon)
        lines += [f"## {title}", ""]
        if info.get("erreur"):
            lines += [f"_{info['erreur']}_", ""]
            continue
        if info:
            lines += [f"EM médian {info['em_median']:.1f} pts : excès = {info['a']:.2f} EM, "
                      f"swing = {info['b']:.2f} EM ; séances sans EM (écartées) : "
                      f"{info['seances_sans_em']}", ""]
        if ev.empty:
            lines += ["_(aucun excès)_", ""]
            continue
        for seg in ("tout", "cash", "globex"):
            sub = ev if seg == "tout" else ev[ev["segment"] == seg]
            lines += [f"### {seg.capitalize()}", "", _table(excess.summarize(sub)), ""]
        if variant == "A":
            lines += ["### Le gris détaillé (descriptif, seuil 0,35 inchangé)", "",
                      "Force du flux des 5 dernières minutes, rapportée au brut : "
                      "`soutien` = dans le sens de l'excès, `contre` = opposé.", "",
                      _table(excess.summarize(ev[ev["couleur"] == "gris"], key="nuance",
                                              order=())), ""]
            rt = retests(ev)
            lines += ["## Second test — ton entrée (variante en points)", "",
                      f"Après l'excès : réaction d'au moins {REACTION_PTS:g} pts, puis retour à "
                      f"moins de {RETEST_PTS:g} pts de l'extrême. Voyant lu À CE MOMENT. "
                      "`cont_*` = jusqu'où le prix dépasse l'extrême précédent ; "
                      f"`rempli_%` = ordre à {ENTRY_OFFSET:g} pts de l'extrême touché ; "
                      "retours et MAE comptés depuis ce prix d'entrée, ordres remplis seulement.",
                      "", f"Excès suivis d'un second test : {len(rt)} sur {len(ev)}", ""]
            if rt.empty:
                lines += ["_(aucun second test)_", ""]
            else:
                for seg in ("tout", "cash", "globex"):
                    sub = rt if seg == "tout" else rt[rt["segment"] == seg]
                    lines += [f"### {seg.capitalize()}", "",
                              _table(excess.summarize_retests(sub)), ""]
                lines += ["### Gris détaillé au second test", "",
                          _table(excess.summarize_retests(rt[rt["couleur"] == "gris"],
                                                          key="nuance", order=())), "",
                          "### Par sens de l'excès", ""]
                for sens in ("baissier", "haussier"):
                    lines += [f"Excès {sens} (fade = "
                              f"{'achat' if sens == 'baissier' else 'vente'}) :", "",
                              _table(excess.summarize_retests(rt[rt["sens"] == sens])), ""]
            lines += ["## Variante A — par sens de l'excès (au franchissement)", ""]
        if variant != "A":
            lines += ["Par sens de l'excès (toutes séances) :", ""]
        for sens in ("baissier", "haussier"):
            lines += [f"Excès {sens} (fade = {'achat' if sens == 'baissier' else 'vente'}) :",
                      "", _table(excess.summarize(ev[ev["sens"] == sens])), ""]
    lines += ["Lecture : au SECOND TEST, le voyant n'est utile que si le ROUGE montre une "
              "continuation nettement plus longue (cont_p90, cont>=50_%) que le VERT. ",
              "Lecture (franchissement) : le voyant n'est utile que si le ROUGE montre une excursion adverse "
              "nettement plus longue (mae_p90, mae>=50_%) et moins de retours que le VERT. "
              "Regarder d'abord n et l'intervalle retour_10_ic95 : avec quelques dizaines "
              "d'excès, seuls de gros écarts comptent."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbols", nargs="*", default=["NQ"])
    ap.add_argument("--excess", type=float, default=100.0, help="excès en points")
    ap.add_argument("--swing", type=float, default=60.0, help="retournement qui confirme un swing")
    ap.add_argument("--horizon", type=int, default=120, help="minutes suivies après l'excès")
    args = ap.parse_args(argv)
    outdir = SETTINGS.data_dir / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    for sym in args.symbols:
        md = report(sym.upper(), args.excess, args.swing, args.horizon)
        path = outdir / f"exces_{sym.upper()}_{datetime.now(ET):%Y-%m-%d}.md"
        path.write_text(md, encoding="utf-8")
        print(md)
        print(f"\n→ {path}\n")


if __name__ == "__main__":
    main()
