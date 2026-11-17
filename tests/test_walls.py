"""Murs de gamma (correctif du 10/10/2026) : Call Wall / Put Support sur les
concentrations de gamma call / put séparées, GEX net et brut explicites,
classement GEX1-5 inchangé, et formats consommés par l'API, l'export
TradingView et le bot Discord."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gex import metrics
from gex.metrics import ET
from tests.test_metrics import far_expiry, make_chain


def _chain(rows, spot=100.0):
    return metrics.enrich(make_chain(spot, rows))


def _row(typ, strike, oi, exp=None):
    return {"expiry": exp or far_expiry(), "type": typ, "strike": float(strike),
            "open_interest": float(oi)}


# --- définitions ---------------------------------------------------------------

def test_gex_par_type_net_et_brut():
    df = _chain([_row("C", 105, 1000), _row("P", 105, 600), _row("P", 95, 400)])
    parts = metrics.gex_by_type(df)
    assert (parts["gex_calls"] >= 0).all() and (parts["gex_puts"] <= 0).all()
    assert np.allclose(parts["gex_net"], parts["gex_calls"] + parts["gex_puts"])
    assert np.allclose(parts["gex_gross"], parts["gex_calls"] - parts["gex_puts"])
    # brut = Σ |GEX par contrat| (définition de positioning.book_summary)
    assert parts["gex_gross"].sum() == pytest.approx(df["gex"].abs().sum())
    # net par strike = l'agrégat historique (convention de signe inchangée)
    assert np.allclose(parts["gex_net"], df.groupby("strike")["gex"].sum().loc[parts.index])


def test_call_wall_concentration_call_compensee_par_des_puts():
    """105 : gros gamma call compensé par encore plus de gamma put -> GEX net
    négatif. Avec l'ancienne définition (net), le Call Wall tombait sur 110 ;
    la concentration de calls est pourtant à 105."""
    df = _chain([_row("C", 105, 5000), _row("P", 105, 6000),
                 _row("C", 110, 800), _row("P", 95, 300)])
    k = metrics.key_levels(df, 100.0)
    assert k["call_wall"] == 105.0
    assert k["net_gex_max_above"] == 110.0          # ancienne définition, conservée


def test_put_support_concentration_put_compensee_par_des_calls():
    df = _chain([_row("P", 95, 5000), _row("C", 95, 6000),
                 _row("P", 90, 800), _row("C", 105, 300)])
    k = metrics.key_levels(df, 100.0)
    assert k["put_support"] == 95.0
    assert k["net_gex_min_below"] == 90.0


def test_cote_au_dessus_et_en_dessous_du_spot():
    """Un énorme mur de calls SOUS le spot ne devient pas le Call Wall, ni un
    énorme mur de puts AU-DESSUS le Put Support."""
    df = _chain([_row("C", 92, 9000), _row("C", 108, 500),
                 _row("P", 106, 9000), _row("P", 96, 500)])
    k = metrics.key_levels(df, 100.0)
    assert k["call_wall"] == 108.0 and k["put_support"] == 96.0


def test_aucun_strike_admissible():
    df = _chain([_row("P", 110, 500), _row("C", 90, 500)])     # tout du mauvais côté
    k = metrics.key_levels(df, 100.0)
    assert k["call_wall"] is None and k["put_support"] is None
    assert metrics.key_levels(df.iloc[0:0], 100.0)["call_wall"] is None


def test_meme_definition_au_spot_structurel():
    """Avec ref_spot (gamma recalculé à la clôture), même règle : la
    concentration de calls gagne malgré un net négatif."""
    df = _chain([_row("C", 105, 5000), _row("P", 105, 6000), _row("C", 110, 800)])
    k = metrics.key_levels(df, 100.0, ref_spot=101.0)
    assert k["call_wall"] == 105.0 and k["net_gex_max_above"] == 110.0


# --- GEX1-5 : classement inchangé ---------------------------------------------

def _ancien_classement(df, n=5, ref_spot=None):
    """Copie de l'algorithme d'avant le correctif (classement |GEX net|)."""
    nearest = df["expiry"].min()
    sub = df[df["expiry"] == nearest]
    if ref_spot:
        agg = metrics.gex_at_spot(sub, ref_spot).rename("gex").reset_index()
        agg = agg.rename(columns={"index": "strike"})
    else:
        agg = sub.groupby("strike")["gex"].sum().reset_index()
    agg = agg.loc[agg["gex"].abs().nlargest(n).index]
    return agg.sort_values("gex", key=abs, ascending=False).reset_index(drop=True)


@pytest.mark.parametrize("ref_spot", [None, 101.0])
def test_classement_gex1_5_inchange(ref_spot):
    rng = np.random.default_rng(3)
    rows = [_row(t, k, float(rng.integers(50, 5000)))
            for k in range(80, 121, 2) for t in "CP"]
    df = _chain(rows)
    nouveau = metrics.top_gex_levels(df, ref_spot=ref_spot)
    ancien = _ancien_classement(df, ref_spot=ref_spot)
    assert nouveau["strike"].tolist() == ancien["strike"].tolist()
    assert np.allclose(nouveau["gex"], ancien["gex"])
    assert nouveau["rank"].tolist() == [1, 2, 3, 4, 5]
    # décomposition disponible, cohérente avec le net
    assert np.allclose(nouveau["gex_calls"] + nouveau["gex_puts"], nouveau["gex"])
    assert np.allclose(nouveau["gex_gross"], nouveau["gex_calls"] - nouveau["gex_puts"])


# --- consommateurs ---------------------------------------------------------------

def test_compute_levels_garde_ses_champs():
    df = _chain([_row("C", 105, 5000), _row("P", 105, 6000), _row("C", 110, 800),
                 _row("P", 95, 700)])
    res = metrics.compute_levels(df, 100.0, 100.0, bucket="Tout")
    assert {"call_wall", "put_support", "d1_min", "d1_max", "max_pain"} <= set(res["keys"])
    assert {"strike", "gex", "rank", "expiry"} <= set(res["levels"].columns)


def test_api_levels_formats():
    from tests.test_api import _client, _seed
    _seed("TSTW")
    body = _client().get("/api/v1/TSTW/levels").get_json()
    kl = body["key_levels"]
    assert {"call_wall", "put_support", "d1_min", "d1_max"} <= set(kl)
    assert {"net_gex_max_above", "net_gex_min_below"} <= set(kl)
    for w in body["gex_walls"]:
        assert {"strike", "gex", "expiry"} <= set(w)            # champs historiques
        assert w["gex_calls"] + w["gex_puts"] == pytest.approx(w["gex"])
        assert w["gex_gross"] == pytest.approx(w["gex_calls"] - w["gex_puts"])


def test_export_tradingview_libelles_inchanges():
    from gex.app import tv_levels_string
    levels = pd.DataFrame({"strike": [110.0], "gex": [2e9], "rank": [1],
                           "expiry": [far_expiry()], "gex_calls": [3e9],
                           "gex_puts": [-1e9], "gex_gross": [4e9]})
    s = tv_levels_string(levels, None, None, {"call_wall": 105.0, "put_support": 95.0}, None)
    assert "105.00,Call Wall,res" in s and "95.00,Put Support,sup" in s


def test_backtest_meme_perimetre_de_niveaux(monkeypatch, tmp_path):
    from gex import backtest
    df = _chain([_row("C", 105, 5000), _row("P", 105, 6000), _row("C", 110, 800),
                 _row("P", 95, 700)])
    df["spot"] = 100.0
    monkeypatch.setattr(backtest.store, "load_first_snapshot", lambda s, d: df)
    monkeypatch.setattr(backtest.store, "previous_close_spot", lambda s, d: 100.0)
    out = backtest.session_levels("TST", "2026-10-09")
    assert "net_gex_max_above" not in out and "net_gex_min_below" not in out
    assert out["call_wall"] == 105.0


def _bot():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "discord_bot"))
    pytest.importorskip("discord")
    import bot
    return bot


def test_bot_commande_niveaux(monkeypatch):
    bot = _bot()
    data = {"spot": 100.0, "zero_gamma": 98.0, "hvl": 99.0, "scale": "NQ",
            "key_levels": {"call_wall": 105.0, "put_support": 95.0, "d1_min": 97.0,
                           "d1_max": 103.0, "max_pain": 100.0,
                           "net_gex_max_above": 110.0, "net_gex_min_below": 90.0},
            "gex_walls": [{"strike": 105.0, "gex": -1e9, "expiry": "2026-10-16",
                           "gex_calls": 2e9, "gex_puts": -3e9, "gex_gross": 5e9}]}
    monkeypatch.setattr(bot, "fetch", lambda path: data)
    sent = []

    class Ctx:
        async def send(self, msg):
            sent.append(msg)
    asyncio.run(bot.niveaux.callback(Ctx(), "NQ"))
    txt = sent[0]
    assert "Call Wall (γ calls) 105" in txt and "Put Support (γ puts) 95" in txt
    assert "Murs GEX (net) : 105 (-1.00 Bn put)" in txt


def test_bot_photo_15h15_lit_les_memes_cles(monkeypatch):
    bot = _bot()
    stored = {}
    monkeypatch.setattr(bot, "_journal", lambda: object())
    monkeypatch.setattr(bot, "fetch", lambda path: {
        "spot": 100.0, "zero_gamma": 98.0, "hvl": None,
        "key_levels": {"call_wall": 105.0, "put_support": 95.0, "d1_min": 97.0,
                       "d1_max": 103.0}, "gex_walls": []})
    monkeypatch.setattr(bot.journal, "set_metric",
                        lambda jc, **kw: stored.__setitem__(kw["name"], kw["value_num"]))
    bot._snapshot_niveaux("2026-10-09", "2026-10-09T15:15:00")
    assert stored["lvl_1515_call_wall"] == 105.0 and stored["lvl_1515_put_support"] == 95.0


def test_script_de_comparaison_avant_apres(tmp_path, monkeypatch):
    """Le script lit les snapshots, ne modifie rien, et montre l'écart."""
    from gex import store
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    df = _chain([_row("C", 105, 5000), _row("P", 105, 6000), _row("C", 110, 800),
                 _row("P", 95, 700)])
    df["spot"] = 100.0
    store.save_snapshot("NQ", df, datetime(2026, 10, 9, 15, 0))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import compare_walls
    t = compare_walls.compare("NQ")
    assert len(t) == 1
    assert t["call_wall_net (avant)"].iloc[0] == 110.0 and t["call_wall (après)"].iloc[0] == 105.0
    s = compare_walls.summary(t)
    assert s["Call Wall changé"] == 1 and s["Put Support changé"] == 0
