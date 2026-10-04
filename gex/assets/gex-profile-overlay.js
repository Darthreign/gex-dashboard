/* Profil de GEX par strike en overlay du graphique Lightweight Charts
 * (/scalp v2, 2026-10-03) — porte sur ce graphique la fonctionnalité
 * centrale de la page "heatmap" (gex/app.py::heatmap_fig), qui n'avait pas
 * encore d'équivalent ici (niveaux/confluence/order-flow en avaient déjà
 * un). Deux séries superposées par strike, en barres horizontales ancrées
 * au bord droit du pane (convention "volume profile") :
 *   - open interest (épaisse, en fond) : positionnement installé.
 *   - volume du jour (fine, devant) : ce qui se traite/se couvre MAINTENANT.
 * Longueur = magnitude, couleur = signe (vert net call-heavy, rouge
 * net put-heavy) — même lecture que les barres Plotly de la heatmap,
 * juste tournée en overlay plutôt qu'en graphique à part.
 *
 * API Lightweight Charts v4 (ISeriesPrimitive) — même pattern que le
 * moteur de dessin extrait d'OpenCharts (gex-drawing-tools.js) : une
 * primitive attachée à la série, qui dessine sur son propre calque canvas
 * sans jamais toucher aux données de la série elle-même.
 */
(function () {
  "use strict";

  function drawBar(ctx, paneW, yCss, hCss, value, maxAbs, maxBarCssW, alpha, vpr, hpr) {
    if (!value || !maxAbs) return;
    const wPx = Math.min(maxBarCssW, (Math.abs(value) / maxAbs) * maxBarCssW) * hpr;
    const yPx = yCss * vpr;
    const hPx = Math.max(1, hCss * vpr);
    const positive = value >= 0;
    ctx.fillStyle = positive ? `rgba(25, 158, 112, ${alpha})` : `rgba(230, 103, 103, ${alpha})`;
    ctx.fillRect(paneW - wPx, yPx - hPx / 2, wPx, hPx);
  }

  class GexProfileRenderer {
    constructor(entries) { this._entries = entries; }
    draw(target) {
      const entries = this._entries;
      if (!entries || !entries.length) return;
      target.useBitmapCoordinateSpace((scope) => {
        const ctx = scope.context;
        const paneW = scope.bitmapSize.width;
        const maxBarCssW = (scope.bitmapSize.width / scope.horizontalPixelRatio) * 0.30;
        const barHCss = 3;
        entries.forEach((e) => {
          // OI en fond (plus épaisse, plus transparente), volume devant.
          drawBar(ctx, paneW, e.y, barHCss * 2.2, e.oi, e.maxAbs, maxBarCssW, 0.45,
                  scope.verticalPixelRatio, scope.horizontalPixelRatio);
          drawBar(ctx, paneW, e.y, barHCss, e.vol, e.maxAbs, maxBarCssW, 0.85,
                  scope.verticalPixelRatio, scope.horizontalPixelRatio);
        });
      });
    }
  }

  class GexProfilePaneView {
    constructor(source) { this._source = source; this._entries = []; }
    update() {
      const chart = this._source._chart, series = this._source._series,
            data = this._source._data, visible = this._source._visible;
      if (!chart || !series || !visible || !data.length) { this._entries = []; return; }
      let maxAbs = 0;
      for (const d of data) {
        maxAbs = Math.max(maxAbs, Math.abs(d.oi || 0), Math.abs(d.vol || 0));
      }
      const entries = [];
      for (const d of data) {
        const y = series.priceToCoordinate(d.price);
        if (y === null) continue;
        entries.push({ y, oi: d.oi || 0, vol: d.vol || 0, maxAbs });
      }
      this._entries = entries;
    }
    renderer() { return new GexProfileRenderer(this._entries); }
  }

  class GexProfilePrimitive {
    constructor() {
      this._chart = null;
      this._series = null;
      this._requestUpdate = null;
      this._data = [];
      this._visible = true;
      this._paneViews = [new GexProfilePaneView(this)];
    }
    attached(param) {
      this._chart = param.chart;
      this._series = param.series;
      this._requestUpdate = param.requestUpdate;
      this.requestUpdate();
    }
    detached() { this._chart = null; this._series = null; this._requestUpdate = null; }
    requestUpdate() { if (this._requestUpdate) this._requestUpdate(); }
    setData(levels) { this._data = levels || []; this.requestUpdate(); }
    setVisible(v) { this._visible = !!v; this.requestUpdate(); }
    updateAllViews() { this._paneViews.forEach((v) => v.update()); }
    paneViews() { return this._paneViews; }
    priceAxisViews() { return []; }
  }

  window.GexProfilePrimitive = GexProfilePrimitive;
})();
