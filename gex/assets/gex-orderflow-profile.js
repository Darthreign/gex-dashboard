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
 * que sur tout le pane. POC = ligne pleine dorée, VAH/VAL = pointillés
 * dorés — convention standard du volume profile, pas une invention maison.
 *
 * Rendu en profil SCINDÉ façon footprint (2026-10-04, demande explicite :
 * "Dessine le axée sur la jambe et le volume complet à droite et le delta
 * à gauche de l'axe") : un axe vertical fixe à x0 (l'ancrage de la jambe),
 * volume TOTAL par palier en barre neutre à DROITE de l'axe, delta
 * acheteur/vendeur (buy-sell, signé) en barre colorée à GAUCHE — au lieu
 * d'une unique barre à droite colorée par le signe du delta (ancien rendu,
 * mélangeait les deux informations). Chaque côté a sa propre échelle
 * (maxVol / maxDelta de la jambe), largeur de barre plafonnée à un budget
 * de pixels fixe indépendant de la durée de la jambe.
 */
(function () {
  "use strict";

  const PROFILE_MAX_BAR_PX = 70; // avant mise à l'échelle par horizontalPixelRatio

  class OrderFlowProfileRenderer {
    constructor(entries) { this._entries = entries; }
    draw(target) {
      const entries = this._entries;
      if (!entries || !entries.length) return;
      target.useBitmapCoordinateSpace((scope) => {
        const ctx = scope.context;
        const hpr = scope.horizontalPixelRatio, vpr = scope.verticalPixelRatio;
        const maxBarPx = PROFILE_MAX_BAR_PX * hpr;
        entries.forEach((leg) => {
          if (leg.x0 === null || leg.x1 === null) return;
          const axisX = leg.x0 * hpr, x1 = leg.x1 * hpr;
          const barH = Math.max(1, 3 * vpr);
          const alpha = leg.current ? 0.50 : 0.30;
          for (const b of leg.buckets) {
            if (b.y === null) continue;
            const yPx = b.y * vpr;
            // Volume total, à droite de l'axe, couleur neutre (le delta à
            // gauche porte déjà l'info acheteur/vendeur).
            const volPx = leg.maxVol ? Math.min(maxBarPx, (b.vol / leg.maxVol) * maxBarPx) : 0;
            ctx.fillStyle = `rgba(148, 163, 184, ${alpha})`;
            ctx.fillRect(axisX, yPx - barH / 2, volPx, barH);
            // Delta (achats - ventes), à gauche de l'axe, signé.
            const delta = b.buy - b.sell;
            const deltaPx = leg.maxDelta ? Math.min(maxBarPx, (Math.abs(delta) / leg.maxDelta) * maxBarPx) : 0;
            ctx.fillStyle = delta >= 0 ? `rgba(25, 158, 112, ${alpha})` : `rgba(230, 103, 103, ${alpha})`;
            ctx.fillRect(axisX - deltaPx, yPx - barH / 2, deltaPx, barH);
          }
          // Axe de référence (volume à droite / delta à gauche), borné aux
          // paliers réellement tracés (pas VAH/VAL, qui peuvent être null).
          const ys = leg.buckets.map((b) => b.y).filter((y) => y !== null);
          if (ys.length) {
            ctx.strokeStyle = `rgba(226, 232, 240, ${leg.current ? 0.35 : 0.2})`;
            ctx.lineWidth = Math.max(1, vpr);
            ctx.beginPath();
            ctx.moveTo(axisX, Math.min(...ys) * vpr - 4 * vpr);
            ctx.lineTo(axisX, Math.max(...ys) * vpr + 4 * vpr);
            ctx.stroke();
          }
          if (leg.pocY !== null) {
            ctx.strokeStyle = "#f0b90b";
            ctx.lineWidth = Math.max(1, 1.5 * vpr);
            ctx.beginPath();
            ctx.moveTo(axisX - maxBarPx, leg.pocY * vpr);
            ctx.lineTo(x1, leg.pocY * vpr);
            ctx.stroke();
          }
          ctx.setLineDash([4 * hpr, 3 * hpr]);
          ctx.strokeStyle = "rgba(240, 185, 11, 0.55)";
          ctx.lineWidth = Math.max(1, vpr);
          for (const y of [leg.vahY, leg.valY]) {
            if (y === null) continue;
            ctx.beginPath();
            ctx.moveTo(axisX - maxBarPx, y * vpr);
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
        const maxDelta = leg.buckets.reduce((m, b) => Math.max(m, Math.abs(b.buy - b.sell)), 0);
        return {
          x0: ts.timeToCoordinate(nearestBarTime(leg.t0)),
          x1: ts.timeToCoordinate(nearestBarTime(leg.t1)),
          current: !!leg.current,
          maxVol: maxVol,
          maxDelta: maxDelta,
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
          // HVN (violet, #9c6ade = 156,106,222) et LVN (bleu, #3987e5 =
          // 57,135,229 — même bleu que "Calls achetés" ailleurs dans le
          // dashboard, cf. hedge_fig) : deux teintes distinctes demandées
          // le 2026-10-05, jusque-là identiques (seule l'opacité différait).
          const rgb = isHvn ? "156, 106, 222" : "57, 135, 229";
          const grad = ctx.createLinearGradient(0, yTop, 0, yBottom);
          const frac = (y) => Math.min(1, Math.max(0, (y * vpr - yTop) / Math.max(1, yBottom - yTop)));
          grad.addColorStop(0, `rgba(${rgb}, 0)`);
          if (z.yHi !== null && z.yLo !== null && z.yHi !== undefined && z.yHi !== z.yLo) {
            const a = frac(Math.min(z.yHi, z.yLo)), b = frac(Math.max(z.yHi, z.yLo));
            grad.addColorStop(a, `rgba(${rgb}, ${peakAlpha})`);
            grad.addColorStop(b, `rgba(${rgb}, ${peakAlpha})`);
          } else {
            grad.addColorStop(Math.min(1, Math.max(0, (yCenter - yTop) / (yBottom - yTop))),
                              `rgba(${rgb}, ${peakAlpha})`);
          }
          grad.addColorStop(1, `rgba(${rgb}, 0)`);
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
            bucket = this._source._bucketSize, visible = this._source._untestedVisible;
      if (!series || !visible || !zones.length) { this._zones = []; return; }
      const spread = (bucket || 0) * UNTESTED_SPREAD;
      // zone fusionnée (low/high, cf. GexMergeUntested) : plateau d'intensité
      // de low à high, dégradé au-delà ; nœud seul : pic au centre
      this._zones = zones.map((z) => {
        const hi = z.high !== undefined ? z.high : z.price;
        const lo = z.low !== undefined ? z.low : z.price;
        return {
          kind: z.kind,
          yTop: series.priceToCoordinate(hi + spread),
          yBottom: series.priceToCoordinate(lo - spread),
          yCenter: series.priceToCoordinate(z.price),
          yHi: series.priceToCoordinate(hi),
          yLo: series.priceToCoordinate(lo),
        };
      });
    }
    renderer() { return new UntestedZoneRenderer(this._zones); }
  }

  class UntestedZoneAxisView {
    constructor(source, zone) { this._source = source; this._zone = zone; this._y = null; }
    update() { this._y = this._source._series ? this._source._series.priceToCoordinate(this._zone.price) : null; }
    coordinate() { return this._y ?? -1; }
    visible() { return this._y !== null; }
    tickVisible() { return true; }
    text() {
      const z = this._zone, tag = z.kind === "hvn" ? "HVN" : "LVN";
      return z.n > 1 ? `${tag} ${Math.round(z.low)}–${Math.round(z.high)} (${z.n})`
                     : `${tag} ${Math.round(z.price)}`;
    }
    textColor() { return "#ffffff"; }
    backColor() { return this._zone.kind === "hvn" ? "#9c6ade" : "#3987e5"; }
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
      this._untestedVisible = true;
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
    // Visibilité des zones HVN/LVN non testées, INDÉPENDANTE de setVisible()
    // ci-dessus (demande explicite : "Ajoute la désactivation des HVN/LVN
    // (différenciation du VP)" — pouvoir masquer les zones sans masquer le
    // profil de volume par jambe, et inversement).
    setUntestedVisible(v) { this._untestedVisible = !!v; this.requestUpdate(); }
    updateAllViews() {
      this._paneViews.forEach((v) => v.update());
      this._axisViews.forEach((v) => v.update());
    }
    paneViews() { return this._paneViews; }
    priceAxisViews() { return this._untestedVisible ? this._axisViews : []; }
  }

  window.OrderFlowProfilePrimitive = OrderFlowProfilePrimitive;

  // Fusion des nœuds HVN/LVN non testés en ZONES (2026-10-16, demande de
  // l'utilisateur) : deux nœuds de MÊME nature distants d'au plus `gap`
  // points sont réunis (de proche en proche) ; jamais un HVN avec un LVN.
  // `nodes` arrive du plus récent au plus ancien (cf. scalp_orderflow_untested) :
  // une zone prend le rang de son nœud le plus récent, et seules les `max`
  // zones les plus récentes sont gardées. gap = 0 : aucune fusion.
  window.GexMergeUntested = function (nodes, gap, max) {
    const ranked = (nodes || []).map((n, i) => Object.assign({ rank: i }, n));
    const out = [];
    ["hvn", "lvn"].forEach((kind) => {
      const list = ranked.filter((n) => n.kind === kind).sort((a, b) => a.price - b.price);
      let cur = null;
      list.forEach((n) => {
        if (cur && gap > 0 && n.price - cur.high <= gap + 1e-9) {
          cur.high = n.price; cur.n += 1; cur.rank = Math.min(cur.rank, n.rank);
        } else {
          cur = { kind, low: n.price, high: n.price, n: 1, rank: n.rank };
          out.push(cur);
        }
      });
    });
    out.forEach((z) => { z.price = (z.low + z.high) / 2; });
    return out.sort((a, b) => a.rank - b.rank).slice(0, max || out.length);
  };
})();
