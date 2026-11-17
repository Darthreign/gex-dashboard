"""Figures Plotly du dashboard -> description pour Lightweight Charts v5.

Les graphiques temporels du dashboard sont construits par des fonctions
Plotly éprouvées (titres, couleurs, sources, cas « pas de données ») qui
servent aussi aux PNG du bot Discord. Plutôt que de dupliquer cette logique,
on traduit la figure en une description simple que `gex/assets/gex-lw.js`
sait dessiner avec Lightweight Charts :

    {"title": "...", "message": None, "height": 300,
     "legend": [{"name", "color"}], "range": [t0, t1] | None,
     "rangeButtons": bool,
     "series": [{"name", "type": "Line|Histogram|Candlestick",
                 "pane": 0, "scale": "right|left",
                 "options": {...}, "data": [...]}],
     "lines": [{"series": 0, "price", "color", "title", "style"}]}

Les temps sont des secondes Unix (UTC) : le navigateur les affiche dans son
fuseau local, comme le graphique /scalp. Les x Plotly sont des heures locales
naïves (cf. app.to_local), reconverties ici avec le fuseau de la machine.
"""
from __future__ import annotations

import re
from datetime import datetime

import numpy as np
import pandas as pd

LOCAL_TZ = datetime.now().astimezone().tzinfo
_DASH = {"dash": 2, "dot": 1, "dashdot": 3, "longdash": 2, "solid": 0, None: 0}


def _epoch(x) -> np.ndarray:
    """x Plotly (heures locales naïves) -> secondes Unix, NaN si invalide."""
    ts = pd.to_datetime(pd.Series(list(x)), errors="coerce")
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(LOCAL_TZ, ambiguous="NaT", nonexistent="NaT")
    unit = {"s": 1.0, "ms": 1e3, "us": 1e6, "ns": 1e9}[ts.dt.unit]
    out = ts.astype("int64").to_numpy(dtype=float) / unit
    out[ts.isna().to_numpy()] = np.nan
    return out


def _clean_title(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", " — ", text)
    return re.sub(r"<[^>]+>", "", text).strip()


def _color(c, default="#c3c2b7"):
    if c is None or isinstance(c, (list, tuple, np.ndarray)):
        return default
    return str(c)


def _points(times: np.ndarray, values: np.ndarray, colors=None) -> list[dict]:
    """Points triés à la seconde, temps uniques (le dernier l'emporte : un
    cumul par print garde sa valeur de fin de seconde), sans NaN."""
    keep = np.isfinite(times) & np.isfinite(values)
    if not keep.any():
        return []
    t, v = np.floor(times[keep]), values[keep]      # LW : secondes entières
    c = None if colors is None else np.asarray(colors, dtype=object)[keep]
    order = np.argsort(t, kind="stable")
    t, v = t[order], v[order]
    c = None if c is None else c[order]
    last = np.r_[t[1:] != t[:-1], True]          # dernière occurrence de chaque temps
    out = []
    for i in np.flatnonzero(last):
        p = {"time": float(t[i]), "value": float(v[i])}
        if c is not None:
            p["color"] = str(c[i])
        out.append(p)
    return out


def _axis_layout(fig, yaxis: str) -> tuple[int, str]:
    """(panneau, échelle) d'un axe y Plotly : un axe superposé ('overlaying')
    va sur l'échelle de gauche du même panneau ; un axe à domaine séparé
    devient un panneau à part (cas des panneaux empilés de flow_fig)."""
    if yaxis in (None, "y"):
        return 0, "right"
    lay = fig.layout[yaxis.replace("y", "yaxis", 1)]
    if lay is not None and lay.overlaying:
        return 0, "left"
    return 1, "right"


def fig_to_spec(fig, height: int | None = None) -> dict:
    lay = fig.layout
    title = _clean_title(lay.title.text if lay.title else None)
    spec = {"title": title, "message": None,
            "height": int(height or lay.height or 300),
            "legend": [], "range": None, "rangeButtons": False,
            "series": [], "lines": []}
    xr = lay.xaxis.range if lay.xaxis else None
    if xr and len(xr) == 2:
        r = _epoch(xr)
        if np.isfinite(r).all():
            spec["range"] = [float(r[0]), float(r[1])]
    spec["rangeButtons"] = bool(lay.xaxis and lay.xaxis.rangeselector
                                and lay.xaxis.rangeselector.buttons)

    for tr in fig.data:
        pane, scale = _axis_layout(fig, getattr(tr, "yaxis", None))
        if tr.type == "candlestick":
            t = _epoch(tr.x)
            o, h, low, c = (np.asarray(v, dtype=float) for v in (tr.open, tr.high, tr.low, tr.close))
            keep = np.isfinite(t) & np.isfinite(c)
            order = np.argsort(t[keep], kind="stable")
            data = [{"time": float(a), "open": float(b), "high": float(d), "low": float(e),
                     "close": float(f)}
                    for a, b, d, e, f in zip(*(arr[keep][order] for arr in (t, o, h, low, c)))]
            up = _color(tr.increasing.line.color if tr.increasing else None, "#199e70")
            dn = _color(tr.decreasing.line.color if tr.decreasing else None, "#e66767")
            spec["series"].append({
                "name": tr.name or "", "type": "Candlestick", "pane": pane, "scale": scale,
                "options": {"upColor": up, "downColor": dn, "wickUpColor": up,
                            "wickDownColor": dn, "borderVisible": False},
                "data": data})
            continue
        if tr.x is None or tr.y is None:
            continue
        t = _epoch(tr.x)
        y = np.asarray(tr.y, dtype=float)
        if tr.type == "bar":
            mc = tr.marker.color if tr.marker else None
            colors = mc if isinstance(mc, (list, tuple, np.ndarray)) else None
            base = _color(mc, "#3987e5")
            spec["series"].append({
                "name": tr.name or "", "type": "Histogram", "pane": pane, "scale": scale,
                "options": {"color": base, "priceLineVisible": False},
                "data": _points(t, y, colors)})
            spec["legend"].append({"name": tr.name or "", "color": base})
        elif tr.type == "scatter":
            line = tr.line
            color = _color(line.color if line else None)
            opts = {"color": color, "lineWidth": max(1, int(round((line.width or 2) if line else 2))),
                    "lineStyle": _DASH.get(line.dash if line else None, 0),
                    "lineType": 1 if (line and line.shape in ("hv", "vh")) else 0,
                    "priceLineVisible": False, "lastValueVisible": True}
            spec["series"].append({"name": tr.name or "", "type": "Line", "pane": pane,
                                   "scale": scale, "options": opts, "data": _points(t, y)})
            spec["legend"].append({"name": tr.name or "", "color": color})

    # lignes horizontales (add_hline -> shape de type 'line' sur toute la largeur)
    # étiquettes par prix, dans l'ordre : deux lignes au même prix (Flip et
    # HVL confondus) gardent chacune la leur
    labels: dict[float, list[str]] = {}
    for a in (lay.annotations or ()):
        if a.y is not None and a.text:
            labels.setdefault(round(float(a.y), 6), []).append(_clean_title(a.text))
    for sh in (lay.shapes or ()):
        if sh.type != "line" or sh.y0 is None or sh.y0 != sh.y1:
            continue
        price = float(sh.y0)
        pane, _ = _axis_layout(fig, sh.yref or "y")
        target = next((i for i, s in enumerate(spec["series"]) if s["pane"] == pane), None)
        if target is None:
            continue
        color = _color(sh.line.color if sh.line else None, "#383835")
        pending = labels.get(round(price, 6)) or []
        title = pending.pop(0) if pending else ""
        spec["lines"].append({"series": target, "price": price, "color": color,
                              "title": title,
                              "style": _DASH.get(sh.line.dash if sh.line else None, 0),
                              "axisLabel": bool(title)})

    if not any(s["data"] for s in spec["series"]):
        msgs = [_clean_title(a.text) for a in (lay.annotations or ()) if a.text]
        spec["message"] = msgs[0] if msgs else "—"
        spec["series"] = []
        spec["lines"] = []
    if not lay.showlegend:
        spec["legend"] = [] if len(spec["legend"]) <= 1 else spec["legend"]
    return spec
