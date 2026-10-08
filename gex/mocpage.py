"""Page /moc : pression mécanique estimée sur la clôture (cf. gex/moc.py).

Même principe que /scalp : une page dédiée, NQ et ES seulement, le reste du
dashboard masqué (classe `moc-page` sur <body>, cf. style.css). Un bandeau
(compte à rebours + sens et taille de la pression en contrats), des tuiles
par composante, le profil « si la clôture se fait à X », la dernière heure
de prix avec le flux agresseur et la pression estimée au fil du temps, puis
l'historique de validation (scripts/moc_report.py).

Rien n'est calculé hors de la page : l'intervalle `moc-tick` n'est actif
que sur /moc, et le calcul est partagé entre onglets (broadcast.shared).
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, dcc, html
from dash.exceptions import PreventUpdate

from . import broadcast, moc, positioning, store
from .i18n import t
from .metrics import ET

REFRESH_MS = 5000
TRAIL_EVERY_S = 15.0
_TRAIL: dict[str, deque] = {}
_TRAIL_LOCK = threading.Lock()


def is_moc_path(path: str | None) -> bool:
    p = path or "/"
    return p == "/moc" or p.startswith("/moc/")


# --- calcul -------------------------------------------------------------------

def live_estimate(symbol: str, now_et: datetime | None = None) -> dict | None:
    """Estimation sur l'état courant des chaînes de la famille (STATE, ou son
    miroir en mode moteur)."""
    from .api import _futures_last_price
    from .app import chain_state
    now_et = now_et or datetime.now(ET)
    fam = moc.FAMILIES[symbol]
    chains, flows = [], {}
    for s in fam["chains"]:
        st = chain_state(s)
        if st.enriched is None or st.snapshot is None:
            continue
        chains.append(moc.ChainInput(s, st.enriched, float(st.snapshot.spot)))
        flows[s] = positioning.daily_taker_flow(s, now_et.date())
    if not chains:
        return None
    fut = _futures_last_price(symbol)
    if not fut:
        fs = chain_state(symbol).snapshot
        fut = float(fs.spot) if fs is not None else None
    idx = next((c.spot for c in chains if c.symbol == fam["index"]), None)
    ret = moc.day_return(idx, store.previous_close_spot(fam["index"], now_et.strftime("%Y-%m-%d")))
    letf, custom = moc.letf_config(symbol)
    est = moc.estimate(symbol, chains, flows, fut, ret, now_et, letf)
    est["letf_custom"] = custom
    est["phase"] = moc.session_phase(now_et)
    _record_trail(symbol, est, now_et)
    return est


def _record_trail(symbol: str, est: dict, now_et: datetime) -> None:
    """Pression estimée au fil de la dernière heure (mémoire du processus)."""
    if est["phase"] not in ("fenetre", "noii") or est["contracts"] is None:
        return
    with _TRAIL_LOCK:
        q = _TRAIL.setdefault(symbol, deque(maxlen=400))
        if q and q[-1][0].date() != now_et.date():
            q.clear()
        if q and (now_et - q[-1][0]).total_seconds() < TRAIL_EVERY_S:
            return
        q.append((now_et, est["contracts"], est["options_total"], est["letf"]["total"] or 0.0))


def trail(symbol: str) -> pd.DataFrame:
    with _TRAIL_LOCK:
        rows = list(_TRAIL.get(symbol, ()))
    return pd.DataFrame(rows, columns=["ts", "contracts", "options", "letf"])


# --- rendu --------------------------------------------------------------------

def _usd(v: float | None) -> str:
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:+.2f} Md$"
    if a >= 1e6:
        return f"{v / 1e6:+.0f} M$"
    return f"{v / 1e3:+.0f} k$"


def _side(v: float | None, lang: str) -> str:
    if v is None or abs(v) < 1:
        return t(lang, "moc_flat")
    return t(lang, "moc_buy") if v > 0 else t(lang, "moc_sell")


def countdown(now_et: datetime) -> str:
    close = datetime.combine(now_et.date(), moc.CLOSE, ET)
    s = int((close - now_et).total_seconds())
    if s <= 0 or now_et.weekday() >= 5:
        return "—"
    h, rem = divmod(s, 3600)
    # signe moins : un compte à rebours, pas une heure
    return f"−{h}h{rem // 60:02d}" if h else f"−{rem // 60:02d}:{rem % 60:02d}"


def banner(symbol: str, est: dict | None, lang: str, now_et: datetime) -> html.Div:
    if est is None:
        return html.Div(t(lang, "moc_waiting"), className="hint")
    k = est["contracts"]
    size = f"{k:+,.0f} {symbol}" if k is not None else _usd(est["total"])
    # ton : la pression n'est vraiment lisible qu'à l'approche de la cloche
    tone = "neutral" if est["phase"] in ("avant", "clos") else (
        "ok" if (k or 0) > 0 else "alert" if (k or 0) < 0 else "neutral")
    title = f"MOC {symbol} · {_side(est['total'], lang)} {size}"
    detail = t(lang, "moc_detail", opt=_usd(est["options_total"]),
               exp=_usd(est["expiring_total"]), charm=_usd(est["charm_total"]),
               letf=_usd(est["letf"]["total"]))
    phase = html.Span(t(lang, f"moc_phase_{est['phase']}"), className="moc-phase")
    return html.Div([
        html.Div([html.Div(title, className="sc-banner-title"),
                  html.Div(detail, className="sc-banner-detail"),
                  html.Div(phase, className="sc-lights")], className="sc-banner-main"),
        html.Div([html.Div(countdown(now_et), className="moc-countdown"),
                  html.Div(t(lang, "moc_to_close"), className="stat-sub")],
                 className="sc-banner-side moc-clock"),
    ], className=f"sc-banner sc-tone-{tone}")


def cards(symbol: str, est: dict | None, lang: str) -> list:
    from .app import C, card
    if est is None:
        return []

    def acc(v):
        return None if not v else C["pos"] if v > 0 else C["neg"]
    out = []
    for s in moc.FAMILIES[symbol]["chains"]:
        p = est["options"].get(s)
        if p is None:
            out.append(card(s, "—", t(lang, "moc_chain_missing")))
            continue
        out.append(card(t(lang, "moc_card_chain", sym=s), _usd(p["total"]),
                        t(lang, "moc_card_chain_sub", exp=_usd(p["expiring"]),
                          charm=_usd(p["charm"])), acc(p["total"])))
    cash = est.get("cash_unwind_total") or 0.0
    if cash:
        per = moc.FAMILIES[symbol]["fut_mult"] * est["fut_price"] if est["fut_price"] else None
        out.append(card(t(lang, "moc_card_cash"), _usd(cash),
                        (f"{cash / per:+,.0f} {symbol} · " if per else "") + t(lang, "moc_card_cash_sub")))
    r = est["day_return"]
    out.append(card(t(lang, "moc_card_letf"), _usd(est["letf"]["total"]),
                    (f"{r:+.2%} · " if r is not None else "")
                    + _letf_source(symbol, lang),
                    acc(est["letf"]["total"])))
    mags = est["magnets"]
    if mags:
        m = mags[0]
        out.append(card(t(lang, "moc_card_magnet", chain=m["chain"]), f"{m['strike']:,.0f}",
                        " · ".join(f"{x['strike']:,.0f} ({x['dist']:+,.0f})" for x in mags)))
    return out


def _letf_source(symbol: str, lang: str) -> str:
    from .letf_aum import freshness
    day, stale = freshness(symbol)
    if day is None:
        return t(lang, "moc_letf_default")
    return t(lang, "moc_letf_stale" if stale else "moc_letf_asof", day=day)


def profile_fig(symbol: str, est: dict | None, lang: str) -> go.Figure:
    from .app import C, base_layout, empty_fig
    title = t(lang, "moc_profile_title")
    if est is None or est["profile"] is None or est["profile"].empty:
        return empty_fig(t(lang, "moc_waiting"), title)
    p = est["profile"]
    per = (moc.FAMILIES[symbol]["fut_mult"] * est["fut_price"]) if est["fut_price"] else None
    # le rééquilibrage dépend aussi du niveau de clôture (rendement du jour)
    r0 = est["day_return"]
    lev = sum((L * L - L) * aum for L, aum in moc.letf_config(symbol)[0].values())
    tot = p["total"] + (lev * ((1 + r0) * (1 + p["move"]) - 1) if r0 is not None else 0.0)
    y = tot / per if per else tot / 1e6
    x = (est["fut_price"] * (1 + p["move"])) if est["fut_price"] else p["move"] * 100
    colors = [C["pos"] if v > 0 else C["neg"] for v in y]
    fig = go.Figure(go.Bar(x=x, y=y, marker_color=colors,
                           hovertemplate="%{x:,.0f} → %{y:+,.0f}<extra></extra>"))
    lay = base_layout(title, height=340)
    lay["yaxis"]["title"] = symbol if per else "M$"
    fig.update_layout(**lay)
    if est["fut_price"]:
        fig.add_vline(x=est["fut_price"], line_color=C["spot"], line_dash="dot")
    for m in est["magnets"]:
        if est["fut_price"] and m["chain"] == symbol:
            fig.add_vline(x=m["strike"], line_color=C["lvl"], line_width=1)
    return fig


def tape_fig(symbol: str, lang: str) -> go.Figure:
    """Dernière heure : prix (1 min), delta agresseur par minute et pression
    estimée au fil du temps."""
    from plotly.subplots import make_subplots

    from .app import C, _scalp_day_ticks, base_layout, empty_fig
    title = t(lang, "moc_tape_title")
    ticks = _scalp_day_ticks(symbol, sides_only=True)
    if ticks is None or ticks.empty:
        return empty_fig(t(lang, "moc_no_ticks"), title)
    ts = pd.to_datetime(ticks["ts"], unit="s", utc=True).dt.tz_convert(ET)
    end = ts.max()
    start = end - timedelta(minutes=60)
    keep = (ts >= start).to_numpy()
    d = ticks[keep].assign(m=ts[keep].dt.floor("min").to_numpy())
    if d.empty:
        return empty_fig(t(lang, "moc_no_ticks"), title)
    sgn = d["side"].map({"BUY": 1, "SELL": -1}).fillna(0)
    g = d.assign(sv=sgn * d["volume"]).groupby("m").agg(
        o=("price", "first"), h=("price", "max"), l=("price", "min"), c=("price", "last"),
        delta=("sv", "sum"))
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.68, 0.32],
                        vertical_spacing=0.04, specs=[[{"secondary_y": True}], [{}]])
    fig.add_trace(go.Candlestick(x=g.index, open=g["o"], high=g["h"], low=g["l"], close=g["c"],
                                 increasing_line_color=C["pos"], decreasing_line_color=C["neg"],
                                 name=symbol), row=1, col=1)
    tr = trail(symbol)
    if not tr.empty:
        fig.add_trace(go.Scatter(x=tr["ts"], y=tr["contracts"], mode="lines",
                                 line=dict(color=C["zg"], width=2),
                                 name=t(lang, "moc_trail")), row=1, col=1, secondary_y=True)
    fig.add_trace(go.Bar(x=g.index, y=g["delta"],
                         marker_color=[C["pos"] if v > 0 else C["neg"] for v in g["delta"]],
                         name="delta"), row=2, col=1)
    lay = base_layout(title, height=460)
    fig.update_layout(**lay)
    fig.update_layout(xaxis_rangeslider_visible=False)
    for ax in ("xaxis", "xaxis2", "yaxis", "yaxis2", "yaxis3"):
        fig.layout[ax].update(gridcolor=C["grid"], tickfont=dict(color=C["muted"]))
    fig.layout["yaxis2"].update(showgrid=False, title=t(lang, "moc_trail"))
    noii = datetime.combine(end.date(), moc.NOII_START, ET)
    if start <= noii <= end + timedelta(minutes=10):
        fig.add_vline(x=noii, line_color=C["muted"], line_dash="dot")
    return fig


def history_table(symbol: str, lang: str) -> html.Div:
    from .moc_report import history_path
    p = history_path(symbol)
    if not p.exists():
        return html.Div(t(lang, "moc_hist_none"), className="hint")
    h = pd.read_csv(p).dropna(subset=["contracts", "move_pts"]).tail(15).iloc[::-1]
    if h.empty:
        return html.Div(t(lang, "moc_hist_none"), className="hint")
    hit = (h["contracts"].apply(lambda v: v > 0) == (h["move_pts"] > 0)).mean()
    head = html.Tr([html.Th(x) for x in (t(lang, "moc_h_day"), t(lang, "moc_h_est"),
                                         t(lang, "moc_h_move"), "")])
    rows = [html.Tr([html.Td(r.day), html.Td(f"{r.contracts:+,.0f}"),
                     html.Td(f"{r.move_pts:+,.2f}"),
                     html.Td("✓" if (r.contracts > 0) == (r.move_pts > 0) else "✗",
                             className="moc-ok" if (r.contracts > 0) == (r.move_pts > 0)
                             else "moc-ko")])
            for r in h.itertuples()]
    return html.Div([
        html.Div(t(lang, "moc_hist_title", n=len(h), hit=f"{hit:.0%}"), className="moc-hist-title"),
        html.Table([html.Thead(head), html.Tbody(rows)], className="moc-hist"),
    ])


# --- page ---------------------------------------------------------------------

def layout() -> html.Div:
    return html.Div(id="pane-moc", children=[
        dcc.Interval(id="moc-tick", interval=REFRESH_MS, disabled=True),
        html.Div(id="moc-banner", className="sc-bannerbox moc-bannerbox"),
        html.Div(id="moc-cards", className="cards moc-cards"),
        html.Div([
            html.Div(dcc.Graph(id="moc-tape", config={"displaylogo": False}), className="moc-col"),
            html.Div(dcc.Graph(id="moc-profile", config={"displaylogo": False}), className="moc-col"),
        ], className="moc-row"),
        html.Div(id="moc-history", className="moc-history"),
        html.Div(id="moc-note", className="hint moc-note"),
    ])


def register(app) -> None:
    @app.callback(Output("moc-tick", "disabled"), Input("url", "pathname"))
    def moc_tick_on(path):
        return not is_moc_path(path)

    @app.callback(
        [Output("moc-banner", "children"), Output("moc-cards", "children"),
         Output("moc-tape", "figure"), Output("moc-profile", "figure"),
         Output("moc-history", "children"), Output("moc-note", "children")],
        [Input("moc-tick", "n_intervals"), Input("url", "pathname"),
         Input("symbol", "value"), Input("lang", "value")],
    )
    @broadcast.shared(ttl=REFRESH_MS / 1000 - 0.5, skip=(0,))
    def refresh_moc(_, path, symbol, lang):
        if not is_moc_path(path) or symbol not in moc.FAMILIES:
            raise PreventUpdate
        now = datetime.now(ET)
        est = live_estimate(symbol, now)
        note = t(lang, "moc_note")
        if est is not None and est["missing"]:
            note = t(lang, "moc_missing", chains=", ".join(est["missing"])) + " " + note
        return (banner(symbol, est, lang, now), cards(symbol, est, lang),
                tape_fig(symbol, lang), profile_fig(symbol, est, lang),
                history_table(symbol, lang), note)
