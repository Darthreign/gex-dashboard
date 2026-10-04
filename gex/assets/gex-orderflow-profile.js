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

  // Zones HVN/LVN non testées (2026-10-04) — un palier de volume profile
  // est une ZONE de prix (sa largeur de bucket), pas un tick exact : demande
  // explicite de l'utilisateur ("en général c'est une zone pas un prix
  // fixe"), remplace les price lines fines utilisées au premier jet. Bande
  // semi-transparente sur toute la largeur du pane (pas la jambe) — ce sont
  // des niveaux de référence durables, pas liés à une plage temporelle
  // précise comme les jambes elles-mêmes.
  class UntestedZoneRenderer {
    constructor(zones) { this._zones = zones; }
    draw(target) {
      const zones = this._zones;
      if (!zones || !zones.length) return;
      target.useBitmapCoordinateSpace((scope) => {
        const ctx = scope.context;
        const paneW = scope.bitmapSize.width;
        const vpr = scope.verticalPixelRatio;
        for (const z of zones) {
          if (z.yTop === null || z.yBottom === null) continue;
          const yTop = Math.min(z.yTop, z.yBottom) * vpr;
          const yBottom = Math.max(z.yTop, z.yBottom) * vpr;
          const h = Math.max(1, yBottom - yTop);
          const isHvn = z.kind === "hvn";
          // HVN : violet plus dense (niveau "aimant", prix s'y arrête
          // souvent). LVN : plus clair/fin (prix traverse vite d'habitude).
          ctx.fillStyle = isHvn ? "rgba(156, 106, 222, 0.22)" : "rgba(156, 106, 222, 0.10)";
          ctx.fillRect(0, yTop, paneW, h);
          ctx.strokeStyle = isHvn ? "rgba(156, 106, 222, 0.75)" : "rgba(156, 106, 222, 0.45)";
          ctx.lineWidth = Math.max(1, (isHvn ? 1.2 : 1) * vpr);
          ctx.setLineDash(isHvn ? [] : [4 * vpr, 3 * vpr]);
          ctx.beginPath();
          ctx.moveTo(0, z.yCenter * vpr);
          ctx.lineTo(paneW, z.yCenter * vpr);
          ctx.stroke();
          ctx.setLineDash([]);
        }
      });
    }
  }

  class UntestedZonePaneView {
    constructor(source) { this._source = source; this._zones = []; }
    update() {
      const series = this._source._series, zones = this._source._untested,
            bucket = this._source._bucketSize, visible = this._source._visible;
      if (!series || !visible || !zones.length) { this._zones = []; return; }
      const half = (bucket || 0) / 2;
      this._zones = zones.map((z) => ({
        kind: z.kind,
        yTop: series.priceToCoordinate(z.price + half),
        yBottom: series.priceToCoordinate(z.price - half),
        yCenter: series.priceToCoordinate(z.price),
      }));
    }
    renderer() { return new UntestedZoneRenderer(this._zones); }
  }

  class UntestedZoneAxisView {
    constructor(source, zone) { this._source = source; this._zone = zone; this._y = null; }
    update() { this._y = this._source._series ? this._source._series.priceToCoordinate(this._zone.price) : null; }
    coordinate() { return this._y ?? -1; }
    visible() { return this._y !== null; }
    tickVisible() { return true; }
    text() { return (this._zone.kind === "hvn" ? "HVN" : "LVN") + " " + Math.round(this._zone.price); }
    textColor() { return "#ffffff"; }
    backColor() { return this._zone.kind === "hvn" ? "#9c6ade" : "#6b5a8e"; }
  }

  class OrderFlowProfilePrimitive {
    constructor() {
      this._chart = null;
      this._series = null;
      this._requestUpdate = null;
      this._legs = [];
      this._untested = [];
      this._bucketSize = 0;
      this._visible = true;
      this._legsView = new OrderFlowProfilePaneView(this);
      this._untestedView = new UntestedZonePaneView(this);
      this._paneViews = [this._legsView, this._untestedView];
      this._axisViews = [];
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
    setUntested(zones, bucketSize) {
      this._untested = zones || [];
      this._bucketSize = bucketSize || 0;
      this._axisViews = this._untested.map((z) => new UntestedZoneAxisView(this, z));
      this.requestUpdate();
    }
    setVisible(v) { this._visible = !!v; this.requestUpdate(); }
    updateAllViews() {
      this._paneViews.forEach((v) => v.update());
      this._axisViews.forEach((v) => v.update());
    }
    paneViews() { return this._paneViews; }
    priceAxisViews() { return this._visible ? this._axisViews : []; }
  }

  window.OrderFlowProfilePrimitive = OrderFlowProfilePrimitive;
})();
