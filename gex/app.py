"""Dashboard Dash : GEX/DEX par strike, indicateurs, flux delta, skew IV.

Palette : polarité (GEX/flux +/-) en diverging bleu↔rouge, identité
(calls/puts, expirations) sur les slots catégoriels — thème sombre.
Interface FR/EN (gex/i18n.py) ; termes de trading standards dans les deux.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, ctx, dcc, html
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate

from . import digest, metrics, scales, scalp, store
from .bars import swing_move, volume_bars, zigzag
from .api import _futures_last_price, register_api
from .tt_web import connection_status, register_oauth
from .config import SETTINGS, UNDERLYINGS, targets
from .i18n import LANGS, regime_text, t, wall_labels
from .metrics import ET, EXPIRY_BUCKETS
from . import idxopt
from .rtquote import PUBLIC_QUOTES, QUOTES, credentials_present
from .scheduler import STATE, market_is_open

try:
    # gex/confluence.py est délibérément HORS GIT (.git/info/exclude, choix
    # de l'utilisateur) — jamais partagé entre machines. Import défensif :
    # app.py (suivi par git) ne doit jamais planter au démarrage sur une
    # machine où ce fichier n'existe pas (ex. un autre clone du dépôt). La
    # piste "niveaux combinés multi-familles" (roadmap /scalp v2) se dégrade
    # silencieusement en absence plutôt que de casser toute la page /scalp.
    from . import confluence as _confluence
except ImportError:
    _confluence = None
from .scheduler import native_index_key as scheduler_native_key

# --- Palette (mode sombre, cf. skill dataviz) ---
log = logging.getLogger(__name__)

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
TABS = ("main", "profile", "greeks2", "heat", "pos", "tape")


def to_local(ts: pd.Series) -> pd.Series:
    """Timestamps stockés naïfs en heure de New York → heure locale (naïve)."""
    return (
        pd.to_datetime(ts)
        .dt.tz_localize(ET, ambiguous="NaT", nonexistent="NaT")
        .dt.tz_convert(LOCAL_TZ)
        .dt.tz_localize(None)
    )


# Diviseur pour convertir un Series datetime64 tz-aware en epoch SECONDES via
# `.astype("int64")` — la résolution native (ns/us/ms/s) dépend de la version
# de pandas (ex. pandas 3.x choisit souvent "us", pas "ns"), donc un `// 10**9`
# en dur suppose à tort des nanosecondes et divise l'epoch par 1000 en trop
# dès que la résolution réelle est "us" (bug vérifié le 2026-10-04 : bougies
# datées 21/01/1970 sur /scalp v2, cf. audit-bug-epoch-microsecondes mémoire).
_EPOCH_DIVISOR = {"s": 1, "ms": 10**3, "us": 10**6, "ns": 10**9}


def _epoch_seconds(ts: pd.Series) -> pd.Series:
    """Series datetime64 tz-aware (ou naïve) -> epoch UTC entier en secondes,
    quelle que soit la résolution native de la Series."""
    return ts.astype("int64") // _EPOCH_DIVISOR[ts.dt.unit]


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


def _split_title(title: str) -> str:
    """Titre long sur deux lignes (coupé au dernier « — ») : sur téléphone un titre
    d'une seule ligne dépasse la largeur du graphe. Les titres courts, et ceux
    sans tiret cadratin, restent tels quels."""
    plain = re.sub(r"<[^>]+>", "", title)
    if len(plain) <= 52 or " — " not in title:
        return title
    if "<a " in title:                      # titre cliquable : coupe au premier tiret
        return title.replace(" — ", "<br>", 1)
    head, _, tail = title.rpartition(" — ")
    return f"{head}<br><sub>{tail}</sub>"


def base_layout(title: str, height: int = 420) -> dict:
    title = _split_title(title)
    deux_lignes = "<br>" in title
    return dict(
        title=dict(text=title, font=dict(size=13, color=C["ink"], family=FONT),
                   x=0.012, y=0.97, xanchor="left"),
        template=None,
        paper_bgcolor=C["surface"],
        plot_bgcolor=C["surface"],
        font=dict(family=FONT, size=11, color=C["ink2"]),
        margin=dict(l=58, r=18, t=58 if deux_lignes else 42, b=38),
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
    lay["margin"]["t"] = max(62, lay["margin"]["t"] + 20)
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


def _apply_user_zoom(lay: dict, relayout: dict | None) -> None:
    """Réapplique un zoom d'axe fait à la souris, lu dans `relayoutData`.

    La heatmap se régénère sur `tick` : sans cela, chaque rafraîchissement
    remettrait l'échelle des prix à sa vue complète. On ne touche qu'aux axes
    que l'utilisateur a RÉELLEMENT bougés — une plage explicite dans
    relayoutData —, et un double-clic (qui renvoie `axis.autorange: true`)
    laisse repartir en automatique, comme attendu.
    """
    if not relayout:
        return
    for axe in ("yaxis", "xaxis"):
        lo = relayout.get(f"{axe}.range[0]")
        hi = relayout.get(f"{axe}.range[1]")
        if lo is not None and hi is not None:
            lay[axe]["range"] = [lo, hi]
            lay[axe]["autorange"] = False


def heatmap_fig(symbol: str, lang: str, day: str | None = None,
                window: float = 0.04, xf=None, unit: str | None = None,
                levels_shown: list[str] | None = None,
                relayout: dict | None = None) -> go.Figure:
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
    # spot reste toujours affiché, comme référence de lecture systématique.
    # Murs classés au spot structurel (clôture veille), comme partout ailleurs.
    _ref = ref_spot(symbol, spot)
    keys = metrics.key_levels(sel, spot, ref_spot=_ref, all_expiries=True)
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
        walls = metrics.top_gex_levels(sel, ref_spot=_ref, all_expiries=True)
        labels = wall_labels(walls) if not walls.empty else {}
        for lv in walls.itertuples():
            items.append(dict(y=xf(lv.strike), label=labels.get(lv.strike, "GEX"),
                              color=C["lvl"], dash="dot"))
    _draw_levels(fig, items, xf(lo), xf(hi))

    lay = with_legend(base_layout(title, height=560))
    lay["barmode"] = "overlay"
    lay["yaxis"]["title"] = dict(text=t(lang, "heat_axis_strike"),
                                 font=dict(color=C["muted"]))
    # Déverrouille l'axe des prix : le montage à deux axes X superposés le
    # passait en fixedrange automatiquement, ce qui empêchait TOUT zoom
    # vertical (la molette ne bougeait que l'horizontale). Explicitement à
    # False, on peut resserrer la fenêtre de prix à la molette ou en glissant
    # sur l'axe — et _apply_user_zoom rend ce zoom persistant.
    lay["yaxis"]["fixedrange"] = False
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
    # Persistance de l'état d'interaction : la heatmap se régénère toutes les
    # quelques secondes (callback sur `tick`). Sans uirevision, un zoom manuel
    # sur l'axe des prix — pour resserrer la fenêtre — serait remis à zéro à
    # chaque rafraîchissement. La clé garde le zoom tant que le CONTEXTE ne
    # change pas ; elle exclut volontairement `levels_shown` (basculer un
    # niveau ne doit pas recadrer) et la langue, mais inclut symbole/jour/
    # échelle/fenêtre, où un recadrage automatique EST voulu.
    lay["uirevision"] = f"{symbol}-{day}-{unit}-{window}"
    # Réapplique le zoom manuel courant (cf. _apply_user_zoom). Placé APRÈS la
    # plage de séance par défaut : si l'utilisateur a resserré, sa fenêtre
    # prime ; sinon on garde la vue complète de la séance.
    _apply_user_zoom(lay, relayout)
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


def _fmt_notional(v) -> str:
    """Notionnel en $ / k$ / M$ selon l'ordre de grandeur, sans jamais afficher
    « 0 k$ » : un petit ticket vaut quelques centaines de dollars, pas zéro."""
    if not v:
        return "—"
    if v >= 1e6:
        return f"{v / 1e6:.1f} M$"
    if v >= 1e3:
        return f"{v / 1e3:.0f} k$"
    return f"{v:.0f} $"


def tape_table(symbol: str, lang: str, min_size: float = 0.0,
               include_combos: bool = True) -> html.Div:
    """Tableau des dernières transactions du sous-jacent, les plus récentes en
    haut (cf. flowtape.recent_prints).

    Le côté agresseur colore la ligne : vert = acheteur, rouge = vendeur —
    la même sémantique que partout dans le tableau. Les jambes de combos sont
    grisées et signalées, jamais fondues dans le flux directionnel.
    """
    from .flowtape import TAPE

    rows = TAPE.recent_prints(symbol, min_size=min_size,
                              include_combos=include_combos, limit=60)
    if not rows:
        etat, _ = TAPE.status()
        msg = (t(lang, "tape_empty_off") if etat == "off"
               else t(lang, "tape_empty_wait"))
        return html.Div(msg, className="hint")

    entete = [t(lang, k) for k in ("tape_col_time", "tape_col_contract",
                                   "tape_col_side", "tape_col_size",
                                   "tape_col_price", "tape_col_notional")]
    trs = [html.Tr([html.Th(h) for h in entete])]
    for r in rows:
        achat = r["side"] == "BUY"
        vente = r["side"] == "SELL"
        couleur = C["pos"] if achat else C["neg"] if vente else C["muted"]
        contrat = (f"{int(r['strike'])}{r['type']}"
                   if r["strike"] is not None and r["type"] else "—")
        side_txt = ("ACHAT" if achat else "VENTE" if vente else "?")
        # heure locale de la machine (= celle de l'utilisateur, l'appli tourne
        # chez lui), cohérente avec to_local() utilisé sur tous les graphes.
        # r["t"] est un epoch absolu, donc la conversion de fuseau est exacte.
        heure = datetime.fromtimestamp(r["t"], tz=LOCAL_TZ).strftime("%H:%M:%S")
        notio = _fmt_notional(r["notional"])
        prix = f"{r['price']:.2f}" if r["price"] is not None else "—"
        style = {"color": couleur}
        if r["combo"]:
            style = {"color": C["muted"], "opacity": "0.65"}
        trs.append(html.Tr([
            html.Td(heure, className="tape-td tape-mono"),
            html.Td([contrat, html.Span(" ⛓", title=t(lang, "tape_combo"))]
                    if r["combo"] else contrat, className="tape-td"),
            html.Td(side_txt, className="tape-td", style={"color": couleur,
                                                          "fontWeight": "600"}),
            html.Td(f"{int(r['size'])}", className="tape-td tape-mono tape-num"),
            html.Td(prix, className="tape-td tape-mono tape-num"),
            html.Td(notio, className="tape-td tape-mono tape-num"),
        ], style=style))
    return html.Table(trs, className="tape-table")


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
    # Cumul remis à zéro à l'open US (9h30 ET) : le pré-marché se cumule à part,
    # la séance repart de 0 (deux segments, la courbe saute à l'ouverture).
    apres_open = (tape["timestamp"] >= pd.Timestamp(f"{day} 09:30")).to_numpy()

    def cum(col: str) -> np.ndarray:
        v = tape[col].fillna(0.0).to_numpy()
        out = np.cumsum(np.where(apres_open, 0.0, v))
        out[apres_open] = np.cumsum(v[apres_open])
        return out

    if "net_delta" in tape.columns:
        net = cum("net_delta") / 1e6
        unit, axis = t(lang, "unit_musd"), t(lang, "axis_tape_delta")
    else:
        net = cum("net_contracts")
        unit, axis = t(lang, "unit_contracts"), t(lang, "axis_tape")
    calls = cum("net_calls")
    puts = cum("net_puts")

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


HEDGE_COLS = ("hedge_call_buy", "hedge_put_sell", "hedge_put_buy", "hedge_call_sell")
LIVE_WINDOW_S = 300        # mode live : 5 min glissantes, une barre par seconde


def hedge_frame(symbol: str, day: str, window_min: int = 15) -> pd.DataFrame:
    """Barres 1 min de couverture dealers du jour : disque + minute en cours
    (mémoire). Les journées écrites avant l'ajout des colonnes `hedge_*` donnent
    des zéros plutôt qu'une erreur. `window_min` = 0 : toute la séance."""
    from .flowtape import TAPE
    disk = store.load_tape(symbol, day)
    live = pd.DataFrame(TAPE.live_rows(symbol))
    if not live.empty:
        # la minute en cours (et celles en attente de flush) écrasent le disque
        disk = live if disk.empty else pd.concat([disk, live], ignore_index=True)
    if disk.empty:
        return disk
    df = disk.drop_duplicates(subset="timestamp", keep="last").sort_values("timestamp")
    for c in HEDGE_COLS:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = df[c].fillna(0.0)
    # Reset à l'open US (9h30 ET = 15h30 Paris) : une fois la séance ouverte,
    # ni la fenêtre glissante ni le cumul ne remontent avant l'ouverture. Avant
    # l'open, on garde tout le pré-marché du jour.
    start = None
    if window_min:
        start = df["timestamp"].max() - pd.Timedelta(minutes=window_min)
    open_ts = pd.Timestamp(f"{day} 09:30")
    if (df["timestamp"] >= open_ts).any():
        start = open_ts if start is None else max(start, open_ts)
    if start is not None:
        df = df[df["timestamp"] >= start]
    return df.reset_index(drop=True)


def _hedge_series(symbol: str, window_min: int, day: str):
    """Calcul partagé par `hedge_fig` (Plotly, /scalpv1) et
    `scalp_v2_hedge_data` (JSON, /scalp v2 Lightweight Charts) — même
    donnée, deux rendus. Renvoie `(ts, cats, cum, live, xrange)` ou `None`
    si rien à montrer (cf. docstring de `hedge_fig` pour le détail des
    modes live/fenêtre)."""
    live = window_min < 0          # mode « live » : un point PAR PRINT, fenêtre glissante
    now = time.time()
    if live:
        from .flowtape import TAPE
        pts = TAPE.live_points(symbol, LIVE_WINDOW_S, now)
        if not pts:
            return None
        x0 = now - LIVE_WINDOW_S
        # départ à 0 au bord gauche, un point par print, prolongé jusqu'à
        # « maintenant » : les courbes défilent même sans nouveau print
        epochs = [x0] + [p[0] for p in pts] + [now]
        cats = []
        for k in range(len(HEDGE_COLS)):
            v = np.array([p[1] if p[2] == k else 0.0 for p in pts]) / 1e6
            c = np.concatenate([[0.0], np.cumsum(v)])
            cats.append(np.append(c, c[-1]))
        ts = (pd.to_datetime(epochs, unit="s", utc=True)
              .tz_convert(LOCAL_TZ).tz_localize(None))
        xrange = [ts[0], ts[-1]]
    else:
        df = hedge_frame(symbol, day, window_min)
        if df.empty or not any(df[c].abs().sum() > 0 for c in HEDGE_COLS):
            return None
        ts = to_local(df["timestamp"])
        cats = [np.cumsum(df[c].to_numpy()) / 1e6 for c in HEDGE_COLS]
        xrange = None
    return ts, cats, sum(cats), live, xrange


def hedge_fig(symbol: str, lang: str, window_min: int = 15,
              day: str | None = None) -> go.Figure:
    """Pression de couverture des dealers sur le sous-jacent, en direct.

    Courbe BLANCHE = net cumulé (M$ de sous-jacent que les dealers doivent
    acheter au-dessus de zéro, vendre en dessous). Quatre courbes de couleur =
    le cumul de chaque catégorie : calls achetés (bleu) et puts vendus (bleu
    clair) poussent à l'achat ; puts achetés (rouge) et calls vendus (rose)
    poussent à la vente.

    Mode live (`window_min` < 0) : un point PAR PRINT sur 5 min glissantes, remise
    à zéro au bord gauche. Autres fenêtres : une marche par minute. Lecture de
    flux, pas un signal : on ne sait pas si le dealer ouvre ou ferme, ni à quel
    rythme il se couvre."""
    day = day or datetime.now(ET).strftime("%Y-%m-%d")
    title = t(lang, "hedge_title")
    res = _hedge_series(symbol, window_min, day)
    if res is None:
        return empty_fig(t(lang, "hedge_empty"), title)
    ts, cats, cum, live, xrange = res
    total = float(cum[-1])
    verdict = t(lang, "hedge_buy" if total >= 0 else "hedge_sell")
    lab = (f"live {LIVE_WINDOW_S // 60} min" if live else
           f"{window_min} min" if window_min else t(lang, "hedge_session"))
    hfmt = "%H:%M:%S" if live else "%H:%M"
    fig = go.Figure()
    # 4 courbes de cumul par catégorie (mêmes couleurs que les anciennes barres)
    # + la courbe BLANCHE du net ; toutes en marches : un print = un cran
    spec = (("hedge_leg_call_buy", C["pos"]), ("hedge_leg_put_sell", "#8dbbf0"),
            ("hedge_leg_put_buy", C["neg"]), ("hedge_leg_call_sell", "#f0a3a3"))
    for (key, color), y in zip(spec, cats):
        fig.add_scatter(x=ts, y=y, mode="lines", name=t(lang, key),
                        line=dict(color=color, width=1.4, shape="hv"),
                        hovertemplate=(f"%{{x|{hfmt}}}<br>{t(lang, key)}: "
                                       f"%{{y:+.1f}} $M<extra></extra>"))
    fig.add_scatter(x=ts, y=cum, mode="lines", name=t(lang, "hedge_cum"),
                    line=dict(color=C["ink"], width=2.6, shape="hv"),
                    hovertemplate=(f"%{{x|{hfmt}}}<br>{t(lang, 'hedge_cum')}: "
                                   f"%{{y:+.1f}} $M<extra></extra>"))
    lay = with_legend(base_layout(
        f"{title} — {lab} : {total:+.0f} $M → {verdict}", height=340))
    # 5 entrées de légende assez longues : sur une largeur étroite (page /scalp),
    # Plotly les passe sur DEUX lignes plutôt qu'une, et la marge par défaut
    # (pensée pour une seule ligne) laisse la seconde chevaucher le haut des
    # courbes. Marge fixe, pas un réglage utilisateur : le graphe est reconstruit
    # à chaque cycle (2 s), rien de manuel ne survivrait de toute façon.
    lay["margin"]["t"] += 26
    lay["legend"]["y"] = 1.22
    lay["yaxis"]["title"] = dict(text=t(lang, "hedge_axis_cum"),
                                 font=dict(color=C["muted"]))
    if xrange:
        lay["xaxis"]["range"] = xrange
    # horodatage à la SECONDE sur l'axe en live (sinon la minute)
    lay["xaxis"]["tickformat"] = "%H:%M:%S" if live else "%H:%M"
    fig.update_layout(**lay)
    fig.add_hline(y=0, line_color=C["axis"], line_width=1)
    return fig


def scalp_v2_hedge_data(symbol: str, window_min: int, day: str) -> dict:
    """Même donnée que `hedge_fig` (via `_hedge_series`, partagée — pas de
    second calcul), en JSON pour les LineSeries Lightweight Charts de
    `/scalp` v2. `/scalpv1` continue d'utiliser `hedge_fig` (Plotly)
    inchangée."""
    res = _hedge_series(symbol, window_min, day)
    if res is None:
        return {"series": []}
    ts, cats, cum, live, xrange = res
    # ts est naïf en heure LOCALE (cf. to_local/le bloc live de
    # _hedge_series) — reconverti en epoch UTC pour Lightweight Charts, qui
    # affiche déjà dans le fuseau du NAVIGATEUR (même logique que les
    # bougies de scalp_v2_chart_data).
    epoch = _epoch_seconds(pd.Series(ts).dt.tz_localize(LOCAL_TZ)).to_numpy()
    spec = (("Calls achetés", "#3987e5"), ("Puts vendus", "#8dbbf0"),
            ("Puts achetés", "#e66767"), ("Calls vendus", "#f0a3a3"))
    series = [{"name": name, "color": color,
              "points": [{"time": int(e), "value": float(v)} for e, v in zip(epoch, y)]}
             for (name, color), y in zip(spec, cats)]
    series.append({"name": "Net cumulé", "color": "#ffffff", "width": 3,
                   "points": [{"time": int(e), "value": float(v)} for e, v in zip(epoch, cum)]})
    return {"series": series}


# --- Page « Scalp » ---------------------------------------------------------
# Un seul écran pour le scalping contrarien sur rejet : spot et extension, échelle
# de niveaux avec distances, courbe de couverture live, gros prints. Les niveaux
# et le régime se recalculent au plus toutes les 10 s (ils bougent avec les pulls,
# pas avec chaque print) ; le spot, l'échelle et les graphes suivent le rythme du
# tape (2 s).
_SCALP_CACHE: dict[str, tuple[float, dict]] = {}
SCALP_CACHE_S = 10.0

# --- Journal du bandeau (cf. gex/scalp.py should_log_signal) -----------------
# Dernier (state, direction) VU pour chaque symbole, pour ne journaliser qu'une
# transition (cf. docstring de should_log_signal) — un seul process dashboard,
# donc un dict de module suffit même avec plusieurs onglets ouverts.
_SCALP_SIGNAL_SEEN: dict[str, tuple[str, int]] = {}
# Dernière alerte réellement ÉCRITE par symbole : (state, direction, epoch) —
# le garde-fou anti-flapping (cf. scalp.should_log_signal cooldown_s).
_SCALP_SIGNAL_LAST_LOGGED: dict[str, tuple[str, int, float]] = {}
_JOURNAL_CONN = None
_JOURNAL_LOCK = threading.Lock()


def _journal():
    """Connexion (partagée, créée au premier besoin) vers le même journal
    SQLite que le bot Discord (`data/journal/journal.sqlite`, WAL — accès
    concurrent sûr). None si indisponible (le bandeau continue de fonctionner
    sans, seul le journal en est privé)."""
    global _JOURNAL_CONN
    if _JOURNAL_CONN is not None:
        return _JOURNAL_CONN
    try:
        import sys
        root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(root / "discord_bot"))
        import journal
        _JOURNAL_CONN = journal.connect(SETTINGS.data_dir / "journal" / "journal.sqlite")
    except Exception:  # noqa: BLE001 — le journal ne doit jamais casser la page
        log.exception("Journal des signaux /scalp indisponible")
        _JOURNAL_CONN = False
    return _JOURNAL_CONN or None


def log_scalp_signal(symbol: str, a: dict, spot: float, move: float | None,
                     net: float, gross: float, basis: str = "fenetre_5min") -> None:
    """Journalise une TRANSITION vers un état-signal (cf. scalp.should_log_signal)
    — jamais à chaque cycle. Étanche : toute erreur reste locale à cette fonction.

    `basis` : quelle mesure de mouvement a produit `a` — `"fenetre_5min"`
    (`/scalpv1`, `scalp_inputs`) ou `"swing_v60"` (`/scalp` v2,
    `scalp_inputs_swing`). Clé de dédup/cooldown PAR (symbole, basis), pas
    juste par symbole : les deux moteurs peuvent tourner en même temps (deux
    onglets, un sur chaque page) et ne doivent jamais se marcher dessus — sans
    ça, une transition vue par l'un réinitialiserait le cooldown de l'autre.
    `basis` est aussi écrit en base (cf. journal.py) pour ne jamais remélanger
    deux méthodologies dans une même colonne sans distinction, comme ça a été
    le cas par accident avec le bug spot=30040 (cf. mémoire du projet
    audit-bug-spot-amplification-30040)."""
    key = (symbol, basis)
    prev = _SCALP_SIGNAL_SEEN.get(key)
    state, direction = a["state"], a["direction"]
    _SCALP_SIGNAL_SEEN[key] = (state, direction)
    now_epoch = time.time()
    if not scalp.should_log_signal(prev, state, direction,
                                   _SCALP_SIGNAL_LAST_LOGGED.get(key), now_epoch):
        return
    conn = _journal()
    if conn is None:
        return
    try:
        import journal
        now = datetime.now(LOCAL_TZ)
        with _JOURNAL_LOCK:
            journal.record_scalp_signal(
                conn, date=now.date().isoformat(), ts=now.isoformat(), symbol=symbol,
                state=state, tone=a["tone"], direction=direction, title=a["title"],
                spot=spot, move_pts=move, net_musd=net, gross_musd=gross, basis=basis)
        _SCALP_SIGNAL_LAST_LOGGED[key] = (state, direction, now_epoch)
    except Exception:  # noqa: BLE001 — ne doit jamais casser le bandeau
        log.exception("Écriture du signal /scalp échouée (%s, %s, %s)", symbol, state, basis)


# Salves d'absorption déjà journalisées par symbole (leur `ts` = fin de salve),
# pour ne pas réécrire la même ligne à chaque cycle où elle reste dans la
# fenêtre glissante (cf. scalp_absorption_recent, relue toutes les 2 s).
_ABSORB_LOGGED: dict[str, deque] = {}
ABSORB_LOGGED_KEEP = 200


def log_absorption_levels(symbol: str, recent: list[dict]) -> None:
    """Journalise les salves d'absorption NOUVELLES de `recent` (cf.
    scalp_absorption_recent) — pour les croiser après coup avec les signaux du
    bandeau (même base, mêmes conventions date/ts/symbol/price)."""
    if not recent:
        return
    seen = _ABSORB_LOGGED.setdefault(symbol, deque(maxlen=ABSORB_LOGGED_KEEP))
    nouvelles = [a for a in recent if a["ts"] not in seen]
    if not nouvelles:
        return
    conn = _journal()
    for a in nouvelles:
        seen.append(a["ts"])                # marqué vu même si l'écriture échoue
        if conn is None:
            continue
        try:
            import journal
            ts = datetime.fromtimestamp(a["ts"], tz=LOCAL_TZ)
            hvl = a.get("hvl") or {}
            with _JOURNAL_LOCK:
                journal.record_absorption(
                    conn, date=ts.date().isoformat(), ts=ts.isoformat(), symbol=symbol,
                    side=a["side"], price=a["price"], ratio=a.get("ratio"),
                    total=a.get("total"), n_prints=a.get("n_prints"),
                    hvl_price=hvl.get("price"), hvl_delta=hvl.get("delta"),
                    hvl_side=hvl.get("side"))
        except Exception:  # noqa: BLE001 — ne doit jamais casser le bandeau
            log.exception("Écriture de l'absorption /scalp échouée (%s, %.2f)",
                          symbol, a["price"])


def scalp_context(symbol: str) -> dict | None:
    """Niveaux, régime, VIX et ouverture de séance, mis en cache 10 s."""
    now = time.time()
    hit = _SCALP_CACHE.get(symbol)
    if hit and now - hit[0] < SCALP_CACHE_S:
        return hit[1]
    st = chain_state(symbol)
    with STATE.lock:
        df, snap, summary = st.enriched, st.snapshot, st.summary
    if df is None or snap is None:
        return None
    ref = ref_spot(symbol, snap.spot)
    side_spot = snap.spot if market_is_open() else ref
    res = metrics.compute_levels(df, ref, side_spot, bucket="Tout")
    levels = res["levels"]
    walls = []
    if not levels.empty:
        labels = wall_labels(levels)
        walls = [(labels[lv.strike], lv.strike, lv.gex) for lv in levels.itertuples()]
    hist = store.load_history(symbol)
    rd = None
    if summary is not None:
        rd = digest.symbol_reading(summary.net_gex, summary.net_dex,
                                   hist["net_gex"] if not hist.empty and "net_gex" in hist else None)
    day = datetime.now(ET).strftime("%Y-%m-%d")
    bars = store.load_prices(symbol, day)
    open_ = None
    if not bars.empty:
        rth = bars[pd.to_datetime(bars["timestamp"]) >= pd.Timestamp(f"{day} 09:30")]
        open_ = float(rth["open"].iloc[0]) if not rth.empty else None
    ctx = {"snap_spot": snap.spot,
           "zg": summary.zero_gamma if summary else None,
           "hvl": metrics.zero_gamma(df, snap.spot, weight_col="volume"),
           "keys": res["keys"], "walls": walls, "gamma": rd["gamma"] if rd else None,
           "open": open_, "vix": digest._current_vix()}
    _SCALP_CACHE[symbol] = (now, ctx)
    return ctx


def _sc_fmt(v: float, dec: int = 0) -> str:
    return f"{v:+,.{dec}f}".replace("-", "−")


def scalp_absorption(symbol: str) -> dict | None:
    """Salve d'absorption fraîche pour `symbol` ("NQ"/"ES"), ou None — même
    logique double-mode que `_futures_last_price` (gex/api.py) : mode séparé, lit
    le miroir RemoteTape (déjà relayé depuis TickCapture, cf. capturebus) ; mode
    autonome, lit TickCapture directement dans ce process."""
    from . import flowtape
    from .capturebus import remote_url
    if remote_url():
        return flowtape.TAPE.absorption(symbol)
    from .tickcapture import CAPTURE
    return CAPTURE.absorption_now(symbol)


def scalp_absorption_recent(symbol: str) -> list[dict]:
    """Derniers niveaux d'absorption détectés pour `symbol` (jusqu'à 3, le plus
    récent d'abord) — même logique double-mode que `scalp_absorption`."""
    from . import flowtape
    from .capturebus import remote_url
    if remote_url():
        return flowtape.TAPE.absorption_recent(symbol)
    from .tickcapture import CAPTURE
    return CAPTURE.absorption_recent(symbol)


def _absorb_line(symbol: str, a: dict, active: bool, lang: str) -> html.Div:
    cote = t(lang, "sc_absorb_support" if a["side"] == "SELL" else "sc_absorb_resistance")
    ratio = a.get("ratio")
    age = max(0.0, time.time() - a["ts"])
    age_txt = (t(lang, "sc_absorb_age_s", n=age) if age < 90
              else t(lang, "sc_absorb_age_min", n=age / 60))
    hvl = a.get("hvl")
    # Confirmation par le volume profile de séance (cf. gex/iceberg.py::hvl_near) :
    # ce niveau concentre aussi beaucoup de volume ET un delta marqué depuis
    # l'ouverture — un HVL avec delta fort réagit souvent (tape reading).
    hvl_txt = (t(lang, "sc_absorb_hvl_confirmed" if hvl and hvl["side"] == a["side"]
                else "sc_absorb_hvl") if hvl else "")
    txt = (f"🧊 {cote} {a['price']:,.2f} ({ratio:.0f}x{'' if ratio and ratio < 100 else '+'})"
          f"{hvl_txt} — {age_txt}")
    title = t(lang, "sc_absorb_tooltip", n=a["n_prints"], total=a["total"])
    if hvl:
        title += t(lang, "sc_absorb_tooltip_hvl", price=hvl["price"], vol=hvl["vol"],
                  delta=hvl["delta"])
    return html.Div(txt, className="sc-absorb-row" + (" sc-absorb-active" if active else "")
                    + (" sc-absorb-hvl" if hvl else ""), title=title)


def scalp_absorb_panel(symbol: str, recent: list[dict], fresh: dict | None,
                       lang: str) -> html.Div:
    """Zone d'absorption AFFICHÉE EN PERMANENCE (jamais masquée) : les derniers
    niveaux détectés sur la fenêtre glissante (cf. TickCapture.absorption_recent),
    la plus récente en tête ; elle clignote tant qu'elle est encore fraîche
    (cf. gex/iceberg.py). Vide -> message neutre, pas une zone qui disparaît."""
    fresh_ts = fresh["ts"] if fresh else None
    body = ([_absorb_line(symbol, a, a["ts"] == fresh_ts, lang) for a in recent] if recent
           else [html.Div(t(lang, "sc_absorb_empty"), className="sc-absorb-empty")])
    return html.Div([html.Div(t(lang, "sc_absorb_title"), className="sc-absorb-title"),
                     *body], id="sc-absorb", className="sc-absorb")


def scalp_head(symbol: str, lang: str, ctx: dict, spot: float) -> html.Div:
    ext = scalp.extension_pts(spot, ctx["open"])
    code, etat = scalp.session_state(datetime.now(ET), lang)
    zg = ctx["zg"]
    chips = [html.Span(etat, className=f"sc-chip sc-state-{code}")]
    if ctx["gamma"]:
        neg = "Négatif" in ctx["gamma"]
        chips.append(html.Span(digest.gamma_label(lang, ctx["gamma"]),
                               className="sc-chip " + ("sc-neg" if neg else "sc-pos")))
    if zg is not None:
        chips.append(html.Span(f"{_sc_fmt(spot - zg)} pts / Flip {zg:.0f}", className="sc-chip"))
    vix = ctx["vix"]
    if vix is not None:
        g = digest.vix_grade(vix)
        note = (t(lang, "sc_vix_high_note") if vix > digest.VIX_SEUIL
                else t(lang, "sc_vix_low_note") if vix < digest.VIX_BAS else "")
        chips.append(html.Span(f"VIX {vix:.1f} {digest.vix_grade_label(lang, g['label'])}{note}",
                               className="sc-chip"))
    ext_txt = (t(lang, "sc_since_open", pts=_sc_fmt(ext)) if ext is not None else "")
    return html.Div([
        html.Div([html.Span(f"{spot:,.2f}", id="sc-live-price", className="sc-spot"),
                  html.Span(symbol, className="sc-sym"),
                  html.Span(ext_txt, className="sc-ext " + (
                      "sc-pos" if (ext or 0) >= 0 else "sc-neg"))], className="sc-spotrow"),
        html.Div(chips, className="sc-chips"),
    ])


_SC_KIND_COLOR = {"cw": "cw", "ps": "ps", "zg": "zg", "hvl": "hvl", "d1": "d1", "gex": "lvl"}


def is_scalp_path(path: str | None) -> bool:
    """True pour `/scalp` (v2, en construction) ET `/scalpv1` (page actuelle,
    gelée) — les deux affichent aujourd'hui le même rendu, cf. commentaire du
    clientside_callback `scalp-page` dans `register_callbacks`. Dispatch
    explicite plutôt que l'accident que `"/scalpv1".startswith("/scalp")` soit
    vrai : le jour où `/scalp` a un contenu propre, le branchement se fait
    précisément ici."""
    p = path or "/"
    return p == "/scalp" or p.startswith("/scalp/") or p == "/scalpv1" or p.startswith("/scalpv1/")


def scalp_inputs(symbol: str, spot: float) -> tuple[float | None, float, float]:
    """(mouvement sur 5 min en points, flux net M$, flux brut M$) pour le bandeau.

    Le mouvement compare le spot live au dernier cours connu il y a >= 5 min
    (bougies 1 min du jour, écrites par le process capture). None si le jour n'a
    pas de bougie assez ancienne (hors séance, flux coupé)."""
    from .flowtape import TAPE
    move = None
    day = datetime.now(ET).strftime("%Y-%m-%d")
    bars = _load_prices_cached(symbol, day)
    if not bars.empty:
        cible = pd.Timestamp(datetime.now(ET).replace(tzinfo=None)) - pd.Timedelta(seconds=scalp.WINDOW_S)
        ts = pd.to_datetime(bars["timestamp"])
        vieux = bars[ts <= cible]
        recent = pd.Timestamp(datetime.now(ET).replace(tzinfo=None)) - ts.iloc[-1] < pd.Timedelta(minutes=10)
        if not vieux.empty and recent:
            move = float(spot) - float(vieux["close"].iloc[-1])
    pts = TAPE.live_points(symbol, scalp.WINDOW_S)
    net = sum(p[1] for p in pts) / 1e6
    gross = sum(abs(p[1]) for p in pts) / 1e6
    return move, net, gross


def scalp_inputs_swing(symbol: str, spot: float) -> tuple[float | None, float, float]:
    """Variante swing-ancrée de `scalp_inputs`, pour `/scalp` v2 UNIQUEMENT —
    même calcul de flux (`TAPE.live_points`, inchangé), seul le mouvement
    change de base : dernier pivot zigzag confirmé plutôt que la bougie d'il
    y a 5 min (cf. gex/bars.py::swing_move). Validé le 2026-10-03 sur le cas
    réel du 30/09 (cf. mémoire du projet roadmap-scalp-v2) : la fenêtre fixe
    annonçait "sans soutien" en pleine poussée et "soutenu" pile au sommet —
    la lecture swing donnait une structure cohérente aux deux moments.
    N'EST PAS câblée sur `/scalpv1`, qui garde `scalp_inputs` sans y toucher."""
    from .flowtape import TAPE
    from .tickcapture import _session_day
    move = None
    day = _session_day(time.time())
    ticks = store.load_ticks(symbol, day)
    if not ticks.empty:
        ticks = ticks[ticks["side"].isin(("BUY", "SELL"))].sort_values("ts", kind="stable")
        recent_ticks = ticks[ticks["ts"] >= time.time() - 90 * 60]
        if not recent_ticks.empty:
            move = swing_move(recent_ticks, spot, bar_volume=60.0,
                              min_move=scalp.move_threshold(symbol) * 0.6)
    pts = TAPE.live_points(symbol, scalp.WINDOW_S)
    net = sum(p[1] for p in pts) / 1e6
    gross = sum(abs(p[1]) for p in pts) / 1e6
    return move, net, gross


def scalp_banner(symbol: str, ctx: dict, spot: float, lang: str,
                 absorb: dict | None = None, *, swing: bool = False) -> html.Div:
    """`swing=True` (réservé à `/scalp` v2, cf. `refresh_scalp`) : mouvement
    ancré-structure (`scalp_inputs_swing`) au lieu de la fenêtre fixe 5 min —
    `/scalpv1` appelle toujours cette fonction avec `swing=False` (défaut),
    comportement strictement inchangé."""
    basis = "swing_v60" if swing else "fenetre_5min"
    move, net, gross = (scalp_inputs_swing if swing else scalp_inputs)(symbol, spot)
    zg = ctx.get("zg")
    neg = bool(ctx.get("gamma")) and "Négatif" in ctx["gamma"]
    a = scalp.assess(symbol, move, net, gross, neg, (zg - spot) if zg is not None else None, lang)
    log_scalp_signal(symbol, a, spot, move, net, gross, basis=basis)
    recent = scalp_absorption_recent(symbol)
    log_absorption_levels(symbol, recent)
    voyants = [html.Span(f"{'●' if on else '○'} {t(lang, f'sc_light_{name}')}",
                         className="sc-light" + (" on" if on else ""))
               for name, on in a["lights"].items()]
    return html.Div([
        html.Div([
            html.Div(a["title"], className="sc-banner-title"),
            html.Div(a["detail"], className="sc-banner-detail"),
            html.Div(voyants, className="sc-lights"),
        ], className="sc-banner-main"),
        # à droite du bandeau (passe en dessous si la place manque) : la place
        # vide de la bannière était l'endroit naturel plutôt qu'un bloc de plus
        html.Div(scalp_absorb_panel(symbol, recent, absorb, lang), className="sc-banner-side"),
    ], className=f"sc-banner sc-tone-{a['tone']}")


def scalp_ladder(symbol: str, ctx: dict, spot: float) -> html.Div:
    rungs = scalp.build_ladder(symbol, spot, ctx["zg"], ctx["hvl"], ctx["keys"], ctx["walls"])
    if not rungs:
        return html.Div("Niveaux indisponibles", className="hint")
    haut, bas = scalp.nearest(rungs)
    rows, spot_placed = [], False
    spot_row = html.Div([html.Span("SPOT", className="sc-name"),
                         html.Span(f"{spot:,.2f}", className="sc-price"),
                         html.Span("", className="sc-dist")], className="sc-row sc-spot-row")
    for r in rungs:
        if not spot_placed and r.price < spot:
            rows.append(spot_row)
            spot_placed = True
        color = C[_SC_KIND_COLOR[r.kind]]
        cls = "sc-row" + (" sc-near" if r.near else "")
        if r is haut or r is bas:
            cls += " sc-next"
        gex = (f" {r.gex / 1e9:+.1f}".replace("-", "−") + " Bn") if r.gex is not None else ""
        rows.append(html.Div([
            html.Span([html.Span("", className="sc-dot", style={"background": color}),
                       r.name, html.Span(gex, className="sc-gex")], className="sc-name"),
            html.Span(f"{r.price:,.0f}", className="sc-price"),
            html.Span(_sc_fmt(r.dist), className="sc-dist " + ("sc-pos" if r.dist >= 0 else "sc-neg")),
        ], className=cls))
    if not spot_placed:
        rows.append(spot_row)
    return html.Div(rows, className="sc-rows")



_LIVE_BARS: dict[str, dict[datetime, dict]] = {}
LIVE_BARS_KEEP_MIN = 5     # de quoi couvrir le retard de flush_prices (vidé toutes les 30 s)


def _update_live_bar(symbol: str, spot: float, now_et: datetime) -> dict[datetime, dict]:
    """Bougies 1 min reconstruites à partir des échantillons de spot vus par CE
    process (un par cycle du callback /scalp, toutes les 2 s — cf. dcc.Interval
    "tape-tick") : pas de vraies données tick ici, le dashboard est séparé de la
    capture (cf. gex/capturebus.py).

    ⚠️ On garde les quelques DERNIÈRES minutes, pas seulement celle en cours : la
    minute qui vient de se terminer n'est pas encore forcément sur disque
    (flush_prices ne vide que toutes les 30 s) alors que la nôtre a déjà basculé
    sur la suivante — la jeter immédiatement créait un trou d'une bougie entre
    la dernière écrite et la nouvelle minute en cours (constaté le 2026-09-30).
    Le disque reste la source de vérité dès qu'il rattrape : cf. scalp_price_fig,
    qui ne complète que les minutes manquantes après le dernier point du disque."""
    minute = now_et.replace(second=0, microsecond=0, tzinfo=None)
    bars = _LIVE_BARS.setdefault(symbol, {})
    cur = bars.get(minute)
    if cur is None:
        bars[minute] = {"open": spot, "high": spot, "low": spot, "close": spot}
    else:
        cur["high"] = max(cur["high"], spot)
        cur["low"] = min(cur["low"], spot)
        cur["close"] = spot
    cutoff = minute - timedelta(minutes=LIVE_BARS_KEEP_MIN)
    for m in [m for m in bars if m < cutoff]:
        del bars[m]
    return bars


_PRICES_CACHE: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
PRICES_CACHE_S = 2.0


def _load_prices_cached(symbol: str, day: str) -> pd.DataFrame:
    """`store.load_prices`, mis en cache quelques secondes.

    ⚠️ Sans ce cache, un lecteur toutes les 250 ms (cf. dcc.Interval
    "tape-tick") entre en collision avec l'écriture atomique périodique du
    process capture (os.replace, qui exige un accès exclusif sous Windows) —
    constaté le 2026-10-01 : échec de flush NQ quasi continu pendant ~30 min
    (`gex.store` : « verrouillé par un autre processus », PID du dashboard).
    2 s suffit largement (une bougie ne change qu'une fois par minute, ou par
    cycle de rendu pour la minute en cours via _update_live_bar) et retombe
    le risque de collision à une fraction de celui à 250 ms."""
    key = (symbol, day)
    now = time.time()
    hit = _PRICES_CACHE.get(key)
    if hit and now - hit[0] < PRICES_CACHE_S:
        return hit[1]
    bars = store.load_prices(symbol, day)
    _PRICES_CACHE[key] = (now, bars)
    return bars


def scalp_price_fig(symbol: str, ctx: dict, spot: float, minutes: int = 180) -> go.Figure:
    """Sous-jacent en bougies 1 min (les `minutes` dernières) avec les niveaux de
    l'échelle en lignes horizontales. Lit les bougies ACHEVÉES écrites par le
    process capture, et complète tout ce qui manque encore après la dernière —
    la minute en cours, et la précédente si le disque n'a pas encore rattrapé
    son retard de flush (cf. _update_live_bar) — sans ça, le graphe ne montre
    jamais rien de moins de 1-2 min, voire un trou d'une bougie au changement
    de minute. Hors séance, retombe sur le dernier jour disponible (sans
    bougie live, ce jour-là n'est plus "en cours")."""
    title = f"{symbol} · bougies 1 min"
    today = datetime.now(ET).strftime("%Y-%m-%d")
    day = today
    bars = _load_prices_cached(symbol, day)
    if bars.empty:
        days = store.price_days(symbol)
        if days:
            day = days[-1]
            bars = _load_prices_cached(symbol, day)
            title += f" · dernier jour disponible ({day})"
            # hors séance : on montre la séance US (jusqu'à la clôture 16h ET), pas la nuit
            bars = bars[pd.to_datetime(bars["timestamp"]) <= pd.Timestamp(f"{day} 16:00")]
    if bars.empty:
        return empty_fig("Pas de bougies disponibles.", title)
    ts = pd.to_datetime(bars["timestamp"])
    bars = bars[ts >= ts.iloc[-1] - pd.Timedelta(minutes=minutes)]
    if day == today:
        live_bars = _update_live_bar(symbol, spot, datetime.now(ET))
        last_ts = bars["timestamp"].iloc[-1] if not bars.empty else None
        manquantes = sorted(m for m in live_bars if last_ts is None or m > last_ts)
        if manquantes:
            bars = pd.concat([bars, pd.DataFrame([
                {"timestamp": m, **live_bars[m]} for m in manquantes])], ignore_index=True)
    x = to_local(bars["timestamp"])
    fig = go.Figure(go.Candlestick(
        x=x, open=bars["open"], high=bars["high"], low=bars["low"], close=bars["close"],
        increasing_line_color=C["pos"], decreasing_line_color=C["neg"],
        increasing_fillcolor=C["pos"], decreasing_fillcolor=C["neg"], name=symbol))
    lo, hi = float(bars["low"].min()), float(bars["high"].max())
    pad = max(2 * scalp.near_threshold(symbol) * 4, 0.5 * (hi - lo))
    for r in scalp.build_ladder(symbol, spot, ctx["zg"], ctx["hvl"], ctx["keys"], ctx["walls"]):
        if lo - pad <= r.price <= hi + pad:
            fig.add_hline(y=r.price, line_color=C[_SC_KIND_COLOR[r.kind]], line_width=1,
                          line_dash="dash" if r.kind in ("zg", "cw", "ps") else "dot",
                          annotation_text=f"{r.name} {r.price:,.0f}", annotation_position="right",
                          annotation_font=dict(size=10, color=C[_SC_KIND_COLOR[r.kind]]))
    lay = base_layout(title, height=360)
    lay["margin"]["r"] = 96
    lay["xaxis"]["rangeslider"] = dict(visible=False)
    lay["yaxis"]["range"] = [lo - pad * 0.4, hi + pad * 0.4]
    fig.update_layout(**lay)
    return fig


def _scalp_live_spot(symbol: str, ctx: dict) -> float:
    """Dernier prix RÉELLEMENT échangé (jamais le milieu bid/ask, qui peut
    tomber entre deux pas de cotation — cf. gex/rtquote.py Tick.price).

    ⚠️ Jusqu'au 2026-10-03, cette ligne lisait QUOTES.last() (flux Quote
    CONFLATÉ, un appel API par cycle) avec repli sur ctx["snap_spot"]
    (snapshot d'options périmé) si QUOTES.last() renvoyait une valeur fausse
    — sous charge (tape-tick à 250ms, cf. passation 2026-10-01), ce repli se
    déclenchait pendant que snap_spot lui-même était figé, journalisant un
    spot à des centaines de points du marché réel (cf. mémoire du projet
    audit-bug-spot-amplification-30040 : 46 signaux amplification pollués,
    spot=30040 immobile pendant des heures). `_futures_last_price`
    (gex/api.py) est la source tick-accurate déjà utilisée pour la
    résolution des outcomes (gex/scheduler.py::resolve_scalp_signals) — même
    source ici, cohérence entre ce qui déclenche un signal et ce qui le
    résout. Factorisée le 2026-10-03 (soir) : utilisée par `refresh_scalp`
    ET `refresh_scalp_lw`, un seul endroit à corriger si ça change encore."""
    raw = _futures_last_price(symbol)
    if raw is None:
        raw = QUOTES.last(symbol) if credentials_present() else None
    raw = float(raw) if raw else float(ctx["snap_spot"])
    return scalp.round_to_tick(symbol, raw)


# SPX/NDX/QQQ/SPY/NQ/ES uniquement — exactement les familles citées dans la
# demande d'origine (roadmap /scalp v2, piste "niveaux combinés"), pas les
# constituants individuels (NVDA, SMH…) qui en gonfleraient le bruit.
_CONFLUENCE_FAMILIES = ("SPX", "NDX", "QQQ", "SPY", "NQ", "ES")


# Cache séparé de _SCALP_CACHE (même principe, 10s) — AJOUTÉ le 2026-10-03
# (soir) après avoir cassé /scalp en live : boucler sur 6 familles (STATE.lock
# + metrics.zero_gamma x2 par famille) à CHAQUE cycle de tape-tick (1s) a
# saturé le serveur mono-thread (CPU cumulé très supérieur au temps écoulé,
# bandeau resté vide plusieurs dizaines de secondes). Ces niveaux ne
# bougent pas à la seconde près, un cache de 10s est largement suffisant.
_CONFLUENCE_CACHE: dict[str, tuple[float, list[dict]]] = {}
_ORDER_FLOW_CACHE: dict[str, tuple[float, list[dict]]] = {}
SCALP_HEAVY_CACHE_S = 10.0


def scalp_confluence_zones(symbol: str) -> list[dict]:
    """Zones où des niveaux de familles DIFFÉRENTES (SPX/NDX/QQQ/NQ/ES)
    tombent proches une fois transposés sur l'échelle de `symbol` — cf.
    gex/confluence.py (cluster_levels) pour la mécanique, gex/scales.py
    (déjà utilisée ailleurs dans ce fichier pour l'échelle d'affichage) pour
    la transposition : même infrastructure, pas une conversion réinventée.

    Renvoie [] si `gex/confluence.py` est absent (machine différente — cf.
    l'import défensif en tête de fichier) ou si `symbol` n'est pas NQ/ES
    (seules familles pour lesquelles `cluster_levels` a un seuil défini).
    Ne garde que les zones d'au moins 2 niveaux : un niveau seul n'est pas
    une confluence, juste un niveau — cf. docstring de confluence.py."""
    if _confluence is None or not _confluence.supported(symbol):
        return []
    now = time.time()
    hit = _CONFLUENCE_CACHE.get(symbol)
    if hit and now - hit[0] < SCALP_HEAVY_CACHE_S:
        return hit[1]
    combined: dict[str, float] = {}
    for src in _CONFLUENCE_FAMILIES:
        st = chain_state(src)
        with STATE.lock:
            df, snap = st.enriched, st.snapshot
        if df is None or snap is None:
            continue
        own_levels = _confluence.collect_levels(df, snap.spot)
        if not own_levels:
            continue
        if src == symbol:
            for name, price in own_levels.items():
                combined[f"{src}:{name}"] = price
            continue
        xf, _ratio, mode = _transform_for(src, symbol)
        if mode == "native":
            continue  # transposition impossible (spot cible absent) -> écarté plutôt que faux
        for name, price in own_levels.items():
            combined[f"{src}:{name}"] = xf(price)
    zones = _confluence.cluster_levels(combined, symbol)
    # Vérifié en live le 2026-10-03 : avec 6 familles, le chaînage de
    # cluster_levels (documenté dans confluence.py — une grappe dense peut
    # s'étendre bien au-delà du seuil si les niveaux s'enchaînent de proche
    # en proche) a produit une zone "Confluence x58" large de PLUSIEURS
    # CENTAINES de points — techniquement une vraie grappe chaînée, mais
    # inutilisable affichée comme UN niveau (elle ne dit plus "ici",
    # seulement "quelque part dans cette fourchette"). Plafond à 3x le seuil
    # de clustering du symbole : filtre d'AFFICHAGE seulement, ne touche pas
    # à cluster_levels ni à ses seuils (le chantier de l'utilisateur).
    max_width = _confluence.CLUSTER_POINTS[symbol] * 3
    kept = [{"price": (z.low + z.high) / 2, "width": z.width, "n": len(z),
            "names": sorted(z.levels.keys())}
           for z in zones if len(z) >= 2 and z.width <= max_width]
    # Classées CL1 (confluence la plus forte) à CL10 — demande explicite de
    # l'utilisateur, remplace l'étiquette brute "Confluence x{n}" : un rang
    # relatif (1 = plus de familles convergentes) se lit plus vite qu'un
    # décompte, et plafonne l'affichage à 10 zones (les plus fortes) plutôt
    # que toutes celles qui dépassent le seuil minimal de 2.
    kept.sort(key=lambda z: -z["n"])
    out = [dict(z, rank=i + 1) for i, z in enumerate(kept[:10])]
    _CONFLUENCE_CACHE[symbol] = (now, out)
    return out


def scalp_order_flow_zones(symbol: str, day_ticks: pd.DataFrame) -> list[dict]:
    """Zones HVL (volume élevé + delta net marqué) de la séance — footprint
    simplifié, cf. gex/iceberg.py::hvl_levels pour la définition exacte et
    les seuils (HVL_MIN_VOL_RATIO/HVL_MIN_DELTA_FRACTION). Seuils laissés TELS
    QUELS : diagnostiqués trop stricts le 2026-10-03 (0/257 confirmations sur
    3 jours, cf. mémoire du projet roadmap-scalp-v2), mais recalibrer cette
    méthodologie est le chantier de l'utilisateur (piste 3, absorption), pas
    le mien — affiche fidèlement ce que la fonction renvoie aujourd'hui,
    même si ça reste souvent vide.

    Construction du volume profile VECTORISÉE (groupby pandas), PAS une
    boucle Python ligne à ligne sur des centaines de milliers de ticks —
    c'est exactement le genre de travail CPU synchrone par cycle qui a
    saturé le serveur 1-thread Werkzeug le 2026-10-01 (cf. passation).

    Mis en cache 10s (cf. _ORDER_FLOW_CACHE) : même `groupby().apply()`
    vectorisé reste un travail non négligeable sur une séance pleine (des
    centaines de milliers de ticks, potentiellement des milliers de paliers
    de prix) — inutile de le refaire à chaque cycle de 1s."""
    if day_ticks.empty:
        return []
    now = time.time()
    hit = _ORDER_FLOW_CACHE.get(symbol)
    if hit and now - hit[0] < SCALP_HEAVY_CACHE_S:
        return hit[1]
    from . import iceberg as ib
    # Entièrement vectorisé — ni .map(lambda) ni .groupby().apply(lambda),
    # tous les deux en réalité un appel Python PAR LIGNE/PAR GROUPE malgré
    # l'air "vectorisé". Sur une vraie séance (repli historique ajouté ce
    # soir, des centaines de milliers de ticks), ça a fait exploser le
    # temps CPU et bloqué tout /scalp derrière le thread unique Werkzeug —
    # constaté en live le 2026-10-03, pas une supposition.
    bucket_size = ib._vp_bucket(symbol)
    bucket_key = (day_ticks["price"] / bucket_size).round() * bucket_size
    g = pd.DataFrame({
        "vol": day_ticks["volume"],
        "ask_vol": day_ticks["volume"].where(day_ticks["side"] == "BUY", 0),
        "bid_vol": day_ticks["volume"].where(day_ticks["side"] == "SELL", 0),
        "_bucket": bucket_key,
    }).groupby("_bucket").sum()
    levels = {float(price): {"vol": float(row["vol"]), "ask_vol": float(row["ask_vol"]),
                             "bid_vol": float(row["bid_vol"])}
             for price, row in g.iterrows()}
    out = [{"price": h["price"], "vol": h["vol"], "side": h["side"]}
          for h in ib.hvl_levels(levels)]
    _ORDER_FLOW_CACHE[symbol] = (now, out)
    return out


_GEX_PROFILE_CACHE: dict[str, tuple[float, list[dict]]] = {}


def scalp_gex_profile(symbol: str, spot: float, window: float = 0.04) -> list[dict]:
    """Profil de GEX par strike (pondéré open interest ET volume du jour),
    même calcul que `heatmap_fig` — porté sur `/scalp` v2 : c'était la seule
    vraie fonctionnalité de la page heatmap qui n'avait pas déjà un
    équivalent sur le nouveau graphique (niveaux/confluence/order-flow y
    sont déjà). Réutilise `_chain_for_day` et
    `metrics.gex_by_strike_weighted` tels quels plutôt que de recalculer
    quoi que ce soit — même source de vérité que la heatmap, jamais deux
    formules pour le même nombre.

    L'open interest décrit le positionnement installé, le volume du jour ce
    qui se traite et se couvre maintenant — un strike lourd en volume mais
    absent en open interest prend de l'importance en séance sans figurer
    dans la structure de la veille. D'où les DEUX séries plutôt qu'une.

    Mis en cache 10s (cf. _GEX_PROFILE_CACHE, même principe que
    confluence/order-flow) : lit la chaîne enrichie complète à chaque appel,
    pas gratuit à chaque cycle de 1s."""
    now = time.time()
    hit = _GEX_PROFILE_CACHE.get(symbol)
    if hit and now - hit[0] < SCALP_HEAVY_CACHE_S:
        return hit[1]
    today = datetime.now(ET).strftime("%Y-%m-%d")
    df, chain_spot = _chain_for_day(symbol, today)
    if df is None or df.empty or not chain_spot:
        return []
    lo, hi = spot * (1 - window), spot * (1 + window)
    sel = df[df["strike"].between(lo, hi)]
    if sel.empty:
        return []
    oi = metrics.gex_by_strike_weighted(sel, spot, "open_interest") / 1e9
    vol = metrics.gex_by_strike_weighted(sel, spot, "volume") / 1e9
    strikes = sorted(set(oi.index) | set(vol.index))
    out = [{"price": float(k), "oi": float(oi.get(k, 0.0)), "vol": float(vol.get(k, 0.0))}
          for k in strikes]
    _GEX_PROFILE_CACHE[symbol] = (now, out)
    return out


_ORDERFLOW_PROFILE_CACHE: dict[str, tuple[float, dict]] = {}
VALUE_AREA_PCT = 0.70  # convention standard (≈1 écart-type) du volume profile

# Palier DÉDIÉ au profil de volume par jambe (leg profile + zones HVN/LVN non
# testées), découplé de `ib._vp_bucket` (5pts, pensé pour la détection
# HVL/absorption — un autre chantier, cf. passation). Demande explicite de
# l'utilisateur, prix NQ/ES avançant par 0.25 : "on a des trucs tous les 5
# points mini" après avoir vu le profil — trop grossier pour être lisible à
# l'échelle d'une jambe de swing (quelques dizaines de points).
ORDERFLOW_VP_BUCKET = {"NQ": 1.0, "ES": 1.0}
DEFAULT_ORDERFLOW_VP_BUCKET = 1.0


def _orderflow_vp_bucket(symbol: str) -> float:
    return ORDERFLOW_VP_BUCKET.get(symbol.upper(), DEFAULT_ORDERFLOW_VP_BUCKET)


def _volume_profile(leg_ticks: pd.DataFrame, symbol: str) -> dict | None:
    """Profil de volume par palier de prix sur un intervalle de ticks donné —
    paliers/colonnes selon `_orderflow_vp_bucket` (1pt par défaut, PAS
    `ib._vp_bucket` qui sert la détection HVL/absorption, un chantier
    séparé). Ajoute POC (palier le plus traité) et la zone de valeur à 70 %
    (VAH/VAL) : depuis le POC, on étend d'un palier à la fois du côté
    (haut ou bas) le plus volumineux jusqu'à couvrir VALUE_AREA_PCT du volume
    total — définition standard du volume profile, pas une invention maison."""
    if leg_ticks.empty:
        return None
    bucket_size = _orderflow_vp_bucket(symbol)
    bucket_key = (leg_ticks["price"] / bucket_size).round() * bucket_size
    g = pd.DataFrame({
        "vol": leg_ticks["volume"],
        "buy": leg_ticks["volume"].where(leg_ticks["side"] == "BUY", 0),
        "sell": leg_ticks["volume"].where(leg_ticks["side"] == "SELL", 0),
        "_bucket": bucket_key,
    }).groupby("_bucket").sum()
    if g.empty or g["vol"].sum() <= 0:
        return None
    sorted_idx = list(g.index.sort_values())
    poc_price = float(g["vol"].idxmax())
    poc_pos = sorted_idx.index(poc_price)
    lo_pos = hi_pos = poc_pos
    total = float(g["vol"].sum())
    covered = float(g.loc[poc_price, "vol"])
    target = total * VALUE_AREA_PCT
    while covered < target and (lo_pos > 0 or hi_pos < len(sorted_idx) - 1):
        vol_below = float(g.loc[sorted_idx[lo_pos - 1], "vol"]) if lo_pos > 0 else -1.0
        vol_above = float(g.loc[sorted_idx[hi_pos + 1], "vol"]) if hi_pos < len(sorted_idx) - 1 else -1.0
        if vol_above >= vol_below:
            hi_pos += 1
            covered += vol_above
        else:
            lo_pos -= 1
            covered += vol_below
    buckets = [{"price": float(p), "vol": float(r["vol"]), "buy": float(r["buy"]),
               "sell": float(r["sell"])} for p, r in g.iterrows()]
    return {"poc": poc_price, "vah": float(sorted_idx[hi_pos]), "val": float(sorted_idx[lo_pos]),
           "buckets": buckets}


def _scalp_swing_legs(day_ticks: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Pivots swing CONFIRMÉS (cf. gex/bars.py::zigzag), même moteur que le
    bandeau (bar_volume=60, min_move=`scalp.move_threshold * 0.6`) — factorisé
    pour être appelé soit sur une fenêtre récente (jambes courantes, rapide),
    soit sur la séance ENTIÈRE (recherche de zones non testées, plus lent,
    cf. docstrings des deux appelants ci-dessous)."""
    if day_ticks.empty:
        return pd.DataFrame(columns=["ts", "price", "kind"])
    bars = volume_bars(day_ticks, bar_volume=60.0)
    if len(bars) < 5:
        return pd.DataFrame(columns=["ts", "price", "kind"])
    swings = zigzag(bars, min_move=scalp.move_threshold(symbol) * 0.6)
    return swings[~swings["kind"].str.endswith("?")].reset_index(drop=True)


def scalp_orderflow_profile(symbol: str, day_ticks: pd.DataFrame) -> dict:
    """Profils de volume ancrés sur les DEUX dernières jambes de swing (la en
    cours ET la dernière confirmée, pas que celle qui se dessine — sinon le
    contexte d'un retracement se perd, précision explicite de l'utilisateur)
    — remplace/complète `scalp_order_flow_zones`.

    Fenêtré à 90 min AVANT volume_bars (même convention que
    `scalp_inputs_swing`) : les deux jambes gardées sont par construction
    récentes, pas besoin de scanner toute la séance pour les trouver — et
    volume_bars (qui itère les ticks un par un) prenait jusqu'à 6s pour ES
    sur une séance entière (500k-1,4M ticks), bien trop lent pour un moteur
    planifié toutes les 8s (mesuré en direct le 2026-10-04).

    `untested` (zones HVN/LVN non revisitées) vient d'une fonction SÉPARÉE
    (`scalp_orderflow_untested`, plus bas) qui scanne la séance COMPLÈTE en
    arrière-plan, pas limitée à 90 min — demande explicite de l'utilisateur
    après avoir vu la fenêtre : "il peut tourner sur la plage complète en
    arrière-plan mais afficher d'abord 90 min". Les deux parties ont des
    coûts et des fraîcheurs différents, d'où deux fonctions et deux caches
    plutôt qu'un seul calcul qui devrait choisir entre rapide et complet.

    Mis en cache 10s (cf. _ORDERFLOW_PROFILE_CACHE)."""
    empty = {"legs": [], "untested": []}
    if day_ticks.empty:
        return empty
    now = time.time()
    hit = _ORDERFLOW_PROFILE_CACHE.get(symbol)
    if hit and now - hit[0] < SCALP_HEAVY_CACHE_S:
        return hit[1]
    last_ts_all = float(day_ticks["ts"].iloc[-1])
    anchor = last_ts_all if last_ts_all < time.time() - 3600 else time.time()
    recent_ticks = day_ticks[day_ticks["ts"] >= anchor - 90 * 60]
    confirmed = _scalp_swing_legs(recent_ticks, symbol)
    legs = []
    if not confirmed.empty:
        last_ts = float(confirmed.iloc[-1]["ts"])
        now_ts = float(recent_ticks["ts"].iloc[-1])
        # (t0, t1, en_cours) : la jambe en cours (dernier pivot confirmé ->
        # maintenant) d'abord, puis l'avant-dernière -> dernière si elle existe.
        bounds = [(last_ts, now_ts, True)]
        if len(confirmed) >= 2:
            bounds.append((float(confirmed.iloc[-2]["ts"]), last_ts, False))
        for t0, t1, is_current in bounds:
            leg_ticks = recent_ticks[(recent_ticks["ts"] >= t0) & (recent_ticks["ts"] <= t1)]
            prof = _volume_profile(leg_ticks, symbol)
            if prof:
                prof.update(t0=int(t0), t1=int(t1), current=is_current)
                legs.append(prof)
    out = {"legs": legs, "untested": scalp_orderflow_untested(symbol, day_ticks),
          # Largeur du palier : un HVN/LVN est une ZONE (ce palier de prix),
          # pas un tick exact — demande explicite de l'utilisateur, affichée
          # côté client comme une bande plutôt qu'une ligne fine.
          "bucket_size": _orderflow_vp_bucket(symbol)}
    _ORDERFLOW_PROFILE_CACHE[symbol] = (now, out)
    return out


_ORDERFLOW_UNTESTED_CACHE: dict[str, tuple[float, list[dict]]] = {}
SCALP_FULLSCAN_CACHE_S = 60.0  # bien plus large que SCALP_HEAVY_CACHE_S (10s)
SCALP_UNTESTED_MAX = 20  # zones HVN/LVN affichées, les plus récentes (cf. docstring)


def scalp_orderflow_untested(symbol: str, day_ticks: pd.DataFrame) -> list[dict]:
    """Zones HVN/LVN (High/Low Volume Node) non revisitées — scanne la
    séance ENTIÈRE (pas une fenêtre de 90 min comme `scalp_orderflow_profile`
    ci-dessus) : demande explicite de l'utilisateur après avoir vu la
    fenêtre de 90 min, "il peut tourner sur la plage complète en arrière-
    plan mais afficher d'abord 90 min" — ces zones anciennes n'ont aucune
    raison d'être bornées aux deux dernières jambes, seul le coût de calcul
    (volume_bars sur toute la séance, jusqu'à 6-8s pour ES) le justifiait.

    Mis en cache SCALP_FULLSCAN_CACHE_S (60s, pas 10s) : appelé à chaque
    cycle du moteur planifié (8s) comme tout le reste, mais ne recalcule
    pour de vrai qu'une fois sur ~7-8 cycles — le reste du temps, simple
    lecture de cache, exactement comme les autres indicateurs. Un TTL plus
    large qu'un calcul plus lent, pas une exception à la règle."""
    if day_ticks.empty:
        return []
    now = time.time()
    hit = _ORDERFLOW_UNTESTED_CACHE.get(symbol)
    if hit and now - hit[0] < SCALP_FULLSCAN_CACHE_S:
        return hit[1]
    confirmed = _scalp_swing_legs(day_ticks, symbol)
    if len(confirmed) < 3:
        out: list[dict] = []
        _ORDERFLOW_UNTESTED_CACHE[symbol] = (now, out)
        return out
    last_ts = float(confirmed.iloc[-1]["ts"])
    touched_since = day_ticks[day_ticks["ts"] >= last_ts]
    lo_touched = float(touched_since["price"].min()) if not touched_since.empty else None
    hi_touched = float(touched_since["price"].max()) if not touched_since.empty else None
    # POC (= HVN) et palier le moins traité (= LVN) de CHAQUE jambe antérieure
    # à la dernière confirmée — plus de plafond à 4 jambes maintenant que ce
    # scan tourne à part, en arrière-plan, sur son propre cache 60s.
    untested = []
    for i in range(0, len(confirmed) - 2):
        t0, t1 = float(confirmed.iloc[i]["ts"]), float(confirmed.iloc[i + 1]["ts"])
        leg_ticks = day_ticks[(day_ticks["ts"] >= t0) & (day_ticks["ts"] <= t1)]
        prof = _volume_profile(leg_ticks, symbol)
        if not prof or not prof["buckets"]:
            continue
        lvn_price = min(prof["buckets"], key=lambda b: b["vol"])["price"]
        for price, kind in ((prof["poc"], "hvn"), (lvn_price, "lvn")):
            revisited = (lo_touched is not None and lo_touched <= price <= hi_touched)
            if not revisited:
                untested.append({"price": price, "kind": kind})
    # Déduplique par PRIX SEUL (pas (prix, nature)) EN PARTANT DE LA FIN —
    # bug vu en direct le 2026-10-04 : dédupliquer par (prix, nature)
    # laissait passer "HVN non testé" ET "LVN non testé" EMPILÉS au même
    # prix (le POC d'une jambe peut coïncider avec le palier le moins
    # traité d'une jambe voisine) — contradictoire à l'affichage, le même
    # prix ne peut pas être À LA FOIS le plus et le moins traité pour
    # qui regarde le graphique. Entre deux occurrences du même prix, on
    # garde la plus RÉCENTE (construit en ordre chronologique, dédupliqué
    # en sens inverse).
    #
    # Espacement minimum (2 paliers) entre deux zones gardées : des jambes
    # courtes/serrées (consolidation) produisent des extrêmes sur des
    # paliers ADJACENTS d'une jambe à l'autre — sans ce filtre, l'affichage
    # devient un escalier dense (ex. 31040/31035/31030 à la suite) plutôt
    # que quelques niveaux significatifs.
    #
    # Plafonné à SCALP_UNTESTED_MAX (20) : le scan lui-même porte sur la
    # séance complète (pas de limite de jambes), mais 150+ lignes de prix
    # rendraient le graphique illisible — seules les plus récentes (donc
    # les plus proches de la structure actuelle) ont une vraie valeur de
    # lecture pour un scalpeur. Demande explicite de l'utilisateur : scan
    # complet en arrière-plan, affichage limité.
    min_gap = _orderflow_vp_bucket(symbol) * 2
    seen_price: set[float] = set()
    kept_prices: list[float] = []
    out = []
    for u in reversed(untested):
        price = u["price"]
        if price in seen_price:
            continue
        if any(abs(price - p) < min_gap for p in kept_prices):
            continue
        seen_price.add(price)
        kept_prices.append(price)
        out.append(u)
        if len(out) >= SCALP_UNTESTED_MAX:
            break
    _ORDERFLOW_UNTESTED_CACHE[symbol] = (now, out)
    return out


# Moteur planifié des indicateurs /scalp (2026-10-04) — demande explicite de
# l'utilisateur : "plutôt que les recalculer par graphique, un moteur qui les
# calcule et remplit un fichier de données qui est ensuite envoyé à tous les
# onglets qui le demande". Jusqu'ici, scalp_context/scalp_confluence_zones/
# scalp_order_flow_zones/scalp_gex_profile/scalp_orderflow_profile étaient
# tous mis en cache 10s mais calculés PARESSEUSEMENT — le premier onglet (de
# potentiellement 15 traders sur le même symbole) à arriver après expiration
# du cache payait le calcul. Ici, un calcul planifié en continu, qu'un onglet
# soit ouvert ou non : les requêtes ne font plus jamais que LIRE un cache
# déjà chaud.
SCALP_SCHED_SYMBOLS = ("NQ", "ES")  # les deux seuls symboles de /scalp


def _refresh_scalp_indicators() -> None:
    """Rafraîchit en bloc les caches de /scalp v2 pour chaque symbole
    scalpé. Chaque étape isolée dans son propre try/except : un calcul raté
    pour un symbole/indicateur ne doit ni bloquer les autres ni faire
    planter le scheduler lui-même (qui tournerait alors en silence, sans
    qu'on s'en aperçoive avant longtemps — bien pire qu'une erreur visible
    une fois dans les logs)."""
    for symbol in SCALP_SCHED_SYMBOLS:
        ctx = None
        try:
            ctx = scalp_context(symbol)
        except Exception:  # noqa: BLE001
            log.exception("Rafraîchissement planifié scalp_context échoué (%s)", symbol)
        day_ticks = pd.DataFrame()
        try:
            day_ticks = _scalp_day_ticks(symbol)
        except Exception:  # noqa: BLE001
            log.exception("Rafraîchissement planifié ticks du jour échoué (%s)", symbol)
        try:
            scalp_order_flow_zones(symbol, day_ticks)
            scalp_orderflow_profile(symbol, day_ticks)
        except Exception:  # noqa: BLE001
            log.exception("Rafraîchissement planifié order-flow échoué (%s)", symbol)
        try:
            scalp_confluence_zones(symbol)
        except Exception:  # noqa: BLE001
            log.exception("Rafraîchissement planifié confluence échoué (%s)", symbol)
        if ctx is not None:
            try:
                spot = _scalp_live_spot(symbol, ctx)
                scalp_gex_profile(symbol, spot)
            except Exception:  # noqa: BLE001
                log.exception("Rafraîchissement planifié profil gamma échoué (%s)", symbol)


_SCALP_INDICATOR_SCHED = None


def start_scalp_indicator_scheduler() -> None:
    """Démarre le moteur ci-dessus — appelé UNE fois depuis `gex/run.py`
    (jamais depuis `create_app()` : les tests construisent l'app à répétition
    sans jamais vouloir de vrai travail de fond, exactement pourquoi
    `gex/scheduler.py::start_scheduler` est déjà séparé de `create_app()`
    aujourd'hui — même principe ici, pas une nouvelle règle).

    Scheduler DÉDIÉ, distinct de celui de `gex/scheduler.py` (qui pilote
    l'ingestion critique — chaînes d'options, flush des fichiers) : une
    lenteur ou une erreur ici ne doit jamais retarder ces tâches-là, et
    inversement. Intervalle (8s) sous le TTL des caches (10s, cf.
    SCALP_CACHE_S/SCALP_HEAVY_CACHE_S) : jamais trouvé expiré par une
    requête, toujours rafraîchi juste avant."""
    global _SCALP_INDICATOR_SCHED
    if _SCALP_INDICATOR_SCHED is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone="America/New_York")
    sched.add_job(_refresh_scalp_indicators, "interval", seconds=8,
                 max_instances=1, coalesce=True)
    sched.start()
    _SCALP_INDICATOR_SCHED = sched


# Sélecteur de TF (/scalp v2, demandé explicitement le 2026-10-03 après
# avoir découvert que les barres-volume espacent le temps de façon
# irrégulière, déroutant face à un graphique "1 min" classique) : un préfixe
# "t" = minutes (bougies classiques, ré-échantillonnées depuis les bougies
# 1 min déjà captées) ou "v" = volume (barres-volume, base du swing validé
# le 2026-10-03, cf. gex/bars.py). "t" n'a PAS de pivots swing — la
# détection n'a été validée que sur base volume, pas étendue sans preuve.
CHART_TF_OPTIONS = [
    {"label": "1 min", "value": "t1"}, {"label": "5 min", "value": "t5"},
    {"label": "10 min", "value": "t10"}, {"label": "15 min", "value": "t15"},
    {"label": "1h", "value": "t60"}, {"label": "4h", "value": "t240"},
    {"label": "6 vol", "value": "v6"}, {"label": "60 vol", "value": "v60"},
    {"label": "600 vol", "value": "v600"},
]
CHART_TF_DEFAULT = "t1"  # 1 min par défaut, familier — pas le même usage que
# le bar_volume=60 du bandeau (scalp_inputs_swing), qui reste fixe en
# interne quel que soit le TF choisi ici pour l'affichage.


def _resample_price_bars(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Bougies `df` (1 min, colonnes timestamp/open/high/low/close, naïves
    en heure ET — cf. to_local) ré-échantillonnées à `minutes`."""
    idx = pd.to_datetime(df["timestamp"])
    out = (df.set_index(idx)[["open", "high", "low", "close"]]
          .resample(f"{minutes}min").agg(
              {"open": "first", "high": "max", "low": "min", "close": "last"})
          .dropna().reset_index(names="timestamp"))
    return out


def _scalp_day_ticks(symbol: str) -> pd.DataFrame:
    """Ticks BUY/SELL de la séance en cours, repli sur le dernier jour
    disponible si elle est encore vide (nuit, week-end) — factorisé le
    2026-10-04 : utilisé par `scalp_v2_chart_data` ET par le moteur planifié
    `_refresh_scalp_indicators` ci-dessous, jamais deux copies de cette
    logique de repli qui pourraient diverger."""
    from .tickcapture import _session_day
    day = _session_day(time.time())
    day_ticks = store.load_ticks(symbol, day)
    if not day_ticks.empty:
        day_ticks = day_ticks[day_ticks["side"].isin(("BUY", "SELL"))]
    if day_ticks.empty:
        # Repli : séance en cours encore vide — dernier jour avec des ticks,
        # même principe que `scalp_price_fig` pour les bougies Plotly.
        days = store.tick_days(symbol)
        if days:
            day_ticks = store.load_ticks(symbol, days[-1])
            if not day_ticks.empty:
                day_ticks = day_ticks[day_ticks["side"].isin(("BUY", "SELL"))]
    return day_ticks


def _scalp_indicator_snapshot(symbol: str, ctx: dict, spot: float,
                              day_ticks: pd.DataFrame) -> dict:
    """Niveaux/confluence/order-flow/profils — la partie de
    `scalp_v2_chart_data` PARTAGÉE entre tous les onglets ouverts sur ce
    symbole (contrairement aux bougies, qui dépendent du choix de TF
    propre à chaque onglet). Factorisée le 2026-10-04 : utilisée par
    `scalp_v2_chart_data` ET par le flux SSE
    `/api/v1/<symbol>/scalp-indicators-stream` — un seul assemblage, jamais
    deux copies qui pourraient diverger. `day_ticks` passé par l'appelant
    (pas recalculé ici) : les deux appelants en ont de toute façon besoin
    pour autre chose (bougies volume, boucle SSE), pas la peine de lire
    `store.load_ticks` deux fois pour le même instant."""
    levels = [{"name": r.name, "price": r.price, "color": C[_SC_KIND_COLOR[r.kind]]}
             for r in scalp.build_ladder(symbol, spot, ctx["zg"], ctx["hvl"], ctx["keys"], ctx["walls"])]
    confluence = scalp_confluence_zones(symbol)
    try:
        gex_profile = scalp_gex_profile(symbol, spot)
    except Exception:  # noqa: BLE001 — un profil raté ne doit jamais casser le graphique
        log.exception("Profil gamma /scalp v2 indisponible (%s)", symbol)
        gex_profile = []
    order_flow = scalp_order_flow_zones(symbol, day_ticks)
    try:
        orderflow_profile = scalp_orderflow_profile(symbol, day_ticks)
    except Exception:  # noqa: BLE001 — un profil raté ne doit jamais casser le graphique
        log.exception("Profil order-flow par jambe /scalp v2 indisponible (%s)", symbol)
        orderflow_profile = {"legs": [], "untested": []}
    return {"levels": levels, "confluence": confluence, "order_flow": order_flow,
           "gex_profile": gex_profile, "orderflow_profile": orderflow_profile}


def scalp_v2_chart_data(symbol: str, ctx: dict, spot: float,
                        tf: str = CHART_TF_DEFAULT, lookback_min: int | None = None) -> dict:
    """Bougies (volume OU temps, cf. `tf`) + pivots swing (base volume
    seulement) + niveaux (GEX/HVL/Flip/murs) pour le graphique TradingView
    Lightweight Charts de `/scalp` v2 — remplace `scalp_price_fig` (Plotly,
    bougies 1 min) SEULEMENT sur la nouvelle page, `/scalpv1` continue
    d'utiliser cette dernière inchangée.

    `ctx`/`spot` : mêmes objets que `scalp_price_fig` (pas de second calcul
    de ladder) — `levels`/`confluence`/`order_flow` renvoyés même sans
    bougies (le graphique peut afficher les niveaux avant d'avoir des
    données), et restent calculés sur les ticks bruts quelle que soit `tf`
    (structure de séance, pas le choix d'affichage)."""
    from .tickcapture import _session_day
    day = _session_day(time.time())
    day_ticks = _scalp_day_ticks(symbol)
    snap = _scalp_indicator_snapshot(symbol, ctx, spot, day_ticks)
    levels, confluence = snap["levels"], snap["confluence"]
    order_flow, gex_profile = snap["order_flow"], snap["gex_profile"]
    orderflow_profile = snap["orderflow_profile"]
    empty = {"candles": [], "markers": [], "levels": levels, "confluence": confluence,
            "order_flow": order_flow, "gex_profile": gex_profile,
            "orderflow_profile": orderflow_profile, "symbol": symbol}

    kind, size = tf[0], int(tf[1:])

    if kind == "t":
        # Indépendant de day_ticks à dessein : les bougies 1 min déjà
        # captées (store.load_prices) sont une source À PART, avec son
        # propre repli (store.price_days, même pattern que scalp_price_fig)
        # — ne doit JAMAIS dépendre de la présence de ticks bruts du jour.
        # Bug vérifié le 2026-10-03 : la version précédente retournait vide
        # dès que day_ticks était vide, même avec des bougies disponibles.
        bars = store.load_prices(symbol, day)
        if bars.empty:
            days = store.price_days(symbol)
            if days:
                bars = store.load_prices(symbol, days[-1])
        if bars.empty:
            return empty
        if size > 1:
            bars = _resample_price_bars(bars, size)
        n_bars = lookback_min // size if lookback_min else 150
        bars = bars.tail(max(n_bars, 1))
        if bars.empty:
            return empty
        epoch = _epoch_seconds(pd.Series(to_local(bars["timestamp"])).dt.tz_localize(LOCAL_TZ))
        candles = [{"time": int(e), "open": float(r.open), "high": float(r.high),
                    "low": float(r.low), "close": float(r.close)}
                  for e, r in zip(epoch, bars.itertuples())]
        return {"candles": candles, "markers": [], "levels": levels,
                "confluence": confluence, "order_flow": order_flow,
                "gex_profile": gex_profile, "orderflow_profile": orderflow_profile,
                "symbol": symbol}

    # kind == "v" : barres-volume, base du swing (gex/bars.py, validé le 2026-10-03)
    if day_ticks.empty:
        return empty
    ticks = day_ticks.sort_values("ts", kind="stable")
    # En repli sur un jour passé, "maintenant" n'a aucun sens pour la
    # fenêtre glissante — on prend les dernières `lookback_min` minutes DE
    # CETTE séance plutôt que de tout filtrer à vide.
    last_ts = float(ticks["ts"].iloc[-1])
    anchor = last_ts if last_ts < time.time() - 3600 else time.time()
    cutoff = anchor - (lookback_min or 90) * 60
    ticks = ticks[ticks["ts"] >= cutoff]
    if ticks.empty:
        return empty
    bars_df = volume_bars(ticks, bar_volume=float(size))
    if bars_df.empty:
        return empty
    candles = [{"time": int(r.ts_open), "open": r.open, "high": r.high,
                "low": r.low, "close": r.close} for r in bars_df.itertuples()]
    markers = []
    if len(bars_df) >= 5:
        swings = zigzag(bars_df, min_move=scalp.move_threshold(symbol) * 0.6)
        confirmed = swings[~swings["kind"].str.endswith("?")]
        markers = [{"time": int(r.ts), "price": r.price, "kind": r.kind}
                   for r in confirmed.itertuples()]
    return {"candles": candles, "markers": markers, "levels": levels, "confluence": confluence,
            "order_flow": order_flow, "gex_profile": gex_profile,
            "orderflow_profile": orderflow_profile, "symbol": symbol}


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
    # EXACTEMENT le même texte que le bot (digest.symbol_reading), pour que le
    # bandeau et les posts Discord disent la même chose. Traduit selon la langue.
    hist = store.load_history(symbol)
    netgex_hist = hist["net_gex"] if not hist.empty and "net_gex" in hist else None
    rd = digest.symbol_reading(s.net_gex, s.net_dex, netgex_hist, lang=lang)
    key = ("neg" if rd["gamma"] == "Fort Gamma Négatif"
           else "zg" if rd["gamma"] == "Gamma Négatif" else "ok")
    color = C[key]
    # « \n → … » (ligne de lecture du risque) rendue sur une seconde ligne.
    text_children = []
    for i, ligne in enumerate(rd["text"].split("\n")):
        if i:
            text_children.append(html.Br())
        text_children.append(ligne)
    return html.Div(
        [
            html.Div(t(lang, "regime_label"), className="regime-label",
                     style={"color": color}),
            html.Div(text_children, className="regime-text"),
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


# --- Export d'un graphique en image (pour le bot Discord, etc.) -----------
# Chaque graphique du dashboard doit pouvoir sortir en PNG à la demande, pas
# seulement la heatmap : un ami qui demande « la courbe du Delta de NQ » doit
# la recevoir comme n'importe quel autre. D'où ce dispatch unique par nom.
CHART_NAMES = ("gex", "dex", "heatmap", "flow", "gflow", "tape", "history",
               "spotzg", "smile", "profile", "profile_exp", "vanna", "charm", "oi")


def _figure_for(symbol: str, name: str, lang: str = "fr", bucket: str = "Tout",
                window: float | None = None, scale: str | None = None) -> go.Figure | None:
    """Reconstruit un graphique hors du contexte Dash. `bucket` (échéance :
    0DTE / Semaine / Mois / Tout), `window` (concentration, ex. 0.02) et `scale`
    (échelle d'affichage, ex. NQ pour transposer NDX en prix NQ) sont réglables
    — sinon défauts (Tout, 4 %, échelle native). Renvoie None si le nom est
    inconnu ou si les données manquent."""
    if name not in CHART_NAMES:
        return None
    if bucket not in BUCKET_KEYS:
        bucket = "Tout"
    win = window if window is not None else 0.04    # défaut concentration ±4 %
    today = datetime.now(ET).strftime("%Y-%m-%d")
    today_d = datetime.now(ET).date()
    # Échelle : native par défaut ; sinon transpose les prix vers `scale`
    # (ex. GEX du NDX affiché en prix NQ), comme le sélecteur du dashboard.
    xf, _, _ = _transform_for(symbol, (scale or symbol).upper())

    # Graphiques qui lisent le disque directement (jour + réglages par défaut).
    if name == "heatmap":
        return heatmap_fig(symbol, lang, today, win, xf, symbol, None)
    if name == "flow":
        return flow_fig(symbol, lang, today)
    if name == "gflow":
        return gamma_flow_fig(symbol, lang, today)
    if name == "tape":
        return tape_fig(symbol, lang, today)
    if name == "history":
        return history_fig(symbol, lang)
    if name == "spotzg":
        return spot_zg_fig(symbol, lang)

    # Graphiques qui ont besoin de la chaîne enrichie courante.
    st = chain_state(symbol)
    with STATE.lock:
        df, snap = st.enriched, st.snapshot
    if df is None or snap is None:
        return None
    spot = snap.spot
    zg = metrics.zero_gamma(df, spot)
    sel = df[metrics.bucket_mask(df, bucket, today_d)]
    b_lbl = t(lang, BUCKET_KEYS[bucket])
    # Mêmes murs que le dashboard : structural = clôture veille (magnitude),
    # live = spot courant en séance (côté), périmètre = bucket affiché.
    structural = ref_spot(symbol, spot)
    live = spot if market_is_open() else structural

    if name == "gex":
        res = metrics.compute_levels(df, structural, live, bucket=bucket, today=today_d)
        hvl = metrics.zero_gamma(df, spot, weight_col="volume")
        return exposure_fig(sel, spot, zg, "gex",
                            t(lang, "gex_title", bucket=b_lbl), lang,
                            levels=res["levels"], hvl=hvl, xf=xf, keys=res["keys"], window=win)
    if name == "dex":
        res = metrics.compute_levels(df, structural, live, bucket=bucket, today=today_d)
        hvl = metrics.zero_gamma(df, spot, weight_col="volume")
        return exposure_fig(sel, spot, zg, "dex",
                            t(lang, "dex_title", bucket=b_lbl), lang,
                            hvl=hvl, xf=xf, keys=res["keys"], level_set="regime", window=win)
    if name == "smile":
        return smile_fig(sel, spot, lang)
    if name == "profile":
        return profile_fig(df, spot, zg, lang, window or 0.08, xf)
    if name == "profile_exp":
        return profile_by_expiry_fig(df, spot, lang, window or 0.08, xf)
    if name in ("vanna", "charm"):
        sec = metrics.add_second_order(sel, spot)
        col = "vex" if name == "vanna" else "cex"
        title = t(lang, "vex_title" if name == "vanna" else "cex_title")
        return second_order_fig(sec, spot, col, title, win, xf)
    if name == "oi":
        prev = store.load_previous_snapshot(symbol, today)
        if prev is None:
            return None
        prev_day, prev_df = prev
        chg = metrics.oi_change(prev_df, df)
        return oi_change_fig(chg, spot, lang, prev_day, win, xf)
    return None


def chart_png(symbol: str, name: str, lang: str = "fr", bucket: str = "Tout",
              window: float | None = None, scale: str | None = None) -> bytes | None:
    """PNG d'un graphique, ou None si indisponible. `bucket`/`window`/`scale`
    réglables (échéance, concentration, échelle d'affichage). Fond opaque (le
    thème sombre a un fond transparent par défaut, illisible dans Discord)."""
    fig = _figure_for(symbol, name, lang, bucket, window, scale)
    if fig is None:
        return None
    fig.update_layout(paper_bgcolor=C["surface"], plot_bgcolor=C["surface"])
    # Round-trip via l'encodeur JSON de Plotly : kaleido sérialise avec orjson,
    # qui refuse les Timestamp pandas présents dans les bornes d'axe (heatmap,
    # history fixent un `range` en Timestamps). L'encodeur Plotly les convertit
    # proprement en chaînes ISO ; from_json reconstruit une figure sérialisable.
    import plotly.io as pio
    fig = pio.from_json(pio.to_json(fig))
    return fig.to_image(format="png", width=1100, height=620, scale=2)


def create_app() -> Dash:
    # assets/ vit dans le package (gex/assets) pour survivre à un pip install ;
    # Dash les sert dans tous les cas sous /assets.
    app = Dash(__name__, title="GEX Dashboard", update_title=None,
               assets_folder=str(Path(__file__).resolve().parent / "assets"))
    # Le ticker de prix (cf. plus bas) cible "sc-live-price", qui n'existe que
    # dans le sous-arbre RENVOYÉ par refresh_scalp — absent de app.layout au
    # démarrage. Sans ceci, Dash refuse l'enregistrement du callback.
    app.config.suppress_callback_exceptions = True
    enabled = targets()

    def ctl(label_id, control):
        """Contrôle étiqueté : la légende dit ce que le segment pilote."""
        return html.Div([html.Span(id=label_id, className="ctl-label"), control],
                        className="ctl")

    app.layout = html.Div([
        # l'URL choisit la page : « / » = vue complète, « /scalp » = mode scalping
        # (NQ et ES seulement). Même application, mêmes callbacks : la page ne fait
        # que masquer / montrer des blocs (cf. classe body.scalp-page dans style.css).
        dcc.Location(id="url", refresh=False),
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
                    dcc.Link("⚡ Mode scalping", id="scalp-link", href="/scalp",
                             className="linkbtn scalp-link"),
                    dcc.Link("← Vue complète", id="full-link", href="/",
                             className="linkbtn full-link"),
                    # page statique servie depuis assets/ (nouvel onglet)
                    html.A(id="faq-link", className="linkbtn", href="/assets/faq.html",
                           target="_blank", children="FAQ"),
                    # état du flux temps réel : masqué tant qu'aucun identifiant
                    # n'est configuré (cas de l'installation par défaut)
                    html.Div([html.Span(className="rt-dot"),
                              html.Span(id="rt-label")],
                             id="rt-badge", className="rt-badge",
                             style={"display": "none"}),
                    # Personnalisation /scalp — repliée en menu déroulant
                    # (2026-10-04, demande explicite : le panneau ouvert en
                    # permanence en haut de page prenait trop de place).
                    # Visible seulement sur /scalp et /scalpv1 (même CSS que
                    # scalp-link : body.scalp-page), à côté de l'indicateur
                    # dxFeed. Le panneau .sc-controls lui-même n'a pas bougé
                    # de place dans l'arbre (toujours dans le contenu de
                    # /scalp) — seule sa CSS change, position fixed +
                    # display piloté par cette bascule plutôt que toujours
                    # ouvert.
                    html.Button("⚙", id="sc-controls-toggle", className="linkbtn sc-controls-gear",
                                title="Personnalisation de l'affichage"),
                    # Connexion courtier : lien direct vers la route OAuth
                    # (cf. gex/tt_web.py). Masqué une fois connecté — un bouton
                    # « Connecter » affiché en permanence ferait douter de
                    # l'état réel de la connexion.
                    html.A(id="tt-connect", className="linkbtn",
                           href="/oauth/start", children="Connecter tastytrade",
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

            html.Div(id="pane-scalp", children=[
                # Disposition verticale (/scalp v2 seulement) : demande de Noé
                # (Discord, 01/10) — "pouvoir avoir les graphiques verticalement
                # plutôt que horizontalement". HORS de .sc-grid à dessein : un
                # enfant sans zone déclarée dans grid-template-areas se
                # positionne n'importe où/invisible, pas un simple bloc avant la
                # grille. Toujours dans le DOM, masqué par CSS hors
                # body.scalp-v2-page (même pattern que .scalp-link/.full-link) ;
                # état persisté en localStorage côté client, aucun état serveur.
                # Panneau "Personnalisation" — refondu le 2026-10-03 (soir) :
                # la première version (une ligne discrète sous la nav) était
                # restée invisible à l'usage réel. Carte à part entière,
                # avec un titre, pour qu'elle se remarque comme n'importe
                # quelle autre carte de la page.
                html.Div([
                    html.Div("⚙ Personnalisation de l'affichage", className="sc-controls-title"),
                    html.Div([
                        html.Button("↕ Disposition verticale", id="sc-layout-toggle",
                                   className="sc-layout-toggle", n_clicks=0),
                        # Ergonomie/concentration (/scalp v2 seulement) — piste
                        # d'origine : "masquage de graphiques/tape, mode mots-clés
                        # uniquement, pour des utilisateurs ayant des soucis de
                        # concentration". Chaque option toggle une classe body,
                        # cf. style.css ; persisté en localStorage (gex-scalp-ergo).
                        dcc.Checklist(
                            id="sc-ergo-options", className="sc-ergo-options", value=[],
                            options=[
                                {"label": "Masquer niveaux", "value": "hide_ladder"},
                                {"label": "Masquer graphique prix", "value": "hide_price_chart"},
                                {"label": "Masquer couverture dealers", "value": "hide_hedge_chart"},
                                {"label": "Masquer tape", "value": "hide_tape"},
                                {"label": "Mode mots-clés", "value": "keywords_only"},
                            ],
                        ),
                    ], className="sc-controls-row"),
                    # Widgets déplaçables/redimensionnables (2026-10-04) —
                    # remplace les dropdowns "Ordre" 1-4 (CSS `order` pur) par
                    # un vrai glisser-déposer. Rendu possible SANS le risque de
                    # désync React/Dash qu'on redoutait en septembre (cf. git
                    # blame) : GridStack.js (gex/assets/gridstack-all.js,
                    # vendu comme Lightweight Charts) gère lui-même le
                    # sous-arbre DOM de la grille APRÈS son init — exactement
                    # le même principe que le chart LW, que Dash ne redessine
                    # jamais après coup. Dash garde la main sur le CONTENU de
                    # chaque widget (scalp-ladder, scalp-banner, etc. via
                    # leurs Output habituels), jamais sur sa position/taille.
                    html.Span("Glisser l'en-tête d'un bloc pour le déplacer, "
                             "le coin pour le redimensionner.",
                             className="sc-order-hint"),
                ], className="sc-controls"),
                html.Div([
                    html.Div(id="scalp-banner", className="sc-bannerbox"),
                    html.Div(id="scalp-head", className="sc-head"),
                ], className="sc-fixed-top"),
                # Widgets déplaçables/redimensionnables (GridStack.js, cf.
                # commentaire plus haut sur sc-controls). `data-gs-id`
                # seulement ici (Dash n'autorise en HTML que les attributs
                # data-*/aria-*, pas gs-x/y/w/h bruts — GridStack, lui, ne
                # lit QUE gs-x/y/w/h sans préfixe) : le clientside_callback
                # d'init copie data-gs-id -> gs-id puis pose les positions
                # par défaut via grid.load(), pas des attributs HTML.
                html.Div([
                    html.Div([html.Div([
                        html.Span("⠿ Niveaux", className="sc-widget-handle"),
                        html.Div([html.Div("Niveaux · distance au spot (pts)", className="sc-title"),
                                  html.Div(id="scalp-ladder")], className="sc-card sc-ladder"),
                    ], className="grid-stack-item-content"),
                    ], className="grid-stack-item",
                       **{"data-gs-id": "ladder"}),
                    html.Div([html.Div([
                        html.Span("⠿ Graphique", className="sc-widget-handle"),
                        html.Div([dcc.Graph(config=GRAPH_CONFIG, id="scalp-price")],
                                 id="scalp-price-card", className="sc-card sc-underlying"),
                        # Carte v2 (Lightweight Charts) : masquée par défaut
                        # (CSS), affichée seulement sur /scalp EXACT (pas
                        # /scalpv1) — cf. classe body.scalp-v2-page dans
                        # style.css, distincte de body.scalp-page (partagée
                        # par les deux). /scalpv1 ne charge jamais cette
                        # carte, garde scalp-price inchangée.
                        html.Div([
                            # Sélecteur de TF — demandé explicitement le
                            # 2026-10-03 ("donner la possibilité de choisir sa
                            # TF"). Change UNIQUEMENT l'affichage ; le bandeau
                            # (scalp_inputs_swing) reste fixé à bar_volume=60
                            # en interne, quel que soit le choix ici.
                            dcc.Dropdown(id="scalp-chart-tf", className="sc-tf-dd",
                                        clearable=False, searchable=False,
                                        options=CHART_TF_OPTIONS, value=CHART_TF_DEFAULT),
                            # Outils de dessin — moteur extrait d'OpenCharts
                            # (MIT, github.com/dylanpersonguy/OpenCharts) sous
                            # forme de primitive Lightweight Charts, compilé
                            # en bundle vendu dans
                            # gex/assets/gex-drawing-tools.js (cf.
                            # tools/drawing-tools-src/). Tracés persistés en
                            # localStorage par symbole, pas de dialogue de
                            # style pour l'instant (couleur/épaisseur par
                            # défaut seulement) — amélioration possible plus
                            # tard.
                            html.Div([
                                html.Button("⬚", title="Sélection (Échap)", id="scalp-draw-tool-none",
                                            className="sc-draw-btn sc-draw-active", **{"data-tool": "none"}),
                                html.Button("／", title="Tendance (Alt+T)", className="sc-draw-btn",
                                            **{"data-tool": "trendline"}),
                                html.Button("—", title="Horizontale (Alt+H)", className="sc-draw-btn",
                                            **{"data-tool": "horizontal"}),
                                html.Button("¦", title="Verticale", className="sc-draw-btn",
                                            **{"data-tool": "vertical"}),
                                html.Button("↗", title="Rayon", className="sc-draw-btn",
                                            **{"data-tool": "ray"}),
                                html.Button("⇉", title="Canal parallèle", className="sc-draw-btn",
                                            **{"data-tool": "channel"}),
                                html.Button("▭", title="Rectangle (Alt+R)", className="sc-draw-btn",
                                            **{"data-tool": "rectangle"}),
                                html.Button("◯", title="Ellipse", className="sc-draw-btn",
                                            **{"data-tool": "ellipse"}),
                                html.Button("Fib", title="Fibonacci (Alt+F)", className="sc-draw-btn",
                                            **{"data-tool": "fibonacci"}),
                                html.Button("T", title="Texte", className="sc-draw-btn",
                                            **{"data-tool": "text"}),
                                html.Button("🧲", title="Aimant (accroche OHLC)", id="scalp-draw-magnet",
                                            className="sc-draw-btn"),
                                html.Button("↶", title="Annuler (Ctrl+Z)", id="scalp-draw-undo",
                                            className="sc-draw-btn"),
                                html.Button("↷", title="Rétablir (Ctrl+Y)", id="scalp-draw-redo",
                                            className="sc-draw-btn"),
                                html.Button("🗑", title="Tout effacer", id="scalp-draw-clear",
                                            className="sc-draw-btn"),
                            ], id="scalp-draw-toolbar", className="sc-draw-toolbar"),
                            # Indicateurs du graphique (2026-10-04) —
                            # demande explicite de l'utilisateur : "il faut
                            # pouvoir choisir ce qu'on affiche ... tu dois
                            # être réglable comme des indicateurs". Chaque
                            # bouton bascule UNE couche (niveaux GEX/HVL/
                            # Flip/murs, confluence multi-familles,
                            # order-flow HVL, profil de GEX par strike porté
                            # de la page heatmap, pivots swing H/L) — état
                            # partagé window._gexIndicators, persisté en
                            # localStorage, câblé dans le clientside_callback
                            # juste après le rendu du graphique.
                            html.Div([
                                html.Button("Niveaux GEX", id="scalp-ind-levels-toggle",
                                            className="sc-draw-btn sc-ind-btn sc-draw-active",
                                            title="Afficher/masquer les niveaux GEX/HVL/Flip/murs"),
                                html.Button("Confluence", id="scalp-ind-confluence-toggle",
                                            className="sc-draw-btn sc-ind-btn sc-draw-active",
                                            title="Afficher/masquer les zones de confluence multi-familles"),
                                html.Button("Order flow", id="scalp-ind-orderflow-toggle",
                                            className="sc-draw-btn sc-ind-btn sc-draw-active",
                                            title="Afficher/masquer les zones HVL order-flow"),
                                html.Button("Σ Profil gamma", id="scalp-gexprofile-toggle",
                                            className="sc-draw-btn sc-ind-btn sc-draw-active",
                                            title="Afficher/masquer le profil de GEX par strike "
                                                  "(open interest + volume du jour)"),
                                html.Button("Swing H/L", id="scalp-ind-markers-toggle",
                                            className="sc-draw-btn sc-ind-btn sc-draw-active",
                                            title="Afficher/masquer les pivots swing high/low"),
                                html.Button("Profil volume", id="scalp-ind-ofprofile-toggle",
                                            className="sc-draw-btn sc-ind-btn sc-draw-active",
                                            title="Afficher/masquer le profil de volume des 2 "
                                                  "dernières jambes de swing (POC/VAH/VAL)"),
                                html.Button("HVN/LVN", id="scalp-ind-untested-toggle",
                                            className="sc-draw-btn sc-ind-btn sc-draw-active",
                                            title="Afficher/masquer les zones HVN/LVN non "
                                                  "testées (indépendant du profil de volume "
                                                  "par jambe ci-dessus)"),
                            ], className="sc-ind-toolbar"),
                            html.Div(id="scalp-lw-chart", className="sc-lw-chart"),
                        ], id="scalp-lw-card", className="sc-card sc-underlying"),
                        dcc.Store(id="scalp-lw-data"),
                    ], className="grid-stack-item-content"),
                    ], className="grid-stack-item",
                       **{"data-gs-id": "chart"}),
                    html.Div([html.Div([
                        html.Span("⠿ Gros prints", className="sc-widget-handle"),
                        html.Div([
                            html.Div([html.Span("Gros prints", className="sc-title"),
                                      dcc.RadioItems(id="scalp-min", className="seg", inline=True,
                                                     value=5,
                                                     options=[{"label": "Tout", "value": 0},
                                                              {"label": "≥ 5", "value": 5},
                                                              {"label": "≥ 20", "value": 20}])],
                                     className="sc-cardhead"),
                            html.Div(id="scalp-prints"),
                        ], className="sc-card sc-prints"),
                    ], className="grid-stack-item-content"),
                    ], className="grid-stack-item",
                       **{"data-gs-id": "prints"}),
                    html.Div([html.Div([
                        html.Span("⠿ Couverture des dealers", className="sc-widget-handle"),
                        html.Div([
                            html.Div([html.Span("Couverture des dealers", className="sc-title"),
                                      dcc.RadioItems(id="scalp-window", className="seg", inline=True,
                                                     value=-1,
                                                     options=[{"label": "● Live 5 min", "value": -1},
                                                              {"label": "15 min", "value": 15},
                                                              {"label": "30 min", "value": 30}])],
                                     className="sc-cardhead"),
                            html.Div([dcc.Graph(config=GRAPH_CONFIG, id="scalp-hedge")],
                                     id="scalp-hedge-card"),
                            # Carte v2 (Lightweight Charts, LineSeries) :
                            # même bascule Plotly/LW que
                            # scalp-price/scalp-lw-card — /scalpv1 garde
                            # scalp-hedge (Plotly) inchangée.
                            html.Div([html.Div(id="scalp-lw-hedge", className="sc-lw-chart")],
                                     id="scalp-lw-hedge-card"),
                            dcc.Store(id="scalp-lw-hedge-data"),
                        ], className="sc-card sc-hedge"),
                    ], className="grid-stack-item-content"),
                    ], className="grid-stack-item",
                       **{"data-gs-id": "hedge"}),
                ], className="sc-gridstack grid-stack"),
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

            html.Div(id="pane-tape", children=[
                html.Div(id="tape-hint", className="hint"),
                # Pression de couverture des dealers : rafraîchie comme le
                # tableau (toutes les 2 s), minute en cours comprise.
                html.Div([
                    dcc.RadioItems(id="hedge-window", className="seg", inline=True,
                                   value=15,
                                   options=[{"label": "● Live 1 s", "value": -1},
                                            {"label": "5 min", "value": 5},
                                            {"label": "15 min", "value": 15},
                                            {"label": "30 min", "value": 30},
                                            {"label": "Σ", "value": 0}]),
                ], className="daybar"),
                dcc.Graph(config=GRAPH_CONFIG, id="hedge-graph",
                          style={"marginBottom": "12px"}),
                html.Div([
                    html.Span(id="lbl-tape-size", className="ctl-label"),
                    dcc.RadioItems(id="tape-min-size", className="seg", inline=True,
                                   value=0),
                    dcc.Checklist(id="tape-combos", className="check", inline=True,
                                  value=["combos"]),
                ], className="daybar"),
                # Tableau reconstruit à chaque tick — pas un dcc.Graph : une
                # liste de transactions se lit comme un tableau, pas un tracé.
                html.Div(id="tape-table"),
            ]),

            dcc.Interval(id="tick", interval=SETTINGS.flow_interval_s * 1000),
            # le Tape doit défiler vivant, pas au rythme des pulls (60 s).
            # ⚠️ 250 ms MESURÉ et ÉCARTÉ le 2026-10-01, cette fois sans aucun
            # confondu (pas de process orphelin, pas de threaded=True) : même
            # isolé, 250 ms suffit à saturer le serveur de dev Werkzeug, qui
            # est MONO-THREAD — curl direct sur /api/v1/NQ/last (lecture
            # triviale en mémoire) à 2-6 s au lieu de <0.3 s. 1000 ms est la
            # valeur qui tient. Ne pas redescendre sans un vrai serveur
            # multi-worker (gunicorn/waitress), pas juste threaded=True (cf.
            # commit qui l'a retiré — le GIL rend ça contre-productif pour du
            # travail CPU comme la reconstruction des figures Plotly).
            dcc.Interval(id="tape-tick", interval=1000),
            # Ticker de prix /scalp : vrai flux poussé (EventSource, cf.
            # clientside_callback plus bas), aucun sondage — donc pas de dcc.Interval
            # ici. Cible inerte requise par Dash pour un callback JS sans Output
            # visible : jamais affichée, jamais lue.
            html.Div(id="scalp-stream-sink", style={"display": "none"}),
            # le voyant du flux a son propre rythme : une déconnexion doit se
            # voir tout de suite, pas au prochain pull (60 s)
            dcc.Interval(id="rt-tick", interval=5000),
            dcc.Store(id="lang-boot", data=0),
            dcc.Store(id="sc-layout-boot", data=0),
            dcc.Store(id="sc-ergo-boot", data=0),
            dcc.Store(id="sc-grid-boot", data=0),
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
    # Page /scalp : classe sur <body> qui masque le reste du dashboard (cf. style.css)
    #
    # /scalp et /scalpv1 partagent AUJOURD'HUI le même rendu (le chantier v2 n'a
    # pas encore de contenu propre à afficher) — mais `is_scalp_path` distingue
    # déjà les deux explicitement plutôt que de compter sur l'accident que
    # "/scalpv1".startswith("/scalp") est vrai en Python/JS. Le jour où /scalp
    # diverge (nouveaux composants v2), le branchement se fait ICI, sans devoir
    # d'abord défaire un couplage implicite. /scalpv1 ne doit JAMAIS être touché
    # par ce qui est ajouté pour /scalp (cf. mémoire du projet
    # roadmap-scalp-v2 : la page actuelle doit rester vivable pendant tout le
    # chantier, appréciée des scalpeurs testeurs).
    app.clientside_callback(
        """
        function(path) {
            const p = path || '/';
            const isScalpV2 = p === '/scalp' || p.startsWith('/scalp/');
            const isScalpV1 = p === '/scalpv1' || p.startsWith('/scalpv1/');
            document.body.classList.toggle('scalp-page', isScalpV2 || isScalpV1);
            document.body.classList.toggle('scalp-v2-page', isScalpV2);
            return window.dash_clientside.no_update;
        }
        """,
        Output("url", "hash"),
        Input("url", "pathname"),
    )

    # Menu déroulant "⚙ Personnalisation" (2026-10-04) — clic sur l'icône
    # bascule l'ouverture, clic en dehors du panneau (ou sur l'icône, pour
    # refermer) la referme. Listener posé UNE fois (window._gexControlsWired)
    # — ce callback se redéclenche à chaque clic sur l'icône, inutile
    # d'empiler un nouveau listener document à chaque fois.
    app.clientside_callback(
        """
        function(n_clicks) {
            if (!n_clicks) { return window.dash_clientside.no_update; }
            document.body.classList.toggle('sc-controls-open');
            if (!window._gexControlsWired) {
                window._gexControlsWired = true;
                document.addEventListener('click', function(e) {
                    if (!document.body.classList.contains('sc-controls-open')) return;
                    const panel = document.querySelector('.sc-controls');
                    const gear = document.getElementById('sc-controls-toggle');
                    if (panel && (panel.contains(e.target) || (gear && gear.contains(e.target)))) return;
                    document.body.classList.remove('sc-controls-open');
                });
            }
            return window.dash_clientside.no_update;
        }
        """,
        Output("sc-controls-toggle", "title", allow_duplicate=True),
        Input("sc-controls-toggle", "n_clicks"),
        prevent_initial_call=True,
    )

    # Rendu du graphique Lightweight Charts (/scalp v2 uniquement) — crée le
    # chart UNE fois (cf. window._gexLwChart, persiste entre les cycles) puis
    # ne fait plus que `setData`/`setMarkers` dessus, jamais `createChart` à
    # nouveau : recréer détruirait le zoom utilisateur à chaque rafraîchissement
    # (exactement le problème que la migration Plotly->Lightweight Charts est
    # censée régler, cf. mémoire du projet chantier-scalp-sse-integral).
    #
    # ⚠️ Piège vérifié le 2026-10-03 (prototype hors Dash) : `createChart` sur
    # un conteneur dont la taille CSS n'est pas encore stable au montage fige
    # le canvas interne à largeur 0, SANS AUCUNE ERREUR JS — d'où le
    # `width`/`height` explicites ci-dessous plutôt qu'un simple 100%/100vh.
    app.clientside_callback(
        """
        function(data) {
            const container = document.getElementById('scalp-lw-chart');
            if (!container || !data || !window.LightweightCharts) {
                return window.dash_clientside.no_update;
            }

            // ── Outils de dessin (moteur extrait d'OpenCharts, MIT —
            // github.com/dylanpersonguy/OpenCharts — compilé en bundle
            // vanilla dans gex/assets/gex-drawing-tools.js, cf.
            // tools/drawing-tools-src/). Tracés persistés en localStorage,
            // un jeu par symbole. Pas de dialogue de style pour l'instant
            // (couleur/épaisseur par défaut) — amélioration possible plus
            // tard, pas demandée pour cette première passe.
            function drawStorageKey(symbol) { return 'gex-scalp-draw-' + symbol; }

            function loadDrawings(symbol) {
                try {
                    const raw = window.localStorage.getItem(drawStorageKey(symbol));
                    return raw ? JSON.parse(raw) : [];
                } catch (e) { return []; }
            }

            function saveDrawings(symbol, list) {
                try {
                    window.localStorage.setItem(drawStorageKey(symbol), JSON.stringify(list));
                } catch (e) { /* quota / navigation privée : tant pis, pas bloquant */ }
            }

            function sortByZ(list) {
                return list.slice().sort(function(a, b) { return (a.zIndex || 0) - (b.zIndex || 0); });
            }

            function drawState() {
                const g = window._gexDraw;
                return g.bySymbol[g.symbol];
            }

            // Appliers "primitifs" : mutent l'état + localStorage + repoussent
            // vers le manager, SANS toucher à l'historique annuler/rétablir
            // (undo/redo s'appuient dessus pour rejouer une opération sans
            // créer une nouvelle entrée d'historique).
            function applyAdd(d) {
                const st = drawState();
                st.drawings = sortByZ(st.drawings.concat([d]));
                saveDrawings(window._gexDraw.symbol, st.drawings);
                window._gexDraw.manager.setDrawings(st.drawings);
            }
            function applyUpdate(d) {
                const st = drawState();
                st.drawings = sortByZ(st.drawings.map(function(x) { return x.id === d.id ? d : x; }));
                saveDrawings(window._gexDraw.symbol, st.drawings);
                window._gexDraw.manager.setDrawings(st.drawings);
            }
            function applyRemove(id) {
                const st = drawState();
                st.drawings = st.drawings.filter(function(x) { return x.id !== id; });
                saveDrawings(window._gexDraw.symbol, st.drawings);
                window._gexDraw.manager.setDrawings(st.drawings);
            }

            function pushHistory(op) {
                const st = drawState();
                st.undo.push(op);
                if (st.undo.length > 100) st.undo.shift();
                st.redo = [];
            }

            function undo() {
                const st = drawState();
                const op = st.undo.pop();
                if (!op) return;
                if (op.kind === 'add') applyRemove(op.drawing.id);
                else if (op.kind === 'update') applyUpdate(op.before);
                else if (op.kind === 'remove') applyAdd(op.drawing);
                else for (const d of op.drawings) applyAdd(d);
                st.redo.push(op);
            }

            function redo() {
                const st = drawState();
                const op = st.redo.pop();
                if (!op) return;
                if (op.kind === 'add') applyAdd(op.drawing);
                else if (op.kind === 'update') applyUpdate(op.after);
                else if (op.kind === 'remove') applyRemove(op.drawing.id);
                else for (const d of op.drawings) applyRemove(d.id);
                st.undo.push(op);
            }

            function clearAll() {
                const st = drawState();
                if (st.drawings.length > 0) pushHistory({ kind: 'clear', drawings: st.drawings });
                st.drawings = [];
                saveDrawings(window._gexDraw.symbol, st.drawings);
                window._gexDraw.manager.setDrawings(st.drawings);
            }

            function setActiveToolUI(tool) {
                const toolbar = document.getElementById('scalp-draw-toolbar');
                if (!toolbar) return;
                toolbar.querySelectorAll('[data-tool]').forEach(function(btn) {
                    btn.classList.toggle('sc-draw-active', btn.getAttribute('data-tool') === tool);
                });
            }

            function switchDrawSymbol(symbol) {
                const g = window._gexDraw;
                if (!g.bySymbol[symbol]) {
                    g.bySymbol[symbol] = { drawings: sortByZ(loadDrawings(symbol)), undo: [], redo: [] };
                }
                g.symbol = symbol;
                g.manager.setDrawings(g.bySymbol[symbol].drawings);
            }

            function setupDrawingTools(chart, series, container) {
                if (!window.GexDrawingTools) return;  // bundle absent (build pas lancé) : chart marche sans
                window._gexDraw = { bySymbol: {}, manager: null, symbol: null, magnet: 'none' };
                const manager = new window.GexDrawingTools.DrawingToolsManager({
                    chart: chart, series: series, container: container,
                    intervalSec: 60, timeframe: 'scalp',
                    callbacks: {
                        onAdd: function(d) { pushHistory({ kind: 'add', drawing: d }); applyAdd(d); },
                        onUpdate: function(d) {
                            const before = drawState().drawings.find(function(x) { return x.id === d.id; });
                            if (before) pushHistory({ kind: 'update', before: before, after: d });
                            applyUpdate(d);
                        },
                        onRemove: function(id) {
                            const drawing = drawState().drawings.find(function(x) { return x.id === id; });
                            if (drawing) pushHistory({ kind: 'remove', drawing: drawing });
                            applyRemove(id);
                        },
                        onToolFinished: function() { setActiveToolUI('none'); },
                        onSelectTool: function(tool) { manager.setTool(tool); setActiveToolUI(tool); },
                        onUndo: function() { undo(); },
                        onRedo: function() { redo(); },
                    },
                });
                window._gexDraw.manager = manager;

                const toolbar = document.getElementById('scalp-draw-toolbar');
                if (toolbar && !toolbar.dataset.wired) {
                    toolbar.dataset.wired = '1';
                    toolbar.addEventListener('click', function(e) {
                        const btn = e.target.closest('[data-tool]');
                        if (!btn) return;
                        const tool = btn.getAttribute('data-tool');
                        manager.setTool(tool);
                        setActiveToolUI(tool);
                    });
                    const magnetBtn = document.getElementById('scalp-draw-magnet');
                    if (magnetBtn) magnetBtn.addEventListener('click', function() {
                        const g = window._gexDraw;
                        g.magnet = g.magnet === 'none' ? 'weak' : 'none';
                        manager.setMagnetMode(g.magnet);
                        magnetBtn.classList.toggle('sc-draw-active', g.magnet !== 'none');
                    });
                    const undoBtn = document.getElementById('scalp-draw-undo');
                    if (undoBtn) undoBtn.addEventListener('click', undo);
                    const redoBtn = document.getElementById('scalp-draw-redo');
                    if (redoBtn) redoBtn.addEventListener('click', redo);
                    const clearBtn = document.getElementById('scalp-draw-clear');
                    if (clearBtn) clearBtn.addEventListener('click', function() {
                        if (window.confirm('Effacer tous les tracés de ce symbole ?')) clearAll();
                    });
                }
            }
            if (!window._gexLwChart || window._gexLwChart.container !== container) {
                container.innerHTML = '';
                const chart = LightweightCharts.createChart(container, {
                    width: container.clientWidth, height: container.clientHeight,
                    layout: { background: { color: 'transparent' }, textColor: '#cfd3da' },
                    grid: { vertLines: { color: '#1e222a' }, horzLines: { color: '#1e222a' } },
                    // Lightweight Charts n'utilise PAS le fuseau du navigateur
                    // par défaut malgré ce que laissait croire sa doc — il
                    // formate les UTCTimestamp en UTC pur sauf formateur
                    // explicite. Bug réel vérifié en direct le 2026-10-04
                    // (axe/étiquettes affichés en UTC, pas en heure locale).
                    // `new Date(t*1000).toLocaleTimeString()` sans option
                    // `timeZone` explicite utilise le fuseau LOCAL du
                    // navigateur — cohérent avec le reste du dashboard
                    // (to_local() côté Python) sans dépendre d'un fuseau
                    // serveur codé en dur (Europe/Paris), utile si un
                    // scalpeur ouvre la page depuis un autre fuseau.
                    timeScale: {
                        timeVisible: true, secondsVisible: true,
                        tickMarkFormatter: (t, type) => {
                            const d = new Date(t * 1000);
                            return type <= 2
                                ? d.toLocaleDateString([], { day: '2-digit', month: '2-digit' })
                                : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                        },
                    },
                    localization: {
                        timeFormatter: (t) => new Date(t * 1000).toLocaleTimeString(),
                    },
                });
                const series = chart.addCandlestickSeries({
                    upColor: '#199e70', downColor: '#e66767', borderVisible: false,
                    wickUpColor: '#199e70', wickDownColor: '#e66767',
                });
                // ResizeObserver sur le CONTENEUR, pas juste window.resize —
                // la taille du conteneur change aussi sans redimensionner la
                // fenêtre : masquer un bloc voisin (cases "Masquer..."),
                // changer l'ordre des blocs, ou basculer la disposition
                // verticale, bougent tous la taille du conteneur sans
                // déclencher d'évènement 'resize' sur window. Constaté en
                // direct le 2026-10-04 : le graphique restait à sa taille
                // de création dans ces cas. Garde 0×0 : un conteneur
                // temporairement masqué (display:none) rapporte une taille
                // nulle — un resize à 0 fige le canvas interne de la lib de
                // façon permanente (piège déjà documenté pour createChart).
                const onResize = () => {
                    const w = container.clientWidth, h = container.clientHeight;
                    if (w > 0 && h > 0) chart.resize(w, h);
                };
                window.addEventListener('resize', onResize);
                new ResizeObserver(onResize).observe(container);
                // Profil de GEX par strike (porté de la page heatmap,
                // 2026-10-04, cf. gex/assets/gex-profile-overlay.js) —
                // primitive attachée UNE fois comme les outils de dessin,
                // seule setData() change ensuite à chaque cycle.
                let profile = null;
                if (window.GexProfilePrimitive) {
                    profile = new window.GexProfilePrimitive();
                    series.attachPrimitive(profile);
                }
                // Profil de volume par jambe de swing (2026-10-04, cf.
                // gex/assets/gex-orderflow-profile.js) — même principe
                // d'attachement unique que le profil gamma.
                let ofProfile = null;
                if (window.OrderFlowProfilePrimitive) {
                    ofProfile = new window.OrderFlowProfilePrimitive();
                    series.attachPrimitive(ofProfile);
                }
                window._gexLwChart = { container: container, chart: chart, series: series,
                                       priceLines: [], fitted: false, profile: profile,
                                       ofProfile: ofProfile, lastData: null };
                // Indicateurs du graphique (2026-10-04) — niveaux GEX/HVL/
                // Flip/murs, confluence, order-flow, profil gamma par
                // strike (porté de la page heatmap), profil de volume par
                // jambe de swing et pivots swing H/L : chacun
                // individuellement basculable (demande explicite de
                // l'utilisateur, "il faut pouvoir choisir ce qu'on affiche
                // ... tu dois être réglable comme des indicateurs"), état
                // persisté en localStorage, appliqué par applyIndicators()
                // ci-dessous — appelée ici ET par les boutons de bascule
                // (callbacks séparés plus bas), jamais dupliquée.
                let indicators = { levels: true, confluence: true, order_flow: true,
                                   gex_profile: true, markers: true, orderflow_profile: true,
                                   orderflow_untested: true };
                try {
                    const saved = JSON.parse(window.localStorage.getItem('gex-scalp-indicators') || 'null');
                    if (saved) indicators = Object.assign(indicators, saved);
                } catch (e) { /* valeurs par défaut */ }
                window._gexIndicators = indicators;
                // Les boutons sont rendus actifs par défaut côté Python
                // (valeur initiale avant tout chargement) — resynchronise
                // leur classe visuelle sur la préférence réellement chargée
                // (localStorage), pour ne jamais afficher un bouton "actif"
                // dont l'indicateur correspondant est en fait masqué.
                [["scalp-ind-levels-toggle", "levels"], ["scalp-ind-confluence-toggle", "confluence"],
                 ["scalp-ind-orderflow-toggle", "order_flow"],
                 ["scalp-gexprofile-toggle", "gex_profile"],
                 ["scalp-ind-markers-toggle", "markers"],
                 ["scalp-ind-ofprofile-toggle", "orderflow_profile"],
                 ["scalp-ind-untested-toggle", "orderflow_untested"]].forEach(function(pair) {
                    const btn = document.getElementById(pair[0]);
                    if (btn) btn.classList.toggle('sc-draw-active', indicators[pair[1]]);
                });
                window._gexLwChart.applyIndicators = function() {
                    const st = window._gexLwChart;
                    const d = st.lastData;
                    if (!d) return;
                    const ind = window._gexIndicators;
                    for (const pl of st.priceLines) { st.series.removePriceLine(pl); }
                    const simpleLines = ind.levels ? (d.levels || []).map(function(lv) {
                        return st.series.createPriceLine({
                            price: lv.price, color: lv.color, lineWidth: 1,
                            lineStyle: LightweightCharts.LineStyle.Dashed,
                            axisLabelVisible: true, title: lv.name,
                        });
                    }) : [];
                    // Confluence multi-familles (SPX/NDX/QQQ/NQ/ES transposés
                    // sur cette échelle, cf. scalp_confluence_zones) —
                    // classées CL1 (plus de familles convergentes) à CL10,
                    // demande explicite : "On va les nommer CL1 à 10 (1
                    // étant celui ayant le plus de confluence) avec des
                    // lignes pas trop forte". Ligne fine (1px, pas 2) pour
                    // toutes, opacité dégressive selon le rang (CL1 la plus
                    // visible, CL10 la plus discrète) plutôt qu'un aplat
                    // uniforme — la force relative se lit d'un coup d'œil
                    // sans que rien ne crie sur le graphique.
                    const confluenceLines = ind.confluence ? (d.confluence || []).map(function(z) {
                        const alpha = Math.max(0.3, 0.9 - (z.rank - 1) * (0.6 / 9));
                        return st.series.createPriceLine({
                            price: z.price, color: 'rgba(201, 133, 0, ' + alpha + ')', lineWidth: 1,
                            lineStyle: LightweightCharts.LineStyle.Solid,
                            axisLabelVisible: true, title: 'CL' + z.rank,
                        });
                    }) : [];
                    // Zones order flow (HVL volume profil, cf.
                    // scalp_order_flow_zones) : ligne pointillée cyan,
                    // étiquette volume+côté — distincte des niveaux GEX/HVL
                    // simples (tirets) et de la confluence (pleine dorée).
                    const flowLines = ind.order_flow ? (d.order_flow || []).map(function(h) {
                        return st.series.createPriceLine({
                            price: h.price, color: '#3987e5', lineWidth: 1,
                            lineStyle: LightweightCharts.LineStyle.Dotted,
                            axisLabelVisible: true,
                            title: 'HVL ' + (h.side === 'BUY' ? '↑' : '↓') + ' ' + Math.round(h.vol),
                        });
                    }) : [];
                    // Zones HVN/LVN non revisitées (High/Low Volume Node,
                    // terminologie standard du volume profile — PAS le 'HVL'
                    // ci-dessus, concept différent) : rendues par
                    // ofProfile.setUntested() plus bas (bande semi-
                    // transparente sur la largeur du palier, PAS une ligne
                    // fine — demande explicite de l'utilisateur, "en général
                    // c'est une zone pas un prix fixe").
                    st.priceLines = simpleLines.concat(confluenceLines, flowLines);
                    st.series.setMarkers(ind.markers ? (d.markers || []).map(function(m) {
                        const isHigh = m.kind.startsWith('H');
                        return {
                            time: m.time, position: isHigh ? 'aboveBar' : 'belowBar',
                            color: isHigh ? '#e66767' : '#199e70',
                            shape: isHigh ? 'arrowDown' : 'arrowUp',
                            text: m.kind + ' ' + Math.round(m.price),
                        };
                    }) : []);
                    if (st.profile) {
                        st.profile.setVisible(ind.gex_profile);
                        st.profile.setData(d.gex_profile || []);
                    }
                    if (st.ofProfile) {
                        st.ofProfile.setVisible(ind.orderflow_profile);
                        st.ofProfile.setUntestedVisible(ind.orderflow_untested);
                        st.ofProfile.setData(((d.orderflow_profile || {}).legs) || []);
                        st.ofProfile.setUntested(((d.orderflow_profile || {}).untested) || [],
                                                 (d.orderflow_profile || {}).bucket_size);
                    }
                };
                setupDrawingTools(chart, series, container);
            }
            const state = window._gexLwChart;
            // Outils de dessin : tracés persistés par symbole (localStorage),
            // indépendants du chart/series qui restent stables — seul le jeu
            // de tracés affiché change quand le symbole change.
            if (window._gexDraw && window._gexDraw.manager && data.symbol
                    && window._gexDraw.symbol !== data.symbol) {
                switchDrawSymbol(data.symbol);
            }
            // Déduplique les barres au même timestamp entier (activité dense) :
            // Lightweight Charts exige un temps strictement croissant.
            const byTime = new Map();
            for (const c of (data.candles || [])) byTime.set(c.time, c);
            const candles = Array.from(byTime.values()).sort(function(a, b) { return a.time - b.time; });
            state.series.setData(candles);
            // fitContent() UNE SEULE FOIS (premier chargement de données) —
            // le rappeler à chaque rafraîchissement (chaque cycle ~1s)
            // écrase le zoom/pan de l'utilisateur en permanence, exactement
            // le défaut que la migration depuis Plotly devait régler.
            // setData() seul préserve déjà la vue courante, c'est tout le
            // principe de Lightweight Charts. Signalé en direct le
            // 2026-10-03 : "pénible de zoomer et se faire dézoomer".
            if (candles.length && !state.fitted) {
                state.chart.timeScale().fitContent();
                state.fitted = true;
            }
            state.lastData = data;
            state.applyIndicators();
            return window.dash_clientside.no_update;
        }
        """,
        Output("scalp-lw-chart", "title"),
        Input("scalp-lw-data", "data"),
    )

    # Boutons de bascule des indicateurs (sc-ind-toolbar) — chacun flip son
    # propre flag dans window._gexIndicators, persiste, puis ré-applique
    # IMMÉDIATEMENT sur les dernières données reçues (state.lastData) sans
    # attendre le prochain cycle de rafraîchissement (~1s) : le retour
    # visuel au clic doit être instantané.
    for _ind_key, _ind_btn_id in (
        ("levels", "scalp-ind-levels-toggle"),
        ("confluence", "scalp-ind-confluence-toggle"),
        ("order_flow", "scalp-ind-orderflow-toggle"),
        ("gex_profile", "scalp-gexprofile-toggle"),
        ("markers", "scalp-ind-markers-toggle"),
        ("orderflow_profile", "scalp-ind-ofprofile-toggle"),
        ("orderflow_untested", "scalp-ind-untested-toggle"),
    ):
        app.clientside_callback(
            """
            function(n_clicks) {
                if (!n_clicks) { return window.dash_clientside.no_update; }
                const key = '""" + _ind_key + """';
                const btn = document.getElementById('""" + _ind_btn_id + """');
                window._gexIndicators = window._gexIndicators || {};
                const next = !window._gexIndicators[key];
                window._gexIndicators[key] = next;
                if (btn) btn.classList.toggle('sc-draw-active', next);
                try {
                    window.localStorage.setItem('gex-scalp-indicators',
                        JSON.stringify(window._gexIndicators));
                } catch (e) { /* tant pis, pas bloquant */ }
                if (window._gexLwChart && window._gexLwChart.applyIndicators) {
                    window._gexLwChart.applyIndicators();
                }
                return window.dash_clientside.no_update;
            }
            """,
            Output(_ind_btn_id, "title", allow_duplicate=True),
            Input(_ind_btn_id, "n_clicks"),
            prevent_initial_call=True,
        )

    # Couverture des dealers (/scalp v2) — même principe que le graphique de
    # prix : un LineSeries par catégorie, créés UNE fois (persistés dans
    # window._gexLwHedge), seulement `setData` ensuite. Piège du canvas à
    # largeur 0 déjà réglé pour le graphique de prix, même fix ici.
    app.clientside_callback(
        """
        function(data) {
            const container = document.getElementById('scalp-lw-hedge');
            if (!container || !data || !window.LightweightCharts) {
                return window.dash_clientside.no_update;
            }
            if (!window._gexLwHedge || window._gexLwHedge.container !== container) {
                container.innerHTML = '';
                const chart = LightweightCharts.createChart(container, {
                    width: container.clientWidth, height: container.clientHeight,
                    layout: { background: { color: 'transparent' }, textColor: '#cfd3da' },
                    grid: { vertLines: { color: '#1e222a' }, horzLines: { color: '#1e222a' } },
                    // Lightweight Charts n'utilise PAS le fuseau du navigateur
                    // par défaut malgré ce que laissait croire sa doc — il
                    // formate les UTCTimestamp en UTC pur sauf formateur
                    // explicite. Bug réel vérifié en direct le 2026-10-04
                    // (axe/étiquettes affichés en UTC, pas en heure locale).
                    // `new Date(t*1000).toLocaleTimeString()` sans option
                    // `timeZone` explicite utilise le fuseau LOCAL du
                    // navigateur — cohérent avec le reste du dashboard
                    // (to_local() côté Python) sans dépendre d'un fuseau
                    // serveur codé en dur (Europe/Paris), utile si un
                    // scalpeur ouvre la page depuis un autre fuseau.
                    timeScale: {
                        timeVisible: true, secondsVisible: true,
                        tickMarkFormatter: (t, type) => {
                            const d = new Date(t * 1000);
                            return type <= 2
                                ? d.toLocaleDateString([], { day: '2-digit', month: '2-digit' })
                                : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                        },
                    },
                    localization: {
                        timeFormatter: (t) => new Date(t * 1000).toLocaleTimeString(),
                    },
                });
                // Même correctif que le graphique de prix : ResizeObserver
                // sur le conteneur, pas juste window.resize (cf. commentaire
                // détaillé là-bas).
                const onResize = () => {
                    const w = container.clientWidth, h = container.clientHeight;
                    if (w > 0 && h > 0) chart.resize(w, h);
                };
                window.addEventListener('resize', onResize);
                new ResizeObserver(onResize).observe(container);
                window._gexLwHedge = { container: container, chart: chart, series: {}, fitted: false };
            }
            const state = window._gexLwHedge;
            (data.series || []).forEach(function(s) {
                if (!state.series[s.name]) {
                    state.series[s.name] = state.chart.addLineSeries({
                        color: s.color, lineWidth: s.width || 1, title: s.name,
                    });
                }
                state.series[s.name].setData(s.points || []);
            });
            // fitContent() une seule fois — même correctif que le graphique
            // de prix ci-dessus, même raison (zoom écrasé à chaque cycle).
            if (!state.fitted && (data.series || []).length && (data.series[0].points || []).length) {
                state.chart.timeScale().fitContent();
                state.fitted = true;
            }
            return window.dash_clientside.no_update;
        }
        """,
        Output("scalp-lw-hedge", "title"),
        Input("scalp-lw-hedge-data", "data"),
    )

    @app.callback(
        Output("symbol", "value", allow_duplicate=True),
        Input("url", "pathname"),
        State("symbol", "value"),
        prevent_initial_call="initial_duplicate",
    )
    def scalp_symbol(path, symbol):
        """Sur /scalp ou /scalpv1, seuls NQ et ES existent : un autre
        sous-jacent retombe sur NQ."""
        if is_scalp_path(path) and symbol not in ("NQ", "ES"):
            return "NQ"
        raise PreventUpdate

    app.clientside_callback(
        "function(l) { window.localStorage.setItem('gex-lang', l); return window.dash_clientside.no_update; }",
        Output("lang-boot", "data"),
        Input("lang", "value"),
        prevent_initial_call=True,
    )

    # Disposition verticale /scalp v2 (cf. .sc-layout-toggle, style.css) :
    # restaure la préférence au chargement — serveur si identifié (Cloudflare
    # Access, cf. /api/v1/prefs), sinon localStorage (2026-10-04, piste 7
    # roadmap-scalp-v2). `window._gexPrefsReady` : UNE SEULE requête serveur
    # par page, partagée par les 3 préférences ci-dessous (idempotent —
    # `||=` ne relance pas le fetch si une autre de ces callbacks l'a déjà
    # déclenché, peu importe l'ordre d'exécution des callbacks Dash).
    app.clientside_callback(
        """
        async function(_) {
            window._gexPrefsReady = window._gexPrefsReady || fetch('/api/v1/prefs')
                .then(r => r.json())
                .then(d => { window._gexUserEmail = d.email; return d; })
                .catch(() => ({ email: null, prefs: {} }));
            const data = await window._gexPrefsReady;
            const fromServer = data.prefs['scalp-layout'];
            const vertical = fromServer != null ? fromServer === 'vertical'
                : window.localStorage.getItem('gex-scalp-layout') === 'vertical';
            if (vertical) document.body.classList.add('sc-layout-vertical');
            if (fromServer != null) window.localStorage.setItem('gex-scalp-layout', fromServer);
            return window.dash_clientside.no_update;
        }
        """,
        Output("sc-layout-toggle", "title"),
        Input("sc-layout-boot", "data"),
    )
    # Bascule au clic + persiste (localStorage TOUJOURS, serveur SI identifié)
    # — lit l'état actuel sur <body> plutôt que de suivre n_clicks
    # (pair/impair), robuste à un rechargement entre-temps. Répercute aussi
    # sur GridStack (colonne unique) si la grille existe déjà — sinon
    # grid-init la lira elle-même sur <body> à sa propre création (ordre des
    # deux callbacks non garanti au chargement, cf. commentaire plus bas).
    app.clientside_callback(
        """
        function(n_clicks) {
            if (!n_clicks) { return window.dash_clientside.no_update; }
            const vertical = document.body.classList.toggle('sc-layout-vertical');
            const value = vertical ? 'vertical' : 'horizontal';
            window.localStorage.setItem('gex-scalp-layout', value);
            if (window._gexGrid) window._gexGrid.column(vertical ? 1 : 12);
            if (window._gexUserEmail) {
                fetch('/api/v1/prefs', { method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key: 'scalp-layout', value: value }) }).catch(() => {});
            }
            return window.dash_clientside.no_update;
        }
        """,
        Output("sc-layout-toggle", "title", allow_duplicate=True),
        Input("sc-layout-toggle", "n_clicks"),
        prevent_initial_call=True,
    )

    # Ergonomie/concentration /scalp v2 : restaure au chargement (serveur si
    # identifié, sinon localStorage — même principe que la disposition
    # verticale ci-dessus)...
    app.clientside_callback(
        """
        async function(_) {
            window._gexPrefsReady = window._gexPrefsReady || fetch('/api/v1/prefs')
                .then(r => r.json())
                .then(d => { window._gexUserEmail = d.email; return d; })
                .catch(() => ({ email: null, prefs: {} }));
            const data = await window._gexPrefsReady;
            let saved = data.prefs['scalp-ergo'];
            if (saved == null) {
                try { saved = JSON.parse(window.localStorage.getItem('gex-scalp-ergo') || '[]'); }
                catch (e) { saved = []; }
            } else {
                window.localStorage.setItem('gex-scalp-ergo', JSON.stringify(saved));
            }
            return saved;
        }
        """,
        Output("sc-ergo-options", "value"),
        Input("sc-ergo-boot", "data"),
    )
    # ...applique les classes body + masque/affiche les 4 WIDGETS GridStack
    # concernés via grid.removeWidget()/addWidget() (jamais display:none sur
    # un .grid-stack-item — ça laisserait un trou dans la grille, cf.
    # commentaire sur .sc-gridstack dans style.css) + persiste à chaque
    # changement. `window._gexGridVisible`/`window._gexHiddenWidgets` :
    # état partagé avec grid-init (plus bas) pour les deux ordres de
    # chargement possibles (grille créée avant ou après ce callback).
    app.clientside_callback(
        """
        function(values) {
            values = values || [];
            const hideFlag = {
                ladder: values.includes('hide_ladder'),
                chart: values.includes('hide_price_chart'),
                hedge: values.includes('hide_hedge_chart'),
                prints: values.includes('hide_tape'),
            };
            const keywordsOnly = values.includes('keywords_only');
            document.body.classList.toggle('sc-keywords-only', keywordsOnly);
            const hiddenIds = Object.keys(hideFlag).filter(
                id => hideFlag[id] || keywordsOnly);
            window._gexHiddenWidgets = hiddenIds;
            const grid = window._gexGrid;
            if (grid) {
                window._gexGridVisible = window._gexGridVisible
                    || { ladder: true, chart: true, hedge: true, prints: true };
                Object.keys(hideFlag).forEach(function(id) {
                    const el = window._gexGridWidgets && window._gexGridWidgets[id];
                    if (!el) return;
                    const shouldShow = !(hideFlag[id] || keywordsOnly);
                    if (shouldShow !== window._gexGridVisible[id]) {
                        // GridStack v11+ : addWidget() ne prend plus un
                        // HTMLElement déjà existant (il crée un NOUVEL
                        // élément) — makeWidget() re-rattache l'élément
                        // déjà présent dans le DOM, exactement notre cas
                        // (bug vu en direct le 2026-10-04, warning console).
                        if (shouldShow) grid.makeWidget(el); else grid.removeWidget(el, false);
                        window._gexGridVisible[id] = shouldShow;
                    }
                });
            }
            window.localStorage.setItem('gex-scalp-ergo', JSON.stringify(values));
            if (window._gexUserEmail) {
                fetch('/api/v1/prefs', { method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key: 'scalp-ergo', value: values }) }).catch(() => {});
            }
            return window.dash_clientside.no_update;
        }
        """,
        Output("sc-ergo-options", "title"),
        Input("sc-ergo-options", "value"),
    )

    # Widgets déplaçables/redimensionnables (GridStack.js, 2026-10-04) —
    # remplace les anciens dropdowns "Ordre" 1-4. Crée la grille UNE fois
    # (window._gexGrid, persiste entre les cycles comme window._gexLwChart),
    # applique la disposition sauvegardée (serveur si identifié, sinon
    # localStorage), puis persiste à chaque glisser/redimensionner
    # (évènement 'change' de GridStack, pas en continu pendant le geste).
    app.clientside_callback(
        """
        async function(_) {
            if (!window.GridStack) return window.dash_clientside.no_update;
            const el = document.querySelector('.sc-gridstack');
            if (!el || window._gexGrid) return window.dash_clientside.no_update;
            // GridStack lit gs-x/y/w/h SANS préfixe sur chaque
            // .grid-stack-item au moment de l'init — Dash, côté Python,
            // n'autorise que data-gs-id (data-* seulement) sur ces
            // conteneurs, d'où la traduction ici + la position par défaut
            // (première visite, avant toute disposition sauvegardée).
            const DEFAULTS = {
                ladder: { x: 0, y: 0, w: 4, h: 6 }, chart: { x: 4, y: 0, w: 8, h: 6 },
                prints: { x: 0, y: 6, w: 4, h: 5 }, hedge: { x: 4, y: 6, w: 8, h: 5 },
            };
            el.querySelectorAll('.grid-stack-item').forEach(function(item) {
                const id = item.getAttribute('data-gs-id');
                item.setAttribute('gs-id', id);
                const d = DEFAULTS[id];
                if (d) {
                    item.setAttribute('gs-x', d.x); item.setAttribute('gs-y', d.y);
                    item.setAttribute('gs-w', d.w); item.setAttribute('gs-h', d.h);
                }
            });
            const vertical = document.body.classList.contains('sc-layout-vertical');
            const grid = GridStack.init({
                cellHeight: 70, margin: 8, float: true,
                handle: '.sc-widget-handle', oneColumnSize: 900,
                column: vertical ? 1 : 12,
            }, el);
            window._gexGrid = grid;
            window._gexGridWidgets = {};
            el.querySelectorAll('.grid-stack-item').forEach(function(item) {
                window._gexGridWidgets[item.getAttribute('gs-id')] = item;
            });
            // Masquage déjà demandé (cases à cocher) avant que la grille existe ?
            window._gexGridVisible = window._gexGridVisible
                || { ladder: true, chart: true, hedge: true, prints: true };
            (window._gexHiddenWidgets || []).forEach(function(id) {
                const w = window._gexGridWidgets[id];
                if (w && window._gexGridVisible[id]) {
                    grid.removeWidget(w, false);
                    window._gexGridVisible[id] = false;
                }
            });
            // Disposition sauvegardée (positions/tailles des 4 widgets).
            window._gexPrefsReady = window._gexPrefsReady || fetch('/api/v1/prefs')
                .then(r => r.json())
                .then(d => { window._gexUserEmail = d.email; return d; })
                .catch(() => ({ email: null, prefs: {} }));
            const data = await window._gexPrefsReady;
            let layout = data.prefs['scalp-grid'];
            if (layout == null) {
                try { layout = JSON.parse(window.localStorage.getItem('gex-scalp-grid') || 'null'); }
                catch (e) { layout = null; }
            } else {
                window.localStorage.setItem('gex-scalp-grid', JSON.stringify(layout));
            }
            if (layout) grid.load(layout);
            grid.on('change', function() {
                const saved = grid.save(false);
                window.localStorage.setItem('gex-scalp-grid', JSON.stringify(saved));
                if (window._gexUserEmail) {
                    fetch('/api/v1/prefs', { method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ key: 'scalp-grid', value: saved }) }).catch(function() {});
                }
            });
            return window.dash_clientside.no_update;
        }
        """,
        Output("sc-layout-toggle", "title", allow_duplicate=True),
        Input("sc-grid-boot", "data"),
        prevent_initial_call="initial_duplicate",
    )

    # Ticker de prix /scalp : REÇOIT, AFFICHE, rien d'autre ne se met à jour, et
    # rien n'interroge personne — EventSource (Server-Sent Events) ouvre une
    # connexion UNE FOIS, le serveur écrit dessus dès qu'un nouveau prix arrive
    # (cf. gex/api.py `_last_trade_stream`, alimenté depuis TickCapture via
    # capturebus). Pas de dcc.Interval, pas de fetch répété : un vrai flux
    # poussé jusqu'au navigateur, tick-accurate. N'ouvre une connexion que sur
    # /scalp, et seulement pour NQ/ES ; ferme proprement l'ancienne avant d'en
    # ouvrir une nouvelle pour ne jamais en accumuler (changement de symbole,
    # de page, ou rechargement du bloc Python qui réinitialise le span).
    app.clientside_callback(
        """
        function(symbol, path) {
            if (window._scStream) { window._scStream.close(); window._scStream = null; }
            if (!(path || '/').startsWith('/scalp') || !['NQ', 'ES'].includes(symbol)) {
                return window.dash_clientside.no_update;
            }
            const es = new EventSource(`/api/v1/${symbol}/stream`);
            es.onmessage = function(ev) {
                const el = document.getElementById('sc-live-price');
                if (!el) return;
                const px = parseFloat(ev.data);
                if (isNaN(px)) return;
                // même format que le rendu Python (f"{spot:,.2f}") : virgule des
                // milliers, point décimal — sinon le format alterne visuellement
                // entre les deux sources (JS vs Python), un clignotement de plus.
                el.textContent = px.toLocaleString('en-US',
                    {minimumFractionDigits: 2, maximumFractionDigits: 2});
            };
            window._scStream = es;
            return window.dash_clientside.no_update;
        }
        """,
        Output("scalp-stream-sink", "className"),
        Input("symbol", "value"),
        Input("url", "pathname"),
    )

    # Flux SSE des indicateurs /scalp v2 (2026-10-04) — demande explicite :
    # "un moteur qui les calcule et remplit un fichier de données qui est
    # ensuite envoyé à tous les onglets qui le demande". Même principe que
    # le ticker de prix ci-dessus : une connexion EventSource, le serveur
    # pousse dès que le moteur planifié (_refresh_scalp_indicators,
    # gex/app.py) a rafraîchi le cache — plus jamais de polling ~1s par
    # onglet pour niveaux/confluence/order-flow/profils (PARTAGÉS entre
    # tous les traders sur ce symbole). Les bougies (par TF, propres à
    # chaque onglet) restent sur le canal Dash existant, pas concernées ici.
    # Scopé à /scalp EXACT (pas /scalpv1) : window._gexLwChart/
    # applyIndicators n'existent que là.
    app.clientside_callback(
        """
        function(symbol, path) {
            if (window._scIndStream) { window._scIndStream.close(); window._scIndStream = null; }
            const isScalpV2 = (path || '/') === '/scalp' || (path || '/').startsWith('/scalp/');
            if (!isScalpV2 || !['NQ', 'ES'].includes(symbol)) {
                return window.dash_clientside.no_update;
            }
            const es = new EventSource(`/api/v1/${symbol}/scalp-indicators-stream`);
            es.onmessage = function(ev) {
                const st = window._gexLwChart;
                if (!st) return;  // graphique pas encore créé : le prochain cycle Dash suffira
                let snap;
                try { snap = JSON.parse(ev.data); } catch (e) { return; }
                st.lastData = Object.assign({}, st.lastData, snap);
                st.applyIndicators();
            };
            window._scIndStream = es;
            return window.dash_clientside.no_update;
        }
        """,
        Output("scalp-stream-sink", "className", allow_duplicate=True),
        Input("symbol", "value"),
        Input("url", "pathname"),
        prevent_initial_call="initial_duplicate",
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
         Output("heat-levels-label", "children"), Output("heat-levels", "options"),
         Output("tape-hint", "children"), Output("lbl-tape-size", "children"),
         Output("tape-min-size", "options"), Output("tape-combos", "options")],
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
        # seuils de taille : Tout, puis des paliers qui isolent progressivement
        # les blocs. En contrats — la même unité que la colonne « taille ».
        tape_size_opts = [{"label": t(lang, "tape_size_all"), "value": 0},
                          {"label": "≥ 10", "value": 10},
                          {"label": "≥ 50", "value": 50},
                          {"label": "≥ 100", "value": 100}]
        tape_combos_opts = [{"label": t(lang, "tape_show_combos"), "value": "combos"}]
        return (bucket_opts, majors_opts, t(lang, "flow_day_label"),
                t(lang, "last_session"), t(lang, "footer_rt" if credentials_present() else "footer"), opts,
                t(lang, "app_title"),
                t(lang, "lbl_expiry"), t(lang, "lbl_window"), symbol,
                t(lang, "gflow_series_label"), gflow_series_opts,
                t(lang, "tape_series_label"), tape_series_opts,
                t(lang, "tape_note"),
                t(lang, "heat_levels_label"), heat_levels_opts,
                t(lang, "tape_hint"), t(lang, "tape_size_label"),
                tape_size_opts, tape_combos_opts)

    @app.callback(
        [Output("brand-sub", "children"), Output("rt-badge", "style"),
         Output("rt-badge", "className"), Output("rt-badge", "title"),
         Output("rt-label", "children"),
         Output("tt-connect", "style"), Output("tt-connect", "children"),
         Output("tt-connect", "title")],
        [Input("rt-tick", "n_intervals"), Input("lang", "value")],
    )
    def rt_status(_, lang):
        """Provenance du spot affiché, et pastille d'état du flux temps réel.

        Le badge reste masqué sur une installation sans identifiants courtier :
        inutile d'exposer un voyant rouge permanent pour une fonction que
        l'utilisateur n'a pas demandée.
        """
        # Bouton de connexion : proposé tant que le compte n'est pas
        # utilisable, caché dès qu'il l'est.
        etat_tt, detail_tt = connection_status()
        if etat_tt == "connecte":
            bouton = ({"display": "none"}, "", "")
        elif etat_tt == "deconnecte":
            bouton = ({}, t(lang, "tt_connect"), detail_tt)
        else:
            # identifiants d'application absents : rien à autoriser encore,
            # on affiche l'info sans lien cliquable trompeur
            bouton = ({"display": "none"}, "", detail_tt)

        state, detail = QUOTES.status(market_open=market_is_open())
        if state == "off":
            return (t(lang, "brand_sub"), {"display": "none"},
                    "rt-badge", "", "", *bouton)
        key = {"connected": "rt_connected", "degraded": "rt_degraded"}.get(
            state, "rt_disconnected")
        tip = t(lang, key) + (f" ({detail})" if detail else "")
        # le sous-titre ne promet le temps réel que si le flux le tient
        sub = t(lang, "brand_sub_rt" if state == "connected" else "brand_sub")
        return (sub, {}, f"rt-badge rt-{state}", tip, "dxFeed", *bouton)

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
        # Source UNIQUE des niveaux (cf. metrics.compute_levels) : murs classés au
        # spot structurel (clôture veille), côté au spot live, périmètre = bucket.
        _res = metrics.compute_levels(df, ref, side_spot, bucket=bucket)
        levels = _res["levels"]
        if majors and not levels.empty:
            # ne garde que les murs pesant au moins 25 % du plus fort
            levels = levels[levels["gex"].abs() >= 0.25 * levels["gex"].abs().max()]
        hvl = metrics.zero_gamma(df, snap.spot, weight_col="volume")
        keys = _res["keys"]
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
        State("heatmap", "relayoutData"),
    )
    def refresh_heatmap(_, tab, symbol, window, lang, unit, day, levels_shown, relayout):
        # onglet masqué : ne pas relire une quarantaine de fichiers pour rien
        if tab != "heat":
            raise PreventUpdate
        # Un zoom manuel n'est conservé que sur un simple rafraîchissement ou un
        # changement de niveaux/langue. Dès que le CONTEXTE change (symbole,
        # jour, échelle, fenêtre), le zoom d'avant n'a plus de sens — il portait
        # sur une autre plage de prix — donc on repart de la vue complète.
        reset = ctx.triggered_id in ("symbol", "window", "unit", "heat-day")
        xf, _, _ = _transform_for(symbol, unit)
        return (heatmap_fig(symbol, lang, day, window, xf, unit, levels_shown,
                            relayout=None if reset else relayout),
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
        Output("hedge-graph", "figure"),
        [Input("tape-tick", "n_intervals"), Input("tab", "value"),
         Input("symbol", "value"), Input("hedge-window", "value"),
         Input("lang", "value")],
    )
    def refresh_hedge(_, tab, symbol, window, lang):
        if tab != "tape":
            raise PreventUpdate
        return hedge_fig(symbol, lang, int(window or 0))    # -1 = live à la seconde

    @app.callback(
        [Output("scalp-banner", "children"),
         Output("scalp-head", "children"), Output("scalp-ladder", "children"),
         Output("scalp-hedge", "figure"), Output("scalp-prints", "children"),
         Output("scalp-price", "figure")],
        [Input("tape-tick", "n_intervals"), Input("url", "pathname"),
         Input("symbol", "value"), Input("lang", "value"),
         Input("scalp-window", "value"), Input("scalp-min", "value")],
    )
    def refresh_scalp(_, path, symbol, lang, window, min_size):
        if not is_scalp_path(path) or symbol not in ("NQ", "ES"):
            raise PreventUpdate
        hedge = hedge_fig(symbol, lang, int(window if window is not None else -1))
        hedge.update_layout(height=300, uirevision=f"scalp-{symbol}-{window}")
        prints = tape_table(symbol, lang, min_size=float(min_size or 0), include_combos=False)
        ctx = scalp_context(symbol)
        if ctx is None:
            wait = html.Div(t(lang, "waiting_native" if symbol in ("NQ", "ES")
                              else "waiting_first_pull"), className="hint")
            return wait, wait, wait, hedge, prints, empty_fig(t(lang, "sc_waiting_levels"), symbol)
        spot = _scalp_live_spot(symbol, ctx)
        price = scalp_price_fig(symbol, ctx, spot)
        price.update_layout(uirevision=f"scalp-price-{symbol}")
        absorb = scalp_absorption(symbol)
        # swing=True SEULEMENT sur /scalp exact — même garde que scalp-lw-card/
        # refresh_scalp_lw, /scalpv1 ne doit jamais recevoir swing=True.
        return (scalp_banner(symbol, ctx, spot, lang, absorb, swing=(path or "/") == "/scalp"),
                scalp_head(symbol, lang, ctx, spot),
                scalp_ladder(symbol, ctx, spot), hedge, prints, price)

    @app.callback(
        Output("scalp-lw-data", "data"),
        [Input("tape-tick", "n_intervals"), Input("url", "pathname"),
         Input("symbol", "value"), Input("scalp-chart-tf", "value")],
    )
    def refresh_scalp_lw(_, path, symbol, tf):
        """Alimente la carte Lightweight Charts — SEULEMENT sur `/scalp`
        EXACT (pas `/scalpv1`, qui garde `scalp-price`/Plotly inchangée) :
        `is_scalp_path` est volontairement PAS utilisé ici, il reconnaît les
        deux pages, ce callback doit reconnaître uniquement la nouvelle."""
        if (path or "/") != "/scalp" or symbol not in ("NQ", "ES"):
            raise PreventUpdate
        ctx = scalp_context(symbol)
        if ctx is None:
            raise PreventUpdate
        spot = _scalp_live_spot(symbol, ctx)
        return scalp_v2_chart_data(symbol, ctx, spot, tf=tf or CHART_TF_DEFAULT)

    @app.callback(
        Output("scalp-lw-hedge-data", "data"),
        [Input("tape-tick", "n_intervals"), Input("url", "pathname"),
         Input("symbol", "value"), Input("scalp-window", "value")],
    )
    def refresh_scalp_lw_hedge(_, path, symbol, window):
        """Même garde que `refresh_scalp_lw` — `/scalp` EXACT seulement,
        `/scalpv1` garde `scalp-hedge`/Plotly (via `hedge_fig` dans
        `refresh_scalp`) inchangée."""
        if (path or "/") != "/scalp" or symbol not in ("NQ", "ES"):
            raise PreventUpdate
        day = datetime.now(ET).strftime("%Y-%m-%d")
        return scalp_v2_hedge_data(symbol, int(window if window is not None else -1), day)

    @app.callback(
        Output("tape-table", "children"),
        [Input("tape-tick", "n_intervals"), Input("tab", "value"),
         Input("symbol", "value"), Input("tape-min-size", "value"),
         Input("tape-combos", "value"), Input("lang", "value")],
    )
    def refresh_tape(_, tab, symbol, min_size, combos, lang):
        # ne se recalcule que lorsque l'onglet est ouvert : inutile de
        # reconstruire 60 lignes toutes les 2 s en arrière-plan
        if tab != "tape":
            raise PreventUpdate
        return tape_table(symbol, lang, min_size=float(min_size or 0),
                          include_combos=bool(combos))

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
    register_oauth(app)

    @app.server.route("/api/v1/<symbol>/chart/<name>.png")
    def _chart_png(symbol, name):
        """Graphique en PNG à la demande — n'importe lequel, pas juste la
        heatmap (cf. chart_png / CHART_NAMES). Consommé par le bot Discord."""
        from flask import Response, request
        lang = request.args.get("lang", "fr")
        bucket = request.args.get("bucket", "Tout")
        window = request.args.get("window", type=float)   # ex. 0.02, sinon défaut
        scale = request.args.get("scale")                 # échelle d'affichage
        try:
            png = chart_png(symbol.upper(), name.lower(), lang, bucket, window, scale)
        except Exception:  # noqa: BLE001 — un rendu qui échoue ne doit pas 500 salement
            log.exception("Rendu PNG %s/%s", symbol, name)
            png = None
        if png is None:
            return Response(f"graphique indisponible : {name}", status=404)
        # Pas d'en-tête de cache avant ce correctif (2026-10-04) : un graphique
        # qui change en continu avec le marché ne doit JAMAIS être mis en
        # cache (navigateur ou intermédiaire) — sans quoi une requête
        # ultérieure identique (mêmes lang/bucket/window/scale) rafraîchirait
        # silencieusement une image périmée au lieu du graphique réel.
        return Response(png, mimetype="image/png",
                        headers={"Cache-Control": "no-store, max-age=0"})

    # Préférences /scalp par utilisateur (2026-10-04, piste 7 roadmap-scalp-v2)
    # — identité = l'en-tête Cloudflare Access (code à usage unique par
    # e-mail, confirmé en production le même jour). Sans cet en-tête (accès
    # local direct, hors Cloudflare), les deux routes répondent "pas
    # d'identité" sans erreur — le client retombe silencieusement sur le
    # localStorage seul, comportement inchangé pour qui n'est pas identifié.
    @app.server.route("/api/v1/prefs", methods=["GET"])
    def _prefs_get():
        from flask import jsonify, request
        email = request.headers.get("Cf-Access-Authenticated-User-Email")
        if not email:
            return jsonify({"email": None, "prefs": {}})
        conn = _journal()
        if conn is None:
            return jsonify({"email": email, "prefs": {}})
        with _JOURNAL_LOCK:
            import journal
            prefs = journal.get_user_prefs(conn, email)
        return jsonify({"email": email, "prefs": prefs})

    @app.server.route("/api/v1/prefs", methods=["POST"])
    def _prefs_set():
        from flask import jsonify, request
        email = request.headers.get("Cf-Access-Authenticated-User-Email")
        if not email:
            return jsonify({"ok": False, "reason": "no-identity"}), 200
        body = request.get_json(silent=True) or {}
        key, value = body.get("key"), body.get("value")
        if not key:
            return jsonify({"ok": False, "reason": "missing-key"}), 400
        conn = _journal()
        if conn is None:
            return jsonify({"ok": False, "reason": "journal-unavailable"}), 200
        try:
            with _JOURNAL_LOCK:
                import journal
                journal.set_user_pref(conn, email, key, value)
        except Exception:  # noqa: BLE001 — une préférence ratée ne doit jamais 500
            log.exception("Écriture préférence /scalp échouée (%s, %s)", email, key)
            return jsonify({"ok": False, "reason": "write-failed"}), 200
        return jsonify({"ok": True})

    # Flux SSE des indicateurs /scalp v2 (2026-10-04) — pousse
    # levels/confluence/order_flow/gex_profile/orderflow_profile dès que le
    # moteur planifié (_refresh_scalp_indicators) les a rafraîchis, plutôt
    # que chaque onglet les redemande en boucle (demande explicite de
    # l'utilisateur, cf. commentaire sur _refresh_scalp_indicators).
    # N'APPELLE JAMAIS les calculs directement — ne fait que relire les
    # fonctions, qui lisent elles-mêmes leur cache en premier (déjà chaud
    # grâce au moteur planifié) : ce flux ne recalcule donc rien, il ne fait
    # que repousser ce qui existe déjà vers le client, dès que ça change
    # (signature = les horodatages des 4 caches lourds, pas le contenu —
    # bien moins cher à comparer).
    @app.server.route("/api/v1/<symbol>/scalp-indicators-stream")
    def _scalp_indicators_stream(symbol):
        from flask import Response
        symbol = symbol.upper()
        if symbol not in SCALP_SCHED_SYMBOLS:
            return Response("symbole non couvert (NQ/ES seulement)", status=404)

        def gen():
            last_sig = None
            while True:
                try:
                    ctx = scalp_context(symbol)
                    if ctx is not None:
                        spot = _scalp_live_spot(symbol, ctx)
                        sig = (_CONFLUENCE_CACHE.get(symbol, (0.0,))[0],
                              _ORDER_FLOW_CACHE.get(symbol, (0.0,))[0],
                              _GEX_PROFILE_CACHE.get(symbol, (0.0,))[0],
                              _ORDERFLOW_PROFILE_CACHE.get(symbol, (0.0,))[0])
                        if sig != last_sig:
                            last_sig = sig
                            day_ticks = _scalp_day_ticks(symbol)
                            snap = _scalp_indicator_snapshot(symbol, ctx, spot, day_ticks)
                            yield f"data: {json.dumps(snap)}\n\n"
                except Exception:  # noqa: BLE001 — un cycle raté ne doit jamais fermer le flux
                    log.exception("Flux SSE indicateurs /scalp échoué (%s)", symbol)
                time.sleep(1.0)

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
