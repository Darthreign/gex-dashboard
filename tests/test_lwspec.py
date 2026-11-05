"""Figures Plotly -> descriptions Lightweight Charts (gex/lwspec.py) et flux
SSE des graphiques LW (LW_CHARTS, /api/v1/lw/<nom>)."""
from datetime import datetime

import plotly.graph_objects as go
import pytest

from gex import lwspec


def _ts(h, m, s=0):
    return datetime(2026, 10, 8, h, m, s)


def test_scatter_en_marches_et_points_dedupliques():
    fig = go.Figure()
    fig.add_scatter(x=[_ts(10, 0, 1), _ts(10, 0, 0), _ts(10, 0, 0), _ts(10, 1)],
                    y=[2.0, 1.0, 1.5, float("nan")], name="Net",
                    line=dict(color="#fff", width=2.6, shape="hv", dash="dot"))
    spec = lwspec.fig_to_spec(fig, height=300)
    s = spec["series"][0]
    assert s["type"] == "Line" and s["options"]["lineType"] == 1
    assert s["options"]["lineStyle"] == 1 and s["options"]["lineWidth"] == 3
    # trié, temps uniques (dernier gardé), NaN retiré
    assert [p["value"] for p in s["data"]] == [1.5, 2.0]
    times = [p["time"] for p in s["data"]]
    assert times == sorted(times) and times[1] - times[0] == 1
    assert spec["height"] == 300 and spec["message"] is None


def test_barres_couleurs_par_point_et_axe_superpose():
    fig = go.Figure()
    fig.add_bar(x=[_ts(10, 0), _ts(10, 1)], y=[3, -2], marker_color=["#0f0", "#f00"], name="Flux")
    fig.add_scatter(x=[_ts(10, 0), _ts(10, 1)], y=[3, 1], name="Cumul", yaxis="y2")
    fig.update_layout(yaxis2=dict(overlaying="y", side="left"))
    spec = lwspec.fig_to_spec(fig)
    bar, line = spec["series"]
    assert bar["type"] == "Histogram" and [p["color"] for p in bar["data"]] == ["#0f0", "#f00"]
    assert (line["pane"], line["scale"]) == (0, "left")


def test_panneau_separe_et_bougies():
    fig = go.Figure()
    fig.add_candlestick(x=[_ts(10, 0), _ts(10, 1)], open=[1, 2], high=[3, 3], low=[0, 1],
                        close=[2, 1], name="Px")
    fig.add_scatter(x=[_ts(10, 0)], y=[5], yaxis="y2", name="Bas")
    fig.update_layout(yaxis=dict(domain=[0.4, 1]), yaxis2=dict(domain=[0, 0.35]))
    spec = lwspec.fig_to_spec(fig)
    c, low = spec["series"]
    assert c["type"] == "Candlestick" and c["data"][1]["close"] == 1.0
    assert low["pane"] == 1


def test_lignes_horizontales_meme_prix_gardent_leur_etiquette():
    fig = go.Figure()
    fig.add_scatter(x=[_ts(10, 0), _ts(10, 1)], y=[100, 101])
    fig.add_hline(y=99, line_color="#c98500", annotation_text="Flip 99")
    fig.add_hline(y=99, line_color="#199e70", annotation_text="HVL 99")
    fig.add_hline(y=0, line_color="#333")
    spec = lwspec.fig_to_spec(fig)
    titles = [(l["color"], l["title"]) for l in spec["lines"]]
    assert ("#c98500", "Flip 99") in titles and ("#199e70", "HVL 99") in titles
    assert ("#333", "") in titles


def test_figure_vide_donne_le_message():
    fig = go.Figure()
    fig.update_layout(title="Titre<br><sup>sous-titre</sup>")
    fig.add_annotation(text="Pas encore de données", x=0.5, y=0.5, showarrow=False)
    spec = lwspec.fig_to_spec(fig)
    assert spec["message"] == "Pas encore de données" and spec["series"] == []
    assert spec["title"] == "Titre — sous-titre"


def test_lw_params_filtre_et_symbole():
    from gex.app import lw_params
    assert lw_params({"symbol": "XXX"}) is None
    p = dict(lw_params({"symbol": "SPX", "day": "2026-10-08", "evil": "1"}))
    assert p == {"symbol": "SPX", "day": "2026-10-08", "lang": "fr"}


@pytest.mark.parametrize("name", ["flow", "gflow", "tape", "history", "spotzg", "hedge", "heatmap"])
def test_chaque_graphique_produit_une_description(name, tmp_path, monkeypatch):
    from gex.app import LW_CHARTS
    from gex.config import SETTINGS
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    spec = LW_CHARTS[name][0]({"symbol": "SPX", "lang": "fr", "series": "net"})
    assert set(spec) >= {"title", "message", "series", "lines", "height"}
    # données, profil seul (heatmap sans bougies) ou message : jamais muet
    assert spec["message"] or spec["series"] or spec.get("profile")


def test_route_lw_inconnue_404():
    from gex.app import create_app
    client = create_app().server.test_client()
    assert client.get("/api/v1/lw/nope?symbol=SPX").status_code == 404
    assert client.get("/api/v1/lw/flow?symbol=XXX").status_code == 404
