"""Dashboard Dash : GEX/DEX par strike, indicateurs, flux delta, skew IV.

Palette : polarité (GEX/flux +/-) en diverging bleu↔rouge, identité
(calls/puts, expirations) sur les slots catégoriels — thème sombre.
Interface FR/EN (gex/i18n.py) ; termes de trading standards dans les deux.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, ctx, dcc, html
from dash.dependencies import Input, Output, State

from . import metrics, store
from .config import SETTINGS, UNDERLYINGS
from .i18n import LANGS, t, wall_labels
from .metrics import ET, EXPIRY_BUCKETS
from .scheduler import STATE

# --- Palette (mode sombre, cf. skill dataviz) ---
C = {
    "surface": "#1a1a19",
    "page": "#0d0d0d",
    "ink": "#ffffff",
    "ink2": "#c3c2b7",
    "muted": "#898781",
    "grid": "#2c2c2a",
    "axis": "#383835",
    "pos": "#3987e5",   # GEX positif / flux acheteur (bleu)
    "neg": "#e66767",   # GEX négatif / flux vendeur (rouge)
    "spot": "#ffffff",
    "zg": "#c98500",    # jaune sombre — Gamma Flip
    "lvl": "#9085e9",   # violet — niveaux GEX 0DTE
    "hvl": "#199e70",   # aqua — HVL (bascule pondérée par le volume du jour)
    "cw": "#3987e5",    # bleu — Call Wall (résistance, au-dessus du spot)
    "ps": "#e66767",    # rouge — Put Support (support, sous le spot)
    "d1": "#898781",    # gris — bornes 1D Min / 1D Max (move attendu)
    "cat": ["#3987e5", "#d95926", "#199e70", "#c98500"],  # slots 1-4
}

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Fuseau local de la machine — tous les axes temps sont affichés en heure locale
LOCAL_TZ = datetime.now().astimezone().tzinfo

BUCKET_KEYS = {"0DTE": "bucket_0DTE", "Semaine": "bucket_week",
               "Mois": "bucket_month", "Tout": "bucket_all"}


def to_local(ts: pd.Series) -> pd.Series:
    """Timestamps stockés naïfs en heure de New York → heure locale (naïve)."""
    return (
        pd.to_datetime(ts)
        .dt.tz_localize(ET, ambiguous="NaT", nonexistent="NaT")
        .dt.tz_convert(LOCAL_TZ)
        .dt.tz_localize(None)
    )


def base_layout(title: str, height: int = 420) -> dict:
    return dict(
        title=dict(text=title, font=dict(size=14, color=C["ink"], family=FONT), x=0.02),
        template=None,
        paper_bgcolor=C["surface"],
        plot_bgcolor=C["surface"],
        font=dict(family=FONT, size=11, color=C["ink2"]),
        margin=dict(l=60, r=20, t=44, b=40),
        height=height,
        xaxis=dict(gridcolor=C["grid"], zerolinecolor=C["axis"], linecolor=C["axis"], tickfont=dict(color=C["muted"])),
        yaxis=dict(gridcolor=C["grid"], zerolinecolor=C["axis"], linecolor=C["axis"], tickfont=dict(color=C["muted"])),
        hoverlabel=dict(bgcolor=C["page"], font=dict(family=FONT, color=C["ink"])),
        showlegend=False,
    )


def empty_fig(msg: str, title: str = "") -> go.Figure:
    fig = go.Figure()
    fig.update_layout(**base_layout(title))
    fig.add_annotation(text=msg, showarrow=False, font=dict(color=C["muted"], size=13))
    return fig


def _bar_width(strikes: np.ndarray) -> float:
    diffs = np.diff(np.sort(np.unique(strikes)))
    return float(np.median(diffs)) * 0.75 if len(diffs) else 1.0


def exposure_fig(df: pd.DataFrame, spot: float, zg: float | None, col: str, title: str,
                 lang: str, levels: pd.DataFrame | None = None, hvl: float | None = None,
                 window: float = 0.04, basis: float = 0.0,
                 keys: dict | None = None) -> go.Figure:
    # `basis` décale l'échelle de prix vers le future (0 = points d'indice).
    lo, hi = spot * (1 - window), spot * (1 + window)
    d = df[df["strike"].between(lo, hi)]
    agg = metrics.exposure_by_strike(d, col)
    if agg.empty:
        return empty_fig(t(lang, "no_data_window"), title)
    net = agg["net"].to_numpy() / 1e9
    strikes = agg["strike"].to_numpy() + basis
    spot = spot + basis
    zg = zg + basis if zg is not None else None
    hvl = hvl + basis if hvl is not None else None
    lo, hi = lo + basis, hi + basis
    colors = np.where(net >= 0, C["pos"], C["neg"])
    fig = go.Figure(
        go.Bar(
            y=strikes, x=net, orientation="h",
            width=_bar_width(strikes),
            marker=dict(color=colors, line=dict(width=0)),
            customdata=np.stack([agg["C"] / 1e9, agg["P"] / 1e9], axis=-1),
            hovertemplate=(
                f"{t(lang, 'hover_strike')} %{{y}}<br>{t(lang, 'hover_net')}: %{{x:.2f}} $Bn"
                "<br>Calls: %{customdata[0]:.2f} $Bn"
                "<br>Puts: %{customdata[1]:.2f} $Bn<extra></extra>"
            ),
        )
    )
    fig.update_layout(**base_layout(title, height=560))
    fig.update_xaxes(title_text=t(lang, "axis_bn_per_move"), title_font=dict(color=C["muted"]))
    fig.add_hline(y=spot, line_color=C["spot"], line_dash="dot", line_width=1,
                  annotation_text=f"Spot {spot:.0f}", annotation_font_color=C["ink"],
                  annotation_position="top right")
    if zg is not None and lo <= zg <= hi:
        fig.add_hline(y=zg, line_color=C["zg"], line_dash="dash", line_width=1,
                      annotation_text=f"Gamma Flip {zg:.0f}", annotation_font_color=C["zg"],
                      annotation_position="bottom left")
    if hvl is not None and lo <= hvl <= hi:
        fig.add_hline(y=hvl, line_color=C["hvl"], line_dash="dash", line_width=1,
                      annotation_text=f"HVL {hvl:.0f}", annotation_font_color=C["hvl"],
                      annotation_position="bottom right")
    for key, color, label, dash in (
        ("call_wall", C["cw"], "Call Wall", "solid"),
        ("put_support", C["ps"], "Put Support", "solid"),
        ("d1_max", C["d1"], "1D Max", "dot"),
        ("d1_min", C["d1"], "1D Min", "dot"),
    ):
        v = (keys or {}).get(key)
        if v is None:
            continue
        y = v + basis
        if lo <= y <= hi:
            fig.add_hline(y=y, line_color=color, line_dash=dash, line_width=1.5,
                          annotation_text=f"{label} {y:.0f}",
                          annotation_font=dict(color=color, size=10),
                          annotation_position="top right")
    if levels is not None and not levels.empty:
        labels = wall_labels(levels)
        for lv in levels.itertuples():
            y = lv.strike + basis
            if not (lo <= y <= hi):
                continue
            fig.add_hline(
                y=y, line_color=C["lvl"], line_dash="dashdot",
                line_width=1, opacity=0.8,
                annotation_text=f"{labels[lv.strike]} {y:.0f}",
                annotation_font=dict(color=C["lvl"], size=10),
                annotation_position="top left",
            )
    return fig


def available_flow_days(symbol: str) -> list[str]:
    root = SETTINGS.data_dir / "flows" / symbol
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.parquet"))


def flow_fig(symbol: str, lang: str, day: str | None = None) -> go.Figure:
    day = day or datetime.now(ET).strftime("%Y-%m-%d")
    flows = store.load_flows(symbol, day)
    title = t(lang, "flow_title")
    if flows.empty:
        return empty_fig(t(lang, "no_flow_day", day=day), title)
    ts = to_local(flows["timestamp"])
    vals = flows["flow_total"].to_numpy() / 1e6
    cum = np.cumsum(vals)
    fig = go.Figure()
    fig.add_bar(x=ts, y=vals, name=t(lang, "legend_flow"),
                marker=dict(color=np.where(vals >= 0, C["pos"], C["neg"]), line=dict(width=0)),
                hovertemplate=f"%{{x|%H:%M}}<br>{t(lang, 'hover_flow')}: %{{y:.1f}} $M<extra></extra>")
    fig.add_scatter(x=ts, y=cum, mode="lines", name=t(lang, "legend_cum"), yaxis="y2",
                    line=dict(color=C["ink2"], width=2),
                    hovertemplate=f"%{{x|%H:%M}}<br>{t(lang, 'hover_cum')}: %{{y:.1f}} $M<extra></extra>")
    lay = base_layout(title, height=300)
    # deux panneaux empilés partageant l'axe temps (pas de double axe trompeur)
    lay["yaxis"] = dict(domain=[0.55, 1.0], gridcolor=C["grid"], zerolinecolor=C["axis"],
                        title=dict(text=t(lang, "axis_m_per_min"), font=dict(color=C["muted"])),
                        tickfont=dict(color=C["muted"]))
    lay["yaxis2"] = dict(domain=[0.0, 0.45], gridcolor=C["grid"], zerolinecolor=C["axis"],
                         title=dict(text=t(lang, "axis_cum_m"), font=dict(color=C["muted"])),
                         tickfont=dict(color=C["muted"]))
    lay["height"] = 380
    fig.update_layout(**lay)
    return fig


def history_fig(symbol: str, lang: str) -> go.Figure:
    title = t(lang, "hist_title")
    hist = store.load_history(symbol)
    if hist.empty or len(hist) < 2:
        return empty_fig(t(lang, "not_enough_history"), title)
    ts = to_local(hist["timestamp"])
    fig = go.Figure()
    fig.add_scatter(x=ts, y=hist["net_gex"] / 1e9, mode="lines", name="GEX",
                    line=dict(color=C["cat"][0], width=2),
                    hovertemplate="%{x|%d/%m %H:%M}<br>GEX: %{y:.1f} $Bn<extra></extra>")
    fig.update_layout(**base_layout(title, height=300))
    return fig


def spot_zg_fig(symbol: str, lang: str) -> go.Figure:
    title = t(lang, "spotzg_title")
    hist = store.load_history(symbol)
    if hist.empty or len(hist) < 2:
        return empty_fig(t(lang, "not_enough_history"), title)
    ts = to_local(hist["timestamp"])
    fig = go.Figure()
    fig.add_scatter(x=ts, y=hist["spot"], mode="lines", name=t(lang, "legend_spot"),
                    line=dict(color=C["cat"][0], width=2),
                    hovertemplate="%{x|%d/%m %H:%M}<br>Spot: %{y:.0f}<extra></extra>")
    fig.add_scatter(x=ts, y=hist["zero_gamma"], mode="lines", name=t(lang, "legend_zg"),
                    line=dict(color=C["zg"], width=2, dash="dash"),
                    hovertemplate="%{x|%d/%m %H:%M}<br>Gamma Flip: %{y:.0f}<extra></extra>")
    lay = base_layout(title, height=300)
    lay["showlegend"] = True
    lay["legend"] = dict(orientation="h", y=1.12, font=dict(color=C["ink2"]))
    fig.update_layout(**lay)
    return fig


def smile_fig(df: pd.DataFrame, spot: float, lang: str) -> go.Figure:
    title = t(lang, "smile_title")
    d = df[(df["iv"] > 0.01) & (df["open_interest"] > 0)
           & df["strike"].between(spot * 0.85, spot * 1.15)]
    # IV OTM : puts sous le spot, calls au-dessus (le smile standard)
    otm = d[((d["type"] == "P") & (d["strike"] <= spot)) | ((d["type"] == "C") & (d["strike"] > spot))]
    expiries = sorted(otm["expiry"].unique())[:4]
    if not expiries:
        return empty_fig(t(lang, "no_iv"), title)
    fig = go.Figure()
    for i, exp in enumerate(expiries):
        e = otm[otm["expiry"] == exp].sort_values("strike")
        smoothed = e.groupby("strike")["iv"].mean()
        fig.add_scatter(x=smoothed.index, y=smoothed * 100, mode="lines",
                        name=str(exp), line=dict(color=C["cat"][i % 4], width=2),
                        hovertemplate=f"{exp}<br>{t(lang, 'hover_strike')} %{{x}}<br>IV: %{{y:.1f}}%<extra></extra>")
    lay = base_layout(title, height=300)
    lay["showlegend"] = True
    lay["legend"] = dict(orientation="h", y=1.15, font=dict(color=C["ink2"]))
    fig.update_layout(**lay)
    fig.add_vline(x=spot, line_color=C["spot"], line_dash="dot", line_width=1)
    fig.update_yaxes(title_text=t(lang, "axis_iv"), title_font=dict(color=C["muted"]))
    return fig


def card(label: str, value: str, sub: str = "", accent: str | None = None) -> html.Div:
    return html.Div(
        [
            html.Div(label, style={"fontSize": "11px", "color": C["muted"], "textTransform": "uppercase", "letterSpacing": "0.05em"}),
            html.Div(value, style={"fontSize": "26px", "color": accent or C["ink"], "fontWeight": "600", "margin": "2px 0"}),
            html.Div(sub, style={"fontSize": "11px", "color": C["ink2"]}),
        ],
        style={
            "background": C["surface"], "borderRadius": "8px", "padding": "12px 16px",
            "border": "1px solid rgba(255,255,255,0.10)", "flex": "1", "minWidth": "140px",
        },
    )


def build_cards(symbol: str, lang: str, basis: float = 0.0) -> list:
    st = STATE.get(symbol)
    with STATE.lock:
        s = st.summary
        err = STATE.last_error
    if s is None:
        return [card(t(lang, "card_status"), "…", err or t(lang, "waiting_short"))]
    zg_txt = f"{s.zero_gamma + basis:.0f}" if s.zero_gamma else "n/a"
    zg_sub = ""
    if s.zero_gamma:
        d = s.spot - s.zero_gamma  # écart inchangé par le basis
        zg_sub = t(lang, "card_zg_sub", sign="+" if d >= 0 else "",
                   pts=f"{d:.0f}", reg="+" if d >= 0 else "-")
    gex_color = C["pos"] if s.net_gex >= 0 else C["neg"]
    feed_local = s.timestamp.replace(tzinfo=ET).astimezone(LOCAL_TZ)
    return [
        card(t(lang, "card_spot"), f"{s.spot + basis:,.0f}",
             t(lang, "card_feed", local=f"{feed_local:%H:%M:%S}", et=f"{s.timestamp:%H:%M}")),
        card(t(lang, "card_net_gex"), f"{s.net_gex / 1e9:+.1f} $Bn",
             t(lang, "stabilizing") if s.net_gex >= 0 else t(lang, "destabilizing"),
             accent=gex_color),
        card(t(lang, "card_zero_gamma"), zg_txt, zg_sub, accent=C["zg"]),
        card(t(lang, "card_gex_0dte"), f"{s.net_gex_0dte / 1e9:+.1f} $Bn"),
        card(t(lang, "card_pc_oi"), f"{s.pc_oi:.2f}"),
        card(t(lang, "card_pc_vol"), f"{s.pc_volume:.2f}"),
    ]


def create_app() -> Dash:
    app = Dash(__name__, title="GEX Dashboard")
    enabled = [u for u in UNDERLYINGS.values() if u.enabled]
    radio_style = dict(inputStyle={"marginRight": "4px"},
                       labelStyle={"marginRight": "14px", "color": C["ink2"]})
    app.layout = html.Div(
        style={"background": C["page"], "minHeight": "100vh", "padding": "16px 24px",
               "fontFamily": FONT, "color": C["ink"]},
        children=[
            html.Div(
                [
                    html.H1(t("fr", "app_title"), id="app-title",
                            style={"fontSize": "20px", "margin": "0", "fontWeight": "600"}),
                    html.Div(
                        [
                            dcc.RadioItems(
                                id="symbol",
                                options=[{"label": u.label, "value": u.key} for u in enabled],
                                value=enabled[0].key, inline=True, **radio_style,
                            ),
                            dcc.RadioItems(id="bucket", value="Tout", inline=True, **radio_style),
                            dcc.Checklist(id="majors", value=[], inline=True,
                                          inputStyle={"marginRight": "4px"},
                                          labelStyle={"color": C["ink2"]}),
                            dcc.RadioItems(
                                id="window",
                                options=[{"label": "±2%", "value": 0.02},
                                         {"label": "±4%", "value": 0.04},
                                         {"label": "±10%", "value": 0.10}],
                                value=0.04, inline=True, **radio_style,
                            ),
                            dcc.RadioItems(id="unit", value="index", inline=True, **radio_style),
                            dcc.RadioItems(
                                id="lang",
                                options=[{"label": l.upper(), "value": l} for l in LANGS],
                                value="fr", inline=True, **radio_style,
                            ),
                        ],
                        style={"display": "flex", "gap": "20px", "alignItems": "center",
                               "flexWrap": "wrap"},
                    ),
                ],
                style={"display": "flex", "justifyContent": "space-between",
                       "alignItems": "center", "marginBottom": "16px", "flexWrap": "wrap", "gap": "8px"},
            ),
            html.Div(id="cards", style={"display": "flex", "gap": "12px", "marginBottom": "12px", "flexWrap": "wrap"}),
            html.Div(id="levels", style={"display": "flex", "gap": "10px", "marginBottom": "16px",
                                         "flexWrap": "wrap", "alignItems": "center"}),
            html.Div(
                [
                    dcc.Graph(id="gex-strike", style={"flex": "1", "minWidth": "440px"}),
                    dcc.Graph(id="dex-strike", style={"flex": "1", "minWidth": "440px"}),
                ],
                style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "marginBottom": "12px"},
            ),
            html.Div(
                [
                    html.Span(id="flow-day-label", style={"color": C["muted"], "fontSize": "12px"}),
                    dcc.Dropdown(id="flow-day", clearable=False,
                                 style={"width": "160px", "color": "#111"}),
                    html.Button(
                        id="flow-today", n_clicks=0,
                        style={"background": C["surface"], "color": C["ink2"],
                               "border": "1px solid rgba(255,255,255,0.15)",
                               "borderRadius": "6px", "padding": "5px 12px",
                               "fontSize": "12px", "cursor": "pointer"},
                    ),
                ],
                style={"display": "flex", "gap": "8px", "alignItems": "center",
                       "marginBottom": "4px"},
            ),
            dcc.Graph(id="flow", style={"marginBottom": "12px"}),
            html.Div(
                [
                    dcc.Graph(id="gex-history", style={"flex": "1", "minWidth": "340px"}),
                    dcc.Graph(id="spot-zg", style={"flex": "1", "minWidth": "340px"}),
                    dcc.Graph(id="smile", style={"flex": "1", "minWidth": "340px"}),
                ],
                style={"display": "flex", "gap": "12px", "flexWrap": "wrap"},
            ),
            dcc.Interval(id="tick", interval=SETTINGS.flow_interval_s * 1000),
            dcc.Store(id="lang-boot", data=0),
            html.Div(id="footer",
                     style={"color": C["muted"], "fontSize": "11px", "marginTop": "12px"}),
        ],
    )

    def _chip(children, accent):
        return html.Span(
            children,
            style={"background": C["surface"], "border": f"1px solid {accent}",
                   "borderRadius": "6px", "padding": "4px 10px", "fontSize": "13px"},
        )

    def levels_strip(levels: pd.DataFrame | None, lang: str,
                     hvl: float | None = None, zg: float | None = None,
                     basis: float = 0.0, future: str | None = None,
                     keys: dict | None = None) -> list:
        if levels is None or levels.empty:
            return [html.Span(t(lang, "levels_unavailable"),
                              style={"color": C["muted"], "fontSize": "12px"})]
        exp = levels["expiry"].iloc[0]
        labels = wall_labels(levels)
        items = [html.Span(t(lang, "levels_prefix", exp=f"{exp:%d/%m}"),
                           style={"color": C["muted"], "fontSize": "12px", "marginRight": "4px"})]
        if basis and future:
            items.append(html.Span(
                t(lang, "basis_note", fut=future, basis=basis),
                style={"color": C["hvl"], "fontSize": "11px", "marginRight": "4px"}))
        if zg is not None:
            items.append(_chip([html.B("Gamma Flip ", style={"color": C["zg"]}),
                                f"{zg + basis:.0f}"], C["zg"]))
        if hvl is not None:
            items.append(_chip([html.B("HVL ", style={"color": C["hvl"]}),
                                f"{hvl + basis:.0f}"], C["hvl"]))
        # niveaux directionnels (support/résistance) et bornes de move attendu
        for key, color, label in (("call_wall", C["cw"], "Call Wall"),
                                  ("put_support", C["ps"], "Put Support"),
                                  ("d1_min", C["d1"], "1D Min"),
                                  ("d1_max", C["d1"], "1D Max")):
            v = (keys or {}).get(key)
            if v is not None:
                items.append(_chip([html.B(f"{label} ", style={"color": color}),
                                    f"{v + basis:.0f}"], color))
        for lv in levels.itertuples():
            side = t(lang, "side_call") if lv.gex > 0 else t(lang, "side_put")
            items.append(_chip(
                [html.B(f"{labels[lv.strike]} ", style={"color": C["lvl"]}),
                 f"{lv.strike + basis:.0f} ",
                 html.Span(f"({lv.gex / 1e9:+.1f} $Bn {side})",
                           style={"color": C["ink2"], "fontSize": "11px"})],
                "rgba(255,255,255,0.10)",
            ))
        return items

    # Détection de la langue du navigateur au chargement ; un choix manuel
    # (bouton FR/EN) est mémorisé dans localStorage et prime sur la détection.
    app.clientside_callback(
        """
        function(_) {
            const saved = window.localStorage.getItem('gex-lang');
            if (saved === 'fr' || saved === 'en') return saved;
            const nav = (navigator.language || 'en').slice(0, 2).toLowerCase();
            return nav === 'fr' ? 'fr' : 'en';
        }
        """,
        Output("lang", "value"),
        Input("lang-boot", "data"),
    )
    app.clientside_callback(
        "function(l) { window.localStorage.setItem('gex-lang', l); return window.dash_clientside.no_update; }",
        Output("lang-boot", "data"),
        Input("lang", "value"),
        prevent_initial_call=True,
    )

    @app.callback(
        [Output("bucket", "options"), Output("majors", "options"),
         Output("flow-day-label", "children"), Output("flow-today", "children"),
         Output("footer", "children"), Output("unit", "options")],
        [Input("lang", "value"), Input("symbol", "value")],
    )
    def apply_lang(lang, symbol):
        bucket_opts = [{"label": t(lang, BUCKET_KEYS[b]), "value": b} for b in EXPIRY_BUCKETS]
        majors_opts = [{"label": t(lang, "majors_only"), "value": "on"}]
        fut = UNDERLYINGS[symbol].future
        unit_opts = [{"label": t(lang, "unit_index"), "value": "index"},
                     {"label": fut or t(lang, "unit_futures"), "value": "futures",
                      "disabled": fut is None}]
        return (bucket_opts, majors_opts, t(lang, "flow_day_label"),
                t(lang, "last_session"), t(lang, "footer"), unit_opts)

    @app.callback(
        [Output("cards", "children"), Output("levels", "children"), Output("gex-strike", "figure"),
         Output("dex-strike", "figure"), Output("flow", "figure"),
         Output("gex-history", "figure"), Output("spot-zg", "figure"),
         Output("smile", "figure")],
        [Input("tick", "n_intervals"), Input("symbol", "value"),
         Input("bucket", "value"), Input("window", "value"),
         Input("majors", "value"), Input("flow-day", "value"),
         Input("lang", "value"), Input("unit", "value")],
    )
    def refresh(_, symbol, bucket, window, majors, flow_day, lang, unit):
        st = STATE.get(symbol)
        with STATE.lock:
            df = st.enriched
            snap = st.snapshot
            summary = st.summary
        bucket_label = t(lang, BUCKET_KEYS[bucket])
        if df is None or snap is None:
            wait = t(lang, "waiting_first_pull")
            return (
                build_cards(symbol, lang),
                levels_strip(None, lang),

                empty_fig(wait, t(lang, "gex_title", bucket=bucket_label)),
                empty_fig(wait, t(lang, "dex_title", bucket=bucket_label)),
                empty_fig(wait, t(lang, "flow_title")),
                empty_fig(wait, t(lang, "hist_title")),
                empty_fig(wait, t(lang, "spotzg_title")),
                empty_fig(wait, t(lang, "smile_title")),
            )
        today = datetime.now(ET).date()
        sel = df[metrics.bucket_mask(df, bucket, today)]
        zg = summary.zero_gamma if summary else None

        # uirevision : tant que la révision ne change pas, Plotly conserve le
        # zoom/pan de l'utilisateur à travers les refresh de dcc.Interval.
        def _pin(fig, rev):
            fig.update_layout(uirevision=rev)
            return fig

        # basis futures : déjà calculé au pull (et historisé) — pas recalculé
        # à chaque interaction UI. Il décroît vers 0 à l'approche de l'échéance.
        fut = UNDERLYINGS[symbol].future
        basis = 0.0
        if unit == "futures" and fut and summary is not None:
            basis = summary.basis or 0.0
        rev = f"{symbol}-{bucket}-{window}-{unit}"
        levels = metrics.top_gex_levels(df)
        if majors and not levels.empty:
            # ne garde que les murs pesant au moins 25 % du plus fort
            levels = levels[levels["gex"].abs() >= 0.25 * levels["gex"].abs().max()]
        hvl = metrics.zero_gamma(df, snap.spot, weight_col="volume")
        keys = metrics.key_levels(df, snap.spot)
        return (
            build_cards(symbol, lang, basis),
            levels_strip(levels, lang, hvl, zg, basis, fut, keys),
            _pin(exposure_fig(sel, snap.spot, zg, "gex",
                              t(lang, "gex_title", bucket=bucket_label), lang,
                              levels=levels, hvl=hvl, window=window, basis=basis,
                              keys=keys), rev),
            _pin(exposure_fig(sel, snap.spot, zg, "dex",
                              t(lang, "dex_title", bucket=bucket_label), lang,
                              window=window, basis=basis), rev),
            _pin(flow_fig(symbol, lang, flow_day), f"{symbol}-{flow_day}"),
            _pin(history_fig(symbol, lang), symbol),
            _pin(spot_zg_fig(symbol, lang), symbol),
            _pin(smile_fig(sel, snap.spot, lang), rev),
        )

    @app.callback(
        [Output("flow-day", "options"), Output("flow-day", "value")],
        [Input("symbol", "value"), Input("tick", "n_intervals")],
        State("flow-day", "value"),
    )
    def update_flow_days(symbol, _, current):
        days = available_flow_days(symbol)
        opts = [{"label": d, "value": d} for d in days]
        # sur un tick, ne pas écraser la sélection de l'utilisateur ;
        # sur changement de sous-jacent (ou sélection invalide), dernier jour
        if ctx.triggered_id == "tick" and current in days:
            return opts, current
        return opts, (days[-1] if days else None)

    @app.callback(
        Output("flow-day", "value", allow_duplicate=True),
        Input("flow-today", "n_clicks"),
        State("symbol", "value"),
        prevent_initial_call=True,
    )
    def back_to_today(_, symbol):
        today = datetime.now(ET).strftime("%Y-%m-%d")
        days = available_flow_days(symbol)
        # le jour courant s'il a des flux, sinon le plus récent disponible
        return today if today in days else (days[-1] if days else None)

    return app
