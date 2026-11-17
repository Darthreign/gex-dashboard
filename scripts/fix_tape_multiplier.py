"""Barres de tape NQ/ES valorisées avec un mauvais multiplicateur : détection
et correction (défaut corrigé le 10/10/2026, cf. flowtape.multiplier_of).

    python scripts/fix_tape_multiplier.py              # NQ et ES, rapport seulement
    python scripts/fix_tape_multiplier.py NQ --apply   # corrige (sauvegarde d'abord)

Le défaut : tant que le cache du multiplicateur était vide (démarrage de la
capture, capture séparée sans moteur), les options sur futures étaient
valorisées à 100 $/point au lieu de 20 (NQ) ou 50 (ES). L'erreur est un pur
facteur multiplicatif (×5 sur NQ, ×2 sur ES) sur TOUTES les colonnes en
dollars d'une barre (prime, delta, gamma, pression de couverture) ; les
colonnes en contrats sont justes.

Méthode, minute par minute :
1. Les prints BRUTS enregistrés (optprints) portent, pour chaque transaction,
   taille, côté, delta et prix du sous-jacent : on en tire la pression de
   couverture brute Σ taille × |delta| × multiplicateur RÉEL × sous-jacent.
2. On la compare à celle de la barre sur disque (Σ |hedge_*|). Rapport ≈ 1 :
   barre juste ; ≈ 5 (NQ) ou ≈ 2 (ES) : barre fausse ; autre chose : on ne
   touche à rien.
3. Une barre fausse est divisée par le facteur exact : les prints servent à
   DÉTECTER l'erreur, pas à recalculer les montants (le prix du sous-jacent
   stocké avec le print n'est pas exactement celui utilisé à l'époque).

Une minute n'est jugée que si les prints bruts sont complets (même nombre de
prints valorisés que la barre) ; sinon elle est « indéterminée » et laissée
telle quelle. Les barres déjà étiquetées (colonne `mult_source`, écrite depuis
le correctif) ne sont pas touchées.

Sans --apply : rapport seulement (data/reports/tape_multiplicateur_<SYM>_<date>.md),
aucun fichier de données modifié. Avec --apply : chaque fichier corrigé est
d'abord copié dans data/backups/tape/<SYM>/.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gex import store  # noqa: E402
from gex.config import CONTRACT_MULTIPLIER, SETTINGS  # noqa: E402
from gex.flowtape import FUTURE_POINT_VALUE  # noqa: E402
from gex.metrics import ET  # noqa: E402

DOLLAR_COLS = ("net_premium", "net_delta", "net_gamma", "net_gamma_calls",
               "net_gamma_puts", "hedge_call_buy", "hedge_call_sell",
               "hedge_put_buy", "hedge_put_sell")
HEDGE_COLS = ("hedge_call_buy", "hedge_call_sell", "hedge_put_buy", "hedge_put_sell")
TOLERANCE = 0.15          # écart relatif admis (le sous-jacent a pu bouger un peu)
RAW_COLS = ["ts_recv", "size", "side", "spread", "delta", "und"]


def _raw_reference(symbol: str, day: str, mult: float) -> pd.DataFrame:
    """Par minute (heure ET naïve, comme les barres) : pression de couverture
    brute recalculée au multiplicateur réel, et nombre de prints valorisés."""
    d0 = datetime.fromisoformat(day)
    parts = [store.load_optprints(symbol, (d0 + timedelta(days=k)).strftime("%Y-%m-%d"),
                                  columns=RAW_COLS) for k in (-1, 0, 1)]
    raw = pd.concat([p for p in parts if not p.empty], ignore_index=True) \
        if any(not p.empty for p in parts) else pd.DataFrame()
    if raw.empty or not set(RAW_COLS) <= set(raw.columns):
        return pd.DataFrame(columns=["gross_ref", "n_ref"])
    # mêmes filtres que flowtape.ingest_print pour la partie valorisée en delta
    ok = (~raw["spread"].fillna(False).astype(bool)
          & raw["side"].isin(("BUY", "SELL"))
          & raw["delta"].notna() & raw["und"].notna())
    r = raw[ok]
    minute = pd.to_datetime((r["ts_recv"] // 60) * 60, unit="s", utc=True) \
        .dt.tz_convert(ET).dt.tz_localize(None)
    gross = r["size"] * r["delta"].abs() * mult * r["und"]
    return pd.DataFrame({"minute": minute, "gross": gross}).groupby("minute") \
        .agg(gross_ref=("gross", "sum"), n_ref=("gross", "size"))


def audit_day(symbol: str, day: str) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """(barres avec leur statut, barres corrigées) ou None sans fichier."""
    tape = store.load_tape(symbol, day)
    if tape.empty or not set(HEDGE_COLS) <= set(tape.columns):
        return None
    mult = FUTURE_POINT_VALUE[symbol]
    factor = CONTRACT_MULTIPLIER / mult
    ref = _raw_reference(symbol, day, mult)
    t = tape.copy()
    ts = pd.to_datetime(t["timestamp"])
    gross_disk = t[list(HEDGE_COLS)].abs().sum(axis=1)
    has_dollars = t[[c for c in DOLLAR_COLS if c in t]].abs().sum(axis=1) > 0
    tagged = t["mult_source"].notna() if "mult_source" in t else pd.Series(False, index=t.index)
    g_ref = ts.map(ref["gross_ref"]) if not ref.empty else pd.Series(np.nan, index=t.index)
    n_ref = ts.map(ref["n_ref"]) if not ref.empty else pd.Series(np.nan, index=t.index)
    n_disk = t["delta_prints"] if "delta_prints" in t else pd.Series(np.nan, index=t.index)

    status = []
    for i in t.index:
        if tagged[i]:
            status.append("étiquetée (après correctif)")
        elif not has_dollars[i]:
            status.append("sans montant")
        elif not (g_ref[i] > 0):
            status.append("indéterminée (pas de prints bruts)")
        elif n_ref[i] != n_disk[i]:
            status.append("indéterminée (prints bruts incomplets)")
        else:
            r = gross_disk[i] / g_ref[i]
            if abs(r - 1) <= TOLERANCE:
                status.append("juste")
            elif abs(r / factor - 1) <= TOLERANCE:
                status.append(f"fausse (×{factor:g})")
            else:
                status.append("incohérente")
    t["statut"] = status
    t["rapport"] = gross_disk / g_ref

    fixed = tape.copy()
    wrong = t["statut"].str.startswith("fausse")
    for c in DOLLAR_COLS:
        if c in fixed:
            fixed.loc[wrong, c] = fixed.loc[wrong, c] / factor
    if "mult" not in fixed:
        fixed["mult"] = np.nan
    if "mult_source" not in fixed:
        fixed["mult_source"] = None
    fixed["mult"] = fixed["mult"].astype(float)
    fixed["mult_source"] = fixed["mult_source"].astype(object)
    fixed.loc[wrong, "mult"] = mult
    fixed.loc[wrong, "mult_source"] = "corrige"
    good = t["statut"] == "juste"
    fixed.loc[good, "mult"] = mult
    fixed.loc[good, "mult_source"] = "verifie"
    return t, fixed


def run(symbol: str, apply: bool = False) -> pd.DataFrame:
    rows = []
    for day in store.tape_days(symbol):
        res = audit_day(symbol, day)
        if res is None:
            continue
        t, fixed = res
        counts = t["statut"].value_counts().to_dict()
        n_wrong = int(t["statut"].str.startswith("fausse").sum())
        n_good = int((t["statut"] == "juste").sum())
        rows.append({"jour": day, "barres": len(t), **counts})
        # écrire aussi une journée ENTIÈREMENT juste : sinon ses barres ne sont
        # jamais étiquetées « verifie » et restent inutilisables en aval
        if apply and (n_wrong or n_good):
            src = SETTINGS.data_dir / "tape" / symbol / f"{day}.parquet"
            dst = SETTINGS.data_dir / "backups" / "tape" / symbol / f"{day}.parquet"
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():                      # jamais écraser la sauvegarde d'origine
                shutil.copy2(src, dst)
            store._write_atomic(fixed, src)
    return pd.DataFrame(rows).fillna(0)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbols", nargs="*", default=sorted(FUTURE_POINT_VALUE))
    ap.add_argument("--apply", action="store_true",
                    help="corrige les barres fausses (sauvegarde dans data/backups/ d'abord)")
    args = ap.parse_args(argv)
    outdir = SETTINGS.data_dir / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    for sym in args.symbols:
        sym = sym.upper()
        if sym not in FUTURE_POINT_VALUE:
            print(f"{sym} : pas un future suivi, rien à vérifier")
            continue
        r = run(sym, apply=args.apply)
        mode = "CORRIGÉ (originaux sauvegardés dans data/backups/tape/)" if args.apply \
            else "rapport seulement, aucun fichier modifié"
        md = (f"# Multiplicateur du tape {sym} — {datetime.now(ET):%Y-%m-%d %H:%M}\n\n"
              f"Mode : {mode}\n\n"
              + ("```\n" + r.to_string(index=False) + "\n```\n" if not r.empty
                 else "_(aucune barre de tape)_\n"))
        path = outdir / f"tape_multiplicateur_{sym}_{datetime.now(ET):%Y-%m-%d}.md"
        path.write_text(md, encoding="utf-8")
        print(md)
        print(f"→ {path}")


if __name__ == "__main__":
    main()
