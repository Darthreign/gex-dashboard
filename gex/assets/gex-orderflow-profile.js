/* Profil de volume ancré sur les jambes de swing (/scalp v2, 2026-10-04) —
 * demande explicite de l'utilisateur : "on va d'abord voir comment faire un
 * chart order flow avec LightWeight chart (traçage auto des volume
 * profile/delta sur trace entre swing high/low)". Garde les DEUX dernières
 * jambes (en cours + dernière confirmée) — pas que celle qui se dessine,
 * sinon le contexte d'un retracement se perd (précision explicite).
 *
 * Même pattern ISeriesPrimitive que gex-profile-overlay.js (profil de GEX
 * par strike) et le moteur de dessin OpenCharts — mais ancré sur la PLAGE
 * TEMPORELLE de chaque jambe (x0/x1 convertis via timeToCoordinate) plutôt
 * que sur tout le pane. Barres = volume par palier (couleur = delta
 * acheteur/vendeur), POC = ligne pleine dorée, VAH/VAL = pointillés dorés —
 * convention standard du volume profile, pas une invention maison.
 */
(function () {
  "use strict";

  class OrderFlowProfileRenderer {
    constructor(entries) { this._entries = entries; }
    draw(target) {
      const entries = this._entries;
      if (!entries || !entries.length) return;
      target.useBitmapCoordinateSpace((scope) => {
        const ctx = scope.context;
        const hpr = scope.horizontalPixelRatio, vpr = scope.verticalPixelRatio;
        entries.forEach((leg) => {
          if (leg.x0 === null || leg.x1 === null) return;
          const x0 = Math.min(leg.x0, leg.x1) * hpr, x1 = Math.max(leg.x0, leg.x1) * hpr;
          const legWidthPx = Math.max(4 * hpr, x1 - x0);
          const barH = Math.max(1, 3 * vpr);
          const alpha = leg.current ? 0.50 : 0.30;
          for (const b of leg.buckets) {
            if (b.y === null || !leg.maxVol) continue;
            const wPx = Math.min(legWidthPx * 0.92, (b.vol / leg.maxVol) * legWidthPx * 0.92);
            const yPx = b.y * vpr;
            const delta = b.buy - b.sell;
            ctx.fillStyle = delta >= 0 ? `rgba(25, 158, 112, ${alpha})` : `rgba(230, 103, 103, ${alpha})`;
            ctx.fillRect(x0, yPx - barH / 2, wPx, barH);
          }
          if (leg.pocY !== null) {
            ctx.strokeStyle = "#f0b90b";
            ctx.lineWidth = Math.max(1, 1.5 * vpr);
            ctx.beginPath();
            ctx.moveTo(x0, leg.pocY * vpr);
            ctx.lineTo(x1, leg.pocY * vpr);
            ctx.stroke();
          }
          ctx.setLineDash([4 * hpr, 3 * hpr]);
          ctx.strokeStyle = "rgba(240, 185, 11, 0.55)";
          ctx.lineWidth = Math.max(1, vpr);
          for (const y of [leg.vahY, leg.valY]) {
            if (y === null) continue;
            ctx.beginPath();
            ctx.moveTo(x0, y * vpr);
            ctx.lineTo(x1, y * vpr);
            ctx.stroke();
          }
          ctx.setLineDash([]);
        });
      });
    }
  }

  class OrderFlowProfilePaneView {
    constructor(source) { this._source = source; this._entries = []; }
    update() {
      const chart = this._source._chart, series = this._source._series,
            legs = this._source._legs, visible = this._source._visible;
      if (!chart || !series || !visible || !legs.length) { this._entries = []; return; }
      const ts = chart.timeScale();
      this._entries = legs.map((leg) => {
        const maxVol = leg.buckets.reduce((m, b) => Math.max(m, b.vol), 0);
        return {
          x0: ts.timeToCoordinate(leg.t0),
          x1: ts.timeToCoordinate(leg.t1),
          current: !!leg.current,
          maxVol: maxVol,
          pocY: series.priceToCoordinate(leg.poc),
          vahY: series.priceToCoordinate(leg.vah),
          valY: series.priceToCoordinate(leg.val),
          buckets: leg.buckets.map((b) => ({ y: series.priceToCoordinate(b.price), vol: b.vol, buy: b.buy, sell: b.sell })),
        };
      });
    }
    renderer() { return new OrderFlowProfileRenderer(this._entries); }
  }

  class OrderFlowProfilePrimitive {
    constructor() {
      this._chart = null;
      this._series = null;
      this._requestUpdate = null;
      this._legs = [];
      this._visible = true;
      this._paneViews = [new OrderFlowProfilePaneView(this)];
    }
    attached(param) {
      this._chart = param.chart;
      this._series = param.series;
      this._requestUpdate = param.requestUpdate;
      this.requestUpdate();
    }
    detached() { this._chart = null; this._series = null; this._requestUpdate = null; }
    requestUpdate() { if (this._requestUpdate) this._requestUpdate(); }
    setData(legs) { this._legs = legs || []; this.requestUpdate(); }
    setVisible(v) { this._visible = !!v; this.requestUpdate(); }
    updateAllViews() { this._paneViews.forEach((v) => v.update()); }
    paneViews() { return this._paneViews; }
    priceAxisViews() { return []; }
  }

  window.OrderFlowProfilePrimitive = OrderFlowProfilePrimitive;
})();
