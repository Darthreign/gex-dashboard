"""Dashboard Dash : GEX/DEX par strike, indicateurs, flux delta, skew IV.

Palette : polarité (GEX/flux +/-) en diverging bleu↔rouge, identité
(calls/puts, expirations) sur les slots catégoriels — thème sombre.
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
    "zg": "#c98500",    # jaune sombre — niveau zero gamma
    "lvl": "#9085e9",   # violet — niveaux GEX 0DTE (GEX1..5)
    "hvl": "#199e70",   # aqua — HVL (bascule pondérée par le volume du jour)
    "cat": ["#3987e5", "#d95926", "#199e70", "#c98500"],  # slots 1-4
}

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Fuseau local de la machine — tous les axes temps sont affichés en heure locale
LOCAL_TZ = datetime.now().astimezone().tzinfo


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


def empty_fig(msg: str = "En attente du premier pull CBOE…", title: str = "") -> go.Figure:
    fig = go.Figure()
    fig.update_layout(**base_layout(title))
    fig.add_annotation(text=msg, showarrow=False, font=dict(color=C["muted"], size=13))
    return fig


def _bar_width(strikes: np.ndarray) -> float:
    diffs = np.diff(np.sort(np.unique(strikes)))
    return float(np.median(diffs)) * 0.75 if len(diffs) else 1.0


def exposure_fig(df: pd.DataFrame, spot: float, zg: float | None, col: str, title: str,
                 levels: pd.DataFrame | None = None, hvl: float | None = None,
                 window: float = 0.04) -> go.Figure:
    lo, hi = spot * (1 - window), spot * (1 + window)
    d = df[df["strike"].between(lo, hi)]
    agg = metrics.exposure_by_strike(d, col)
    if agg.empty:
        return empty_fig("Pas de données dans la fenêtre de strikes", title)
    net = agg["net"].to_numpy() / 1e9
    strikes = agg["strike"].to_numpy()
    colors = np.where(net >= 0, C["pos"], C["neg"])
    fig = go.Figure(
        go.Bar(
            y=strikes, x=net, orientation="h",
            width=_bar_width(strikes),
            marker=dict(color=colors, line=dict(width=0)),
            customdata=np.stack([agg["C"] / 1e9, agg["P"] / 1e9], axis=-1),
            hovertemplate=(
                "Strike %{y}<br>Net: %{x:.2f} $Bn"
                "<br>Calls: %{customdata[0]:.2f} $Bn"
                "<br>Puts: %{customdata[1]:.2f} $Bn<extra></extra>"
            ),
        )
    )
    fig.update_layout(**base_layout(title, height=560))
    fig.update_xaxes(title_text="$Bn par 1% de move", title_font=dict(color=C["muted"]))
    fig.add_hline(y=spot, line_color=C["spot"], line_dash="dot", line_width=1,
                  annotation_text=f"Spot {spot:.0f}", annotation_font_color=C["ink"],
                  annotation_position="top right")
    if zg is not None and lo <= zg <= hi:
        fig.add_hline(y=zg, line_color=C["zg"], line_dash="dash", line_width=1,
                      annotation_text=f"Flip (zero γ) {zg:.0f}", annotation_font_color=C["zg"],
                      annotation_position="bottom left")
    if hvl is not None and lo <= hvl <= hi:
        fig.add_hline(y=hvl, line_color=C["hvl"], line_dash="dash", line_width=1,
                      annotation_text=f"HVL {hvl:.0f}", annotation_font_color=C["hvl"],
                      annotation_position="bottom right")
    if levels is not None and not levels.empty:
        for lv in levels.itertuples():
            if not (lo <= lv.strike <= hi):
                continue
            fig.add_hline(
                y=lv.strike, line_color=C["lvl"], line_dash="dashdot",
                line_width=1, opacity=0.8,
                annotation_text=f"GEX{lv.rank} {lv.strike:.0f}",
                annotation_font=dict(color=C["lvl"], size=10),
                annotation_position="top left",
            )
    return fig


FLOW_TITLE = "Flux delta options (proxy Δvolume×δ, barres 1 min — délayé ~15 min)"


def available_flow_days(symbol: str) -> list[str]:
    root = SETTINGS.data_dir / "flows" / symbol
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.parquet"))


def flow_fig(symbol: str, day: str | None = None) -> go.Figure:
    day = day or datetime.now(ET).strftime("%Y-%m-%d")
    flows = store.load_flows(symbol, day)
    if flows.empty:
        return empty_fig(f"Aucun flux enregistré le {day}", FLOW_TITLE)
    ts = to_local(flows["timestamp"])
    vals = flows["flow_total"].to_numpy() / 1e6
    cum = np.cumsum(vals)
    fig = go.Figure()
    fig.add_bar(x=ts, y=vals, name="Flux/min",
                marker=dict(color=np.where(vals >= 0, C["pos"], C["neg"]), line=dict(width=0)),
                hovertemplate="%{x|%H:%M}<br>Flux: %{y:.1f} $M<extra></extra>")
    fig.add_scatter(x=ts, y=cum, mode="lines", name="Cumul", yaxis="y2",
                    line=dict(color=C["ink2"], width=2),
                    hovertemplate="%{x|%H:%M}<br>Cumul: %{y:.1f} $M<extra></extra>")
    lay = base_layout(FLOW_TITLE, height=300)
    # cumul en sous-échelle séparée sans second axe visible serait trompeur :
    # on empile deux panneaux partageant l'axe temps
    lay["yaxis"] = dict(domain=[0.55, 1.0], gridcolor=C["grid"], zerolinecolor=C["axis"],
                        title=dict(text="$M/min", font=dict(color=C["muted"])), tickfont=dict(color=C["muted"]))
    lay["yaxis2"] = dict(domain=[0.0, 0.45], gridcolor=C["grid"], zerolinecolor=C["axis"],
                         title=dict(text="Cumul $M", font=dict(color=C["muted"])), tickfont=dict(color=C["muted"]))
    lay["height"] = 380
    fig.update_layout(**lay)
    return fig


HIST_TITLE = "GEX net — historique ($Bn par 1%)"


def history_fig(symbol: str) -> go.Figure:
    hist = store.load_history(symbol)
    if hist.empty or len(hist) < 2:
        return empty_fig("Historique insuffisant (revenez après quelques snapshots)", HIST_TITLE)
    ts = to_local(hist["timestamp"])
    fig = go.Figure()
    fig.add_scatter(x=ts, y=hist["net_gex"] / 1e9, mode="lines", name="GEX net",
                    line=dict(color=C["cat"][0], width=2),
                    hovertemplate="%{x|%d/%m %H:%M}<br>GEX net: %{y:.1f} $Bn<extra></extra>")
    lay = base_layout(HIST_TITLE, height=300)
    fig.update_layout(**lay)
    return fig


SPOTZG_TITLE = "Spot vs Zero Gamma"


def spot_zg_fig(symbol: str) -> go.Figure:
    hist = store.load_history(symbol)
    if hist.empty or len(hist) < 2:
        return empty_fig("Historique insuffisant", SPOTZG_TITLE)
    ts = to_local(hist["timestamp"])
    fig = go.Figure()
    fig.add_scatter(x=ts, y=hist["spot"], mode="lines", name="Spot",
                    line=dict(color=C["cat"][0], width=2),
                    hovertemplate="%{x|%d/%m %H:%M}<br>Spot: %{y:.0f}<extra></extra>")
    fig.add_scatter(x=ts, y=hist["zero_gamma"], mode="lines", name="Zero gamma",
                    line=dict(color=C["zg"], width=2, dash="dash"),
                    hovertemplate="%{x|%d/%m %H:%M}<br>Zero γ: %{y:.0f}<extra></extra>")
    lay = base_layout(SPOTZG_TITLE, height=300)
    lay["showlegend"] = True
    lay["legend"] = dict(orientation="h", y=1.12, font=dict(color=C["ink2"]))
    fig.update_layout(**lay)
    return fig


def smile_fig(df: pd.DataFrame, spot: float) -> go.Figure:
    d = df[(df["iv"] > 0.01) & (df["open_interest"] > 0)
           & df["strike"].between(spot * 0.85, spot * 1.15)]
    # IV OTM : puts sous le spot, calls au-dessus (le smile standard)
    otm = d[((d["type"] == "P") & (d["strike"] <= spot)) | ((d["type"] == "C") & (d["strike"] > spot))]
    expiries = sorted(otm["expiry"].unique())[:4]
    if not expiries:
        return empty_fig("Pas d'IV exploitable", "Skew IV (options OTM) par expiration")
    fig = go.Figure()
    for i, exp in enumerate(expiries):
        e = otm[otm["expiry"] == exp].sort_values("strike")
        smoothed = e.groupby("strike")["iv"].mean()
        fig.add_scatter(x=smoothed.index, y=smoothed * 100, mode="lines",
                        name=str(exp), line=dict(color=C["cat"][i % 4], width=2),
                        hovertemplate=f"{exp}<br>Strike %{{x}}<br>IV: %{{y:.1f}}%<extra></extra>")
    lay = base_layout("Skew IV (options OTM) par expiration", height=300)
    lay["showlegend"] = True
    lay["legend"] = dict(orientation="h", y=1.15, font=dict(color=C["ink2"]))
    fig.update_layout(**lay)
    fig.add_vline(x=spot, line_color=C["spot"], line_dash="dot", line_width=1)
    fig.update_yaxes(title_text="IV %", title_font=dict(color=C["muted"]))
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


def build_cards(symbol: str) -> list:
    st = STATE.get(symbol)
    with STATE.lock:
        s = st.summary
        err = STATE.last_error
    if s is None:
        return [card("Statut", "…", err or "en attente du premier pull")]
    zg_txt = f"{s.zero_gamma:.0f}" if s.zero_gamma else "n/a"
    zg_sub = ""
    if s.zero_gamma:
        d = s.spot - s.zero_gamma
        zg_sub = f"spot {'+' if d >= 0 else ''}{d:.0f} pts ({'γ+ ' if d >= 0 else 'γ- '}régime)"
    gex_color = C["pos"] if s.net_gex >= 0 else C["neg"]
    feed_local = s.timestamp.replace(tzinfo=ET).astimezone(LOCAL_TZ)
    return [
        card("Spot (délayé 15 min)", f"{s.spot:,.0f}",
             f"feed {feed_local:%H:%M:%S} ({s.timestamp:%H:%M} ET)"),
        card("GEX net / 1%", f"{s.net_gex / 1e9:+.1f} $Bn",
             "stabilisant" if s.net_gex >= 0 else "déstabilisant", accent=gex_color),
        card("Zero Gamma", zg_txt, zg_sub, accent=C["zg"]),
        card("GEX 0DTE", f"{s.net_gex_0dte / 1e9:+.1f} $Bn"),
        card("P/C Open Interest", f"{s.pc_oi:.2f}"),
        card("P/C Volume", f"{s.pc_volume:.2f}"),
    ]


def create_app() -> Dash:
    app = Dash(__name__, title="GEX Dashboard")
    enabled = [u for u in UNDERLYINGS.values() if u.enabled]
    app.layout = html.Div(
        style={"background": C["page"], "minHeight": "100vh", "padding": "16px 24px",
               "fontFamily": FONT, "color": C["ink"]},
        children=[
            html.Div(
                [
                    html.H1("Gamma / Delta Exposure", style={"fontSize": "20px", "margin": "0", "fontWeight": "600"}),
                    html.Div(
                        [
                            dcc.RadioItems(
                                id="symbol",
                                options=[{"label": u.label, "value": u.key} for u in enabled],
                                value=enabled[0].key, inline=True,
                                inputStyle={"marginRight": "4px"},
                                labelStyle={"marginRight": "16px", "color": C["ink2"]},
                            ),
                            dcc.RadioItems(
                                id="bucket",
                                options=[{"label": b, "value": b} for b in EXPIRY_BUCKETS],
                                value="Tout", inline=True,
                                inputStyle={"marginRight": "4px"},
                                labelStyle={"marginRight": "16px", "color": C["ink2"]},
                            ),
                            dcc.Checklist(
                                id="majors",
                                options=[{"label": "Murs majeurs seulement", "value": "on"}],
                                value=[], inline=True,
                                inputStyle={"marginRight": "4px"},
                                labelStyle={"color": C["ink2"]},
                            ),
                            dcc.RadioItems(
                                id="window",
                                options=[{"label": "±2%", "value": 0.02},
                                         {"label": "±4%", "value": 0.04},
                                         {"label": "±10%", "value": 0.10}],
                                value=0.04, inline=True,
                                inputStyle={"marginRight": "4px"},
                                labelStyle={"marginRight": "12px", "color": C["ink2"]},
                            ),
                        ],
                        style={"display": "flex", "gap": "32px", "alignItems": "center"},
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
                    html.Span("Jour de flux :", style={"color": C["muted"], "fontSize": "12px"}),
                    dcc.Dropdown(id="flow-day", clearable=False,
                                 style={"width": "160px", "color": "#111"}),
                    html.Button(
                        "Dernière séance", id="flow-today", n_clicks=0,
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
            html.Div(
                "Données CBOE delayed (~15 min) — outil d'analyse, pas d'exécution.",
                style={"color": C["muted"], "fontSize": "11px", "marginTop": "12px"},
            ),
        ],
    )

    def _chip(children, accent):
        return html.Span(
            children,
            style={"background": C["surface"], "border": f"1px solid {accent}",
                   "borderRadius": "6px", "padding": "4px 10px", "fontSize": "13px"},
        )

    def levels_strip(levels: pd.DataFrame, hvl: float | None = None,
                     zg: float | None = None) -> list:
        if levels is None or levels.empty:
            return [html.Span("Niveaux GEX 0DTE : indisponibles", style={"color": C["muted"], "fontSize": "12px"})]
        exp = levels["expiry"].iloc[0]
        items = [html.Span(f"Niveaux 0DTE ({exp:%d/%m}) :",
                           style={"color": C["muted"], "fontSize": "12px", "marginRight": "4px"})]
        if zg is not None:
            items.append(_chip([html.B("Flip ", style={"color": C["zg"]}), f"{zg:.0f}"], C["zg"]))
        if hvl is not None:
            items.append(_chip([html.B("HVL ", style={"color": C["hvl"]}), f"{hvl:.0f}"], C["hvl"]))
        for lv in levels.itertuples():
            side = "call" if lv.gex > 0 else "put"
            items.append(_chip(
                [html.B(f"GEX{lv.rank} ", style={"color": C["lvl"]}),
                 f"{lv.strike:.0f} ",
                 html.Span(f"({lv.gex / 1e9:+.1f} $Bn {side})",
                           style={"color": C["ink2"], "fontSize": "11px"})],
                "rgba(255,255,255,0.10)",
            ))
        return items

    @app.callback(
        [Output("cards", "children"), Output("levels", "children"), Output("gex-strike", "figure"),
         Output("dex-strike", "figure"), Output("flow", "figure"),
         Output("gex-history", "figure"), Output("spot-zg", "figure"),
         Output("smile", "figure")],
        [Input("tick", "n_intervals"), Input("symbol", "value"),
         Input("bucket", "value"), Input("window", "value"),
         Input("majors", "value"), Input("flow-day", "value")],
    )
    def refresh(_, symbol, bucket, window, majors, flow_day):
        st = STATE.get(symbol)
        with STATE.lock:
            df = st.enriched
            snap = st.snapshot
            summary = st.summary
        if df is None or snap is None:
            return (
                build_cards(symbol),
                levels_strip(None),
                empty_fig(title="Gamma Exposure par strike"),
                empty_fig(title="Delta Exposure par strike"),
                empty_fig(title=FLOW_TITLE),
                empty_fig(title=HIST_TITLE),
                empty_fig(title=SPOTZG_TITLE),
                empty_fig(title="Skew IV (options OTM) par expiration"),
            )
        today = datetime.now(ET).date()
        sel = df[metrics.bucket_mask(df, bucket, today)]
        zg = summary.zero_gamma if summary else None
        # uirevision : tant que la révision ne change pas, Plotly conserve le
        # zoom/pan de l'utilisateur à travers les refresh de dcc.Interval.
        # Changer de sous-jacent/fenêtre/bucket réinitialise la vue (voulu).
        def _pin(fig, rev):
            fig.update_layout(uirevision=rev)
            return fig

        rev = f"{symbol}-{bucket}-{window}"
        levels = metrics.top_gex_levels(df)
        if majors and not levels.empty:
            # ne garde que les murs pesant au moins 25 % du plus fort
            levels = levels[levels["gex"].abs() >= 0.25 * levels["gex"].abs().max()]
        hvl = metrics.zero_gamma(df, snap.spot, weight_col="volume")
        return (
            build_cards(symbol),
            levels_strip(levels, hvl, zg),
            _pin(exposure_fig(sel, snap.spot, zg, "gex", f"Gamma Exposure par strike — {bucket}",
                              levels=levels, hvl=hvl, window=window), rev),
            _pin(exposure_fig(sel, snap.spot, zg, "dex", f"Delta Exposure par strike — {bucket}",
                              window=window), rev),
            _pin(flow_fig(symbol, flow_day), f"{symbol}-{flow_day}"),
            _pin(history_fig(symbol), symbol),
            _pin(spot_zg_fig(symbol), symbol),
            _pin(smile_fig(sel, snap.spot), rev),
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
