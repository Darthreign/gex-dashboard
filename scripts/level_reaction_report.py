"""Un niveau GEX est-il traversé, jusqu'où va le prix, et se retrace-t-il ?
Selon le signe du GEX du niveau.

    python scripts/level_reaction_report.py
    python scripts/level_reaction_report.py --start 2026-01-01 --perimetre 0dte

Pour chaque séance de l'année : les 5 plus gros strikes en |GEX net| (chaîne
reconstruite depuis l'open interest Databento, comme la piste 2 du rapport
d'excès), chacun avec le signe de son GEX. Un niveau GEX est une ZONE, pas une
ligne : ± 5 pts autour du strike (`--zone`). Premier contact du prix avec la
zone, puis, SANS limite de temps (jusqu'à 16h00 ET) : traversée (le prix sort
par le côté opposé) ou rejet (le prix recule de 20 pts, `--retracement`, depuis
son point le plus profond dans la zone) ; après une traversée, l'expédition
maximale au-delà de la zone jusqu'au premier retracement de 20 pts, et la
profondeur de ce retracement (idem après un rejet, dans l'autre sens). Voir
gex/levelreact.py.

Les mêmes mesures sont faites sur des niveaux TÉMOINS (les vrais niveaux
décalés de ±30 / ±40 pts au moins) : c'est ce qui dit si le niveau compte, ou si
c'est le marché qui fait pareil partout. Repère : une marche aléatoire traverse
environ 61 % du temps (± 5 pts, retracement de 20 pts).

Aucun seuil n'est calé sur les données : toutes les séances sont lues. Les
intervalles et p-values tirent des SÉANCES entières (les niveaux d'une même
séance ne sont pas indépendants), avec correction de Holm sur l'ensemble des
comparaisons. Lecture seule ; rapport dans
data/reports/level_reaction_<SYMBOLE>_<date>.md.

Il faut les fichiers Databento `definition` + `statistics` (cache du rapport
d'excès) et les ticks du future (live ou import/ticks_full).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gex import dbgamma, levelreact, rates  # noqa: E402
from gex.config import SETTINGS  # noqa: E402
from gex.metrics import ET  # noqa: E402

import excess_report  # noqa: E402

SYMBOL = "NQ"


def window_bounds(day: str, fenetre: str) -> tuple[float, float]:
    """(début, fin) de la fenêtre étudiée, epoch s : séance cash 9h30-16h00 ET
    du jour `day`, ou séance Globex complète (18h00 ET la veille -> 17h00)."""
    d = date.fromisoformat(day)
    if fenetre == "globex":
        start = float(dbgamma.session_start(day).timestamp())
        end = datetime.combine(d, time(17, 0), tzinfo=ET).timestamp()
    else:
        start = datetime.combine(d, time(9, 30), tzinfo=ET).timestamp()
        end = datetime.combine(d, time(16, 0), tzinfo=ET).timestamp()
    return start, end


def collect(symbol: str, start: str | None, end: str | None, perimetre: str, fenetre: str,
            n_levels: int, zones: tuple[float, ...] = (levelreact.ZONE_PTS,),
            retrace: float = levelreact.RETRACE_PTS) -> pd.DataFrame:
    defs, stats = excess_report.gamma_context()
    if defs.empty or stats.empty:
        raise SystemExit("Il faut les fichiers Databento `definition` ET `statistics` "
                         "dans data/import/databento_raw/")
    all_days = sorted(d for d in excess_report._tick_days(symbol)
                      if date.fromisoformat(d).weekday() < 5)
    rate, cache, rows, skipped = rates.current_rate(), {}, [], 0
    for day in all_days:
        if (start and day < start) or (end and day > end):
            continue
        ts, px = excess_report.session_path(symbol, day)
        if len(px) < 1000:
            continue
        w0, w1 = window_bounds(day, fenetre)
        keep = (ts >= w0) & (ts < w1)
        if keep.sum() < 500:
            continue
        close, close_ts = excess_report._prev_close(symbol, day, all_days, cache)
        if close is None:
            skipped += 1
            continue
        chain = dbgamma.session_chain(stats, defs, day, close, close_ts, rate)
        levels = levelreact.session_levels(chain, w0, close, rate, perimetre, n_levels)
        if levels.empty:
            skipped += 1
            continue
        for z in zones:
            rows += levelreact.session_events(day, ts[keep], px[keep], levels, z, retrace, w1)
    if skipped:
        print(f"{skipped} séance(s) sans clôture de la veille ou sans chaîne : écartées.")
    return pd.DataFrame(rows)


def _table(df: pd.DataFrame) -> str:
    return "```\n" + df.round(1).to_string() + "\n```"


def report(ev: pd.DataFrame, symbol: str, perimetre: str, fenetre: str, n_boot: int) -> str:
    """Un bloc par largeur de zone ; la première (celle de `--zone` donnée en
    premier) est la zone PRINCIPALE, les suivantes une vérification de
    sensibilité (chacune avec sa propre correction de Holm : choisir après coup
    la zone qui arrange invaliderait les p-values)."""
    if ev.empty:
        return _report_zone(ev, symbol, perimetre, fenetre, n_boot,
                            levelreact.ZONE_PTS, levelreact.RETRACE_PTS, True)
    zones = list(ev["zone"].unique())                    # ordre de `--zone`
    return "\n\n".join(
        _report_zone(ev[ev["zone"] == z], symbol, perimetre, fenetre, n_boot, z,
                     float(ev["retrace"].iloc[0]), k == 0) for k, z in enumerate(zones))


def _report_zone(ev: pd.DataFrame, symbol: str, perimetre: str, fenetre: str, n_boot: int,
                 zone: float, retrace: float, principal: bool) -> str:
    titre = (f"# Niveaux GEX : traversée, expédition et retracement — {symbol}" if principal
             else f"# Sensibilité : zone de ± {zone:g} pts")
    lines = [titre, f"_{datetime.now(ET):%Y-%m-%d %H:%M}_", ""]
    if ev.empty:
        return "\n".join(lines + ["_(aucune séance exploitable)_"])
    real = ev[ev["groupe"] != "témoin"]
    base = 100 * levelreact.baseline_crossing(zone, retrace)
    lines += [
        f"{ev['day'].nunique()} séances, {len(real)} niveaux "
        f"({(real['groupe'] == 'positif').sum()} positifs, {(real['groupe'] == 'négatif').sum()} "
        f"négatifs). Périmètre des options : `{perimetre}` ; fenêtre : "
        f"{'séance cash 9h30-16h00 ET' if fenetre == 'cash' else 'séance Globex'}.", "",
        "Niveaux = les 5 strikes de plus fort |GEX net| à l'ouverture de la fenêtre (calls +, "
        "puts −, évalué à la clôture de la veille). Un niveau est une **zone** de "
        f"± {zone:g} pts autour du strike. **Contact** : le prix entre dans la zone (les contacts "
        "des 30 dernières minutes sont écartés). **Traversée** : le prix atteint le bord "
        f"opposé. **Rejet** : le prix recule de {retrace:g} pts depuis son point le plus profond "
        "dans la zone sans l'avoir traversée. Aucune limite de temps : on suit jusqu'à 16h00. "
        "**Expédition** : distance maximale au-delà de la zone (bord de sortie après une "
        "traversée, bord d'entrée après un rejet) jusqu'au premier retracement de "
        f"{retrace:g} pts ; **retracement** : la profondeur de ce premier recul "
        "(`retracement_%` = part des cas où il survient avant 16h00 ; `retr_méd` = sa profondeur "
        "médiane ; `retour_zone_%` = part où il revient jusqu'au bord de la zone). "
        "**Témoin** : les vrais niveaux décalés d'au moins ±30 / ±40 pts (zones de même "
        f"largeur, jamais chevauchantes). **Repère** : une marche aléatoire traverserait "
        f"environ {base:.0f} % du temps.",
        ("" if principal else "_Vérification de sensibilité : la zone principale est celle du "
         "premier bloc. Ne pas retenir la zone qui arrange : les p-values supposent une zone "
         "choisie d'avance._"), ""]
    res = levelreact.analyse(ev, n_boot)
    lines += ["## Traversée ou rejet", "", _table(res["resume"]), "",
              "## Après une traversée (distances en points)", "",
              _table(res["apres_traversee"]), "",
              "## Après un rejet (expédition dans l'autre sens, depuis le bord d'entrée)", "",
              _table(res["apres_rejet"]), ""]
    tests = res["tests"]
    if not tests.empty:
        shown = tests.copy()
        shown["p"] = shown["p"].map(lambda x: f"{x:.2g}")
        shown["p_holm"] = shown["p_holm"].map(lambda x: f"{x:.2g}")
        lines += ["## Comparaisons (séances rééchantillonnées, correction de Holm)", "",
                  f"{len(shown)} comparaisons. `écart` : différence A − B, en points de "
                  "pourcentage (taux de traversée) ou en points (expéditions). `tient` : "
                  "significatif même après correction ; `fragile` : significatif seul, pas "
                  "après ; `pas démontré` : compatible avec le hasard.", "",
                  _table(shown.set_index(["mesure", "comparaison"])), ""]
    lines += ["Lecture : « seuls les niveaux positifs font réagir, les négatifs sont des "
              "tremplins » serait étayé si les niveaux positifs sont moins traversés que les "
              "négatifs (positif − négatif < 0, verdict `tient`) et/ou si l'expédition après "
              "traversée est plus longue pour les négatifs. Si positif et négatif ne se "
              "distinguent pas du témoin, ce sont les niveaux GEX eux-mêmes qui n'apportent "
              "rien au-delà du bruit du marché."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--perimetre", default="tdb", choices=("tdb", "tdb2", "0dte", "45j"),
                    help="options retenues (défaut tdb : celles du tableau de bord)")
    ap.add_argument("--fenetre", default="cash", choices=("cash", "globex"))
    ap.add_argument("--niveaux", type=int, default=5)
    ap.add_argument("--retracement", type=float, default=levelreact.RETRACE_PTS,
                    help="recul minimal (pts) pour parler de rejet / de retracement "
                         "(défaut 20)")
    ap.add_argument("--zone", type=float, nargs="+", default=[levelreact.ZONE_PTS],
                    help="demi-largeur(s) de la zone d'un niveau, en points (défaut 5 ; "
                         "plusieurs valeurs : la première est la principale, les autres une "
                         "vérification de sensibilité)")
    ap.add_argument("--boot", type=int, default=2000, help="tirages de séances")
    args = ap.parse_args(argv)
    ev = collect(args.symbol, args.start, args.end, args.perimetre, args.fenetre, args.niveaux,
                 tuple(args.zone), args.retracement)
    md = report(ev, args.symbol, args.perimetre, args.fenetre, args.boot)
    outdir = SETTINGS.data_dir / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"level_reaction_{args.symbol}_{datetime.now(ET):%Y-%m-%d}.md"
    path.write_text(md, encoding="utf-8")
    print(md)
    print(f"\n→ {path}")


if __name__ == "__main__":
    main()
