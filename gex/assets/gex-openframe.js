/* Cadre d'amplitude de l'open US + mouvement attendu, graphique /scalp —
 * portage de l'indicateur TradingView « Cadre d'amplitude moyen – Open US »
 * de l'utilisateur (2026-10-18).
 *
 * Le serveur (/api/v1/<symbole>/open-frame, gex/openframe.py) donne les
 * ingrédients ; ici on construit les lignes selon les réglages du panneau ⚙
 * (window._gexSettings.of) comme le faisait le script Pine :
 *   cadre    : open du jour, haut / bas (centré : open ± moyenne / 2 ;
 *              extensions : open + montée moyenne / open − baisse moyenne),
 *              extensions ± 50 % / 100 % de l'amplitude, ajustement facultatif
 *              par vol implicite de la veille / sa moyenne ;
 *   mouvement attendu : point de départ ± amplitude × multiplicateur σ, deux
 *              versions — vol implicite (VXN / VIX) et straddle de l'échéance
 *              proche (comparaison demandée par l'utilisateur).
 * Chaque ligne est un segment limité à sa plage horaire (accroché aux
 * bougies affichées), pas une ligne de prix sur tout le graphique.
 *
 *   GexOpenFrame.update(st, d, settings, visible)   // depuis applyIndicators
 */
(function () {
  "use strict";
  const REFRESH_MS = 60000;
  const COL = { open: "#FFEB3B", high: "#00E676", low: "#FF5252", iv: "#FF9800",
                straddle: "#22d3ee", fill: "rgba(33, 150, 243, 0.10)" };
  const ANCHOR_TXT = { close: "clôture de la veille", globex: "ouverture Globex",
                       midnight: "minuit NY", open: "open 9h30" };
  const st0 = { data: null, key: null, at: 0, loading: false };

  // --- remplissage du cadre (primitive : rectangle entre deux temps) -------
  class BoxRenderer {
    constructor(b) { this._b = b; }
    draw(target) {
      const b = this._b;
      if (!b) return;
      target.useBitmapCoordinateSpace((scope) => {
        const ctx = scope.context, h = scope.horizontalPixelRatio, v = scope.verticalPixelRatio;
        const x0 = Math.min(b.x0, b.x1) * h, x1 = Math.max(b.x0, b.x1) * h;
        const y0 = Math.min(b.y0, b.y1) * v, y1 = Math.max(b.y0, b.y1) * v;
        ctx.fillStyle = COL.fill;
        ctx.fillRect(x0, y0, Math.max(1, x1 - x0), Math.max(1, y1 - y0));
      });
    }
  }
  class BoxView {
    constructor(src) { this._src = src; this._b = null; }
    update() {
      const s = this._src, box = s._box;
      if (!box || !s._chart || !s._series) { this._b = null; return; }
      const ts = s._chart.timeScale();
      const x0 = ts.timeToCoordinate(box.t0), x1 = ts.timeToCoordinate(box.t1);
      const y0 = s._series.priceToCoordinate(box.top), y1 = s._series.priceToCoordinate(box.bot);
      this._b = [x0, x1, y0, y1].some((x) => x === null) ? null : { x0, x1, y0, y1 };
    }
    renderer() { return new BoxRenderer(this._b); }
  }
  class FrameBox {
    constructor() { this._box = null; this._view = new BoxView(this); }
    attached(p) { this._chart = p.chart; this._series = p.series; this._req = p.requestUpdate; }
    detached() { this._chart = this._series = null; }
    set(box) { this._box = box; if (this._req) this._req(); }
    updateAllViews() { this._view.update(); }
    paneViews() { return [this._view]; }
  }

  // --- données ---------------------------------------------------------------
  function refresh(symbol, of) {
    const q = new URLSearchParams({ window: of.window, n: String(of.n), anchor: of.anchor });
    const key = symbol + "?" + q.toString();
    if (st0.loading || (st0.key === key && Date.now() - st0.at < REFRESH_MS)) return;
    st0.loading = true;
    fetch(`/api/v1/${symbol}/open-frame?` + q.toString())
      .then((r) => r.json())
      .then((data) => {
        st0.data = data; st0.key = key; st0.at = Date.now();
        const st = window._gexLwChart;
        if (st && st.applyIndicators) st.applyIndicators();
      })
      .catch(() => {})
      .finally(() => { st0.loading = false; });
  }

  // segment [t0, t1] accroché aux bougies affichées (et pas au-delà du présent)
  function span(times, t0, t1) {
    let a = null, b = null;
    for (const t of times) {
      if (t >= t0 && a === null) a = t;
      if (t <= t1 && t >= t0) b = t;
    }
    return a !== null && b !== null && b > a ? [a, b] : null;
  }

  function lineSeries(st, name, color, width, style) {
    st._ofSeries = st._ofSeries || {};
    let s = st._ofSeries[name];
    if (!s) {
      s = st.chart.addSeries(LightweightCharts.LineSeries, {
        color, lineWidth: width, lineStyle: style, lastValueVisible: false,
        priceLineVisible: false, crosshairMarkerVisible: false, title: "",
        // hors de l'autoscale : une extension lointaine ne doit pas dézoomer
        // le graphique (le bouton « recentrer » garde la main)
        autoscaleInfoProvider: () => null,
      });
      st._ofSeries[name] = s;
    }
    return s;
  }

  function fmt(v) { return v == null || isNaN(v) ? "—" : Math.round(v).toString(); }

  function update(st, d, settings, visible) {
    if (!st || !st.chart || !d) return;
    const of = Object.assign({ window: "0930-1030", n: 60, mode: "center", ext50: true,
                               ext100: true, extend: false, ivadj: false, base: true, fill: true,
                               anchor: "close", mult: 1, ivEm: true, straddleEm: true },
                             (settings || {}).of || {});
    const info = document.getElementById("scalp-em-info");
    if (visible && d.symbol) refresh(d.symbol, of);
    const data = st0.data && st0.data.symbol === d.symbol ? st0.data : null;
    const times = (d.candles || []).map((c) => c.time).sort((a, b) => a - b);
    const want = {};               // nom -> [t0, t1, prix, couleur, épaisseur, style]
    let txt = [];
    if (visible && data && data.frame) {
      const f = data.frame;
      const adj = of.ivadj && f.iv_prev && f.iv_avg ? f.iv_prev / f.iv_avg : 1;
      const rng = f.avg_range * adj;
      const top = of.mode === "ext" ? f.open + f.avg_up * adj : f.open + rng / 2;
      const bot = of.mode === "ext" ? f.open - f.avg_dn * adj : f.open - rng / 2;
      const t1 = of.extend ? f.rth_end : f.end;
      want.open = [f.start, t1, f.open, COL.open, 2, 0];
      want.top = [f.start, t1, top, COL.high, 2, 0];
      want.bot = [f.start, t1, bot, COL.low, 2, 0];
      if (of.ext50) { want.top50 = [f.start, t1, top + rng * 0.5, COL.high, 1, 2];
                      want.bot50 = [f.start, t1, bot - rng * 0.5, COL.low, 1, 2]; }
      if (of.ext100) { want.top100 = [f.start, t1, top + rng, COL.high, 1, 2];
                       want.bot100 = [f.start, t1, bot - rng, COL.low, 1, 2]; }
      st._ofBoxSpec = { t0: f.start, t1, top, bot };
      txt.push(`Cadre ${data.window.slice(0, 2)}h${data.window.slice(2, 4)}-`
               + `${data.window.slice(5, 7)}h${data.window.slice(7, 9)} : moy. ${fmt(f.avg_range)} pts`
               + ` (${f.count}/${data.n} séances${of.ivadj ? `, ×${adj.toFixed(2)} vol` : ""})`
               + ` · open ${fmt(f.open)} · haut ${fmt(top)} / bas ${fmt(bot)}`);
    } else {
      st._ofBoxSpec = null;
    }
    if (visible && data && data.em) {
      const e = data.em, m = of.mult || 1;
      if (of.base) want.emBase = [e.start, e.end, e.base, "rgba(255, 152, 0, 0.6)", 1, 1];
      if (of.ivEm && e.iv_em) {
        want.ivHi = [e.start, e.end, e.base + e.iv_em * m, COL.iv, 2, 1];
        want.ivLo = [e.start, e.end, e.base - e.iv_em * m, COL.iv, 2, 1];
      }
      if (of.straddleEm && e.straddle_em) {
        want.stHi = [e.start, e.end, e.base + e.straddle_em * m, COL.straddle, 2, 1];
        want.stLo = [e.start, e.end, e.base - e.straddle_em * m, COL.straddle, 2, 1];
      }
      txt.push(`Mouvement attendu ×${m}σ depuis ${fmt(e.base)} (${ANCHOR_TXT[e.anchor] || e.anchor}) :`
               + ` ${data.iv_symbol || "IV"} ${e.iv ? e.iv.toFixed(1) : "—"} → ±${fmt(e.iv_em && e.iv_em * m)}`
               + ` · straddle ±${fmt(e.straddle_em && e.straddle_em * m)}`);
    }
    // séries : créées à la demande, vidées quand inutiles
    Object.keys(want).forEach((k) => lineSeries(st, k, want[k][3], want[k][4], want[k][5]));
    Object.keys(st._ofSeries || {}).forEach((k) => {
      const s = st._ofSeries[k], w = want[k];
      const sp = w ? span(times, w[0], w[1]) : null;
      if (w) s.applyOptions({ color: w[3], lineWidth: w[4], lineStyle: w[5] });
      s.setData(sp ? [{ time: sp[0], value: w[2] }, { time: sp[1], value: w[2] }] : []);
    });
    if (!st._ofBox && st.series) { st._ofBox = new FrameBox(); st.series.attachPrimitive(st._ofBox); }
    if (st._ofBox) {
      const b = st._ofBoxSpec, sp = b ? span(times, b.t0, b.t1) : null;
      st._ofBox.set(sp && of.fill !== false ? { t0: sp[0], t1: sp[1], top: b.top, bot: b.bot } : null);
    }
    if (info) {
      info.textContent = visible ? txt.join("  ·  ") : "";
      info.style.display = visible && txt.length ? "" : "none";
    }
  }

  window.GexOpenFrame = { update, _state: st0 };
})();
