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
 * API Lightweight Charts v5 (ISeriesPrimitive, inchangée depuis la v4) — même pattern que le
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
    constructor(entries, barHCss, widthFrac) {
      this._entries = entries; this._barHCss = barHCss; this._widthFrac = widthFrac;
    }
    draw(target) {
      const entries = this._entries;
      if (!entries || !entries.length) return;
      target.useBitmapCoordinateSpace((scope) => {
        const ctx = scope.context;
        const paneW = scope.bitmapSize.width;
        const maxBarCssW = (scope.bitmapSize.width / scope.horizontalPixelRatio) * this._widthFrac;
        const barHCss = this._barHCss;
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
    constructor(source) { this._source = source; this._entries = []; this._barHCss = 3; }
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
        entries.push({ y, oi: d.oi || 0, vol: d.vol || 0, maxAbs, price: d.price });
      }
      this._entries = entries;
      // Hauteur de zone plafonnée à L'ESPACEMENT RÉEL ENTRE STRIKES, converti
      // en pixels AU ZOOM COURANT (2026-10-05, demande explicite : "si je
      // dézoome il se superpose"). Avant : hauteur fixe en pixels CSS,
      // indépendante du zoom — en dézoomant, l'écart entre strikes rétrécit
      // en pixels mais pas la barre, qui finit par chevaucher ses voisines.
      // Recalculé à chaque update() (donc à chaque pan/zoom) à partir de
      // l'écart MINIMUM observé entre deux strikes consécutifs des données
      // réelles plutôt qu'une valeur supposée (5pts) — robuste si la grille
      // de strikes change de pas selon le symbole.
      let minGap = Infinity;
      const prices = [...new Set(entries.map((e) => e.price))].sort((a, b) => a - b);
      for (let i = 1; i < prices.length; i++) {
        const g = prices[i] - prices[i - 1];
        if (g > 0 && g < minGap) minGap = g;
      }
      let gapPx = null;
      if (isFinite(minGap) && prices.length >= 2) {
        const y0 = series.priceToCoordinate(prices[0]);
        const y1 = series.priceToCoordinate(prices[0] + minGap);
        if (y0 !== null && y1 !== null) gapPx = Math.abs(y1 - y0);
      }
      // OI (la plus épaisse, *2.2 dans le renderer) est la contrainte réelle
      // — on plafonne SUR ELLE, pas sur barHCss nu, sinon OI dépasserait
      // quand même le strike malgré le plafond appliqué à vol.
      this._barHCss = gapPx ? Math.max(1, Math.min(3, gapPx / 2.2)) : 3;
    }
    renderer() {
      return new GexProfileRenderer(this._entries, this._barHCss, this._source._widthFrac);
    }
  }

  class GexProfilePrimitive {
    // `options.widthFrac` : longueur maximale des barres, en fraction de la
    // largeur du graphique (30 % par défaut, celle de /scalp v2 ; 65 % sur
    // la heatmap de la page principale, demande du 2026-10-09).
    constructor(options) {
      this._widthFrac = (options && options.widthFrac) || 0.30;
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
