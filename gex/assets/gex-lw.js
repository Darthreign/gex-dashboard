/* Graphiques Lightweight Charts v5 du dashboard, alimentés en SSE.
 *
 * Le serveur pousse une DESCRIPTION de graphique (cf. gex/lwspec.py) :
 * titre, séries (Line / Histogram / Candlestick, panneau, échelle), lignes
 * horizontales, profil de gamma (heatmap), message « pas de données ».
 * Ce module crée le graphique UNE fois par conteneur puis ne fait que
 * mettre à jour les données : le zoom et le défilement de l'utilisateur sont
 * conservés d'une mise à jour à l'autre (recadrage seulement au premier
 * affichage ou quand le contexte — `key` — change).
 *
 *   GexLW.stream('flow', '/api/v1/lw/flow?symbol=SPX&lang=fr')  // ouvre / remplace
 *   GexLW.stream('flow', null)                                 // ferme
 */
(function () {
  "use strict";
  const charts = {};
  const streams = {};

  function lw() { return window.LightweightCharts; }

  // Même formatage horaire que le graphique /scalp : fuseau LOCAL du
  // navigateur (Lightweight Charts formate sinon en UTC).
  const timeOptions = {
    timeScale: {
      timeVisible: true, secondsVisible: false,
      tickMarkFormatter: (t, type) => {
        const d = new Date(t * 1000);
        return type <= 2
          ? d.toLocaleDateString([], { day: "2-digit", month: "2-digit" })
          : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      },
    },
    localization: {
      timeFormatter: (t) => {
        const d = new Date(t * 1000);
        return d.toLocaleDateString([], { day: "2-digit", month: "2-digit" }) + " "
          + d.toLocaleTimeString();
      },
    },
  };

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function create(container, spec) {
    container.innerHTML = "";
    container.classList.add("lw-card");
    const head = el("div", "lw-head");
    const title = el("div", "lw-title");
    const legend = el("div", "lw-legend");
    const tools = el("div", "lw-tools");
    head.append(title, legend, tools);
    const box = el("div", "lw-box");
    const msg = el("div", "lw-msg");
    container.append(head, box, msg);
    box.style.height = (spec.height || 300) + "px";
    const chart = lw().createChart(box, Object.assign({
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: "#c3c2b7",
                panes: { separatorColor: "#2c2c2a" } },
      grid: { vertLines: { color: "#232321" }, horzLines: { color: "#232321" } },
      rightPriceScale: { borderColor: "#383835" },
      leftPriceScale: { borderColor: "#383835", visible: false },
      crosshair: { mode: 0 },
    }, timeOptions));
    return { container, chart, title, legend, tools, box, msg, series: {}, lines: [],
             profile: null, profileSeries: null, key: null, fitted: false, buttons: false };
  }

  function seriesKey(s) { return [s.name, s.type, s.pane || 0, s.scale || "right"].join("|"); }

  function setRangeButtons(st) {
    if (st.buttons) return;
    st.buttons = true;
    [["1J", 1], ["1S", 7], ["1M", 31], ["Tout", 0]].forEach(([label, days]) => {
      const b = el("button", "lw-btn", label);
      b.addEventListener("click", () => {
        const ts = st.chart.timeScale();
        if (!days) { ts.fitContent(); return; }
        let last = 0;
        Object.values(st.series).forEach((s) => {
          const d = s.data(); if (d.length) last = Math.max(last, d[d.length - 1].time);
        });
        if (last) ts.setVisibleRange({ from: last - days * 86400, to: last });
      });
      st.tools.appendChild(b);
    });
  }

  function render(id, spec) {
    const container = document.getElementById(id);
    if (!container || !spec || !lw()) return;
    let st = charts[id];
    if (!st || st.container !== container || !container.contains(st.box)) {
      st = charts[id] = create(container, spec);
    }
    // en-tête : touché seulement s'il a changé (mises à jour à la seconde
    // sur la couverture des dealers — pas de reconstruction du DOM inutile)
    if (st.title.textContent !== (spec.title || "")) st.title.textContent = spec.title || "";
    const legendKey = JSON.stringify(spec.legend || []);
    if (st.legendKey !== legendKey) {
      st.legendKey = legendKey;
      st.legend.innerHTML = "";
      (spec.legend || []).forEach((l) => {
        const item = el("span", "lw-leg");
        const sw = el("i"); sw.style.background = l.color;
        item.append(sw, document.createTextNode(l.name));
        st.legend.appendChild(item);
      });
    }
    if (spec.rangeButtons) setRangeButtons(st);
    const height = (spec.height || 300) + "px";
    if (st.box.style.height !== height) st.box.style.height = height;

    if (spec.message) {
      st.msg.textContent = spec.message;
      st.msg.style.display = "flex";
      st.box.style.visibility = "hidden";
    } else {
      st.msg.style.display = "none";
      st.box.style.visibility = "visible";
    }

    // séries : créées une fois, données remplacées ensuite
    const wanted = {};
    const list = spec.series || [];
    // profil sans prix (jour sans bougies) : série invisible pour porter
    // l'échelle et la primitive
    if (spec.profile && spec.profile.length && !list.length && spec.priceRange && spec.range) {
      list.push({ name: "_", type: "Line", pane: 0, scale: "right",
                  options: { color: "rgba(0,0,0,0)", lastValueVisible: false, priceLineVisible: false },
                  data: [{ time: spec.range[0], value: spec.priceRange[0] },
                         { time: spec.range[1], value: spec.priceRange[1] }] });
    }
    list.forEach((s) => { wanted[seriesKey(s)] = s; });
    Object.keys(st.series).forEach((k) => {
      if (!wanted[k]) {
        if (st.profileSeries === st.series[k]) { st.profile = null; st.profileSeries = null; }
        st.chart.removeSeries(st.series[k]);
        delete st.series[k];
      }
    });
    let usesLeft = false;
    const ordered = [];
    list.forEach((s) => {
      const k = seriesKey(s);
      const scale = s.scale || "right";
      if (scale === "left") usesLeft = true;
      let series = st.series[k];
      if (!series) {
        series = st.chart.addSeries(lw()[s.type + "Series"],
                                    Object.assign({ priceScaleId: scale }, s.options || {}),
                                    s.pane || 0);
        st.series[k] = series;
      } else if (s.options) {
        series.applyOptions(s.options);
      }
      series.setData(s.data || []);
      ordered.push(series);
    });
    st.chart.applyOptions({ leftPriceScale: { visible: usesLeft } });

    // lignes horizontales (niveaux, zéro) : recréées seulement si elles ont
    // changé ou si leurs séries ont été recréées
    const linesKey = JSON.stringify(spec.lines || []) + "|" + Object.keys(st.series).join(",");
    if (st.linesKey !== linesKey) {
    st.linesKey = linesKey;
    st.lines.forEach(([series, line]) => { try { series.removePriceLine(line); } catch (e) {} });
    st.lines = [];
    (spec.lines || []).forEach((l) => {
      const series = ordered[l.series];
      if (!series) return;
      st.lines.push([series, series.createPriceLine({
        price: l.price, color: l.color, lineWidth: 1, lineStyle: l.style || 0,
        axisLabelVisible: !!l.axisLabel, title: l.title || "",
      })]);
    });
    }

    // profil de gamma par strike (heatmap) — même primitive que /scalp v2
    const host = ordered[0];
    if (spec.profile !== undefined && host && window.GexProfilePrimitive) {
      if (st.profileSeries !== host) {
        st.profile = new window.GexProfilePrimitive();
        host.attachPrimitive(st.profile);
        st.profileSeries = host;
      }
      st.profile.setData(spec.profile || []);
    }
    const rangeKey = JSON.stringify(spec.priceRange || null);
    if (host && spec.priceRange && (st.rangeHost !== host || st.rangeKey !== rangeKey)) {
      st.rangeHost = host; st.rangeKey = rangeKey;
      const [lo, hi] = spec.priceRange;
      host.applyOptions({
        autoscaleInfoProvider: (base) => {
          const r = base();
          const min = r && r.priceRange ? Math.min(r.priceRange.minValue, lo) : lo;
          const max = r && r.priceRange ? Math.max(r.priceRange.maxValue, hi) : hi;
          return { priceRange: { minValue: min, maxValue: max } };
        },
      });
    }

    // recadrage : premier affichage ou changement de contexte seulement
    const key = spec.key || id;
    if ((!st.fitted || st.key !== key) && ordered.length && !spec.message) {
      const ts = st.chart.timeScale();
      if (spec.range) {
        try { ts.setVisibleRange({ from: spec.range[0], to: spec.range[1] }); }
        catch (e) { ts.fitContent(); }
      } else {
        ts.fitContent();
      }
      st.fitted = true;
      st.key = key;
    }
  }

  function stream(id, url) {
    const cur = streams[id];
    if (cur && cur.url === url) return;
    if (cur) {
      cur.closed = true;
      if (cur.es) cur.es.close();
      if (cur.timer) clearTimeout(cur.timer);
      delete streams[id];
    }
    if (!url) return;
    const st = { url, es: null, timer: null, delay: 3000, closed: false };
    streams[id] = st;
    // le contexte (URL) change : recadrer au prochain message
    if (charts[id]) charts[id].fitted = false;
    const connect = () => {
      if (st.closed) return;
      const es = new EventSource(url);
      st.es = es;
      es.onmessage = (ev) => {
        st.delay = 3000;
        let spec;
        try { spec = JSON.parse(ev.data); } catch (e) { return; }
        render(id, spec);
      };
      es.onerror = () => {
        es.close();
        if (st.closed || streams[id] !== st) return;
        st.timer = setTimeout(connect, st.delay + Math.random() * 2000);
        st.delay = Math.min(st.delay * 2, 30000);
      };
    };
    connect();
  }

  window.GexLW = { render, stream };
})();
