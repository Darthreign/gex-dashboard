"""Dashboard Dash : GEX/DEX par strike, indicateurs, flux delta, skew IV.

Palette : polarité (GEX/flux +/-) en diverging bleu↔rouge, identité
(calls/puts, expirations) sur les slots catégoriels — thème sombre.
Interface FR/EN (gex/i18n.py) ; termes de trading standards dans les deux.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, ctx, dcc, html
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate

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

TAB_STYLE = {"backgroundColor": "#0d0d0d", "color": "#898781",
             "border": "1px solid #2c2c2a", "padding": "8px 14px", "fontSize": "13px"}
TAB_SELECTED = {"backgroundColor": "#1a1a19", "color": "#ffffff",
                "border": "1px solid #2c2c2a", "borderTop": "2px solid #3987e5",
                "padding": "8px 14px", "fontSize": "13px", "fontWeight": "600"}
HINT_STYLE = {"color": "#898781", "fontSize": "11px", "marginBottom": "8px"}
TABS = ("main", "profile", "greeks2", "pos")


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
        title=dict(text=title, font=dict(size=13, color=C["ink"], family=FONT),
                   x=0.012, y=0.97, xanchor="left"),
        template=None,
        paper_bgcolor=C["surface"],
        plot_bgcolor=C["surface"],
        font=dict(family=FONT, size=11, color=C["ink2"]),
        margin=dict(l=58, r=18, t=42, b=38),
        height=height,
        xaxis=dict(gridcolor=C["grid"], zerolinecolor=C["axis"], linecolor=C["axis"], tickfont=dict(color=C["muted"])),
        yaxis=dict(gridcolor=C["grid"], zerolinecolor=C["axis"], linecolor=C["axis"], tickfont=dict(color=C["muted"])),
        hoverlabel=dict(bgcolor=C["page"], font=dict(family=FONT, color=C["ink"])),
        showlegend=False,
    )


def with_legend(lay: dict) -> dict:
    """Légende en haut à droite + marge suffisante : le titre est aligné à
    gauche, une légende centrée viendrait le chevaucher."""
    lay["showlegend"] = True
    lay["margin"]["t"] = 62
    lay["legend"] = dict(orientation="h", y=1.13, x=1, xanchor="right",
                         font=dict(color=C["ink2"], size=11))
    return lay


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
    lay = with_legend(lay)
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
    lay = with_legend(lay)
    fig.update_layout(**lay)
    fig.add_vline(x=spot, line_color=C["spot"], line_dash="dot", line_width=1)
    fig.update_yaxes(title_text=t(lang, "axis_iv"), title_font=dict(color=C["muted"]))
    return fig


def profile_fig(df: pd.DataFrame, spot: float, zg: float | None, lang: str,
                window: float, basis: float = 0.0) -> go.Figure:
    """Courbe de GEX net en fonction d'un spot hypothétique."""
    title = t(lang, "profile_title")
    res = metrics.gamma_profile(df, spot, range_pct=window, steps=201)
    if res is None:
        return empty_fig(t(lang, "no_data_window"), title)
    grid, prof = res
    x = grid + basis
    y = prof / 1e9
    fig = go.Figure()
    # deux traces pour colorer par polarité sans trompe-l'œil sur l'axe
    fig.add_scatter(x=x, y=np.where(y >= 0, y, np.nan), mode="lines",
                    line=dict(color=C["pos"], width=2), name="GEX +",
                    hovertemplate="%{x:.0f}<br>%{y:.1f} $Bn<extra></extra>")
    fig.add_scatter(x=x, y=np.where(y < 0, y, np.nan), mode="lines",
                    line=dict(color=C["neg"], width=2), name="GEX −",
                    hovertemplate="%{x:.0f}<br>%{y:.1f} $Bn<extra></extra>")
    fig.update_layout(**base_layout(title, height=420))
    fig.update_xaxes(title_text=t(lang, "profile_axis"), title_font=dict(color=C["muted"]))
    fig.update_yaxes(title_text="$Bn / 1%", title_font=dict(color=C["muted"]))
    fig.add_hline(y=0, line_color=C["axis"], line_width=1)
    fig.add_vline(x=spot + basis, line_color=C["spot"], line_dash="dot", line_width=1,
                  annotation_text=f"Spot {spot + basis:.0f}", annotation_font_color=C["ink"])
    if zg is not None:
        fig.add_vline(x=zg + basis, line_color=C["zg"], line_dash="dash", line_width=1,
                      annotation_text=f"Gamma Flip {zg + basis:.0f}",
                      annotation_font_color=C["zg"], annotation_position="bottom right")
    return fig


def profile_by_expiry_fig(df: pd.DataFrame, spot: float, lang: str,
                          window: float, basis: float = 0.0) -> go.Figure:
    """Profil décomposé par bucket d'échéance : ce que pèse le 0DTE seul."""
    title = t(lang, "profile_by_exp")
    today = datetime.now(ET).date()
    fig = go.Figure()
    drawn = 0
    for i, bucket in enumerate(EXPIRY_BUCKETS):
        sub = df[metrics.bucket_mask(df, bucket, today)]
        res = metrics.gamma_profile(sub, spot, range_pct=window, steps=201)
        if res is None:
            continue
        grid, prof = res
        fig.add_scatter(x=grid + basis, y=prof / 1e9, mode="lines",
                        name=t(lang, BUCKET_KEYS[bucket]),
                        line=dict(color=C["cat"][i % 4], width=2),
                        hovertemplate="%{x:.0f}<br>%{y:.1f} $Bn<extra></extra>")
        drawn += 1
    if drawn == 0:
        return empty_fig(t(lang, "no_data_window"), title)
    lay = base_layout(title, height=340)
    lay = with_legend(lay)
    fig.update_layout(**lay)
    fig.add_hline(y=0, line_color=C["axis"], line_width=1)
    fig.add_vline(x=spot + basis, line_color=C["spot"], line_dash="dot", line_width=1)
    fig.update_xaxes(title_text=t(lang, "profile_axis"), title_font=dict(color=C["muted"]))
    return fig


def second_order_fig(df: pd.DataFrame, spot: float, col: str, title: str,
                     window: float, basis: float = 0.0) -> go.Figure:
    """Exposition vanna (vex) ou charm (cex) par strike."""
    lo, hi = spot * (1 - window), spot * (1 + window)
    d = df[df["strike"].between(lo, hi)]
    if d.empty:
        return empty_fig("—", title)
    agg = d.groupby("strike")[col].sum() / 1e6
    strikes = agg.index.to_numpy() + basis
    vals = agg.to_numpy()
    fig = go.Figure(go.Bar(
        y=strikes, x=vals, orientation="h",
        width=_bar_width(agg.index.to_numpy()),
        marker=dict(color=np.where(vals >= 0, C["pos"], C["neg"]), line=dict(width=0)),
        hovertemplate="%{y}<br>%{x:.1f} $M<extra></extra>",
    ))
    fig.update_layout(**base_layout(title, height=460))
    fig.add_hline(y=spot + basis, line_color=C["spot"], line_dash="dot", line_width=1,
                  annotation_text=f"Spot {spot + basis:.0f}", annotation_font_color=C["ink"],
                  annotation_position="top right")
    fig.update_xaxes(title_text="$M", title_font=dict(color=C["muted"]))
    return fig


def oi_change_fig(chg: pd.DataFrame, spot: float, lang: str, prev_day: str,
                  window: float, basis: float = 0.0) -> go.Figure:
    """Variation d'OI par strike, calls et puts distingués (identité, pas polarité)."""
    title = t(lang, "pos_title", day=prev_day)
    if chg.empty:
        return empty_fig(t(lang, "pos_no_prev"), title)
    lo, hi = spot * (1 - window), spot * (1 + window)
    d = chg[chg["strike"].between(lo, hi)]
    if d.empty:
        return empty_fig(t(lang, "no_data_window"), title)
    if (d["d_call"].abs().sum() + d["d_put"].abs().sum()) == 0:
        # même séance des deux côtés : l'OI n'est publié qu'une fois par jour
        return empty_fig(t(lang, "pos_no_change"), title)
    strikes = d["strike"].to_numpy() + basis
    w = _bar_width(d["strike"].to_numpy()) / 2
    fig = go.Figure()
    fig.add_bar(y=strikes, x=d["d_call"] / 1000, orientation="h", width=w,
                name=t(lang, "legend_calls"),
                marker=dict(color=C["cat"][0], line=dict(width=0)),
                hovertemplate="%{y}<br>Calls %{x:+.1f}k<extra></extra>")
    fig.add_bar(y=strikes, x=d["d_put"] / 1000, orientation="h", width=w,
                name=t(lang, "legend_puts"),
                marker=dict(color=C["cat"][1], line=dict(width=0)),
                hovertemplate="%{y}<br>Puts %{x:+.1f}k<extra></extra>")
    lay = base_layout(title, height=520)
    lay = with_legend(lay)
    lay["barmode"] = "group"
    fig.update_layout(**lay)
    fig.add_hline(y=spot + basis, line_color=C["spot"], line_dash="dot", line_width=1,
                  annotation_text=f"Spot {spot + basis:.0f}", annotation_font_color=C["ink"],
                  annotation_position="top right")
    fig.update_xaxes(title_text="Δ OI (milliers de contrats)", title_font=dict(color=C["muted"]))
    return fig


def _basis_for(symbol: str, unit: str, summary) -> float:
    """Décalage à appliquer aux prix affichés (0 en mode indice)."""
    if unit != "futures" or summary is None:
        return 0.0
    return (summary.basis or 0.0) if UNDERLYINGS[symbol].future else 0.0


def card(label: str, value: str, sub: str = "", accent: str | None = None) -> html.Div:
    """Tuile d'indicateur : liseré coloré à gauche quand la valeur porte un signe."""
    return html.Div(
        [
            html.Div(label, className="stat-label"),
            html.Div(value, className="stat-value",
                     style={"color": accent} if accent else None),
            html.Div(sub, className="stat-sub"),
        ],
        className="stat",
        style={"--accent-bar": accent} if accent else None,
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
    # assets/ est à la racine du projet, pas à côté du module gex/
    app = Dash(__name__, title="GEX Dashboard",
               assets_folder=str(Path(__file__).resolve().parent.parent / "assets"))
    enabled = [u for u in UNDERLYINGS.values() if u.enabled]

    def ctl(label_id, control):
        """Contrôle étiqueté : la légende dit ce que le segment pilote."""
        return html.Div([html.Span(id=label_id, className="ctl-label"), control],
                        className="ctl")

    app.layout = html.Div([
        # ------------------------------------------------------ barre haute
        html.Div([
            html.Div([
                html.Div([
                    html.Div("Γ", className="brand-mark"),
                    html.Span(id="app-title"),
                    html.Span(id="brand-sub", className="brand-sub"),
                ], className="brand"),
                html.Div([
                    dcc.RadioItems(
                        id="symbol", className="seg",
                        options=[{"label": u.label, "value": u.key} for u in enabled],
                        value=enabled[0].key, inline=True),
                    dcc.RadioItems(id="unit", className="seg", value="index", inline=True),
                    dcc.RadioItems(
                        id="lang", className="seg",
                        options=[{"label": l.upper(), "value": l} for l in LANGS],
                        value="fr", inline=True),
                    # page statique servie depuis assets/ (nouvel onglet)
                    html.A(id="faq-link", className="linkbtn", href="/assets/faq.html",
                           target="_blank", children="FAQ"),
                ], style={"display": "flex", "gap": "10px", "flexWrap": "wrap",
                          "alignItems": "center"}),
            ], className="topbar-row"),
            html.Div([
                ctl("lbl-bucket", dcc.RadioItems(id="bucket", className="seg",
                                                 value="Tout", inline=True)),
                ctl("lbl-window", dcc.RadioItems(
                    id="window", className="seg",
                    options=[{"label": "±2%", "value": 0.02},
                             {"label": "±4%", "value": 0.04},
                             {"label": "±10%", "value": 0.10}],
                    value=0.04, inline=True)),
                dcc.Checklist(id="majors", className="check", value=[], inline=True),
            ], className="toolbar"),
        ], className="topbar"),

        # ---------------------------------------------------------- contenu
        html.Div([
            html.Div(id="cards", className="cards"),
            html.Div(id="levels", className="chips"),
            dcc.Tabs(id="tab", value="main", className="tabbar", children=[
                dcc.Tab(value=v, label="", id=f"tabh-{v}",
                        className="tab-item", selected_className="tab-item--selected")
                for v in TABS
            ]),

            html.Div(id="pane-main", children=[
                html.Div([
                    dcc.Graph(id="gex-strike"),
                    dcc.Graph(id="dex-strike"),
                ], className="row", style={"marginBottom": "12px"}),
                html.Div([
                    html.Span(id="flow-day-label", className="ctl-label"),
                    dcc.Dropdown(id="flow-day", clearable=False,
                                 style={"width": "160px"}),
                    html.Button(id="flow-today", n_clicks=0, className="btn"),
                ], className="daybar"),
                dcc.Graph(id="flow", style={"marginBottom": "12px"}),
                html.Div([
                    dcc.Graph(id="gex-history"),
                    dcc.Graph(id="spot-zg"),
                    dcc.Graph(id="smile"),
                ], className="row"),
            ]),

            html.Div(id="pane-profile", children=[
                html.Div(id="profile-hint", className="hint"),
                dcc.Graph(id="profile", style={"marginBottom": "12px"}),
                dcc.Graph(id="profile-exp"),
            ]),

            html.Div(id="pane-greeks2", children=[
                html.Div(id="g2-hint", className="hint"),
                html.Div(id="g2-cards", className="cards"),
                html.Div([
                    dcc.Graph(id="vex"),
                    dcc.Graph(id="cex"),
                ], className="row"),
            ]),

            html.Div(id="pane-pos", children=[
                html.Div(id="pos-hint", className="hint"),
                dcc.Graph(id="oi-change"),
            ]),

            dcc.Interval(id="tick", interval=SETTINGS.flow_interval_s * 1000),
            dcc.Store(id="lang-boot", data=0),
            html.Div(id="footer", className="footer"),
        ], className="page"),
    ])

    def _chip(children, accent):
        return html.Span(children, className="chip", style={"--chip-accent": accent})

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
         Output("footer", "children"), Output("unit", "options"),
         Output("app-title", "children"), Output("brand-sub", "children"),
         Output("lbl-bucket", "children"), Output("lbl-window", "children")],
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
                t(lang, "last_session"), t(lang, "footer"), unit_opts,
                t(lang, "app_title"), t(lang, "brand_sub"),
                t(lang, "lbl_expiry"), t(lang, "lbl_window"))

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
        basis = _basis_for(symbol, unit, summary)
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
        [Output(f"pane-{v}", "style") for v in TABS] +
        [Output(f"tabh-{v}", "label") for v in TABS],
        [Input("tab", "value"), Input("lang", "value")],
    )
    def switch_tab(tab, lang):
        styles = [{"display": "block"} if v == tab else {"display": "none"} for v in TABS]
        labels = [t(lang, f"tab_{v}") for v in TABS]
        return styles + labels

    @app.callback(
        [Output("profile", "figure"), Output("profile-exp", "figure"),
         Output("profile-hint", "children")],
        [Input("tick", "n_intervals"), Input("tab", "value"), Input("symbol", "value"),
         Input("window", "value"), Input("lang", "value"), Input("unit", "value")],
    )
    def refresh_profile(_, tab, symbol, window, lang, unit):
        if tab != "profile":   # onglet masqué : rien à recalculer
            raise PreventUpdate
        st = STATE.get(symbol)
        with STATE.lock:
            df, snap, summary = st.enriched, st.snapshot, st.summary
        if df is None or snap is None:
            e = empty_fig(t(lang, "waiting_first_pull"), t(lang, "profile_title"))
            return e, e, t(lang, "profile_hint")
        basis = _basis_for(symbol, unit, summary)
        zg = summary.zero_gamma if summary else None
        # fenêtre élargie : la courbe n'a d'intérêt que si elle montre le flip
        w = max(window, 0.06)
        return (profile_fig(df, snap.spot, zg, lang, w, basis),
                profile_by_expiry_fig(df, snap.spot, lang, w, basis),
                t(lang, "profile_hint"))

    @app.callback(
        [Output("vex", "figure"), Output("cex", "figure"),
         Output("g2-cards", "children"), Output("g2-hint", "children")],
        [Input("tick", "n_intervals"), Input("tab", "value"), Input("symbol", "value"),
         Input("bucket", "value"), Input("window", "value"),
         Input("lang", "value"), Input("unit", "value")],
    )
    def refresh_greeks2(_, tab, symbol, bucket, window, lang, unit):
        if tab != "greeks2":
            raise PreventUpdate
        st = STATE.get(symbol)
        with STATE.lock:
            df, snap, summary = st.enriched, st.snapshot, st.summary
        if df is None or snap is None:
            e = empty_fig(t(lang, "waiting_first_pull"))
            return e, e, [], t(lang, "vex_hint")
        basis = _basis_for(symbol, unit, summary)
        today = datetime.now(ET).date()
        sel = metrics.add_second_order(df[metrics.bucket_mask(df, bucket, today)], snap.spot)
        cards = [
            card(t(lang, "vex_card"), f"{sel['vex'].sum() / 1e9:+.2f} $Bn",
                 t(lang, "vex_title").split("(")[-1].rstrip(")")),
            card(t(lang, "cex_card"), f"{sel['cex'].sum() / 1e9:+.2f} $Bn",
                 t(lang, "cex_title").split("(")[-1].rstrip(")")),
        ]
        return (second_order_fig(sel, snap.spot, "vex", t(lang, "vex_title"), window, basis),
                second_order_fig(sel, snap.spot, "cex", t(lang, "cex_title"), window, basis),
                cards, t(lang, "vex_hint"))

    @app.callback(
        [Output("oi-change", "figure"), Output("pos-hint", "children")],
        [Input("tick", "n_intervals"), Input("tab", "value"), Input("symbol", "value"),
         Input("window", "value"), Input("lang", "value"), Input("unit", "value")],
    )
    def refresh_positioning(_, tab, symbol, window, lang, unit):
        if tab != "pos":
            raise PreventUpdate
        st = STATE.get(symbol)
        with STATE.lock:
            df, snap, summary = st.enriched, st.snapshot, st.summary
        if df is None or snap is None:
            return empty_fig(t(lang, "waiting_first_pull")), t(lang, "pos_hint")
        basis = _basis_for(symbol, unit, summary)
        today = datetime.now(ET).strftime("%Y-%m-%d")
        prev = store.load_previous_snapshot(symbol, today)
        if prev is None:
            return (empty_fig(t(lang, "pos_no_prev"), t(lang, "pos_title", day="—")),
                    t(lang, "pos_hint"))
        prev_day, prev_df = prev
        chg = metrics.oi_change(prev_df, df)
        return (oi_change_fig(chg, snap.spot, lang, prev_day, window, basis),
                t(lang, "pos_hint"))

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
