"""Page scalp en flux poussé (SSE) : blocs calculés, masqués, dédupliqués."""
import json

import plotly.graph_objects as go
import pytest
from dash import html

from gex import app as A
from gex import broadcast


@pytest.fixture()
def stubs(monkeypatch):
    calls = []

    def rec(name, value):
        def f(*a, **k):
            calls.append(name)
            return value() if callable(value) else value
        return f
    monkeypatch.setattr(A, "scalp_context", lambda s: {"snap_spot": 100.0})
    monkeypatch.setattr(A, "_scalp_live_spot", lambda s, c: 100.0)
    monkeypatch.setattr(A, "scalp_absorption", lambda s: None)
    monkeypatch.setattr(A, "scalp_head", rec("head", lambda: html.Div("tête")))
    monkeypatch.setattr(A, "scalp_ladder", rec("ladder", lambda: html.Div("niveaux")))
    monkeypatch.setattr(A, "tape_table", rec("prints", lambda: html.Div("prints")))
    monkeypatch.setattr(A, "cached_hedge_fig", rec("hedge", go.Figure))
    monkeypatch.setattr(A, "scalp_price_fig", rec("price", go.Figure))
    monkeypatch.setattr(A, "scalp_banner", rec("banner", lambda: html.Div("bandeau")))
    return calls


def test_scalp_v2_blocks_without_price_or_banner(stubs):
    out = A.scalp_panels_snapshot("NQ", "fr", -1, 0.0, False, frozenset())
    assert set(out) == {"head", "ladder", "prints", "hedge"}
    assert json.loads(out["ladder"])["props"]["children"] == "niveaux"
    assert "series" in json.loads(out["hedge"])          # description Lightweight Charts


def test_scalpv1_also_gets_banner_and_plotly_price(stubs):
    out = A.scalp_panels_snapshot("NQ", "fr", -1, 0.0, True, frozenset())
    assert {"banner", "price"} <= set(out)


def test_hidden_blocks_are_not_computed(stubs):
    out = A.scalp_panels_snapshot("NQ", "fr", -1, 0.0, True,
                                  frozenset({"hide_ladder", "hide_tape", "hide_price_chart"}))
    assert set(out) == {"banner", "head", "hedge"}
    assert "ladder" not in stubs and "prints" not in stubs and "price" not in stubs


def test_params_from_query():
    lang, window, min_size, v1, hide = A.scalp_panels_params(
        {"lang": "en", "window": "15", "min": "5", "v1": "1", "hide": "hide_tape,bogus"})
    assert (lang, window, min_size, v1, hide) == ("en", 15, 5.0, True, frozenset({"hide_tape"}))
    assert A.scalp_panels_params({"window": "x"})[1] == -1


def test_channel_pushes_only_on_change(stubs, monkeypatch):
    broadcast._channels.clear()
    ch = A.scalp_panels_channel("NQ", "fr", -1, 0.0, False, frozenset())
    produce = ch._producer
    first = produce()
    assert first.startswith("data: ") and first.endswith("\n\n")
    msg = json.loads(first[6:])
    assert set(msg["sig"]) == set(msg["blocks"])
    assert produce() is None                       # rien n'a changé
    monkeypatch.setattr(A, "scalp_ladder", lambda *a: html.Div("niveaux 2"))
    msg2 = json.loads(produce()[6:])
    assert msg2["sig"]["ladder"] != msg["sig"]["ladder"]
    assert msg2["sig"]["head"] == msg["sig"]["head"]


def test_routes_exist():
    from gex.asgi import build
    dash_app = A.create_app()
    paths = [r.path for r in build(dash_app).routes]
    assert "/api/v1/{symbol}/scalp-stream" in paths
    assert any("scalp-stream" in str(r) for r in dash_app.server.url_map.iter_rules())
    # plus aucun sondage à la seconde sur les pages scalp
    assert "tape-tick" not in str(dash_app.layout)


def test_build_fingerprint_stable_and_sensitive():
    app1, app2 = A.create_app(), A.create_app()
    f = A.callbacks_fingerprint(app1)
    assert f == A.callbacks_fingerprint(app2)                  # même code, même empreinte
    client = app1.server.test_client()
    assert client.get("/api/v1/build").get_json()["build"] == f
    from dash import Input, Output
    app2.callback(Output("build-sink", "children"), Input("symbol", "value"))(lambda s: s)
    assert A.callbacks_fingerprint(app2) != f                   # callback changé -> autre empreinte
