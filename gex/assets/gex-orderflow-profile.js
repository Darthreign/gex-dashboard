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
      // leg.t0/t1 sont des timestamps de TICK (pivots de swing), pas alignés
      // sur les bornes de bougie — or timeToCoordinate() de Lightweight
      // Charts exige un temps qui correspond exactement à une bougie
      // existante, sinon il renvoie null et toute la jambe disparaît
      // silencieusement (observé : la jambe en cours allait jusqu'à
      // "maintenant", au-delà même de la dernière bougie close). On
      // remplace chaque t0/t1 par le temps de la bougie la plus proche
      // (recherche dichotomique sur les bougies déjà chargées).
      const bars = series.data ? series.data() : [];
      const nearestBarTime = (t) => {
        if (!bars.length || t === null || t === undefined) return t;
        if (t <= bars[0].time) return bars[0].time;
        if (t >= bars[bars.length - 1].time) return bars[bars.length - 1].time;
        let lo = 0, hi = bars.length - 1;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          if (bars[mid].time < t) lo = mid + 1; else hi = mid;
        }
        const after = bars[lo].time;
        const before = lo > 0 ? bars[lo - 1].time : after;
        return (t - before <= after - t) ? before : after;
      };
      this._entries = legs.map((leg) => {
        const maxVol = leg.buckets.reduce((m, b) => Math.max(m, b.vol), 0);
        return {
          x0: ts.timeToCoordinate(nearestBarTime(leg.t0)),
          x1: ts.timeToCoordinate(nearestBarTime(leg.t1)),
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
  // est une ZONE de prix, pas un tick exact (demande explicite : "en
  // général c'est une zone pas un prix fixe"). Rendu en DÉGRADÉ plutôt
  // qu'un aplat uni (deuxième retour, façon bookmap) : "un LVN c'est un
  // creux de volume, le début du creux est clair, le fond est plus foncé,
  // et en remontant de l'autre côté ça redevient clair" — clair aux bords,
  // foncé exactement au prix du nœud, clair en s'en éloignant, qu'il
  // s'agisse d'un creux (LVN) ou d'un pic (HVN) : l'intensité représente
  // à quel point ce prix précis est significatif, pas juste du volume vs
  // pas de volume. Dégradé vertical pur CSS canvas (createLinearGradient),
  // pas besoin de renvoyer les paliers voisins depuis le serveur — le
  // dégradé s'étend sur plusieurs largeurs de bucket de part et d'autre du
  // centre, pas juste la largeur du palier lui-même (sinon imperceptible
  // à l'écran pour un bucket fin).
  const UNTESTED_SPREAD = 2.5; // en multiples de bucket_size, de chaque côté du centre

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
          if (z.yTop === null || z.yBottom === null || z.yCenter === null) continue;
          const yTop = Math.min(z.yTop, z.yBottom) * vpr;
          const yBottom = Math.max(z.yTop, z.yBottom) * vpr;
          const yCenter = z.yCenter * vpr;
          const isHvn = z.kind === "hvn";
          // Pic d'intensité au centre (le prix du nœud), transparent aux
          // deux bords — clair/foncé/clair, qu'on s'approche par le haut
          // ou par le bas du creux/pic.
          const peakAlpha = isHvn ? 0.42 : 0.26;
          const grad = ctx.createLinearGradient(0, yTop, 0, yBottom);
          grad.addColorStop(0, "rgba(156, 106, 222, 0)");
          grad.addColorStop(Math.min(1, Math.max(0, (yCenter - yTop) / (yBottom - yTop))),
                            `rgba(156, 106, 222, ${peakAlpha})`);
          grad.addColorStop(1, "rgba(156, 106, 222, 0)");
          ctx.fillStyle = grad;
          ctx.fillRect(0, yTop, paneW, Math.max(1, yBottom - yTop));
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
      const spread = (bucket || 0) * UNTESTED_SPREAD;
      this._zones = zones.map((z) => ({
        kind: z.kind,
        yTop: series.priceToCoordinate(z.price + spread),
        yBottom: series.priceToCoordinate(z.price - spread),
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
