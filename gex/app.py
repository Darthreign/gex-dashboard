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

from . import metrics, scales, store
from .api import register_api
from .config import SETTINGS, UNDERLYINGS, targets
from .i18n import LANGS, regime_text, t, wall_labels
from .metrics import ET, EXPIRY_BUCKETS
from . import idxopt
from .rtquote import PUBLIC_QUOTES, QUOTES, credentials_present
from .scheduler import STATE, market_is_open
from .scheduler import native_index_key as scheduler_native_key

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
    "ok": "#199e70",    # vert — donnée temps réel
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
TABS = ("main", "profile", "greeks2", "heat", "pos")


def to_local(ts: pd.Series) -> pd.Series:
    """Timestamps stockés naïfs en heure de New York → heure locale (naïve)."""
    return (
        pd.to_datetime(ts)
        .dt.tz_localize(ET, ambiguous="NaT", nonexistent="NaT")
        .dt.tz_convert(LOCAL_TZ)
        .dt.tz_localize(None)
    )


# --- Titres cliquables vers le guide -------------------------------------
# Chaque titre de graphique renvoie à l'ancre correspondante du guide sur
# GitHub. Plotly rend un sous-ensemble de HTML dans les titres, dont <a> :
# aucun composant supplémentaire n'est nécessaire.
#
# Les ancres sont posées EXPLICITEMENT dans les .md (<a id="..."></a>) plutôt
# que déduites du texte des titres : GitHub dérive ses ancres du libellé, donc
# reformuler un titre casserait silencieusement le lien — et nos titres sont
# traduits, ce qui donnerait deux ancres différentes pour un même graphique.
GUIDE_URL = ("https://github.com/Darthreign/gex-dashboard/blob/main/docs/guide/"
             "{page}#{anchor}")
GUIDE_ANCHORS: dict[str, tuple[str, str]] = {
    "gex_strike": ("1-vue-principale.md", "gex-par-strike"),
    "dex_strike": ("1-vue-principale.md", "dex-par-strike"),
    "flow": ("1-vue-principale.md", "flux-delta"),
    "gflow": ("1-vue-principale.md", "gamma-echange"),
    "tape": ("1-vue-principale.md", "order-flow-signe"),
    "history": ("1-vue-principale.md", "historique"),
    "spot_zg": ("1-vue-principale.md", "spot-vs-flip"),
    "smile": ("1-vue-principale.md", "skew-iv"),
    "profile": ("2-gamma-profile.md", "profil"),
    "vex": ("3-vanna-charm.md", "vanna"),
    "cex": ("3-vanna-charm.md", "charm"),
    "heat": ("4-heatmap.md", "heatmap"),
    "pos": ("5-positionnement.md", "positionnement"),
}


def guided(title: str, key: str) -> str:
    """Titre enrichi d'un lien vers la section du guide qui l'explique.

    Renvoie le titre nu si la clé est inconnue : un lien manquant ne doit
    jamais faire disparaître un titre.
    """
    entry = GUIDE_ANCHORS.get(key)
    if entry is None:
        return title
    page, anchor = entry
    url = GUIDE_URL.format(page=page, anchor=anchor)
    return f'<a href="{url}" target="_blank" style="color:inherit">{title} ↗</a>'


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
        # Pan par défaut : avec le zoom, un simple glissement recadre le
        # graphique sans intention. Le zoom reste accessible à la molette
        # (scrollZoom) et par la barre d'outils.
        dragmode="pan",
    )


# Molette = zoom, barre d'outils allégée des sélections inutiles ici.
GRAPH_CONFIG = {
    "scrollZoom": True,
    "displaylogo": False,
    "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"],
}


def time_range_selector() -> dict:
    """Boutons de période sur les séries temporelles longues."""
    return dict(
        rangeselector=dict(
            buttons=[
                dict(count=1, label="1H", step="hour", stepmode="backward"),
                dict(count=1, label="1J", step="day", stepmode="backward"),
                dict(count=7, label="1S", step="day", stepmode="backward"),
                dict(count=1, label="1M", step="month", stepmode="backward"),
                dict(count=3, label="3M", step="month", stepmode="backward"),
                dict(step="all", label="Tout"),
            ],
            bgcolor=C["surface"], activecolor=C["axis"],
            bordercolor=C["grid"], borderwidth=1,
            font=dict(color=C["ink2"], size=10),
            x=1, xanchor="right", y=1.22, yanchor="top",
        ),
    )


def default_window(ts: pd.Series, days: int = 7) -> list | None:
    """Plage affichée par défaut : les `days` derniers jours, si l'historique
    est plus long. Les boutons de période permettent d'élargir."""
    if ts.empty:
        return None
    end = ts.max()
    start = end - pd.Timedelta(days=days)
    return [start, end] if ts.min() < start else None


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


def tv_levels_string(levels: pd.DataFrame | None, hvl: float | None,
                     zg: float | None, keys: dict | None, xf=None) -> str:
    """Sérialise les niveaux au format attendu par l'indicateur TradingView
    « GEX Levels (Dealer Gamma Exposure) » : ``prix,libellé,type;...``

    Les codes de type (``res``, ``sup``, ``flip``…) pilotent le style de tracé
    côté indicateur. Deux correspondances méritent d'être signalées :
    - HVL est envoyé en ``flip`` : c'est bien une bascule, pondérée par le
      volume du jour plutôt que par l'open interest ;
    - 1D Min/Max part en ``eml``/``emh`` (expected move), ce qu'ils sont —
      les bornes du straddle ATM.

    Les prix sont transposés par ``xf`` : la chaîne sort donc déjà dans
    l'échelle affichée (indice, ES ou NQ), prête pour la zone de collage
    correspondante de l'indicateur.
    """
    xf = xf or (lambda v: v)
    out: list[str] = []
    seen: list[float] = []

    # Deux lignes plus proches que ça sont indiscernables à l'œil sur un
    # graphique, et leurs étiquettes se chevauchent. Le seuil reste très en
    # dessous de l'écart entre deux strikes (25-50 pts sur les indices, 1 $ sur
    # les ETF) : deux murs distincts ne peuvent donc jamais être confondus.
    MERGE_TOL = 0.0002  # 0,02 % — soit ~1,5 pt sur ES

    def add(value, label, kind, dedup=False):
        """dedup : n'écrit pas un mur déjà couvert par un niveau nommé.

        Call Wall et Put Support sont choisis dans le même classement de
        strikes que GEX1-5, et le flip tombe souvent sur un mur : sans ce
        filtre, TradingView superpose des lignes dont les étiquettes se
        recouvrent. Le niveau nommé l'emporte, étant le plus parlant.
        """
        if value is None:
            return
        px = xf(value)
        if dedup and any(abs(px - s) <= MERGE_TOL * abs(px) for s in seen):
            return
        seen.append(px)
        out.append(f"{px:.2f},{label},{kind}")

    add(zg, "Gamma Flip", "flip")
    add(hvl, "HVL", "flip")
    k = keys or {}
    add(k.get("call_wall"), "Call Wall", "res")
    add(k.get("put_support"), "Put Support", "sup")
    add(k.get("d1_max"), "1D Max", "emh")
    add(k.get("d1_min"), "1D Min", "eml")
    if levels is not None and not levels.empty:
        labels = wall_labels(levels)
        for lv in levels.itertuples():
            # gpos/gneg = murs classés par gamma absolu, signe selon calls/puts
            add(lv.strike, labels[lv.strike], "gpos" if lv.gex > 0 else "gneg",
                dedup=True)
    return ";".join(out)


def _draw_levels(fig, items: list[dict], lo: float, hi: float) -> None:
    """Trace des lignes horizontales de niveau.

    Chaque étiquette reste posée SUR sa propre ligne, sans décalage : la
    déplacer pour éviter un voisin la ferait désigner un prix qui n'est pas le
    sien, ce qui trompe davantage qu'un chevauchement visible. Les niveaux
    sont répartis entre les deux graphiques et entre les deux côtés, ce qui
    suffit à les espacer dans la grande majorité des cas.

    items : dicts {y, label, color, dash, side} ; side ∈ {"left", "right"}.
    """
    for it in items:
        if it["y"] is None or not (lo <= it["y"] <= hi):
            continue
        fig.add_hline(
            y=it["y"], line_color=it["color"], line_dash=it.get("dash", "dash"),
            line_width=it.get("width", 1),
            annotation_text=(it["label"] if it.get("short")
                             else f"{it['label']} {it['y']:.0f}"),
            annotation_font=dict(color=it["color"], size=10),
            annotation_position=f"top {it.get('side', 'right')}",
        )


def _bar_width(strikes: np.ndarray) -> float:
    diffs = np.diff(np.sort(np.unique(strikes)))
    return float(np.median(diffs)) * 0.75 if len(diffs) else 1.0


def exposure_fig(df: pd.DataFrame, spot: float, zg: float | None, col: str, title: str,
                 lang: str, levels: pd.DataFrame | None = None, hvl: float | None = None,
                 window: float = 0.04, xf=None,
                 keys: dict | None = None, level_set: str = "walls") -> go.Figure:
    # `xf` transpose les prix vers l'échelle d'affichage choisie.
    xf = xf or (lambda v: v)
    lo, hi = spot * (1 - window), spot * (1 + window)
    d = df[df["strike"].between(lo, hi)]
    agg = metrics.exposure_by_strike(d, col)
    if agg.empty:
        return empty_fig(t(lang, "no_data_window"), title)
    net = agg["net"].to_numpy() / 1e9
    strikes = xf(agg["strike"].to_numpy())
    spot = xf(spot)
    zg = xf(zg) if zg is not None else None
    hvl = xf(hvl) if hvl is not None else None
    lo, hi = xf(lo), xf(hi)
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
    # Niveaux répartis entre les deux graphiques pour ne pas surcharger :
    #   "walls"  (GEX) : murs de gamma — c'est là qu'ils se lisent
    #   "regime" (DEX) : bascules de régime et bornes de move attendu
    items = [dict(y=spot, label="Spot", color=C["spot"], dash="dot", side="right")]
    if level_set == "walls":
        for key, color, label in (("call_wall", C["cw"], "Call Wall"),
                                  ("put_support", C["ps"], "Put Support")):
            v = (keys or {}).get(key)
            if v is not None:
                items.append(dict(y=xf(v), label=label, color=color,
                                  dash="solid", width=1.5, side="right"))
        if levels is not None and not levels.empty:
            labels = wall_labels(levels)
            for lv in levels.itertuples():
                # rang ET prix : les murs sont seuls du côté gauche depuis que
                # les niveaux de régime sont passés sur le graphe DEX, la place
                # est donc disponible
                items.append(dict(y=xf(lv.strike), label=labels[lv.strike],
                                  color=C["lvl"], dash="dashdot", side="left"))
    else:
        items += [dict(y=zg, label="Gamma Flip", color=C["zg"], side="left"),
                  dict(y=hvl, label="HVL", color=C["hvl"], side="left")]
        for key, label in (("d1_max", "1D Max"), ("d1_min", "1D Min")):
            v = (keys or {}).get(key)
            if v is not None:
                items.append(dict(y=xf(v), label=label, color=C["d1"],
                                  dash="dot", width=1.5, side="right"))
    _draw_levels(fig, items, lo, hi)
    return fig


def available_flow_days(symbol: str) -> list[str]:
    root = SETTINGS.data_dir / "flows" / symbol
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.parquet"))


def heatmap_fig(symbol: str, lang: str, day: str | None = None,
                window: float = 0.04, xf=None, unit: str | None = None,
                levels_shown: list[str] | None = None) -> go.Figure:
    """Profil de gamma en barres + parcours du prix, sur un axe de prix commun.

    Deux échelles horizontales partagent l'axe vertical des prix : les barres
    se lisent en $Bn sur l'axe du haut, le prix en heures sur celui du bas.
    C'est ce partage qui fait tout l'intérêt — on voit immédiatement si le
    marché évolue au contact d'une concentration de gamma ou à distance.

    Deux pondérations sont tracées. L'open interest décrit le positionnement
    installé ; le volume du jour, ce qui se traite et donc se couvre
    maintenant. Un strike lourd en volume mais absent en open interest est un
    niveau qui prend de l'importance en séance.
    """
    day = day or datetime.now(ET).strftime("%Y-%m-%d")
    title = guided(t(lang, "heat_title", day=day), "heat")
    xf = xf or (lambda v: v)
    levels_shown = (levels_shown if levels_shown is not None
                    else ["zero_gamma", "call_wall", "put_support"])

    df, spot = _chain_for_day(symbol, day)
    if df is None or df.empty or not spot:
        return empty_fig(t(lang, "heat_none", day=day), title)

    # Parcours du prix : si l'échelle affichée est un future qui a SON PROPRE
    # historique (NQ/ES), on le prend tel quel — inutile de transposer une
    # approximation quand le prix réel existe déjà à cette échelle. Sinon on
    # retombe sur l'historique du symbole natif, passé par xf.
    path, native_price = None, False
    if unit and unit in ("NQ", "ES") and unit != symbol:
        alt = _price_overlay(unit, day)
        if alt is not None and not alt.empty:
            path, native_price = alt, True
    if path is None:
        path = _price_overlay(symbol, day)

    lo, hi = spot * (1 - window), spot * (1 + window)
    sel = df[df["strike"].between(lo, hi)]
    if sel.empty:
        return empty_fig(t(lang, "no_data_window"), title)

    oi = metrics.gex_by_strike_weighted(sel, spot, "open_interest") / 1e9
    vol = metrics.gex_by_strike_weighted(sel, spot, "volume") / 1e9

    fig = go.Figure()
    # Barres épaisses (open interest) en fond, barres fines (volume) devant :
    # superposées plutôt que côte à côte, l'écart entre les deux se lit d'un
    # coup d'œil sur un même strike.
    for serie, name, width, colors in (
        (oi, t(lang, "legend_gex_oi"), 0.75, (C["pos"], C["neg"])),
        (vol, t(lang, "legend_gex_vol"), 0.38, ("#7fb2ee", "#f0a1a1")),
    ):
        if serie.empty:
            continue
        v = serie.to_numpy()
        # Toutes les barres partent de zéro vers la droite : la longueur est la
        # magnitude, directement comparable d'un strike à l'autre, et la couleur
        # porte le signe. Les tracer de part et d'autre de zéro obligerait à
        # comparer deux demi-échelles opposées.
        fig.add_bar(
            y=xf(serie.index.to_numpy()), x=np.abs(v), orientation="h", name=name,
            width=_bar_width(xf(serie.index.to_numpy())) * width,
            marker=dict(color=np.where(v >= 0, colors[0], colors[1]),
                        line=dict(width=0)),
            xaxis="x2", customdata=v,
            hovertemplate=(f"{t(lang, 'hover_strike')} %{{y}}<br>{name}"
                           " %{customdata:+.2f} $Bn<extra></extra>"),
        )

    if path is not None and not path.empty:
        ts = to_local(path["timestamp"])
        # bougies véritables si open/high/low/close sont distincts quelque
        # part (sinon — repli sur les spots de snapshots — les 4 valent la
        # même chose et une bougie n'aurait aucun sens à tracer)
        has_ohlc = (path["open"] != path["close"]).any() or (path["high"] != path["low"]).any()
        _id = (lambda v: v) if native_price else xf
        if has_ohlc:
            fig.add_candlestick(
                x=ts, open=_id(path["open"].to_numpy()), high=_id(path["high"].to_numpy()),
                low=_id(path["low"].to_numpy()), close=_id(path["close"].to_numpy()),
                name=t(lang, "legend_spot"), increasing=dict(line=dict(color=C["pos"])),
                decreasing=dict(line=dict(color=C["neg"])),
            )
        else:
            fig.add_scatter(x=ts, y=_id(path["close"].to_numpy()),
                            mode="lines", name=t(lang, "legend_spot"),
                            line=dict(color="#22d3ee", width=1.3),
                            hovertemplate=(f"%{{x|%H:%M}}<br>{t(lang, 'legend_spot')}"
                                           " %{y:.0f}<extra></extra>"))

    # repères horizontaux, choisis par la checklist de l'onglet — seul le
    # spot reste toujours affiché, comme référence de lecture systématique
    keys = metrics.key_levels(sel, spot)
    items = [dict(y=xf(spot), label=t(lang, "legend_spot"), color=C["spot"], dash="dot")]
    if "zero_gamma" in levels_shown:
        zg = metrics.zero_gamma(df, spot)
        if zg is not None:
            items.append(dict(y=xf(zg), label="Gamma Flip", color=C["zg"], dash="dash"))
    if "hvl" in levels_shown:
        hvl = metrics.zero_gamma(df, spot, weight_col="volume")
        if hvl is not None:
            items.append(dict(y=xf(hvl), label="HVL", color=C["hvl"], dash="dash"))
    for opt_key, key, color, label in (
        ("call_wall", "call_wall", C["cw"], "Call Wall"),
        ("put_support", "put_support", C["ps"], "Put Support"),
        ("d1", "d1_min", C["d1"], "1D Min"),
        ("d1", "d1_max", C["d1"], "1D Max"),
    ):
        if opt_key not in levels_shown:
            continue
        v = keys.get(key)
        if v is not None:
            items.append(dict(y=xf(v), label=label, color=color, dash="dash"))
    if "gex_walls" in levels_shown:
        walls = metrics.top_gex_levels(sel, ref_spot=spot)
        labels = wall_labels(walls) if not walls.empty else {}
        for lv in walls.itertuples():
            items.append(dict(y=xf(lv.strike), label=labels.get(lv.strike, "GEX"),
                              color=C["lvl"], dash="dot"))
    _draw_levels(fig, items, xf(lo), xf(hi))

    lay = with_legend(base_layout(title, height=560))
    lay["barmode"] = "overlay"
    lay["yaxis"]["title"] = dict(text=t(lang, "heat_axis_strike"),
                                 font=dict(color=C["muted"]))
    lay["xaxis"]["title"] = dict(text=t(lang, "heat_axis_time"),
                                 font=dict(color=C["muted"]))
    # Type déclaré explicitement : les seules traces portant des données sont
    # les barres, qui vivent sur le second axe. Sans cela Plotly ne peut pas
    # deviner que l'axe du bas est temporel et l'affiche en nanosecondes.
    lay["xaxis"]["type"] = "date"
    lay["xaxis"]["tickformat"] = "%H:%M"
    # Fenêtre fixée sur la séance : sans cela, une journée peu fournie écrase
    # l'échelle sur quelques minutes et le graphique devient illisible.
    lay["xaxis"]["range"] = _session_range(day)
    # axe des barres en haut, superposé à l'axe temps
    lay["xaxis2"] = dict(overlaying="x", side="top", showgrid=False,
                         zeroline=True, zerolinecolor=C["axis"],
                         rangemode="tozero",   # ancrage à gauche
                         tickfont=dict(color=C["muted"]),
                         title=dict(text=t(lang, "heat_axis_bn"),
                                    font=dict(color=C["muted"])))
    fig.update_layout(**lay)
    return fig


def _session_range(day: str) -> list:
    """Bornes de la séance américaine (9h30-16h15 ET), en heure locale."""
    bounds = pd.Series([pd.Timestamp(f"{day} 09:30"), pd.Timestamp(f"{day} 16:15")])
    return list(to_local(bounds))


def _chain_for_day(symbol: str, day: str) -> tuple[pd.DataFrame | None, float | None]:
    """Chaîne de référence d'une séance et son spot.

    Pour la journée en cours on prend l'état vivant, plus frais que le dernier
    snapshot persisté ; pour une séance passée, le dernier snapshot du jour.
    """
    # Séance passée comme séance en cours : dxFeed s'il a laissé des
    # snapshots, CBOE sinon — la même règle partout, y compris pour relire
    # l'historique.
    if day != datetime.now(ET).strftime("%Y-%m-%d"):
        rt = scheduler_native_key(symbol)
        alt = store.load_last_snapshot(rt, day)
        if alt is not None and not alt.empty and "spot" in alt.columns:
            return alt, float(alt["spot"].iloc[0])
    if day == datetime.now(ET).strftime("%Y-%m-%d"):
        st = chain_state(symbol)
        with STATE.lock:
            df, snap = st.enriched, st.snapshot
        if df is not None and snap is not None:
            return df, snap.spot
    df = store.load_last_snapshot(symbol, day)
    if df is None or df.empty:
        return None, None
    spot = float(df["spot"].iloc[0]) if "spot" in df.columns else None
    return df, spot


def _price_overlay(symbol: str, day: str) -> pd.DataFrame | None:
    """Parcours du prix pour le heatmap : bougies 1 min (open/high/low/close),
    à défaut les spots des snapshots (plus grossiers, une seule valeur par
    pull — open=high=low=close, pas de vraies bougies possibles avec ça)."""
    px = store.load_prices(symbol, day)
    if not px.empty:
        return px.sort_values("timestamp")[["timestamp", "open", "high", "low", "close"]]
    h = store.load_history(symbol)
    if h.empty:
        return None
    hts = pd.to_datetime(h["timestamp"])
    sel = h[hts.dt.strftime("%Y-%m-%d") == day].sort_values("timestamp")
    if sel.empty:
        return None
    out = sel[["timestamp", "spot"]].rename(columns={"spot": "close"})
    out["open"] = out["high"] = out["low"] = out["close"]
    return out


def gamma_flow_fig(symbol: str, lang: str, day: str | None = None,
                   series: list[str] | None = None) -> go.Figure:
    """Gamma échangé cumulé sur la séance, calls contre puts.

    L'équivalent d'un CVD appliqué au gamma : chaque pas de temps ajoute le
    gamma des contrats qui se sont traités, compté positif sur les calls et
    négatif sur les puts. La divergence entre les deux courbes montre de quel
    côté afflue le flux — un décrochage des puts signale un marché qui se
    charge en gamma déstabilisant, terrain d'un retournement.

    Même limite que le flux delta : le sens taker n'est pas observable dans ce
    feed. On mesure l'activité pondérée par le gamma, pas un flux signé.
    """
    day = day or datetime.now(ET).strftime("%Y-%m-%d")
    flows, src = flow_source(symbol, day, ("net_gamma_calls", "net_gamma_puts"))
    signe = src == "dxfeed"
    title = guided(t(lang, "gflow_title_signed" if signe else "gflow_title"), "gflow")
    col_c, col_p = ("net_gamma_calls", "net_gamma_puts") if signe else ("gflow_calls", "gflow_puts")
    if flows.empty or col_c not in flows.columns:
        # colonnes absentes = journée collectée avant l'ajout de cette mesure
        return empty_fig(t(lang, "no_flow_day", day=day), title)
    series = series if series is not None else ["calls", "puts", "net"]
    ts = to_local(flows["timestamp"])
    calls = np.cumsum(flows[col_c].fillna(0.0).to_numpy()) / 1e9
    puts = np.cumsum(flows[col_p].fillna(0.0).to_numpy()) / 1e9
    net = calls + puts

    fig = go.Figure()
    for key, y, name, color in (("calls", calls, t(lang, "legend_gcalls"), C["pos"]),
                                ("puts", puts, t(lang, "legend_gputs"), C["neg"])):
        if key not in series:
            continue
        fig.add_scatter(x=ts, y=y, mode="lines", name=name,
                        line=dict(color=color, width=1.5),
                        hovertemplate=f"%{{x|%H:%M}}<br>{name}: %{{y:+.2f}} $Bn<extra></extra>")
    if "net" in series:
        fig.add_scatter(x=ts, y=net, mode="lines", name=t(lang, "legend_gnet"),
                        line=dict(color=C["ink"], width=2),
                        hovertemplate=f"%{{x|%H:%M}}<br>{t(lang, 'legend_gnet')}: %{{y:+.2f}} $Bn<extra></extra>")
    lay = with_legend(base_layout(title, height=320))
    lay["yaxis"]["title"] = dict(text=t(lang, "axis_gflow_bn"),
                                 font=dict(color=C["muted"]))
    fig.update_layout(**lay)
    fig.add_hline(y=0, line_color=C["axis"], line_width=1)
    return fig


def flow_source(symbol: str, day: str, dx_cols: tuple[str, ...]):
    """(données, source) pour les graphiques de flux, selon UNE règle unique :
    dxFeed s'il est disponible, CBOE sinon.

    C'est la même règle que `chain_state` applique aux chaînes, et elle vaut
    pour tout ce qui s'affiche — sinon l'abonnement temps réel ne sert à rien.
    `tape/` porte le flux réellement SIGNÉ (côté agresseur donné par la
    source) ; `flows/` le proxy Δvolume×δ calculé sur CBOE, non signé et
    délayé de 15 min.

    `dx_cols` : les colonnes dont l'appelant a BESOIN côté dxFeed. Le choix se
    fait sur leur présence, pas sur la seule existence du fichier — une
    journée collectée avant l'ajout d'une mesure a bien un fichier `tape/`,
    mais sans la colonne. Sans ce test, le graphique recevait le tableau
    dxFeed puis y cherchait des colonnes CBOE, et s'affichait VIDE (constaté
    sur les captures du guide, sur le gamma échangé du 2026-07-29).

    ⚠️ Les deux ne couvrent PAS le même périmètre : le proxy CBOE porte sur
    toute la chaîne, le flux signé sur la fenêtre souscrite par flowtape
    (±1,5 %, 2 échéances). Les amplitudes ne sont donc pas comparables d'une
    source à l'autre — d'où la source rendue avec les données, pour que le
    titre du graphique le dise au lieu de le laisser deviner.
    """
    tape = store.load_tape(symbol, day)
    if not tape.empty and all(c in tape.columns for c in dx_cols):
        return tape.sort_values("timestamp"), "dxfeed"
    return store.load_flows(symbol, day), "cboe"


def tape_fig(symbol: str, lang: str, day: str | None = None,
             series: list[str] | None = None) -> go.Figure:
    """Order flow SIGNÉ cumulé sur la séance (cf. gex/flowtape.py).

    À ne pas confondre avec `flow_fig` / `gamma_flow_fig` juste au-dessus :
    ceux-là mesurent une activité pondérée, sans savoir qui a agressé le
    carnet. Ici le côté vient de la source (`aggressorSide`), donc la courbe
    dit réellement si les preneurs de liquidité ont acheté ou vendu.

    Monte = les agresseurs achètent net, descend = ils vendent net. Les
    jambes de combos sont exclues du net (elles ne sont pas directionnelles)
    et les prints sont pondérés par leur taille, jamais comptés à l'unité.
    """
    day = day or datetime.now(ET).strftime("%Y-%m-%d")
    tape = store.load_tape(symbol, day)
    title = guided(t(lang, "tape_title"), "tape")
    if tape.empty:
        return empty_fig(t(lang, "no_tape_day", day=day), title)
    series = series if series is not None else ["net", "calls", "puts"]
    tape = tape.sort_values("timestamp")
    ts = to_local(tape["timestamp"])
    # Courbe pondérée par le DELTA, pas par le nombre de contrats : c'est ce
    # qui en fait une mesure d'impact de couverture (cf. gex/flowtape.py).
    # Les journées collectées avant l'ajout de cette colonne retombent sur le
    # décompte de contrats plutôt que d'afficher une courbe plate.
    if "net_delta" in tape.columns:
        net = np.cumsum(tape["net_delta"].fillna(0.0).to_numpy()) / 1e6
        unit, axis = t(lang, "unit_musd"), t(lang, "axis_tape_delta")
    else:
        net = np.cumsum(tape["net_contracts"].fillna(0.0).to_numpy())
        unit, axis = t(lang, "unit_contracts"), t(lang, "axis_tape")
    calls = np.cumsum(tape["net_calls"].fillna(0.0).to_numpy())
    puts = np.cumsum(tape["net_puts"].fillna(0.0).to_numpy())

    fig = go.Figure()
    if "net" in series:
        fig.add_scatter(x=ts, y=net, mode="lines", name=t(lang, "legend_tape_net"),
                        line=dict(color=C["ink"], width=2.2),
                        hovertemplate=(f"%{{x|%H:%M}}<br>{t(lang, 'legend_tape_net')}:"
                                       f" %{{y:+,.1f}} {unit}<extra></extra>"))
    # calls et puts restent en CONTRATS, sur leur propre axe : mélanger deux
    # unités sur une même échelle donnerait une lecture fausse
    for key, y, name, color in (
        ("calls", calls, t(lang, "legend_tape_calls"), C["pos"]),
        ("puts", puts, t(lang, "legend_tape_puts"), C["neg"]),
    ):
        if key not in series:
            continue
        fig.add_scatter(x=ts, y=y, mode="lines", name=name, yaxis="y2",
                        line=dict(color=color, width=1.3, dash="dot"),
                        hovertemplate=(f"%{{x|%H:%M}}<br>{name}: %{{y:+,.0f}}"
                                       f" {t(lang, 'unit_contracts')}<extra></extra>"))
    lay = with_legend(base_layout(title, height=340))
    lay["yaxis"]["title"] = dict(text=axis, font=dict(color=C["muted"]))
    # marge droite élargie : sans elle le titre du second axe se dessine
    # PAR-DESSUS les courbes (constaté sur les captures du guide)
    lay["margin"]["r"] = 64
    lay["yaxis2"] = dict(overlaying="y", side="right", showgrid=False,
                         zeroline=False, tickfont=dict(color=C["muted"]),
                         title=dict(text=t(lang, "axis_tape"),
                                    font=dict(color=C["muted"]), standoff=8))
    fig.update_layout(**lay)
    fig.add_hline(y=0, line_color=C["axis"], line_width=1)
    return fig


def flow_fig(symbol: str, lang: str, day: str | None = None) -> go.Figure:
    day = day or datetime.now(ET).strftime("%Y-%m-%d")
    flows, src = flow_source(symbol, day, ("net_delta",))
    signe = src == "dxfeed"
    title = guided(t(lang, "flow_title_signed" if signe else "flow_title"), "flow")
    col = "net_delta" if signe else "flow_total"
    if flows.empty or col not in flows.columns:
        return empty_fig(t(lang, "no_flow_day", day=day), title)
    ts = to_local(flows["timestamp"])
    vals = flows[col].fillna(0.0).to_numpy() / 1e6
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
    title = guided(t(lang, "hist_title"), "history")
    hist = store.load_history(symbol)
    if hist.empty or len(hist) < 2:
        return empty_fig(t(lang, "not_enough_history"), title)
    ts = to_local(hist["timestamp"])
    fig = go.Figure()
    fig.add_scatter(x=ts, y=hist["net_gex"] / 1e9, mode="lines", name="GEX",
                    line=dict(color=C["cat"][0], width=2),
                    hovertemplate="%{x|%d/%m %H:%M}<br>GEX: %{y:.1f} $Bn<extra></extra>")
    lay = base_layout(title, height=300)
    lay["margin"]["t"] = 62
    fig.update_layout(**lay)
    fig.update_xaxes(**time_range_selector(), range=default_window(ts))
    return fig


def spot_zg_fig(symbol: str, lang: str) -> go.Figure:
    title = guided(t(lang, "spotzg_title"), "spot_zg")
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
    fig.update_xaxes(**time_range_selector(), range=default_window(ts))
    return fig


def smile_fig(df: pd.DataFrame, spot: float, lang: str) -> go.Figure:
    title = guided(t(lang, "smile_title"), "smile")
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
                window: float, xf=None) -> go.Figure:
    """Courbe de GEX net en fonction d'un spot hypothétique."""
    xf = xf or (lambda v: v)
    title = guided(t(lang, "profile_title"), "profile")
    res = metrics.gamma_profile(df, spot, range_pct=window, steps=201)
    if res is None:
        return empty_fig(t(lang, "no_data_window"), title)
    grid, prof = res
    x = xf(grid)
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
    # Lignes verticales : étiquettes tournées pour courir LE LONG de la ligne.
    # À l'horizontale, elles débordent latéralement et se chevauchent dès que
    # le spot et le flip sont proches — ce qui est le cas le plus fréquent.
    fig.add_vline(x=xf(spot), line_color=C["spot"], line_dash="dot", line_width=1,
                  annotation_text=f"Spot {xf(spot):.0f}",
                  annotation_font=dict(color=C["ink"], size=10),
                  annotation_position="top left", annotation_textangle=-90,
                  annotation_xshift=-2)
    if zg is not None:
        fig.add_vline(x=xf(zg), line_color=C["zg"], line_dash="dash", line_width=1,
                      annotation_text=f"Gamma Flip {xf(zg):.0f}",
                      annotation_font=dict(color=C["zg"], size=10),
                      annotation_position="top right", annotation_textangle=-90,
                      annotation_xshift=2)
    return fig


def profile_by_expiry_fig(df: pd.DataFrame, spot: float, lang: str,
                          window: float, xf=None) -> go.Figure:
    """Profil décomposé par bucket d'échéance : ce que pèse le 0DTE seul."""
    title = guided(t(lang, "profile_by_exp"), "profile")
    xf = xf or (lambda v: v)
    today = datetime.now(ET).date()
    fig = go.Figure()
    drawn = 0
    for i, bucket in enumerate(EXPIRY_BUCKETS):
        sub = df[metrics.bucket_mask(df, bucket, today)]
        res = metrics.gamma_profile(sub, spot, range_pct=window, steps=201)
        if res is None:
            continue
        grid, prof = res
        fig.add_scatter(x=xf(grid), y=prof / 1e9, mode="lines",
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
    fig.add_vline(x=xf(spot), line_color=C["spot"], line_dash="dot", line_width=1)
    fig.update_xaxes(title_text=t(lang, "profile_axis"), title_font=dict(color=C["muted"]))
    return fig


def second_order_fig(df: pd.DataFrame, spot: float, col: str, title: str,
                     window: float, xf=None) -> go.Figure:
    """Exposition vanna (vex) ou charm (cex) par strike."""
    xf = xf or (lambda v: v)
    lo, hi = spot * (1 - window), spot * (1 + window)
    d = df[df["strike"].between(lo, hi)]
    if d.empty:
        return empty_fig("—", title)
    agg = d.groupby("strike")[col].sum() / 1e6
    strikes = xf(agg.index.to_numpy())
    vals = agg.to_numpy()
    fig = go.Figure(go.Bar(
        y=strikes, x=vals, orientation="h",
        width=_bar_width(agg.index.to_numpy()),
        marker=dict(color=np.where(vals >= 0, C["pos"], C["neg"]), line=dict(width=0)),
        hovertemplate="%{y}<br>%{x:.1f} $M<extra></extra>",
    ))
    fig.update_layout(**base_layout(title, height=460))
    fig.add_hline(y=xf(spot), line_color=C["spot"], line_dash="dot", line_width=1,
                  annotation_text=f"Spot {xf(spot):.0f}", annotation_font_color=C["ink"],
                  annotation_position="top right")
    fig.update_xaxes(title_text="$M", title_font=dict(color=C["muted"]))
    return fig


def oi_change_fig(chg: pd.DataFrame, spot: float, lang: str, prev_day: str,
                  window: float, xf=None) -> go.Figure:
    """Variation d'OI par strike, calls et puts distingués (identité, pas polarité)."""
    title = guided(t(lang, "pos_title", day=prev_day), "pos")
    xf = xf or (lambda v: v)
    if chg.empty:
        return empty_fig(t(lang, "pos_no_prev"), title)
    lo, hi = spot * (1 - window), spot * (1 + window)
    d = chg[chg["strike"].between(lo, hi)]
    if d.empty:
        return empty_fig(t(lang, "no_data_window"), title)
    if (d["d_call"].abs().sum() + d["d_put"].abs().sum()) == 0:
        # même séance des deux côtés : l'OI n'est publié qu'une fois par jour
        return empty_fig(t(lang, "pos_no_change"), title)
    strikes = xf(d["strike"].to_numpy())
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
    fig.add_hline(y=xf(spot), line_color=C["spot"], line_dash="dot", line_width=1,
                  annotation_text=f"Spot {xf(spot):.0f}", annotation_font_color=C["ink"],
                  annotation_position="top right")
    fig.update_xaxes(title_text="Δ OI (milliers de contrats)", title_font=dict(color=C["muted"]))
    return fig


def _transform_for(symbol: str, scale_key: str | None):
    """Fonction de transposition des prix vers l'échelle demandée.

    Lit les spots et basis de TOUS les sous-jacents collectés : transposer
    SPX vers NQ suppose de connaître le spot NDX et son basis.
    """
    spots, bases = {}, {}
    for key in UNDERLYINGS:
        st = STATE.get(key)
        with STATE.lock:
            summ = st.summary
        if summ is not None:
            spots[key] = summ.spot
            bases[key] = summ.basis

    # Basis mesuré sur les deux prix réels — mais SEULEMENT quand l'indice et
    # son future cotent ensemble.
    #
    # Hors séance l'indice est figé à sa clôture pendant que le future continue
    # : leur écart n'est alors plus un basis, il absorbe tout le mouvement
    # overnight du future. L'appliquer ferait dériver TOUS les niveaux
    # transposés avec lui — un gap de 330 points sur NQ décalerait les murs
    # d'autant, alors qu'ils décrivent des positions arrêtées la veille.
    #
    # Marché fermé, on garde donc le basis de parité call-put du dernier pull,
    # qui est un vrai coût de portage et reste stable.
    if market_is_open():
        for u in UNDERLYINGS.values():
            if not u.future:
                continue
            idx, fut = QUOTES.price(u.key), QUOTES.price(u.future)
            if idx and fut:
                spots[u.key] = idx
                bases[u.key] = fut - idx

    target = scales.scale_by_key(scale_key) if scale_key else None
    return scales.transform(symbol, target, spots, bases)


def _scale_note(lang: str, symbol: str, scale_key: str | None,
                ratio: float, mode: str) -> str | None:
    """Mention affichée au-dessus des niveaux quand ils sont transposés.

    La transposition croisée (SP <-> ND) est signalée séparément : son ratio
    dérive dans le temps, les niveaux ne sont qu'un repère instantané.
    """
    if mode == "native":
        return None
    target = scales.scale_by_key(scale_key)
    if target is None:
        return None
    if mode == "basis":
        return t(lang, "scale_basis", scale=target.label)
    key = "scale_cross" if target.cross_family(symbol) else "scale_ratio"
    return t(lang, key, scale=target.label, ratio=f"{ratio:.4f}")


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


def ref_spot(symbol: str, fallback: float) -> float:
    """Spot auquel évaluer les murs de gamma : la clôture de la veille.

    L'open interest lu le matin décrit les positions arrêtées à cette clôture.
    L'évaluer au spot courant ferait glisser les murs avec le prix — ils
    désigneraient alors l'endroit où est le marché, pas une zone de couverture.
    """
    return store.previous_close_spot(symbol) or fallback


def live_spot(symbol: str, fallback: float) -> tuple[float, bool]:
    """Spot temps réel si le flux le fournit, sinon celui de la chaîne CBOE.

    Renvoie (prix, vient_du_temps_réel) pour que l'affichage puisse le dire.
    """
    px = QUOTES.price(symbol)
    return (px, True) if px else (fallback, False)


def pc_gauge(symbol: str, lang: str) -> html.Div:
    """Jauge visuelle calls vs puts, sur l'open interest — même donnée que la
    tuile "P/C Open Interest", juste plus lisible d'un coup d'œil qu'un
    ratio brut. part_calls = 1/(1+pc_oi) : dérivable directement du ratio
    déjà stocké (pc_oi = OI puts / OI calls), aucun nouveau calcul requis."""
    st = chain_state(symbol)
    with STATE.lock:
        s = st.summary
    if s is None or not s.pc_oi or s.pc_oi <= 0:
        return html.Div(style={"display": "none"})
    call_share = 1.0 / (1.0 + s.pc_oi)
    put_share = 1.0 - call_share
    return html.Div([
        html.Div([
            html.Span(t(lang, "pc_gauge_calls", pct=f"{call_share * 100:.0f}"),
                     style={"color": C["pos"]}),
            html.Span(t(lang, "pc_gauge_puts", pct=f"{put_share * 100:.0f}"),
                     style={"color": C["neg"]}),
        ], className="pc-gauge-labels"),
        html.Div([
            html.Div(style={"width": f"{call_share * 100:.2f}%"},
                     className="pc-gauge-fill pc-gauge-calls"),
            html.Div(style={"width": f"{put_share * 100:.2f}%"},
                     className="pc-gauge-fill pc-gauge-puts"),
        ], className="pc-gauge-track"),
    ], className="pc-gauge")


_REGIME_SEVERITY_COLOR = {"info": "hvl", "warning": "zg", "danger": "neg"}


def regime_banner(symbol: str, lang: str) -> html.Div:
    """Cadre de lecture croisée Gamma/Delta (cf. metrics.regime_read) :
    mécanique de couverture des dealers, jamais un point d'entrée."""
    st = chain_state(symbol)
    with STATE.lock:
        s = st.summary
    if s is None or s.zero_gamma is None:
        return html.Div(style={"display": "none"})
    dex_hist = store.load_history(symbol).get("net_dex")
    r = metrics.regime_read(s.net_gex, s.net_dex, dex_history=dex_hist)
    color = C[_REGIME_SEVERITY_COLOR[r["severity"]]]
    return html.Div(
        [
            html.Div(t(lang, "regime_label"), className="regime-label",
                     style={"color": color}),
            html.Div(regime_text(lang, r), className="regime-text"),
            html.Div(t(lang, "regime_disclaimer"), className="regime-disclaimer"),
        ],
        className="regime-banner",
        style={"--accent-bar": color},
    )


NATIVE_STALE_S = 600


def chain_state(symbol: str):
    """État de chaîne à AFFICHER pour un sous-jacent.

    Quand un compte courtier est configuré, SPX et NDX ont une chaîne native
    dxFeed sans les 15 min de retard de CBOE (cf. gex/idxopt.py) : c'est elle
    qui porte les niveaux, les tuiles et les graphes de structure. La chaîne
    CBOE continue de tourner en parallèle à 60 s, mais seulement pour le flux
    delta, qui a besoin d'une clé `contract` stable entre deux pulls et d'une
    cadence qu'une collecte native ne tient pas — ces graphiques-là lisent le
    disque, pas cet état, donc rien ne les perturbe.

    Repli sur CBOE si le natif est absent ou dormant depuis plus de
    `NATIVE_STALE_S` : une donnée délayée mais vivante vaut mieux qu'une
    donnée fraîche figée il y a une heure.
    """
    if symbol in idxopt.NATIVE_INDEX and credentials_present():
        native = STATE.get(scheduler_native_key(symbol))
        with STATE.lock:
            s, ts = native.summary, native.last_feed_ts
        if s is not None and ts is not None:
            age = (datetime.now(ET).replace(tzinfo=None) - ts).total_seconds()
            if 0 <= age < NATIVE_STALE_S:
                return native
    return STATE.get(symbol)


def build_cards(symbol: str, lang: str, xf=None, scale: str | None = None) -> list:
    st = chain_state(symbol)
    with STATE.lock:
        s = st.summary
        df = st.enriched
        err = STATE.last_error
    if s is None:
        delayed = PUBLIC_QUOTES.price(symbol) if symbol in ("NQ", "ES") else None
        if symbol in ("NQ", "ES") and not credentials_present():
            # sans compte, aucune collecte n'est en cours ni ne le sera jamais
            # — "collecte en cours" mentirait par optimisme. Si un spot
            # délayé existe, la tuile suivante l'affiche ; sinon la personne
            # a déjà vu pourquoi via l'overlay (native_notice).
            wait = t(lang, "native_no_chain_delayed" if delayed else "native_no_chain")
        else:
            wait = t(lang, "waiting_native" if symbol in ("NQ", "ES") else "waiting_short")
        cards = [card(t(lang, "card_status"), "…", err or wait)]
        if delayed:
            cards.append(card(t(lang, "card_spot_delayed"), f"{delayed:,.2f}",
                              t(lang, "card_spot_delayed_sub"), accent=C["muted"]))
        return cards
    xf = xf or (lambda v: v)

    # Le GEX net dépend surtout du spot : l'open interest ne bouge qu'une fois
    # par jour et l'IV lentement, tandis que le gamma de chaque contrat suit le
    # spot en continu. Un déplacement de 0,4 % change le GEX net de moitié —
    # avec un spot vieux de 15 min, la lecture du régime est fausse en séance.
    # On recalcule donc au spot temps réel quand il est disponible.
    spot, is_live = live_spot(symbol, s.spot)
    net_gex = s.net_gex
    if is_live and df is not None:
        recomputed = metrics.net_gex_at(df, spot)
        if recomputed is not None:
            net_gex = recomputed

    zg_txt = f"{xf(s.zero_gamma):.0f}" if s.zero_gamma else "n/a"
    zg_sub = ""
    if s.zero_gamma:
        d = spot - s.zero_gamma  # écart natif, non transposé
        zg_sub = t(lang, "card_zg_sub", sign="+" if d >= 0 else "",
                   pts=f"{d:.0f}", reg="+" if d >= 0 else "-")
    gex_color = C["pos"] if net_gex >= 0 else C["neg"]
    feed_local = s.timestamp.replace(tzinfo=ET).astimezone(LOCAL_TZ)
    fut_px = QUOTES.price(scale) if scale and scale not in UNDERLYINGS else None
    display_spot = fut_px if fut_px else xf(spot)
    spot_sub = (t(lang, "card_spot_live") if is_live else
                t(lang, "card_feed", local=f"{feed_local:%H:%M:%S}",
                  et=f"{s.timestamp:%H:%M}"))
    return [
        # En échelle future, on affiche le prix RÉEL du future plutôt que le
        # spot indice transposé : hors séance, l'indice est figé et aucune
        # conversion ne peut restituer le mouvement overnight du future.
        card(t(lang, "card_spot_rt") if is_live else t(lang, "card_spot"),
             f"{display_spot:,.0f}", spot_sub,
             accent=C["ok"] if is_live else None),
        card(t(lang, "card_net_gex"), f"{net_gex / 1e9:+.1f} $Bn",
             t(lang, "stabilizing") if net_gex >= 0 else t(lang, "destabilizing"),
             accent=gex_color),
        card(t(lang, "card_net_dex"), f"{s.net_dex / 1e9:+.1f} $Bn",
             t(lang, "dex_long") if s.net_dex >= 0 else t(lang, "dex_short"),
             accent=C["pos"] if s.net_dex >= 0 else C["neg"]),
        card(t(lang, "card_zero_gamma"), zg_txt, zg_sub, accent=C["zg"]),
        card(t(lang, "card_gex_0dte"), f"{s.net_gex_0dte / 1e9:+.1f} $Bn"),
        card(t(lang, "card_pc_oi"), f"{s.pc_oi:.2f}"),
        card(t(lang, "card_pc_vol"), f"{s.pc_volume:.2f}"),
    ]


def create_app() -> Dash:
    # assets/ vit dans le package (gex/assets) pour survivre à un pip install ;
    # Dash les sert dans tous les cas sous /assets.
    app = Dash(__name__, title="GEX Dashboard",
               assets_folder=str(Path(__file__).resolve().parent / "assets"))
    enabled = targets()

    def ctl(label_id, control):
        """Contrôle étiqueté : la légende dit ce que le segment pilote."""
        return html.Div([html.Span(id=label_id, className="ctl-label"), control],
                        className="ctl")

    app.layout = html.Div([
        # bandeau + superposition : NQ/ES sans identifiants dxFeed (cf.
        # native_notice_content) — vides par défaut, peuplés par le callback
        # native_notice sur changement de symbole.
        html.Div(id="native-banner", className="native-banner", style={"display": "none"}),
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
                    dcc.RadioItems(id="unit", className="seg", inline=True),
                    dcc.RadioItems(
                        id="lang", className="seg",
                        options=[{"label": l.upper(), "value": l} for l in LANGS],
                        value="fr", inline=True),
                    # page statique servie depuis assets/ (nouvel onglet)
                    html.A(id="faq-link", className="linkbtn", href="/assets/faq.html",
                           target="_blank", children="FAQ"),
                    # état du flux temps réel : masqué tant qu'aucun identifiant
                    # n'est configuré (cas de l'installation par défaut)
                    html.Div([html.Span(className="rt-dot"),
                              html.Span(id="rt-label")],
                             id="rt-badge", className="rt-badge",
                             style={"display": "none"}),
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
            html.Div(id="pc-gauge"),
            html.Div(id="regime-banner"),
            html.Div([
                html.Div(id="levels", className="chips"),
                # copie des niveaux au format de l'indicateur TradingView
                # (cf. tv_levels_string) — la chaîne suit l'échelle affichée
                dcc.Clipboard(id="tv-copy", className="tv-copy"),
            ], className="levels-row"),
            dcc.Tabs(id="tab", value="main", className="tabbar", children=[
                dcc.Tab(value=v, label="", id=f"tabh-{v}",
                        className="tab-item", selected_className="tab-item--selected")
                for v in TABS
            ]),

            html.Div(id="pane-main", children=[
                html.Div([
                    dcc.Graph(config=GRAPH_CONFIG, id="gex-strike"),
                    dcc.Graph(config=GRAPH_CONFIG, id="dex-strike"),
                ], className="row", style={"marginBottom": "12px"}),
                html.Div([
                    html.Span(id="flow-day-label", className="ctl-label"),
                    dcc.Dropdown(id="flow-day", clearable=False,
                                 style={"width": "160px"}),
                    html.Button(id="flow-today", n_clicks=0, className="btn"),
                ], className="daybar"),
                dcc.Graph(config=GRAPH_CONFIG, id="flow", style={"marginBottom": "12px"}),
                html.Div([
                    html.Span(id="lbl-gflow-series", className="ctl-label"),
                    dcc.Checklist(id="gflow-series", className="check", inline=True,
                                 value=["calls", "puts", "net"]),
                ], className="daybar"),
                dcc.Graph(config=GRAPH_CONFIG, id="gflow", style={"marginBottom": "12px"}),
                # Order flow SIGNÉ : placé juste après les deux proxys non
                # signés, pour que la différence saute aux yeux plutôt que de
                # se deviner. Le bandeau porte la provenance et la licence.
                html.Div([
                    html.Span(id="lbl-tape-series", className="ctl-label"),
                    dcc.Checklist(id="tape-series", className="check", inline=True,
                                 value=["net", "calls", "puts"]),
                ], className="daybar"),
                dcc.Graph(config=GRAPH_CONFIG, id="tape"),
                html.Div(id="tape-note", className="hint",
                         style={"marginBottom": "12px"}),
                html.Div([
                    dcc.Graph(config=GRAPH_CONFIG, id="gex-history"),
                    dcc.Graph(config=GRAPH_CONFIG, id="spot-zg"),
                    dcc.Graph(config=GRAPH_CONFIG, id="smile"),
                ], className="row"),
            ]),

            html.Div(id="pane-profile", children=[
                html.Div(id="profile-hint", className="hint"),
                dcc.Graph(config=GRAPH_CONFIG, id="profile", style={"marginBottom": "12px"}),
                dcc.Graph(config=GRAPH_CONFIG, id="profile-exp"),
            ]),

            html.Div(id="pane-greeks2", children=[
                html.Div(id="g2-hint", className="hint"),
                html.Div(id="g2-cards", className="cards"),
                html.Div([
                    dcc.Graph(config=GRAPH_CONFIG, id="vex"),
                    dcc.Graph(config=GRAPH_CONFIG, id="cex"),
                ], className="row"),
            ]),

            html.Div(id="pane-heat", children=[
                html.Div(id="heat-hint", className="hint"),
                html.Div([
                    # sélecteur propre : les jours disponibles sont ceux des
                    # snapshots de chaîne, pas ceux des fichiers de flux
                    html.Span(id="heat-day-label", className="ctl-label"),
                    dcc.Dropdown(id="heat-day", className="dash-dropdown",
                                 clearable=False, style={"width": "180px"}),
                    html.Span(id="heat-levels-label", className="ctl-label"),
                    dcc.Checklist(id="heat-levels", className="check", inline=True,
                                 value=["zero_gamma", "call_wall", "put_support"]),
                ], className="ctl", style={"flexWrap": "wrap"}),
                dcc.Graph(config=GRAPH_CONFIG, id="heatmap"),
            ]),

            html.Div(id="pane-pos", children=[
                html.Div(id="pos-hint", className="hint"),
                dcc.Graph(config=GRAPH_CONFIG, id="oi-change"),
            ]),

            dcc.Interval(id="tick", interval=SETTINGS.flow_interval_s * 1000),
            # le voyant du flux a son propre rythme : une déconnexion doit se
            # voir tout de suite, pas au prochain pull (60 s)
            dcc.Interval(id="rt-tick", interval=5000),
            dcc.Store(id="lang-boot", data=0),
            html.Div(id="footer", className="footer"),
        ], className="page"),
        dcc.Store(id="native-alt"),  # "NDX" ou "SPY" : cible du bouton OK
        html.Div(id="native-overlay", className="native-overlay", style={"display": "none"}),
    ])

    def _chip(children, accent):
        return html.Span(children, className="chip", style={"--chip-accent": accent})

    def levels_strip(levels: pd.DataFrame | None, lang: str,
                     hvl: float | None = None, zg: float | None = None,
                     xf=None, scale_note: str | None = None,
                     keys: dict | None = None) -> list:
        if levels is None or levels.empty:
            return [html.Span(t(lang, "levels_unavailable"),
                              style={"color": C["muted"], "fontSize": "12px"})]
        exp = levels["expiry"].iloc[0]
        labels = wall_labels(levels)
        xf = xf or (lambda v: v)
        items = [html.Span(t(lang, "levels_prefix", exp=f"{exp:%d/%m}"),
                           style={"color": C["muted"], "fontSize": "12px", "marginRight": "4px"})]
        if scale_note:
            items.append(html.Span(scale_note, className="scale-note"))
        if zg is not None:
            items.append(_chip([html.B("Gamma Flip ", style={"color": C["zg"]}),
                                f"{xf(zg):.0f}"], C["zg"]))
        if hvl is not None:
            items.append(_chip([html.B("HVL ", style={"color": C["hvl"]}),
                                f"{xf(hvl):.0f}"], C["hvl"]))
        # niveaux directionnels (support/résistance) et bornes de move attendu
        for key, color, label in (("call_wall", C["cw"], "Call Wall"),
                                  ("put_support", C["ps"], "Put Support"),
                                  ("d1_min", C["d1"], "1D Min"),
                                  ("d1_max", C["d1"], "1D Max")):
            v = (keys or {}).get(key)
            if v is not None:
                items.append(_chip([html.B(f"{label} ", style={"color": color}),
                                    f"{xf(v):.0f}"], color))
        for lv in levels.itertuples():
            side = t(lang, "side_call") if lv.gex > 0 else t(lang, "side_put")
            items.append(_chip(
                [html.B(f"{labels[lv.strike]} ", style={"color": C["lvl"]}),
                 f"{xf(lv.strike):.0f} ",
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
         Output("app-title", "children"),
         Output("lbl-bucket", "children"), Output("lbl-window", "children"),
         Output("unit", "value"),
         Output("lbl-gflow-series", "children"), Output("gflow-series", "options"),
         Output("lbl-tape-series", "children"), Output("tape-series", "options"),
         Output("tape-note", "children"),
         Output("heat-levels-label", "children"), Output("heat-levels", "options")],
        [Input("lang", "value"), Input("symbol", "value")],
    )
    def apply_lang(lang, symbol):
        bucket_opts = [{"label": t(lang, BUCKET_KEYS[b]), "value": b} for b in EXPIRY_BUCKETS]
        majors_opts = [{"label": t(lang, "majors_only"), "value": "on"}]
        gflow_series_opts = [{"label": t(lang, "legend_gcalls"), "value": "calls"},
                             {"label": t(lang, "legend_gputs"), "value": "puts"},
                             {"label": t(lang, "legend_gnet"), "value": "net"}]
        tape_series_opts = [{"label": t(lang, "legend_tape_net"), "value": "net"},
                            {"label": t(lang, "legend_tape_calls"), "value": "calls"},
                            {"label": t(lang, "legend_tape_puts"), "value": "puts"}]
        heat_levels_opts = [{"label": "Gamma Flip", "value": "zero_gamma"},
                           {"label": "HVL", "value": "hvl"},
                           {"label": "Call Wall", "value": "call_wall"},
                           {"label": "Put Support", "value": "put_support"},
                           {"label": "1D Min/Max", "value": "d1"},
                           {"label": t(lang, "heat_levels_gex_walls"), "value": "gex_walls"}]
        # échelles : le sous-jacent natif, puis les deux futures (la
        # transposition croisée SPX→NQ est le cas d'usage visé). Pour NQ/ES
        # eux-mêmes (chaîne native, pas transposée), la question ne se pose
        # pas : ce sont déjà les futures, un seul choix a du sens.
        if symbol in ("NQ", "ES"):
            opts = [{"label": t(lang, "unit_futures"), "value": symbol}]
        else:
            native_label = (t(lang, "unit_index")
                            if UNDERLYINGS[symbol].future else symbol)
            opts = [{"label": native_label, "value": symbol},
                    {"label": "ES", "value": "ES"},
                    {"label": "NQ", "value": "NQ"}]
        return (bucket_opts, majors_opts, t(lang, "flow_day_label"),
                t(lang, "last_session"), t(lang, "footer"), opts,
                t(lang, "app_title"),
                t(lang, "lbl_expiry"), t(lang, "lbl_window"), symbol,
                t(lang, "gflow_series_label"), gflow_series_opts,
                t(lang, "tape_series_label"), tape_series_opts,
                t(lang, "tape_note"),
                t(lang, "heat_levels_label"), heat_levels_opts)

    @app.callback(
        [Output("brand-sub", "children"), Output("rt-badge", "style"),
         Output("rt-badge", "className"), Output("rt-badge", "title"),
         Output("rt-label", "children")],
        [Input("rt-tick", "n_intervals"), Input("lang", "value")],
    )
    def rt_status(_, lang):
        """Provenance du spot affiché, et pastille d'état du flux temps réel.

        Le badge reste masqué sur une installation sans identifiants courtier :
        inutile d'exposer un voyant rouge permanent pour une fonction que
        l'utilisateur n'a pas demandée.
        """
        state, detail = QUOTES.status(market_open=market_is_open())
        if state == "off":
            return (t(lang, "brand_sub"), {"display": "none"},
                    "rt-badge", "", "")
        key = {"connected": "rt_connected", "degraded": "rt_degraded"}.get(
            state, "rt_disconnected")
        tip = t(lang, key) + (f" ({detail})" if detail else "")
        # le sous-titre ne promet le temps réel que si le flux le tient
        sub = t(lang, "brand_sub_rt" if state == "connected" else "brand_sub")
        return (sub, {}, f"rt-badge rt-{state}", tip, "dxFeed")

    @app.callback(
        [Output("native-banner", "children"), Output("native-banner", "style"),
         Output("native-overlay", "children"), Output("native-overlay", "style"),
         Output("native-alt", "data")],
        [Input("symbol", "value"), Input("lang", "value")],
    )
    def native_notice(symbol, lang):
        """NQ/ES n'existent QUE via dxFeed (pas de repli CBOE pour des options
        sur futures) : sans identifiants, STATE ne sera JAMAIS peuplé pour ces
        deux-là — contrairement à un pull CBOE en échec, qui finit par
        aboutir. Le message doit donc dire "il manque des identifiants", pas
        laisser croire à une collecte en cours qui n'arrivera jamais.

        Exception : si le repli public (PUBLIC_QUOTES) donne déjà un spot
        délayé pour ce symbole, il y a quelque chose à montrer — pas la peine
        de rediriger vers l'alternative transposée (NDX/SPY), la tuile spot
        délayé de build_cards suffit."""
        hidden = {"display": "none"}
        if symbol not in ("NQ", "ES") or credentials_present() or PUBLIC_QUOTES.price(symbol):
            return None, hidden, None, hidden, None
        alt = "NDX" if symbol == "NQ" else "SPY"
        banner = [
            html.Span(t(lang, "native_banner", sym=symbol)),
            html.A(t(lang, "native_more_info"), href="/assets/faq.html#realtime",
                  target="_blank"),
        ]
        overlay = html.Div([
            html.H3(t(lang, "native_overlay_title", sym=symbol)),
            html.P(t(lang, "native_overlay_body", sym=symbol, alt=alt)),
            html.A(t(lang, "native_overlay_link"), href="/assets/faq.html#realtime",
                  target="_blank", style={"display": "block", "marginBottom": "14px"}),
            html.Button(t(lang, "native_overlay_ok", alt=alt),
                       id="native-overlay-ok", n_clicks=0, className="btn"),
        ], className="native-overlay-card")
        return banner, {}, overlay, {}, alt

    @app.callback(
        Output("symbol", "value"),
        Input("native-overlay-ok", "n_clicks"),
        State("native-alt", "data"),
        prevent_initial_call=True,
    )
    def native_overlay_dismiss(n_clicks, alt):
        if not n_clicks or not alt:
            raise PreventUpdate
        return alt

    @app.callback(
        [Output("cards", "children"), Output("regime-banner", "children"),
         Output("pc-gauge", "children")],
        [Input("rt-tick", "n_intervals"), Input("symbol", "value"),
         Input("lang", "value"), Input("unit", "value")],
    )
    def refresh_cards(_, symbol, lang, unit):
        """Tuiles au rythme du flux (5 s) et non des pulls (60 s).

        Le GEX net y est recalculé au spot courant : c'est la valeur qui dit
        si le marché est amorti ou amplifié, et elle se périme en quelques
        minutes. Le recalcul porte sur un seul point de spot, donc son coût
        est négligeable devant la grille de 161 points du Gamma Flip.
        """
        xf, _, _ = _transform_for(symbol, unit)
        return (build_cards(symbol, lang, xf, scale=unit), regime_banner(symbol, lang),
                pc_gauge(symbol, lang))

    @app.callback(
        [Output("levels", "children"), Output("gex-strike", "figure"),
         Output("dex-strike", "figure"), Output("flow", "figure"),
         Output("gflow", "figure"), Output("tape", "figure"),
         Output("gex-history", "figure"), Output("spot-zg", "figure"),
         Output("smile", "figure"), Output("tv-copy", "content"),
         Output("tv-copy", "title")],
        [Input("tick", "n_intervals"), Input("symbol", "value"),
         Input("bucket", "value"), Input("window", "value"),
         Input("majors", "value"), Input("flow-day", "value"),
         Input("lang", "value"), Input("unit", "value"),
         Input("gflow-series", "value"), Input("tape-series", "value")],
    )
    def refresh(_, symbol, bucket, window, majors, flow_day, lang, unit, gflow_series,
                tape_series):
        st = chain_state(symbol)
        with STATE.lock:
            df = st.enriched
            snap = st.snapshot
            summary = st.summary
        bucket_label = t(lang, BUCKET_KEYS[bucket])
        if df is None or snap is None:
            wait = t(lang, "waiting_native" if symbol in ("NQ", "ES") else "waiting_first_pull")
            return (
                levels_strip(None, lang),

                empty_fig(wait, guided(t(lang, "gex_title", bucket=bucket_label), "gex_strike")),
                empty_fig(wait, guided(t(lang, "dex_title", bucket=bucket_label), "dex_strike")),
                empty_fig(wait, t(lang, "flow_title")),
                empty_fig(wait, t(lang, "gflow_title")),
                empty_fig(wait, t(lang, "tape_title")),
                empty_fig(wait, t(lang, "hist_title")),
                empty_fig(wait, t(lang, "spotzg_title")),
                empty_fig(wait, t(lang, "smile_title")),
                "", t(lang, "tv_copy_title", scale=unit),
            )
        today = datetime.now(ET).date()
        sel = df[metrics.bucket_mask(df, bucket, today)]
        zg = summary.zero_gamma if summary else None

        # uirevision : tant que la révision ne change pas, Plotly conserve le
        # zoom/pan de l'utilisateur à travers les refresh de dcc.Interval.
        def _pin(fig, rev):
            fig.update_layout(uirevision=rev)
            return fig

        # transposition vers l'échelle d'affichage (voir gex/scales.py)
        xf, ratio, mode = _transform_for(symbol, unit)
        note = _scale_note(lang, symbol, unit, ratio, mode)
        rev = f"{symbol}-{bucket}-{window}-{unit}"
        ref = ref_spot(symbol, snap.spot)
        # Le côté où chercher résistance et support suit le marché EN SÉANCE
        # seulement. Hors séance, un gap de futures invaliderait des murs avant
        # même l'ouverture du cash : le prix de référence reste alors celui de
        # la clôture, qui est l'état sur lequel le plan a été bâti.
        side_spot = snap.spot if market_is_open() else ref
        levels = metrics.top_gex_levels(df, ref_spot=ref)
        if majors and not levels.empty:
            # ne garde que les murs pesant au moins 25 % du plus fort
            levels = levels[levels["gex"].abs() >= 0.25 * levels["gex"].abs().max()]
        hvl = metrics.zero_gamma(df, snap.spot, weight_col="volume")
        keys = metrics.key_levels(df, side_spot, ref_spot=ref)
        return (
            levels_strip(levels, lang, hvl, zg, xf, note, keys),
            _pin(exposure_fig(sel, snap.spot, zg, "gex",
                              guided(t(lang, "gex_title", bucket=bucket_label), "gex_strike"), lang,
                              levels=levels, hvl=hvl, window=window, xf=xf,
                              keys=keys), rev),
            _pin(exposure_fig(sel, snap.spot, zg, "dex",
                              guided(t(lang, "dex_title", bucket=bucket_label), "dex_strike"), lang,
                              hvl=hvl, window=window, xf=xf, keys=keys,
                              level_set="regime"), rev),
            _pin(flow_fig(symbol, lang, flow_day), f"{symbol}-{flow_day}"),
            _pin(gamma_flow_fig(symbol, lang, flow_day, gflow_series),
                f"g{symbol}-{flow_day}-{gflow_series}"),
            _pin(tape_fig(symbol, lang, flow_day, tape_series),
                f"t{symbol}-{flow_day}-{tape_series}"),
            _pin(history_fig(symbol, lang), symbol),
            _pin(spot_zg_fig(symbol, lang), symbol),
            _pin(smile_fig(sel, snap.spot, lang), rev),
            tv_levels_string(levels, hvl, zg, keys, xf),
            t(lang, "tv_copy_title", scale=unit),
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
        st = chain_state(symbol)
        with STATE.lock:
            df, snap, summary = st.enriched, st.snapshot, st.summary
        if df is None or snap is None:
            e = empty_fig(t(lang, "waiting_first_pull"), t(lang, "profile_title"))
            return e, e, t(lang, "profile_hint")
        xf, _, _ = _transform_for(symbol, unit)
        zg = summary.zero_gamma if summary else None
        # fenêtre élargie : la courbe n'a d'intérêt que si elle montre le flip
        w = max(window, 0.06)
        return (profile_fig(df, snap.spot, zg, lang, w, xf),
                profile_by_expiry_fig(df, snap.spot, lang, w, xf),
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
        st = chain_state(symbol)
        with STATE.lock:
            df, snap, summary = st.enriched, st.snapshot, st.summary
        if df is None or snap is None:
            e = empty_fig(t(lang, "waiting_first_pull"))
            return e, e, [], t(lang, "vex_hint")
        xf, _, _ = _transform_for(symbol, unit)
        today = datetime.now(ET).date()
        sel = metrics.add_second_order(df[metrics.bucket_mask(df, bucket, today)], snap.spot)
        cards = [
            card(t(lang, "vex_card"), f"{sel['vex'].sum() / 1e9:+.2f} $Bn",
                 t(lang, "vex_title").split("(")[-1].rstrip(")")),
            card(t(lang, "cex_card"), f"{sel['cex'].sum() / 1e9:+.2f} $Bn",
                 t(lang, "cex_title").split("(")[-1].rstrip(")")),
        ]
        return (second_order_fig(sel, snap.spot, "vex", guided(t(lang, "vex_title"), "vex"), window, xf),
                second_order_fig(sel, snap.spot, "cex", guided(t(lang, "cex_title"), "cex"), window, xf),
                cards, t(lang, "vex_hint"))

    @app.callback(
        [Output("heat-day", "options"), Output("heat-day", "value"),
         Output("heat-day-label", "children")],
        [Input("symbol", "value"), Input("lang", "value"), Input("tab", "value")],
        State("heat-day", "value"),
    )
    def heat_days(symbol, lang, tab, current):
        days = store.snapshot_days(symbol)
        opts = [{"label": d, "value": d} for d in reversed(days)]
        # conserve le choix de l'utilisateur s'il reste valide après un
        # changement de sous-jacent, sinon bascule sur la séance la plus récente
        value = current if current in days else (days[-1] if days else None)
        return opts, value, t(lang, "heat_day_label")

    @app.callback(
        [Output("heatmap", "figure"), Output("heat-hint", "children")],
        [Input("tick", "n_intervals"), Input("tab", "value"), Input("symbol", "value"),
         Input("window", "value"), Input("lang", "value"), Input("unit", "value"),
         Input("heat-day", "value"), Input("heat-levels", "value")],
    )
    def refresh_heatmap(_, tab, symbol, window, lang, unit, day, levels_shown):
        # onglet masqué : ne pas relire une quarantaine de fichiers pour rien
        if tab != "heat":
            raise PreventUpdate
        xf, _, _ = _transform_for(symbol, unit)
        return (heatmap_fig(symbol, lang, day, window, xf, unit, levels_shown),
                t(lang, "heat_hint"))

    @app.callback(
        [Output("oi-change", "figure"), Output("pos-hint", "children")],
        [Input("tick", "n_intervals"), Input("tab", "value"), Input("symbol", "value"),
         Input("window", "value"), Input("lang", "value"), Input("unit", "value")],
    )
    def refresh_positioning(_, tab, symbol, window, lang, unit):
        if tab != "pos":
            raise PreventUpdate
        st = chain_state(symbol)
        with STATE.lock:
            df, snap, summary = st.enriched, st.snapshot, st.summary
        if df is None or snap is None:
            return empty_fig(t(lang, "waiting_first_pull")), t(lang, "pos_hint")
        xf, _, _ = _transform_for(symbol, unit)
        today = datetime.now(ET).strftime("%Y-%m-%d")
        prev = store.load_previous_snapshot(symbol, today)
        if prev is None:
            return (empty_fig(t(lang, "pos_no_prev"), t(lang, "pos_title", day="—")),
                    t(lang, "pos_hint"))
        prev_day, prev_df = prev
        chg = metrics.oi_change(prev_df, df)
        return (oi_change_fig(chg, snap.spot, lang, prev_day, window, xf),
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

    register_api(app)

    return app
