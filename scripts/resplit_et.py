"""Re-découpe les ticks par SÉANCE CME en heure de New York.

Constat : le dataset avait été découpé à la date de PARIS. Paris ne vaut ET+6
que lorsque les deux zones sont en heure d'été ensemble ; pendant les ~3
semaines/an où les bascules US et UE sont décalées (mi-mars, fin octobre),
l'écart tombe à 5 h et la séance était coupée une heure trop tard — 40 fichiers
portaient une queue appartenant à la séance suivante.

Règle correcte, appliquée ici : date de séance = (heure ET + 6 h).date(),
c'est-à-dire 18:00 ET (ouverture) -> 16:59 ET (clôture) du lendemain.

Sûreté : ne touche qu'aux jours réellement impactés (les fautifs et leurs
voisins qui échangent des lignes), écrit d'abord dans un dossier temporaire,
vérifie la conservation ligne à ligne, puis bascule en gardant une sauvegarde.
Aucune donnée n'est perdue : les lignes sont redistribuées, jamais filtrées.
"""
from __future__ import annotations

import datetime as dt
import shutil
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
DIR = Path(r"D:/Gex/data/import/ticks_full/NQ")
TMP = DIR.parent / "NQ__resplit_tmp"
BAK = DIR.parent / "NQ__backup_avant_ET"


def session_date(ts: pd.Series) -> pd.Series:
    et = pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert(ET)
    return (et + pd.Timedelta(hours=6)).dt.date.astype(str)


def main(apply: bool) -> None:
    files = sorted(DIR.glob("*.parquet"))
    print(f"fichiers : {len(files)}")

    # 1. Repérer les fichiers non conformes et les séances concernées.
    wrong: dict[str, set[str]] = {}
    for p in files:
        day = p.stem
        s = session_date(pd.read_parquet(p, columns=["ts"])["ts"])
        u = set(s.unique())
        if u != {day}:
            wrong[day] = u
    if not wrong:
        print("Tout est déjà conforme — rien à faire.")
        return
    print(f"fichiers non conformes : {len(wrong)}")

    # 2. Jours à reconstruire = fichiers fautifs + séances qui reçoivent leurs
    #    lignes + voisins immédiats (source possible de lignes entrantes).
    touched: set[str] = set()
    for day, sessions in wrong.items():
        touched.add(day)
        touched |= sessions
    span: set[str] = set()
    for d in touched:
        d0 = dt.date.fromisoformat(d)
        for k in (-1, 0, 1):
            span.add((d0 + dt.timedelta(days=k)).isoformat())
    rebuild = sorted(d for d in span if (DIR / f"{d}.parquet").exists())
    print(f"séances à reconstruire : {len(rebuild)}  ({rebuild[0]} -> {rebuild[-1]})")

    if not apply:
        print("\n[DRY-RUN] rien n'est écrit. Relancer avec --apply pour appliquer.")
        return

    # 3. Reconstruction en fenêtre glissante vers un dossier temporaire.
    TMP.mkdir(parents=True, exist_ok=True)
    cache: dict[str, pd.DataFrame] = {}

    def load(day: str) -> pd.DataFrame:
        if day not in cache:
            p = DIR / f"{day}.parquet"
            cache[day] = pd.read_parquet(p) if p.exists() else pd.DataFrame()
            if len(cache) > 8:            # fenêtre glissante : mémoire bornée
                cache.pop(next(iter(cache)))
        return cache[day]

    rows_before = rows_after = 0
    for day in rebuild:
        d0 = dt.date.fromisoformat(day)
        parts = [load((d0 + dt.timedelta(days=k)).isoformat()) for k in (-1, 0, 1)]
        parts = [x for x in parts if not x.empty]
        df = pd.concat(parts, ignore_index=True)
        keep = df[session_date(df["ts"]) == day].copy()
        # ordre de colonnes stable : socle ticks_full puis extras éventuels
        base = [c for c in ["ts", "price", "volume", "side", "source"] if c in keep.columns]
        extra = [c for c in keep.columns if c not in base]
        keep = keep[base + extra].sort_values("ts").reset_index(drop=True)
        keep.to_parquet(TMP / f"{day}.parquet", index=False)
        rows_before += len(load(day))
        rows_after += len(keep)
        print(f"  {day}: {len(load(day)):>7,} -> {len(keep):>7,}")

    # 4. Vérification : aucune ligne perdue sur la plage reconstruite.
    print(f"\nlignes avant {rows_before:,} | après {rows_after:,} | "
          f"écart {rows_after - rows_before:+,}")
    if rows_after != rows_before:
        print("⚠️  Écart de lignes sur la plage — c'est ATTENDU seulement si des "
              "lignes viennent de fichiers hors plage. Vérification globale :")
    # somme totale du dataset (plage reconstruite remplacée par le tmp)
    total_old = total_new = 0
    for p in files:
        n = len(pd.read_parquet(p, columns=["ts"]))
        total_old += n
        total_new += n if p.stem not in set(rebuild) else 0
    for d in rebuild:
        total_new += len(pd.read_parquet(TMP / f"{d}.parquet", columns=["ts"]))
    print(f"TOTAL dataset avant {total_old:,} | après {total_new:,} | "
          f"écart {total_new - total_old:+,}")
    if total_new != total_old:
        print("❌ Perte/duplication détectée — BASCULE ANNULÉE, tmp conservé.")
        sys.exit(1)

    # 5. Bascule : sauvegarde des originaux puis remplacement.
    BAK.mkdir(parents=True, exist_ok=True)
    for d in rebuild:
        shutil.copy2(DIR / f"{d}.parquet", BAK / f"{d}.parquet")
        shutil.move(str(TMP / f"{d}.parquet"), str(DIR / f"{d}.parquet"))
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n✅ {len(rebuild)} séances réécrites en ET. Sauvegarde : {BAK}")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
