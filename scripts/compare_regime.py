"""Régime gamma : le tableau de bord concorde-t-il avec la reconstruction
Databento utilisée par l'étude des excès (piste 2) ?

    python scripts/compare_regime.py

Pour chaque ligne NQ de l'historique du tableau de bord (data/history/
metrics.parquet : spot, GEX net, Gamma Flip, GEX 0DTE, à chaque collecte de la
chaîne native), on recalcule au même instant et au même prix le
positionnement reconstruit (gex/dbgamma.py, open interest Databento connu à
l'ouverture). Trois comparaisons :
1. signe du GEX net (tableau de bord : chaîne filtrée ~3 échéances ± 8 % ;
   reconstruction : toutes échéances <= 45 jours, strikes ± 10 %) ;
2. côté du Gamma Flip (prix au-dessus / en dessous) et écart entre les deux
   niveaux, en points ;
3. LA question pratique : la zone affichée par /scalp (edge.regime_zone :
   frein / transition / accélérateur, Gamma Flip + GEX 0DTE, bande ± 0,15 EM)
   contre le régime de l'étude (signe du GEX net reconstruit) ;
4. contrôle de l'émulation : la zone /scalp RECALCULÉE depuis l'open
   interest Databento (dbgamma.regimes, définition « tdb ») contre la zone
   réellement calculée par le tableau de bord — si elles concordent, le test
   sur l'année de la définition « tableau de bord » (rapport d'excès) vaut
   pour la vraie zone /scalp.

Lecture seule ; rapport dans data/reports/regime_concordance_NQ_<date>.md.
Il faut les fichiers Databento `definition` + `statistics` (cache de
excess_report) et l'historique du tableau de bord (depuis fin août 2026).
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gex import dbgamma, dbtape, edge, rates, store  # noqa: E402
from gex.config import SETTINGS  # noqa: E402
from gex.edge_report import _straddle_em  # noqa: E402
from gex.metrics import ET  # noqa: E402

import excess_report  # noqa: E402

SYMBOL = "NQ"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else f


def dashboard_rows(symbol: str = SYMBOL) -> pd.DataFrame:
    h = store.load_history(symbol)
    if h.empty:
        return h
    ts = pd.to_datetime(h["timestamp"])
    ts = ts.dt.tz_localize(ET, ambiguous="NaT", nonexistent="NaT") if ts.dt.tz is None \
        else ts.dt.tz_convert(ET)
    h = h.assign(ts=ts).dropna(subset=["ts", "spot"])
    h = h.assign(session=dbtape.session_of(h["ts"].dt.tz_convert("UTC")))
    return h.sort_values("ts").reset_index(drop=True)


def compare(rows: pd.DataFrame, defs: pd.DataFrame, stats: pd.DataFrame) -> pd.DataFrame:
    rate = rates.current_rate()
    band = edge.EdgeParams().flip_band_em
    all_days = sorted(d for d in excess_report._tick_days(SYMBOL)
                      if date.fromisoformat(d).weekday() < 5)
    cache: dict = {}
    out = []
    for session, g in rows.groupby("session"):
        close, close_ts = excess_report._prev_close(SYMBOL, session, all_days, cache)
        chain = dbgamma.session_chain(stats, defs, session, close, close_ts, rate)
        if chain is None or chain.empty:
            continue
        em = _straddle_em(SYMBOL, session)
        px_ts, px = excess_report.session_path(SYMBOL, session)
        for r in g.itertuples():
            s = float(r.spot)
            # le spot du tableau de bord peut appartenir à un autre trimestriel
            # que le continu de ticks (semaine d'échéance) : tout est ramené au
            # prix du continu au même instant, comme dans l'étude
            t = r.ts.timestamp()
            j = int(np.searchsorted(px_ts, t, side="right")) - 1 if len(px_ts) else -1
            cont = float(px[j]) if j >= 0 and t - px_ts[j] <= 300 else s
            offset = cont - s
            v = dbgamma.view(chain, t, cont, cont, 1, rate)
            emu = dbgamma.regimes(chain, t, cont, rate)
            zg = _num(getattr(r, "zero_gamma", None))
            gex0 = _num(getattr(r, "net_gex_0dte", None))
            zone = edge.regime_zone(s, zg, em, gex0, band)[0] if em else "inconnu"
            flip_db = cont - v["dist_flip"] if np.isfinite(v["dist_flip"]) else np.nan
            out.append({
                "seance": session, "ts": r.ts, "spot": cont, "ecart_contrat": offset,
                "gex_tdb": _num(r.net_gex), "gex_db": v["gex_net"],
                "flip_tdb": zg + offset if zg is not None else None, "flip_db": flip_db,
                "zone_scalp": zone, "regime_etude": v["regime"],
                "zone_recalculee": emu["regime_tdb"], "flip_recalcule": emu["flip_tdb"]})
    return pd.DataFrame(out)


def summarize(c: pd.DataFrame) -> dict:
    ok = c.dropna(subset=["gex_tdb", "gex_db"])
    sign = float((np.sign(ok["gex_tdb"]) == np.sign(ok["gex_db"])).mean()) if len(ok) else np.nan
    f = c.dropna(subset=["flip_tdb", "flip_db"])
    side = float(((f["spot"] > f["flip_tdb"]) == (f["spot"] > f["flip_db"])).mean()) \
        if len(f) else np.nan
    z = c[c["zone_scalp"].isin(("frein", "accelerateur"))
          & c["regime_etude"].isin(("frein", "accélérateur"))]
    zone = float((z["zone_scalp"].str.replace("accelerateur", "accélérateur")
                  == z["regime_etude"]).mean()) if len(z) else np.nan
    e = c[c["zone_scalp"].isin(("frein", "accelerateur", "transition"))
          & (c["zone_recalculee"] != "inconnu")] if "zone_recalculee" in c else c.iloc[0:0]
    emu = float((e["zone_scalp"].str.replace("accelerateur", "accélérateur")
                 == e["zone_recalculee"]).mean()) if len(e) else np.nan
    return {"lignes": len(c), "seances": c["seance"].nunique(),
            "zone_recalculee_egale_zone_scalp_%": 100 * emu,
            "meme_signe_gex_%": 100 * sign, "meme_cote_du_flip_%": 100 * side,
            "ecart_flip_med_pts": float((f["flip_tdb"] - f["flip_db"]).abs().median())
            if len(f) else np.nan,
            "zone_scalp_egale_regime_%": 100 * zone,
            "zone_scalp_transition_%": 100 * float((c["zone_scalp"] == "transition").mean()),
            "spot_autre_contrat_%": 100 * float((c["ecart_contrat"].abs() > 50).mean())
            if "ecart_contrat" in c else np.nan}


def per_session(c: pd.DataFrame) -> pd.DataFrame:
    cols = ["lignes", "meme_signe_gex_%", "meme_cote_du_flip_%", "ecart_flip_med_pts",
            "zone_scalp_egale_regime_%"]
    return pd.DataFrame({day: {k: summarize(g)[k] for k in cols}
                         for day, g in c.groupby("seance")}).T


def report() -> str:
    lines = [f"# Régime gamma : tableau de bord contre reconstruction — {SYMBOL}",
             f"_{datetime.now(ET):%Y-%m-%d %H:%M}_", ""]
    rows = dashboard_rows()
    if rows.empty:
        return "\n".join(lines + ["_(aucun historique NQ dans data/history/metrics.parquet)_"])
    defs, stats = excess_report.gamma_context()
    c = compare(rows, defs, stats)
    if c.empty:
        return "\n".join(lines + ["_(aucune séance commune avec l'open interest Databento)_"])
    s = summarize(c)
    lines += ["## Synthèse", "", "```\n" + pd.Series(s).round(1).to_string() + "\n```", "",
              "## Zone /scalp (lignes) contre régime de l'étude (colonnes)", "",
              "```\n" + pd.crosstab(c["zone_scalp"], c["regime_etude"], margins=True)
              .to_string() + "\n```", "",
              "## Par séance", "",
              "```\n" + per_session(c).round(1).to_string() + "\n```", "",
              "Lecture : si `zone_scalp_egale_regime_%` est élevé (≥ 80-85 %), la zone de "
              "/scalp peut porter l'avertissement tiré de l'étude ; sinon, il faudrait "
              "afficher le régime calculé comme dans l'étude. `transition` (bande neutre "
              "autour du flip) n'existe que côté /scalp."]
    return "\n".join(lines)


def main() -> None:
    md = report()
    outdir = SETTINGS.data_dir / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"regime_concordance_{SYMBOL}_{datetime.now(ET):%Y-%m-%d}.md"
    path.write_text(md, encoding="utf-8")
    print(md)
    print(f"\n→ {path}")


if __name__ == "__main__":
    main()
