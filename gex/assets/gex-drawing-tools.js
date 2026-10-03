/* Drawing-tools engine extracted from OpenCharts (MIT) - https://github.com/dylanpersonguy/OpenCharts - see tools/drawing-tools-src/LICENSE-opencharts */
var GexDrawingTools = (() => {
  var __defProp = Object.defineProperty;
  var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __hasOwnProp = Object.prototype.hasOwnProperty;
  var __defNormalProp = (obj, key, value) => key in obj ? __defProp(obj, key, { enumerable: true, configurable: true, writable: true, value }) : obj[key] = value;
  var __export = (target, all) => {
    for (var name in all)
      __defProp(target, name, { get: all[name], enumerable: true });
  };
  var __copyProps = (to, from, except, desc) => {
    if (from && typeof from === "object" || typeof from === "function") {
      for (let key of __getOwnPropNames(from))
        if (!__hasOwnProp.call(to, key) && key !== except)
          __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
    }
    return to;
  };
  var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);
  var __publicField = (obj, key, value) => __defNormalProp(obj, typeof key !== "symbol" ? key + "" : key, value);

  // entry.ts
  var entry_exports = {};
  __export(entry_exports, {
    DRAWING_COLORS: () => DRAWING_COLORS,
    DRAWING_WIDTHS: () => DRAWING_WIDTHS,
    DrawingToolsManager: () => DrawingToolsManager
  });

  // helpers/assertions.ts
  function ensureDefined(value) {
    if (value === void 0) {
      throw new Error("Value is undefined");
    }
    return value;
  }

  // plugin-base.ts
  var PluginBase = class {
    constructor() {
      __publicField(this, "_chart");
      __publicField(this, "_series");
      __publicField(this, "_requestUpdate");
      // This method is a class property to maintain the
      // lexical 'this' scope (due to the use of the arrow function)
      // and to ensure its reference stays the same, so we can unsubscribe later.
      __publicField(this, "_fireDataUpdated", (scope) => {
        if (this.dataUpdated) {
          this.dataUpdated(scope);
        }
      });
    }
    requestUpdate() {
      if (this._requestUpdate) this._requestUpdate();
    }
    attached({ chart, series, requestUpdate }) {
      this._chart = chart;
      this._series = series;
      this._series.subscribeDataChanged(this._fireDataUpdated);
      this._requestUpdate = requestUpdate;
      this.requestUpdate();
    }
    detached() {
      var _a;
      (_a = this._series) == null ? void 0 : _a.unsubscribeDataChanged(this._fireDataUpdated);
      this._chart = void 0;
      this._series = void 0;
      this._requestUpdate = void 0;
    }
    get chart() {
      return ensureDefined(this._chart);
    }
    get series() {
      return ensureDefined(this._series);
    }
  };

  // drawing-tools/renderers.ts
  var HANDLE_RADIUS = 5;
  var LINE_WIDTH = 2;
  var FIB_BAND_ALPHA = 0.08;
  var RECT_FILL_ALPHA = 0.14;
  function hexToRgba(hex, alpha) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
    if (!m) return hex;
    return `rgba(${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}, ${alpha})`;
  }
  function toBitmap(scope, x, y) {
    return {
      x: Math.round(x * scope.horizontalPixelRatio),
      y: Math.round(y * scope.verticalPixelRatio)
    };
  }
  function showHandles(e) {
    return e.state === "hovered" || e.state === "selected";
  }
  function dashFor(style) {
    if (style === "dashed") return [6, 6];
    if (style === "dotted") return [2, 3];
    return void 0;
  }
  function applyDash(scope, dash) {
    if (!dash) return;
    scope.context.setLineDash(dash.map((v) => v * scope.horizontalPixelRatio));
  }
  function strokeLine(scope, a, b, color, widthMedia, dash) {
    const ctx = scope.context;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = widthMedia * scope.verticalPixelRatio;
    applyDash(scope, dash);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    ctx.restore();
  }
  function drawHandle(scope, p, color) {
    const ctx = scope.context;
    ctx.save();
    ctx.beginPath();
    ctx.arc(p.x, p.y, HANDLE_RADIUS * scope.horizontalPixelRatio, 0, Math.PI * 2);
    ctx.fillStyle = "#ffffff";
    ctx.fill();
    ctx.lineWidth = 1.5 * scope.horizontalPixelRatio;
    ctx.strokeStyle = color;
    ctx.stroke();
    ctx.restore();
  }
  function renderEntry(scope, e, info) {
    switch (e.d.type) {
      case "trendline":
        renderTrendline(scope, e);
        break;
      case "horizontal":
        renderHorizontal(scope, e);
        break;
      case "rectangle":
        renderRectangle(scope, e);
        break;
      case "fibonacci":
        renderFibonacci(scope, e);
        break;
      case "position":
        renderPosition(scope, e, info);
        break;
      case "vertical":
        renderVertical(scope, e);
        break;
      case "channel":
        renderChannel(scope, e);
        break;
      case "ellipse":
        renderEllipse(scope, e);
        break;
      case "arrow":
        renderArrow(scope, e);
        break;
      case "triangle":
        renderTriangle(scope, e);
        break;
      case "text":
        renderText(scope, e);
        break;
      case "fibextension":
        renderFibonacci(scope, e);
        break;
      default:
        break;
    }
  }
  function renderVertical(scope, e) {
    var _a;
    if (e.x1 === null) return;
    const x = Math.round(e.x1 * scope.horizontalPixelRatio);
    strokeLine(
      scope,
      { x, y: 0 },
      { x, y: scope.bitmapSize.height },
      e.d.color,
      (_a = e.d.width) != null ? _a : 1.5,
      dashFor(e.d.lineStyle)
    );
    if (showHandles(e)) {
      drawHandle(scope, { x, y: Math.round(scope.bitmapSize.height / 2) }, e.d.color);
    }
  }
  function renderChannel(scope, e) {
    var _a, _b, _c, _d;
    if (e.x1 === null || e.y1 === null || e.x2 === null || e.y2 === null) return;
    if (e.y3 == null) return;
    const dy = e.y3 - e.y1;
    const a1 = toBitmap(scope, e.x1, e.y1);
    const b1 = toBitmap(scope, e.x2, e.y2);
    const a2 = toBitmap(scope, e.x1, e.y3);
    const b2 = toBitmap(scope, e.x2, e.y2 + dy);
    const ctx = scope.context;
    ctx.save();
    ctx.fillStyle = hexToRgba((_a = e.d.fillColor) != null ? _a : e.d.color, (_b = e.d.fillOpacity) != null ? _b : 0.1);
    ctx.beginPath();
    ctx.moveTo(a1.x, a1.y);
    ctx.lineTo(b1.x, b1.y);
    ctx.lineTo(b2.x, b2.y);
    ctx.lineTo(a2.x, a2.y);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
    const dash = dashFor(e.d.lineStyle);
    strokeLine(scope, a1, b1, e.d.color, (_c = e.d.width) != null ? _c : LINE_WIDTH, dash);
    strokeLine(scope, a2, b2, e.d.color, (_d = e.d.width) != null ? _d : LINE_WIDTH, dash);
    if (showHandles(e)) {
      drawHandle(scope, a1, e.d.color);
      drawHandle(scope, b1, e.d.color);
      drawHandle(scope, a2, e.d.color);
    }
  }
  function renderEllipse(scope, e) {
    var _a, _b, _c;
    if (e.x1 === null || e.y1 === null || e.x2 === null || e.y2 === null) return;
    const a = toBitmap(scope, e.x1, e.y1);
    const b = toBitmap(scope, e.x2, e.y2);
    const ctx = scope.context;
    const cx = (a.x + b.x) / 2;
    const cy = (a.y + b.y) / 2;
    ctx.save();
    ctx.beginPath();
    ctx.ellipse(cx, cy, Math.abs(b.x - a.x) / 2, Math.abs(b.y - a.y) / 2, 0, 0, Math.PI * 2);
    ctx.fillStyle = hexToRgba((_a = e.d.fillColor) != null ? _a : e.d.color, (_b = e.d.fillOpacity) != null ? _b : RECT_FILL_ALPHA);
    ctx.fill();
    ctx.strokeStyle = e.d.color;
    ctx.lineWidth = ((_c = e.d.width) != null ? _c : 1.5) * scope.verticalPixelRatio;
    applyDash(scope, dashFor(e.d.lineStyle));
    ctx.stroke();
    ctx.restore();
    if (showHandles(e)) drawRectHandles(scope, a, b, e.d.color);
  }
  function renderArrow(scope, e) {
    var _a;
    if (e.x1 === null || e.y1 === null || e.x2 === null || e.y2 === null) return;
    const a = toBitmap(scope, e.x1, e.y1);
    const b = toBitmap(scope, e.x2, e.y2);
    strokeLine(scope, a, b, e.d.color, (_a = e.d.width) != null ? _a : LINE_WIDTH, dashFor(e.d.lineStyle));
    drawArrowhead(scope, a, b, e.d.color);
    if (showHandles(e)) {
      drawHandle(scope, a, e.d.color);
      drawHandle(scope, b, e.d.color);
    }
  }
  function drawArrowhead(scope, from, to, color) {
    const ctx = scope.context;
    const ang = Math.atan2(to.y - from.y, to.x - from.x);
    const len = 12 * scope.horizontalPixelRatio;
    const spread = Math.PI / 7;
    ctx.save();
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(to.x, to.y);
    ctx.lineTo(to.x - len * Math.cos(ang - spread), to.y - len * Math.sin(ang - spread));
    ctx.lineTo(to.x - len * Math.cos(ang + spread), to.y - len * Math.sin(ang + spread));
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }
  function renderTriangle(scope, e) {
    var _a, _b, _c;
    if (e.x1 === null || e.y1 === null || e.x2 === null || e.y2 === null) return;
    const a = toBitmap(scope, e.x1, e.y1);
    const b = toBitmap(scope, e.x2, e.y2);
    const ctx = scope.context;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo((a.x + b.x) / 2, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.lineTo(a.x, b.y);
    ctx.closePath();
    ctx.fillStyle = hexToRgba((_a = e.d.fillColor) != null ? _a : e.d.color, (_b = e.d.fillOpacity) != null ? _b : RECT_FILL_ALPHA);
    ctx.fill();
    ctx.strokeStyle = e.d.color;
    ctx.lineWidth = ((_c = e.d.width) != null ? _c : 1.5) * scope.verticalPixelRatio;
    applyDash(scope, dashFor(e.d.lineStyle));
    ctx.stroke();
    ctx.restore();
    if (showHandles(e)) {
      drawHandle(scope, a, e.d.color);
      drawHandle(scope, b, e.d.color);
    }
  }
  function textFont(d, sizePx) {
    const style = d.italic ? "italic " : "";
    const weight = d.bold ? "bold " : "";
    return `${style}${weight}${Math.round(sizePx)}px sans-serif`;
  }
  function renderText(scope, e) {
    var _a, _b, _c, _d;
    if (e.x1 === null || e.y1 === null) return;
    const ctx = scope.context;
    const hpr = scope.horizontalPixelRatio;
    const size = ((_a = e.d.fontSize) != null ? _a : 14) * scope.verticalPixelRatio;
    const lines = ((_b = e.d.text) != null ? _b : "Text").split("\n");
    const at = toBitmap(scope, e.x1, e.y1);
    const lineH = size * 1.3;
    const pad = 4 * hpr;
    ctx.save();
    ctx.font = textFont(e.d, size);
    ctx.textBaseline = "top";
    const textW = Math.max(...lines.map((l) => ctx.measureText(l).width));
    const boxW = textW + pad * 2;
    const boxH = lines.length * lineH + pad * 2;
    const bx = at.x - pad;
    const by = at.y - pad;
    if (e.d.textBg) {
      ctx.fillStyle = (_c = e.d.textBgColor) != null ? _c : "#1e222d";
      ctx.fillRect(bx, by, boxW, boxH);
    }
    if (e.d.textBorder) {
      ctx.strokeStyle = (_d = e.d.textBorderColor) != null ? _d : e.d.color;
      ctx.lineWidth = hpr;
      ctx.strokeRect(bx, by, boxW, boxH);
    }
    ctx.fillStyle = e.d.color;
    lines.forEach((l, i) => ctx.fillText(l, at.x, at.y + i * lineH));
    if (showHandles(e)) {
      ctx.strokeStyle = e.d.color;
      ctx.lineWidth = hpr;
      ctx.setLineDash([3 * hpr, 3 * hpr]);
      ctx.strokeRect(bx, by, boxW, boxH);
    }
    ctx.restore();
  }
  var POS_GREEN = "#089981";
  var POS_RED = "#f23645";
  function renderPosition(scope, e, info) {
    const { x1, x2, y1, yStop, yTarget } = e;
    if (x1 === null || x2 === null || y1 === null || yStop == null || yTarget == null) return;
    const xa = Math.min(x1, x2);
    const xb = Math.max(x1, x2);
    posZone(scope, xa, xb, y1, yTarget, POS_GREEN);
    posZone(scope, xa, xb, y1, yStop, POS_RED);
    strokeLine(scope, toBitmap(scope, xa, y1), toBitmap(scope, xb, y1), "#d1d4dc", 1.5, [5, 3]);
    if (showHandles(e)) renderPositionHandles(scope, e, xa, xb, y1);
    const lines = positionReadout(e.d, info);
    if (lines.length > 0) {
      const at = toBitmap(scope, xb + 8, y1);
      drawLabelBox(scope, at.x, at.y, lines, e.d.color);
    }
  }
  function posZone(scope, xa, xb, yFrom, yTo, color) {
    const ctx = scope.context;
    const a = toBitmap(scope, xa, Math.min(yFrom, yTo));
    const b = toBitmap(scope, xb, Math.max(yFrom, yTo));
    ctx.save();
    ctx.fillStyle = hexToRgba(color, 0.12);
    ctx.fillRect(a.x, a.y, b.x - a.x, b.y - a.y);
    ctx.strokeStyle = hexToRgba(color, 0.6);
    ctx.lineWidth = scope.verticalPixelRatio;
    ctx.strokeRect(a.x, a.y, b.x - a.x, b.y - a.y);
    ctx.restore();
  }
  function renderPositionHandles(scope, e, xa, xb, y1) {
    const xm = (xa + xb) / 2;
    drawHandle(scope, toBitmap(scope, xm, y1), e.d.color);
    if (e.yTarget != null) drawHandle(scope, toBitmap(scope, xm, e.yTarget), e.d.color);
    if (e.yStop != null) drawHandle(scope, toBitmap(scope, xm, e.yStop), e.d.color);
    drawHandle(scope, toBitmap(scope, xb, y1), e.d.color);
  }
  function positionReadout(d, info) {
    var _a;
    if (d.stopPrice == null || d.targetPrice == null) return [];
    const reward = Math.abs(d.targetPrice - d.price);
    const risk = Math.abs(d.price - d.stopPrice);
    const rr = risk > 0 ? reward / risk : 0;
    const side = d.side === "short" ? "Short" : "Long";
    const lines = [
      `${side}  RR ${rr.toFixed(2)}`,
      `T ${info.priceFormat(d.targetPrice)}  ${signPct(d.targetPrice, d.price)}`,
      `S ${info.priceFormat(d.stopPrice)}  ${signPct(d.stopPrice, d.price)}`
    ];
    if (info.accountEquity > 0 && risk > 0) {
      const riskAmt = info.accountEquity * ((_a = d.riskPct) != null ? _a : 1) / 100;
      const qty = riskAmt / risk;
      lines.push(`Risk $${riskAmt.toFixed(0)} \xB7 Qty ${qty >= 100 ? qty.toFixed(0) : qty.toFixed(2)}`);
    }
    return lines;
  }
  function signPct(v, entry) {
    if (entry === 0) return "0%";
    const p = (v - entry) / entry * 100;
    return `${p >= 0 ? "+" : ""}${p.toFixed(2)}%`;
  }
  function drawLabelBox(scope, x, y, lines, borderColor) {
    const ctx = scope.context;
    const hpr = scope.horizontalPixelRatio;
    const vpr = scope.verticalPixelRatio;
    ctx.save();
    ctx.font = `${Math.round(10 * vpr)}px monospace`;
    const lineH = 13 * vpr;
    const pad = 6 * hpr;
    const width = Math.max(...lines.map((l) => ctx.measureText(l).width)) + pad * 2;
    const height = lines.length * lineH + pad;
    const bx = Math.min(x, scope.bitmapSize.width - width);
    const by = Math.min(Math.max(y, 0), scope.bitmapSize.height - height);
    ctx.fillStyle = "rgba(20, 24, 35, 0.92)";
    ctx.beginPath();
    ctx.roundRect(bx, by, width, height, 4 * hpr);
    ctx.fill();
    ctx.strokeStyle = borderColor;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = "#e0e3ea";
    ctx.textBaseline = "top";
    lines.forEach((l, i) => ctx.fillText(l, bx + pad, by + pad / 2 + i * lineH));
    ctx.restore();
  }
  function renderTrendline(scope, e) {
    var _a, _b;
    if (e.x1 === null || e.y1 === null || e.x2 === null || e.y2 === null) return;
    const a = toBitmap(scope, e.x1, e.y1);
    const b = toBitmap(scope, e.x2, e.y2);
    const seg = (_a = e.seg) != null ? _a : { x1: e.x1, y1: e.y1, x2: e.x2, y2: e.y2 };
    const sa = toBitmap(scope, seg.x1, seg.y1);
    const sb = toBitmap(scope, seg.x2, seg.y2);
    strokeLine(scope, sa, sb, e.d.color, (_b = e.d.width) != null ? _b : LINE_WIDTH, dashFor(e.d.lineStyle));
    if (e.d.arrowEnd) drawArrowhead(scope, sa, sb, e.d.color);
    if (e.d.arrowStart) drawArrowhead(scope, sb, sa, e.d.color);
    if (showHandles(e)) {
      drawHandle(scope, a, e.d.color);
      drawHandle(scope, b, e.d.color);
      drawMidpointHandle(scope, a, b, e.d.color);
    }
    if (e.d.alertEnabled) {
      const right = a.x >= b.x ? a : b;
      drawAlertBadge(scope, right.x, right.y);
    }
    if (e.stats && e.stats.length > 0) drawStatsBox(scope, b, e.stats, e.d, a);
  }
  function drawMidpointHandle(scope, a, b, color) {
    const ctx = scope.context;
    const half = 3 * scope.horizontalPixelRatio;
    const mx = (a.x + b.x) / 2;
    const my = (a.y + b.y) / 2;
    ctx.save();
    ctx.fillStyle = "#ffffff";
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5 * scope.horizontalPixelRatio;
    ctx.beginPath();
    ctx.rect(mx - half, my - half, half * 2, half * 2);
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }
  function drawStatsBox(scope, at, stats, d, from) {
    const ctx = scope.context;
    const hpr = scope.horizontalPixelRatio;
    const vpr = scope.verticalPixelRatio;
    const angleDeg = Math.round(Math.atan2(from.y - at.y, at.x - from.x) * 180 / Math.PI);
    const lines = [...stats, `${angleDeg}\xB0`];
    ctx.save();
    ctx.font = `${Math.round(10 * vpr)}px monospace`;
    const lineH = 13 * vpr;
    const pad = 6 * hpr;
    const width = Math.max(...lines.map((l) => ctx.measureText(l).width)) + pad * 2;
    const height = lines.length * lineH + pad;
    const x = Math.min(at.x + 10 * hpr, scope.bitmapSize.width - width);
    const y = Math.min(at.y + 10 * vpr, scope.bitmapSize.height - height);
    ctx.fillStyle = "rgba(20, 24, 35, 0.92)";
    ctx.beginPath();
    ctx.roundRect(x, y, width, height, 4 * hpr);
    ctx.fill();
    ctx.strokeStyle = d.color;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.fillStyle = "#e0e3ea";
    ctx.textBaseline = "top";
    lines.forEach((line, i) => {
      ctx.fillText(line, x + pad, y + pad / 2 + i * lineH);
    });
    ctx.restore();
  }
  function renderHorizontal(scope, e) {
    var _a;
    if (e.y1 === null) return;
    const y = Math.round(e.y1 * scope.verticalPixelRatio);
    strokeLine(
      scope,
      { x: 0, y },
      { x: scope.bitmapSize.width, y },
      e.d.color,
      (_a = e.d.width) != null ? _a : 1.5,
      dashFor(e.d.lineStyle)
    );
    if (showHandles(e))
      drawHandle(scope, { x: Math.round(scope.bitmapSize.width / 2), y }, e.d.color);
    if (e.d.alertEnabled) {
      drawAlertBadge(scope, scope.bitmapSize.width - 14 * scope.horizontalPixelRatio, y);
    }
  }
  function drawAlertBadge(scope, x, y) {
    const ctx = scope.context;
    const r = 4 * scope.horizontalPixelRatio;
    ctx.save();
    ctx.fillStyle = "#f0b90b";
    ctx.strokeStyle = "#1b1f2a";
    ctx.lineWidth = scope.horizontalPixelRatio;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }
  function renderRectangle(scope, e) {
    var _a;
    if (e.x1 === null || e.y1 === null || e.x2 === null || e.y2 === null) return;
    const a = toBitmap(scope, e.x1, e.y1);
    const b = toBitmap(scope, e.x2, e.y2);
    const ctx = scope.context;
    ctx.save();
    ctx.fillStyle = hexToRgba(e.d.color, RECT_FILL_ALPHA);
    ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
    ctx.strokeStyle = e.d.color;
    ctx.lineWidth = ((_a = e.d.width) != null ? _a : 1.5) * scope.verticalPixelRatio;
    applyDash(scope, dashFor(e.d.lineStyle));
    ctx.strokeRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
    ctx.restore();
    if (showHandles(e)) drawRectHandles(scope, a, b, e.d.color);
  }
  function drawRectHandles(scope, a, b, color) {
    drawHandle(scope, a, color);
    drawHandle(scope, b, color);
    drawHandle(scope, { x: a.x, y: b.y }, color);
    drawHandle(scope, { x: b.x, y: a.y }, color);
  }
  function renderFibonacci(scope, e) {
    if (e.x1 === null || e.x2 === null || !e.fibLevels || e.fibLevels.length === 0) return;
    const xA = Math.round(Math.min(e.x1, e.x2) * scope.horizontalPixelRatio);
    const xB = Math.round(Math.max(e.x1, e.x2) * scope.horizontalPixelRatio);
    renderFibBands(scope, e.fibLevels, xA, xB);
    for (const lvl of e.fibLevels) renderFibLevel(scope, lvl, xA, xB);
    renderFibConnector(scope, e);
  }
  function renderFibBands(scope, levels, xA, xB) {
    const ctx = scope.context;
    for (let i = 0; i < levels.length - 1; i++) {
      const top = levels[i];
      const bottom = levels[i + 1];
      if (top.y === null || bottom.y === null) continue;
      const yA = Math.round(top.y * scope.verticalPixelRatio);
      const yB = Math.round(bottom.y * scope.verticalPixelRatio);
      ctx.fillStyle = hexToRgba(bottom.color, FIB_BAND_ALPHA);
      ctx.fillRect(xA, Math.min(yA, yB), xB - xA, Math.abs(yB - yA));
    }
  }
  function renderFibLevel(scope, lvl, xA, xB) {
    if (lvl.y === null) return;
    const y = Math.round(lvl.y * scope.verticalPixelRatio);
    strokeLine(scope, { x: xA, y }, { x: xB, y }, lvl.color, 1);
    const ctx = scope.context;
    ctx.save();
    ctx.font = `${Math.round(10 * scope.verticalPixelRatio)}px monospace`;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillStyle = lvl.color;
    ctx.fillText(lvl.label, xA - 6 * scope.horizontalPixelRatio, y);
    ctx.restore();
  }
  function renderFibConnector(scope, e) {
    if (e.x1 === null || e.y1 === null || e.x2 === null || e.y2 === null) return;
    const a = toBitmap(scope, e.x1, e.y1);
    const b = toBitmap(scope, e.x2, e.y2);
    strokeLine(scope, a, b, e.d.color, 1, [4, 4]);
    if (showHandles(e)) {
      drawHandle(scope, a, e.d.color);
      drawHandle(scope, b, e.d.color);
    }
  }

  // drawing-tools/resolve.ts
  var FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  var FIB_EXT_LEVELS = [0, 0.618, 1, 1.618, 2.618];
  var FIB_COLORS = [
    "#e91e63",
    "#ff5722",
    "#ff9800",
    "#ffc107",
    "#4caf50",
    "#2196F3",
    "#9c27b0"
  ];
  function makeResolveCtx(chart, series, intervalSec) {
    return { chart, series, intervalSec, data: series.data() };
  }
  function lowerBound(data, time) {
    let lo = 0;
    let hi = data.length;
    while (lo < hi) {
      const mid = lo + hi >> 1;
      if (data[mid].time < time) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }
  function timeToX(ctx, time) {
    const ts = ctx.chart.timeScale();
    const direct = ts.timeToCoordinate(time);
    if (direct !== null) return direct;
    const logical = timeToLogical(ctx, time);
    if (logical === null) return null;
    return logicalToX(ts, logical);
  }
  function logicalToX(ts, logical) {
    const lo = Math.floor(logical);
    const xLo = ts.logicalToCoordinate(lo);
    if (xLo === null) return null;
    const frac = logical - lo;
    if (frac === 0) return xLo;
    const xHi = ts.logicalToCoordinate(lo + 1);
    if (xHi === null) return null;
    return xLo + frac * (xHi - xLo);
  }
  function timeToLogical(ctx, time) {
    const { data, intervalSec } = ctx;
    if (data.length === 0 || intervalSec <= 0) return null;
    const firstTime = data[0].time;
    const lastTime = data[data.length - 1].time;
    if (time <= firstTime) return (time - firstTime) / intervalSec;
    if (time >= lastTime) return data.length - 1 + (time - lastTime) / intervalSec;
    const idx = lowerBound(data, time);
    const t1 = data[idx - 1].time;
    const t2 = data[idx].time;
    return idx - 1 + (time - t1) / Math.max(t2 - t1, 1);
  }
  function xToTime(ctx, x) {
    const direct = ctx.chart.timeScale().coordinateToTime(x);
    if (direct !== null) return direct;
    const { data, intervalSec } = ctx;
    if (data.length === 0 || intervalSec <= 0) return null;
    const logical = ctx.chart.timeScale().coordinateToLogical(x);
    if (logical === null) return null;
    const lastIdx = data.length - 1;
    const lastTime = data[lastIdx].time;
    const firstTime = data[0].time;
    if (logical > lastIdx) return lastTime + Math.round(logical - lastIdx) * intervalSec;
    if (logical < 0) return firstTime + Math.round(logical) * intervalSec;
    return null;
  }
  function resolveEntry(d, ctx, state) {
    const entry = {
      d,
      x1: d.time != null ? timeToX(ctx, d.time) : null,
      y1: Number.isFinite(d.price) ? ctx.series.priceToCoordinate(d.price) : null,
      x2: d.time2 != null ? timeToX(ctx, d.time2) : null,
      y2: d.price2 != null ? ctx.series.priceToCoordinate(d.price2) : null,
      state
    };
    if (d.type === "fibonacci" || d.type === "fibextension") {
      entry.fibLevels = resolveFibLevels(d, ctx.series);
    }
    if (d.type === "trendline") {
      entry.seg = trendlineSegment(entry, ctx.chart.timeScale().width());
      if (state === "selected" || state === "preview") entry.stats = trendlineStats(d, ctx);
    }
    if (d.type === "position") {
      entry.yStop = d.stopPrice != null ? ctx.series.priceToCoordinate(d.stopPrice) : null;
      entry.yTarget = d.targetPrice != null ? ctx.series.priceToCoordinate(d.targetPrice) : null;
    }
    if (d.type === "channel") {
      entry.x3 = d.time3 != null ? timeToX(ctx, d.time3) : null;
      entry.y3 = d.price3 != null ? ctx.series.priceToCoordinate(d.price3) : null;
    }
    return entry;
  }
  function trendlineStats(d, ctx) {
    if (d.price2 == null || d.time == null || d.time2 == null) return [];
    const dp = d.price2 - d.price;
    const pct = d.price !== 0 ? dp / d.price * 100 : 0;
    const bars = ctx.intervalSec > 0 ? Math.round(Math.abs(d.time2 - d.time) / ctx.intervalSec) : 0;
    const formatted = ctx.series.priceFormatter().format(Math.abs(dp));
    return [`${dp >= 0 ? "+" : "\u2212"}${formatted} (${pct.toFixed(2)}%)`, `${bars} bars`];
  }
  function trendlineSegment(e, paneWidth) {
    if (e.x1 === null || e.y1 === null || e.x2 === null || e.y2 === null) return void 0;
    const seg = e.x1 <= e.x2 ? { x1: e.x1, y1: e.y1, x2: e.x2, y2: e.y2 } : { x1: e.x2, y1: e.y2, x2: e.x1, y2: e.y1 };
    return extendSegment(seg, e.d, paneWidth);
  }
  function extendSegment(seg, d, paneWidth) {
    if (!d.extendLeft && !d.extendRight || seg.x2 - seg.x1 < 0.5) return seg;
    const slope = (seg.y2 - seg.y1) / (seg.x2 - seg.x1);
    const out = { ...seg };
    if (d.extendLeft) {
      out.y1 -= slope * out.x1;
      out.x1 = 0;
    }
    if (d.extendRight && paneWidth > out.x2) {
      out.y2 += slope * (paneWidth - out.x2);
      out.x2 = paneWidth;
    }
    return out;
  }
  function fibLevelsFor(d) {
    if (d.fibLevels && d.fibLevels.length > 0) return d.fibLevels;
    return d.type === "fibextension" ? FIB_EXT_LEVELS : FIB_LEVELS;
  }
  function resolveFibLevels(d, series) {
    if (d.price2 == null) return [];
    const formatter = series.priceFormatter();
    return fibLevelsFor(d).map((level, i) => {
      var _a;
      const price = d.price2 + (d.price - d.price2) * level;
      return {
        level,
        price,
        y: series.priceToCoordinate(price),
        color: (_a = FIB_COLORS[i]) != null ? _a : "#2196F3",
        label: `${level} (${formatter.format(price)})`
      };
    });
  }

  // drawing-tools/drawings-primitive.ts
  var DrawingsPaneRenderer = class {
    constructor(entries, info) {
      __publicField(this, "_entries");
      __publicField(this, "_info");
      this._entries = entries;
      this._info = info;
    }
    draw(target) {
      target.useBitmapCoordinateSpace((scope) => {
        for (const e of this._entries) renderEntry(scope, e, this._info);
      });
    }
  };
  var DrawingsPaneView = class {
    constructor(source) {
      __publicField(this, "_source");
      __publicField(this, "_entries", []);
      this._source = source;
    }
    update() {
      this._entries = this._source.resolveEntries();
    }
    renderer() {
      return new DrawingsPaneRenderer(this._entries, this._source.drawInfo());
    }
  };
  var PriceAxisLabelView = class {
    constructor(source, price, color) {
      __publicField(this, "_source");
      __publicField(this, "_price");
      __publicField(this, "_color");
      __publicField(this, "_y", null);
      this._source = source;
      this._price = price;
      this._color = color;
    }
    update() {
      this._y = this._source.series.priceToCoordinate(this._price);
    }
    coordinate() {
      var _a;
      return (_a = this._y) != null ? _a : -1;
    }
    visible() {
      return this._y !== null;
    }
    tickVisible() {
      return true;
    }
    text() {
      return this._source.series.priceFormatter().format(this._price);
    }
    textColor() {
      return "#ffffff";
    }
    backColor() {
      return this._color;
    }
  };
  function appendAxisViews(views, d, selectedIds, source) {
    if (d.type === "horizontal") {
      views.push(new PriceAxisLabelView(source, d.price, d.color));
      return;
    }
    if (!selectedIds.has(d.id)) return;
    if (Number.isFinite(d.price)) views.push(new PriceAxisLabelView(source, d.price, d.color));
    if (d.price2 != null) views.push(new PriceAxisLabelView(source, d.price2, d.color));
  }
  var DrawingsPrimitive = class extends PluginBase {
    constructor() {
      super(...arguments);
      __publicField(this, "_drawings", []);
      __publicField(this, "_preview", null);
      __publicField(this, "_selectedIds", /* @__PURE__ */ new Set());
      __publicField(this, "_hoveredId", null);
      __publicField(this, "_intervalSec", 60);
      __publicField(this, "_accountEquity", 0);
      __publicField(this, "_paneViews", [new DrawingsPaneView(this)]);
      __publicField(this, "_axisViews", []);
    }
    setDrawings(drawings) {
      this._drawings = drawings;
      this._rebuildAxisViews();
      this.requestUpdate();
    }
    setPreview(d) {
      this._preview = d;
      this.requestUpdate();
    }
    setSelected(ids) {
      this._selectedIds = new Set(ids);
      this._rebuildAxisViews();
      this.requestUpdate();
    }
    setHovered(id) {
      if (this._hoveredId === id) return;
      this._hoveredId = id;
      this.requestUpdate();
    }
    setAccountEquity(equity) {
      if (this._accountEquity === equity) return;
      this._accountEquity = equity;
      this.requestUpdate();
    }
    /** Account context the position tool reads for its $-risk / size readout. */
    drawInfo() {
      return {
        accountEquity: this._accountEquity,
        priceFormat: (p) => this.series.priceFormatter().format(p)
      };
    }
    setIntervalSec(intervalSec) {
      if (this._intervalSec === intervalSec) return;
      this._intervalSec = intervalSec;
      this.requestUpdate();
    }
    updateAllViews() {
      for (const v of this._paneViews) v.update();
      for (const v of this._axisViews) v.update();
    }
    paneViews() {
      return this._paneViews;
    }
    priceAxisViews() {
      return this._axisViews;
    }
    resolveEntries() {
      const ctx = makeResolveCtx(this.chart, this.series, this._intervalSec);
      const entries = this._drawings.map((d) => resolveEntry(d, ctx, this._stateFor(d.id)));
      if (this._preview) entries.push(resolveEntry(this._preview, ctx, "preview"));
      return entries;
    }
    _stateFor(id) {
      if (this._selectedIds.has(id)) return "selected";
      if (id === this._hoveredId) return "hovered";
      return "normal";
    }
    _rebuildAxisViews() {
      const views = [];
      for (const d of this._drawings) appendAxisViews(views, d, this._selectedIds, this);
      this._axisViews = views;
    }
  };

  // drawing-tools/geometry.ts
  function dist(a, b) {
    return Math.hypot(a.x - b.x, a.y - b.y);
  }
  function distToSegment(p, a, b) {
    const abx = b.x - a.x;
    const aby = b.y - a.y;
    const lenSq = abx * abx + aby * aby;
    if (lenSq === 0) return dist(p, a);
    const t = Math.max(0, Math.min(1, ((p.x - a.x) * abx + (p.y - a.y) * aby) / lenSq));
    return dist(p, { x: a.x + t * abx, y: a.y + t * aby });
  }
  function snapAngle(anchor, p) {
    const dx = p.x - anchor.x;
    const dy = p.y - anchor.y;
    const r = Math.hypot(dx, dy);
    if (r < 1) return p;
    const step = Math.PI / 4;
    const angle = Math.round(Math.atan2(dy, dx) / step) * step;
    return { x: anchor.x + r * Math.cos(angle), y: anchor.y + r * Math.sin(angle) };
  }
  function pointInBox(p, x1, y1, x2, y2, tolerance) {
    const minX = Math.min(x1, x2) - tolerance;
    const maxX = Math.max(x1, x2) + tolerance;
    const minY = Math.min(y1, y2) - tolerance;
    const maxY = Math.max(y1, y2) + tolerance;
    return p.x >= minX && p.x <= maxX && p.y >= minY && p.y <= maxY;
  }

  // drawing-tools/hit-test.ts
  var HANDLE_TOLERANCE = 8;
  var LINE_TOLERANCE = 6;
  function hitTest(entries, p, scale = 1) {
    const tol = { handle: HANDLE_TOLERANCE * scale, line: LINE_TOLERANCE * scale };
    for (let i = entries.length - 1; i >= 0; i--) {
      const hit = hitEntry(entries[i], p, tol);
      if (hit) return hit;
    }
    return null;
  }
  function hitEntry(e, p, tol) {
    switch (e.d.type) {
      case "trendline":
        return hitTrendline(e, p, tol);
      case "horizontal":
        return hitHorizontal(e, p, tol);
      case "rectangle":
        return hitRectangle(e, p, tol);
      case "fibonacci":
        return hitFibonacci(e, p, tol);
      case "position":
        return hitPosition(e, p, tol);
      case "arrow":
        return hitTrendline(e, p, tol);
      case "fibextension":
        return hitFibonacci(e, p, tol);
      case "vertical":
        return hitVertical(e, p, tol);
      case "channel":
        return hitChannel(e, p, tol);
      case "ellipse":
      case "triangle":
        return hitBoxShape(e, p, tol);
      case "text":
        return hitText(e, p, tol);
      default:
        return null;
    }
  }
  function hitVertical(e, p, tol) {
    if (e.x1 === null || Math.abs(p.x - e.x1) > tol.line) return null;
    return { id: e.d.id, region: { kind: "point", timeKey: "time", priceKey: null } };
  }
  function hitChannel(e, p, tol) {
    const anchor = anchorHit(e, p, tol);
    if (anchor) return anchor;
    if (e.x1 !== null && e.y3 != null && dist(p, { x: e.x1, y: e.y3 }) <= tol.handle) {
      return { id: e.d.id, region: { kind: "point", timeKey: "time3", priceKey: "price3" } };
    }
    if (e.x1 === null || e.y1 === null || e.x2 === null || e.y2 === null || e.y3 == null) return null;
    const dy = e.y3 - e.y1;
    const onMain = distToSegment(p, { x: e.x1, y: e.y1 }, { x: e.x2, y: e.y2 }) <= tol.line;
    const onOff = distToSegment(p, { x: e.x1, y: e.y3 }, { x: e.x2, y: e.y2 + dy }) <= tol.line;
    return onMain || onOff ? bodyHit(e) : null;
  }
  function hitBoxShape(e, p, tol) {
    if (e.x1 === null || e.y1 === null || e.x2 === null || e.y2 === null) return null;
    const corner = rectCornerHit(e, p, tol);
    if (corner) return corner;
    return pointInBox(p, e.x1, e.y1, e.x2, e.y2, tol.line) ? bodyHit(e) : null;
  }
  function hitText(e, p, tol) {
    var _a, _b;
    if (e.x1 === null || e.y1 === null) return null;
    const size = (_a = e.d.fontSize) != null ? _a : 14;
    const lines = ((_b = e.d.text) != null ? _b : "Text").split("\n");
    const longest = lines.reduce((m, l) => Math.max(m, l.length), 4);
    const w = longest * size * 0.6;
    const h = lines.length * size * 1.3;
    const inside = p.x >= e.x1 - tol.line && p.x <= e.x1 + w + tol.line && p.y >= e.y1 - tol.line && p.y <= e.y1 + h + tol.line;
    return inside ? { id: e.d.id, region: { kind: "point", timeKey: "time", priceKey: "price" } } : null;
  }
  function pointHit(e, timeKey, priceKey) {
    return { id: e.d.id, region: { kind: "point", timeKey, priceKey } };
  }
  function hitPosition(e, p, tol) {
    const { x1, x2, y1, yStop, yTarget } = e;
    if (x1 === null || x2 === null || y1 === null) return null;
    const xa = Math.min(x1, x2);
    const xb = Math.max(x1, x2);
    const xm = (xa + xb) / 2;
    if (yTarget != null && dist(p, { x: xm, y: yTarget }) <= tol.handle) {
      return pointHit(e, null, "targetPrice");
    }
    if (yStop != null && dist(p, { x: xm, y: yStop }) <= tol.handle) {
      return pointHit(e, null, "stopPrice");
    }
    if (dist(p, { x: xm, y: y1 }) <= tol.handle) return pointHit(e, null, "price");
    if (dist(p, { x: xb, y: y1 }) <= tol.handle) return pointHit(e, "time2", null);
    const ys = [y1, yStop != null ? yStop : y1, yTarget != null ? yTarget : y1];
    const top = Math.min(...ys);
    const bot = Math.max(...ys);
    const inside = p.x >= xa - tol.line && p.x <= xb + tol.line && p.y >= top - tol.line && p.y <= bot + tol.line;
    return inside ? bodyHit(e) : null;
  }
  function bodyHit(e) {
    return { id: e.d.id, region: { kind: "body" } };
  }
  function anchorHit(e, p, tol) {
    if (e.x1 !== null && e.y1 !== null && dist(p, { x: e.x1, y: e.y1 }) <= tol.handle) {
      return { id: e.d.id, region: { kind: "point", timeKey: "time", priceKey: "price" } };
    }
    if (e.x2 !== null && e.y2 !== null && dist(p, { x: e.x2, y: e.y2 }) <= tol.handle) {
      return { id: e.d.id, region: { kind: "point", timeKey: "time2", priceKey: "price2" } };
    }
    return null;
  }
  function hitTrendline(e, p, tol) {
    var _a;
    const anchor = anchorHit(e, p, tol);
    if (anchor) return anchor;
    const seg = (_a = e.seg) != null ? _a : e.x1 !== null && e.y1 !== null && e.x2 !== null && e.y2 !== null ? { x1: e.x1, y1: e.y1, x2: e.x2, y2: e.y2 } : null;
    if (!seg) return null;
    const near = distToSegment(p, { x: seg.x1, y: seg.y1 }, { x: seg.x2, y: seg.y2 }) <= tol.line;
    return near ? bodyHit(e) : null;
  }
  function hitHorizontal(e, p, tol) {
    if (e.y1 === null || Math.abs(p.y - e.y1) > tol.line) return null;
    return { id: e.d.id, region: { kind: "point", timeKey: null, priceKey: "price" } };
  }
  function hitRectangle(e, p, tol) {
    if (e.x1 === null || e.y1 === null || e.x2 === null || e.y2 === null) return null;
    const corner = rectCornerHit(e, p, tol);
    if (corner) return corner;
    return pointInBox(p, e.x1, e.y1, e.x2, e.y2, tol.line) ? bodyHit(e) : null;
  }
  function rectCornerHit(e, p, tol) {
    const corners = [
      { x: e.x1, y: e.y1, timeKey: "time", priceKey: "price" },
      { x: e.x2, y: e.y2, timeKey: "time2", priceKey: "price2" },
      { x: e.x1, y: e.y2, timeKey: "time", priceKey: "price2" },
      { x: e.x2, y: e.y1, timeKey: "time2", priceKey: "price" }
    ];
    for (const c of corners) {
      if (dist(p, c) <= tol.handle) {
        return { id: e.d.id, region: { kind: "point", timeKey: c.timeKey, priceKey: c.priceKey } };
      }
    }
    return null;
  }
  function hitFibonacci(e, p, tol) {
    const anchor = anchorHit(e, p, tol);
    if (anchor) return anchor;
    if (e.x1 === null || e.x2 === null || !e.fibLevels) return null;
    if (p.x < Math.min(e.x1, e.x2) - tol.line) return null;
    if (p.x > Math.max(e.x1, e.x2) + tol.line) return null;
    const onLevel = e.fibLevels.some((lvl) => lvl.y !== null && Math.abs(p.y - lvl.y) <= tol.line);
    return onLevel ? bodyHit(e) : null;
  }

  // drawing-tools/manager.ts
  var DRAG_COMMIT_THRESHOLD_PX = 10;
  var MAGNET_THRESHOLD_PX = 14;
  var SNAP_ANCHOR_PX = 8;
  var TOUCH_HIT_SCALE = 2;
  var LONG_PRESS_MS = 500;
  var LONG_PRESS_CANCEL_PX = 10;
  var TOOL_SHORTCUTS = {
    KeyT: "trendline",
    KeyH: "horizontal",
    KeyF: "fibonacci",
    KeyR: "rectangle",
    KeyM: "measure"
  };
  function asOhlc(bar) {
    const b = bar;
    const valid = typeof b.open === "number" && typeof b.high === "number" && typeof b.low === "number" && typeof b.close === "number";
    return valid ? b : null;
  }
  function evtFromTouch(e) {
    return {
      shiftKey: false,
      preventDefault: () => e.preventDefault(),
      stopPropagation: () => e.stopPropagation()
    };
  }
  function definedOnly(obj) {
    const out = {};
    for (const key of Object.keys(obj)) {
      if (obj[key] !== void 0) out[key] = obj[key];
    }
    return out;
  }
  function isTextInputTarget(t) {
    if (!(t instanceof HTMLElement)) return false;
    return t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName);
  }
  var DrawingToolsManager = class {
    constructor(opts) {
      __publicField(this, "chart");
      __publicField(this, "series");
      __publicField(this, "container");
      __publicField(this, "cb");
      __publicField(this, "primitive");
      // Mutable: the chart instance persists across timeframe changes (TradingView
      // behavior), so these are updated in place via updateTimeframe rather than
      // recreating the manager.
      __publicField(this, "intervalSec");
      __publicField(this, "timeframe");
      __publicField(this, "tool", "none");
      __publicField(this, "drawings", []);
      __publicField(this, "placing", null);
      __publicField(this, "drag", null);
      __publicField(this, "selectedIds", []);
      __publicField(this, "hoveredId", null);
      __publicField(this, "magnetMode", "none");
      __publicField(this, "stayInMode", false);
      __publicField(this, "longPress", null);
      // The measure tool is a throwaway gesture: its result lingers as a preview
      // (never committed/persisted) until the next pointer-down or Escape.
      __publicField(this, "measureResult", null);
      // Copy/paste clipboard (deep copies of the drawings copied with Ctrl/Cmd+C).
      __publicField(this, "clipboard", []);
      // Per-type style defaults new drawings inherit (set via the settings dialog).
      __publicField(this, "styleDefaults", {});
      // ── Mouse handlers ─────────────────────────────────────────────────
      __publicField(this, "handleMouseDown", (e) => {
        if (e.button !== 0) return;
        const pos = this.eventPos(e);
        if (!pos) return;
        this.clearMeasure();
        if (this.tool !== "none") {
          this.placementStart(pos, e);
          return;
        }
        this.selectionStart(pos, e, false);
      });
      __publicField(this, "handleMouseMove", (e) => {
        const pos = this.eventPos(e);
        if (this.drag) {
          if (pos) this.dragMove(pos, e.shiftKey);
          return;
        }
        if (!pos) {
          this.hoverHit(null);
          return;
        }
        if (this.placing) {
          this.placingMove(pos, e.shiftKey);
          return;
        }
        if (this.tool === "none") this.hoverHit(hitTest(this.resolveAll(), pos));
      });
      __publicField(this, "handleMouseUp", (e) => {
        if (this.drag) {
          this.endDrag();
          return;
        }
        if (this.placing) this.maybeCommitPlacement(this.eventPos(e), e.shiftKey);
      });
      __publicField(this, "handleDblClick", (e) => {
        var _a, _b;
        if (this.tool !== "none") return;
        const pos = this.eventPos(e);
        if (!pos) return;
        const hit = hitTest(this.resolveAll(), pos);
        if (!hit) return;
        e.preventDefault();
        e.stopPropagation();
        this.select([hit.id]);
        (_b = (_a = this.cb).onRequestSettings) == null ? void 0 : _b.call(_a, hit.id);
      });
      __publicField(this, "handleContextMenu", (e) => {
        var _a, _b;
        const pos = this.eventPos(e);
        if (!pos) return;
        const hit = hitTest(this.resolveAll(), pos);
        if (!hit) return;
        e.preventDefault();
        e.stopPropagation();
        if (!this.selectedIds.includes(hit.id)) this.select([hit.id]);
        (_b = (_a = this.cb).onContextMenu) == null ? void 0 : _b.call(_a, hit.id, e.clientX, e.clientY);
      });
      // ── Touch handlers ─────────────────────────────────────────────────
      __publicField(this, "handleTouchStart", (e) => {
        if (e.touches.length !== 1) return;
        const t = e.touches[0];
        const pos = this.posFromClient(t.clientX, t.clientY);
        if (!pos) return;
        if (this.tool !== "none") {
          this.placementStart(pos, evtFromTouch(e));
          return;
        }
        const hit = this.selectionStart(pos, evtFromTouch(e), true);
        if (hit) this.startLongPress(hit.id, pos);
      });
      __publicField(this, "handleTouchMove", (e) => {
        if (e.touches.length !== 1) return;
        const t = e.touches[0];
        const pos = this.posFromClient(t.clientX, t.clientY);
        this.cancelLongPressIfMoved(pos);
        if (this.drag) {
          e.preventDefault();
          if (pos) this.dragMove(pos, false);
          return;
        }
        if (this.placing && pos) this.placingMove(pos, false);
      });
      __publicField(this, "handleTouchEnd", (e) => {
        this.clearLongPress();
        if (this.drag) {
          this.endDrag();
          return;
        }
        if (!this.placing) return;
        const t = e.changedTouches[0];
        this.maybeCommitPlacement(t ? this.posFromClient(t.clientX, t.clientY) : null, false);
      });
      // ── Keyboard ───────────────────────────────────────────────────────
      __publicField(this, "handleKeyDown", (e) => {
        if (isTextInputTarget(e.target)) return;
        if (this.handleHistoryShortcut(e) || this.handleToolShortcut(e)) return;
        if (this.handleClipboardShortcut(e) || this.handleArrowNudge(e)) return;
        if (e.key === "Escape") {
          this.handleEscape();
          return;
        }
        if ((e.key === "Delete" || e.key === "Backspace") && this.selectedIds.length > 0 && !this.drag) {
          e.preventDefault();
          this.removeSelected();
        }
      });
      var _a;
      this.chart = opts.chart;
      this.series = opts.series;
      this.container = opts.container;
      this.cb = opts.callbacks;
      this.intervalSec = opts.intervalSec;
      this.timeframe = opts.timeframe;
      this.primitive = new DrawingsPrimitive();
      this.primitive.setIntervalSec(opts.intervalSec);
      this.primitive.setAccountEquity((_a = opts.accountEquity) != null ? _a : 0);
      this.series.attachPrimitive(this.primitive);
      this.container.addEventListener("mousedown", this.handleMouseDown, true);
      this.container.addEventListener("dblclick", this.handleDblClick);
      this.container.addEventListener("contextmenu", this.handleContextMenu);
      this.container.addEventListener("touchstart", this.handleTouchStart, {
        capture: true,
        passive: false
      });
      window.addEventListener("mousemove", this.handleMouseMove);
      window.addEventListener("mouseup", this.handleMouseUp);
      window.addEventListener("touchmove", this.handleTouchMove, { passive: false });
      window.addEventListener("touchend", this.handleTouchEnd);
      window.addEventListener("touchcancel", this.handleTouchEnd);
      window.addEventListener("keydown", this.handleKeyDown);
    }
    destroy() {
      this.clearLongPress();
      this.container.removeEventListener("mousedown", this.handleMouseDown, true);
      this.container.removeEventListener("dblclick", this.handleDblClick);
      this.container.removeEventListener("contextmenu", this.handleContextMenu);
      this.container.removeEventListener("touchstart", this.handleTouchStart, true);
      window.removeEventListener("mousemove", this.handleMouseMove);
      window.removeEventListener("mouseup", this.handleMouseUp);
      window.removeEventListener("touchmove", this.handleTouchMove);
      window.removeEventListener("touchend", this.handleTouchEnd);
      window.removeEventListener("touchcancel", this.handleTouchEnd);
      window.removeEventListener("keydown", this.handleKeyDown);
      try {
        this.series.detachPrimitive(this.primitive);
      } catch {
      }
      this.container.style.cursor = "";
    }
    setTool(tool) {
      if (this.tool === tool) return;
      this.tool = tool;
      this.cancelPlacement();
      this.applyCursor(null);
    }
    setMagnetMode(mode) {
      this.magnetMode = mode;
    }
    setStyleDefaults(defaults) {
      this.styleDefaults = defaults;
    }
    setStayInDrawingMode(enabled) {
      this.stayInMode = enabled;
    }
    setAccountEquity(equity) {
      this.primitive.setAccountEquity(equity);
    }
    /**
     * Re-point the manager at a new timeframe without recreating it. The chart
     * and drawing primitive stay alive (so drawings never blink out on a TF
     * switch); only the interval used for whitespace extrapolation and the
     * createdTf stamped on new drawings change.
     */
    updateTimeframe(timeframe, intervalSec) {
      this.timeframe = timeframe;
      this.intervalSec = intervalSec;
      this.primitive.setIntervalSec(intervalSec);
    }
    /** External selection (e.g. object tree row click). Unknown ids are dropped. */
    setSelection(ids) {
      this.select(ids.filter((id) => this.drawings.some((d) => d.id === id)));
    }
    setDrawings(drawings) {
      if (this.drag) return;
      this.drawings = drawings.map((d) => ({ ...d }));
      const pruned = this.selectedIds.filter((id) => this.drawings.some((d) => d.id === id));
      if (pruned.length !== this.selectedIds.length) this.select(pruned);
      this.primitive.setDrawings(this.drawings);
    }
    // ── Coordinate helpers ─────────────────────────────────────────────
    ctx() {
      return makeResolveCtx(this.chart, this.series, this.intervalSec);
    }
    /** Pointer position relative to the chart pane, or null when outside it. */
    posFromClient(clientX, clientY) {
      const rect = this.container.getBoundingClientRect();
      const x = clientX - rect.left;
      const y = clientY - rect.top;
      const timeScale = this.chart.timeScale();
      const paneWidth = timeScale.width();
      const paneHeight = rect.height - timeScale.height();
      if (paneWidth <= 0 || x < 0 || y < 0 || x > paneWidth || y > paneHeight) return null;
      return { x, y };
    }
    eventPos(e) {
      return this.posFromClient(e.clientX, e.clientY);
    }
    toPoint(p) {
      const time = xToTime(this.ctx(), p.x);
      const price = this.series.coordinateToPrice(p.y);
      if (time === null || price === null) return null;
      return { time, price };
    }
    resolveAll() {
      const ctx = this.ctx();
      return this.drawings.map((d) => resolveEntry(d, ctx, "normal"));
    }
    // Build a drawing, then merge the user's saved style default for its type
    // (the measure gesture keeps its fixed look).
    makeNew(tool, p1, p2) {
      const d = this.buildNew(tool, p1, p2);
      if (tool === "measure") return d;
      const def = this.styleDefaults[d.type];
      return def ? { ...d, ...definedOnly(def) } : d;
    }
    buildNew(tool, p1, p2) {
      if (tool === "long-position" || tool === "short-position") {
        return this.makePosition(tool === "long-position" ? "long" : "short", p1, p2);
      }
      const base = {
        id: crypto.randomUUID(),
        color: tool === "trendline" ? "#2196F3" : "#f0b90b",
        createdTf: this.timeframe
      };
      if (tool === "horizontal") return { ...base, type: "horizontal", price: p1.price };
      if (tool === "vertical") return { ...base, type: "vertical", price: p1.price, time: p1.time };
      const two = { ...base, price: p1.price, time: p1.time, price2: p2.price, time2: p2.time };
      if (tool === "ray") return { ...two, type: "trendline", extendRight: true };
      if (tool === "extended")
        return { ...two, type: "trendline", extendLeft: true, extendRight: true };
      if (tool === "measure")
        return { ...two, type: "trendline", color: "#b2b5be", lineStyle: "dashed" };
      if (tool === "channel") {
        return { ...two, type: "channel", time3: p1.time, price3: 2 * p1.price - p2.price };
      }
      if (tool === "text") return { ...two, type: "text", text: "Text" };
      return { ...two, type: tool };
    }
    // Position tool: entry = first anchor price; the drag's release price becomes
    // the target and the stop is mirrored 1:1 on the opposite side (draggable
    // afterwards). Side only drives labelling/colour — risk math uses absolutes.
    makePosition(side, p1, p2) {
      const entry = p1.price;
      const target = p2.price;
      return {
        id: crypto.randomUUID(),
        type: "position",
        side,
        color: side === "long" ? "#089981" : "#f23645",
        createdTf: this.timeframe,
        price: entry,
        time: Math.min(p1.time, p2.time),
        time2: Math.max(p1.time, p2.time),
        targetPrice: target,
        stopPrice: entry - (target - entry),
        riskPct: 1
      };
    }
    startLongPress(id, pos) {
      this.clearLongPress();
      const timer = window.setTimeout(() => {
        var _a, _b;
        this.longPress = null;
        this.drag = null;
        this.applyCursor(null);
        (_b = (_a = this.cb).onRequestSettings) == null ? void 0 : _b.call(_a, id);
      }, LONG_PRESS_MS);
      this.longPress = { timer, id, startPos: pos };
    }
    cancelLongPressIfMoved(pos) {
      if (!this.longPress) return;
      if (!pos || dist(pos, this.longPress.startPos) > LONG_PRESS_CANCEL_PX) this.clearLongPress();
    }
    clearLongPress() {
      if (!this.longPress) return;
      window.clearTimeout(this.longPress.timer);
      this.longPress = null;
    }
    /** Ctrl/Cmd+Z undo, Ctrl/Cmd+Shift+Z or Ctrl/Cmd+Y redo. */
    handleHistoryShortcut(e) {
      var _a, _b, _c, _d, _e, _f;
      if (!e.ctrlKey && !e.metaKey) return false;
      if (e.code === "KeyZ") {
        e.preventDefault();
        if (e.shiftKey) (_b = (_a = this.cb).onRedo) == null ? void 0 : _b.call(_a);
        else (_d = (_c = this.cb).onUndo) == null ? void 0 : _d.call(_c);
        return true;
      }
      if (e.code === "KeyY") {
        e.preventDefault();
        (_f = (_e = this.cb).onRedo) == null ? void 0 : _f.call(_e);
        return true;
      }
      return false;
    }
    /** Alt+T/H/F/R arms the matching drawing tool. */
    handleToolShortcut(e) {
      var _a, _b;
      if (!e.altKey || e.ctrlKey || e.metaKey) return false;
      const tool = TOOL_SHORTCUTS[e.code];
      if (!tool) return false;
      e.preventDefault();
      (_b = (_a = this.cb).onSelectTool) == null ? void 0 : _b.call(_a, tool);
      return true;
    }
    /** Ctrl/Cmd+C copy, +V paste, +D duplicate the current selection. */
    handleClipboardShortcut(e) {
      if (!e.ctrlKey && !e.metaKey) return false;
      if (e.code === "KeyC") {
        this.copySelected();
        return true;
      }
      if (e.code === "KeyV") {
        e.preventDefault();
        this.pasteClipboard();
        return true;
      }
      if (e.code === "KeyD") {
        e.preventDefault();
        this.copySelected();
        this.pasteClipboard();
        return true;
      }
      return false;
    }
    /** Arrow keys nudge the selection 1px (10px with Shift). */
    handleArrowNudge(e) {
      const deltas = {
        ArrowLeft: [-1, 0],
        ArrowRight: [1, 0],
        ArrowUp: [0, -1],
        ArrowDown: [0, 1]
      };
      const d = deltas[e.key];
      if (!d || this.selectedIds.length === 0) return false;
      e.preventDefault();
      const step = e.shiftKey ? 10 : 1;
      this.nudgeSelected(d[0] * step, d[1] * step);
      return true;
    }
    copySelected() {
      this.clipboard = this.drawings.filter((d) => this.selectedIds.includes(d.id)).map((d) => ({ ...d }));
    }
    pasteClipboard() {
      if (this.clipboard.length === 0) return;
      const dt = this.intervalSec * 3;
      const created = this.clipboard.map((d) => this.offsetCopy(d, dt));
      this.drawings = [...this.drawings, ...created];
      this.primitive.setDrawings(this.drawings);
      for (const d of created) this.cb.onAdd({ ...d });
      this.select(created.map((c) => c.id));
    }
    offsetCopy(d, dt) {
      return {
        ...d,
        id: crypto.randomUUID(),
        time: d.time != null ? d.time + dt : void 0,
        time2: d.time2 != null ? d.time2 + dt : void 0,
        time3: d.time3 != null ? d.time3 + dt : void 0
      };
    }
    nudgeSelected(dx, dy) {
      const entries = this.resolveAll();
      const updates = [];
      for (const id of this.selectedIds) {
        const origin = this.drawings.find((d) => d.id === id);
        const entry = entries.find((en) => en.d.id === id);
        if (!origin || !entry || origin.locked) continue;
        const u = this.shiftDrawing(origin, entry, dx, dy);
        if (u) updates.push(u);
      }
      if (updates.length === 0) return;
      const byId = new Map(updates.map((u) => [u.id, u]));
      this.drawings = this.drawings.map((d) => {
        var _a;
        return (_a = byId.get(d.id)) != null ? _a : d;
      });
      this.primitive.setDrawings(this.drawings);
      for (const u of updates) this.cb.onUpdate({ ...u });
    }
    handleEscape() {
      this.clearMeasure();
      if (this.tool !== "none") {
        this.tool = "none";
        this.cancelPlacement();
        this.applyCursor(null);
        this.cb.onToolFinished();
        return;
      }
      this.select([]);
    }
    // ── Placement ──────────────────────────────────────────────────────
    placementStart(pos, e) {
      const pt = this.pointFor(pos, e.shiftKey, this.placementAnchorPx());
      if (!pt) return;
      e.preventDefault();
      e.stopPropagation();
      if (this.tool === "horizontal" || this.tool === "vertical" || this.tool === "text") {
        this.commitDrawing(this.makeNew(this.tool, pt, pt));
        return;
      }
      if (this.placing) {
        this.commitPlacement(this.placing.p1, pt);
        return;
      }
      this.placing = { p1: pt, startX: pos.x, startY: pos.y };
      this.primitive.setPreview(this.makeNew(this.tool, pt, pt));
    }
    // Branches the measure tool (a throwaway readout) away from the persist path.
    commitPlacement(p1, p2) {
      if (this.tool === "measure") {
        this.finishMeasure(p1, p2);
        return;
      }
      this.commitDrawing(this.makeNew(this.tool, p1, p2));
    }
    finishMeasure(p1, p2) {
      this.placing = null;
      this.measureResult = {
        id: "measure",
        type: "trendline",
        color: "#b2b5be",
        lineStyle: "dashed",
        price: p1.price,
        time: p1.time,
        price2: p2.price,
        time2: p2.time
      };
      this.primitive.setPreview(this.measureResult);
      this.tool = "none";
      this.applyCursor(null);
      this.cb.onToolFinished();
    }
    clearMeasure() {
      if (!this.measureResult) return;
      this.measureResult = null;
      this.primitive.setPreview(null);
    }
    placingMove(pos, shiftKey) {
      const pt = this.pointFor(pos, shiftKey, this.placementAnchorPx());
      if (!pt || !this.placing) return;
      this.primitive.setPreview(this.makeNew(this.tool, this.placing.p1, pt));
    }
    maybeCommitPlacement(pos, shiftKey) {
      const placing = this.placing;
      if (!pos || dist(pos, { x: placing.startX, y: placing.startY }) < DRAG_COMMIT_THRESHOLD_PX) {
        return;
      }
      const pt = this.pointFor(pos, shiftKey, this.placementAnchorPx());
      if (pt) this.commitPlacement(placing.p1, pt);
    }
    commitDrawing(d) {
      this.placing = null;
      this.primitive.setPreview(null);
      this.drawings = [...this.drawings, d];
      this.primitive.setDrawings(this.drawings);
      this.cb.onAdd({ ...d });
      if (this.stayInMode && this.tool !== "none") return;
      this.select([d.id]);
      this.tool = "none";
      this.applyCursor(null);
      this.cb.onToolFinished();
    }
    cancelPlacement() {
      this.placing = null;
      this.primitive.setPreview(null);
    }
    // ── Selection & dragging ───────────────────────────────────────────
    selectionStart(pos, e, isTouch) {
      const entries = this.resolveAll();
      const hit = hitTest(entries, pos, isTouch ? TOUCH_HIT_SCALE : 1);
      if (!hit) {
        if (!e.shiftKey) this.select([]);
        return null;
      }
      e.preventDefault();
      e.stopPropagation();
      if (e.shiftKey) {
        this.toggleSelection(hit.id);
        return hit;
      }
      if (!this.selectedIds.includes(hit.id)) this.select([hit.id]);
      this.beginDrag(hit, pos, entries, isTouch);
      return hit;
    }
    toggleSelection(id) {
      const next = this.selectedIds.includes(id) ? this.selectedIds.filter((x) => x !== id) : [...this.selectedIds, id];
      this.select(next);
    }
    beginDrag(hit, pos, entries, isTouch) {
      const origin = this.drawings.find((d) => d.id === hit.id);
      const originEntry = entries.find((en) => en.d.id === hit.id);
      if (!origin || !originEntry || origin.locked) return;
      const group = hit.region.kind === "body" ? this.groupFor(hit.id, entries) : [];
      this.drag = {
        hit,
        startX: pos.x,
        startY: pos.y,
        origin: { ...origin },
        originEntry,
        group,
        moved: false,
        isTouch
      };
      this.applyCursor("grabbing");
    }
    /** Other selected, unlocked drawings that ride along on a body drag. */
    groupFor(primaryId, entries) {
      if (this.selectedIds.length < 2 || !this.selectedIds.includes(primaryId)) return [];
      const out = [];
      for (const id of this.selectedIds) {
        if (id === primaryId) continue;
        const origin = this.drawings.find((d) => d.id === id);
        const entry = entries.find((en) => en.d.id === id);
        if (origin && entry && !origin.locked) out.push({ origin: { ...origin }, entry });
      }
      return out;
    }
    dragMove(pos, shiftKey) {
      const drag = this.drag;
      const updates = this.computeDragUpdates(drag, pos, shiftKey);
      if (updates.length === 0) return;
      drag.moved = true;
      const byId = new Map(updates.map((u) => [u.id, u]));
      this.drawings = this.drawings.map((d) => {
        var _a;
        return (_a = byId.get(d.id)) != null ? _a : d;
      });
      this.primitive.setDrawings(this.drawings);
    }
    computeDragUpdates(drag, pos, shiftKey) {
      if (drag.hit.region.kind !== "body") {
        const u = this.dragPoint(drag, pos, shiftKey);
        return u ? [u] : [];
      }
      const dx = pos.x - drag.startX;
      const dy = pos.y - drag.startY;
      const updates = [];
      const primary = this.shiftDrawing(drag.origin, drag.originEntry, dx, dy);
      if (!primary) return [];
      updates.push(primary);
      for (const g of drag.group) {
        const u = this.shiftDrawing(g.origin, g.entry, dx, dy);
        if (u) updates.push(u);
      }
      return updates;
    }
    dragPoint(drag, pos, shiftKey) {
      if (drag.hit.region.kind !== "point") return null;
      const pt = this.pointFor(pos, shiftKey, this.dragAnchorPx(drag));
      if (!pt) return null;
      const updated = { ...drag.origin };
      const { timeKey, priceKey } = drag.hit.region;
      if (timeKey) updated[timeKey] = pt.time;
      if (priceKey) updated[priceKey] = pt.price;
      return updated;
    }
    /** Move a whole drawing by a pixel delta (body / group drag). */
    shiftDrawing(origin, entry, dx, dy) {
      if (origin.type === "horizontal") {
        if (entry.y1 === null) return null;
        const price = this.series.coordinateToPrice(entry.y1 + dy);
        return price === null ? null : { ...origin, price };
      }
      if (origin.type === "vertical") {
        const p = this.shiftPoint(entry.x1, entry.y1, dx, 0);
        return p ? { ...origin, time: p.time } : null;
      }
      if (origin.type === "position") return this.shiftPosition(origin, entry, dx, dy);
      if (origin.type === "channel") return this.shiftChannel(origin, entry, dx, dy);
      const p1 = this.shiftPoint(entry.x1, entry.y1, dx, dy);
      const p2 = this.shiftPoint(entry.x2, entry.y2, dx, dy);
      if (!p1 || !p2) return null;
      return { ...origin, time: p1.time, price: p1.price, time2: p2.time, price2: p2.price };
    }
    // Body-drag a parallel channel: move all three anchors together.
    shiftChannel(origin, entry, dx, dy) {
      var _a;
      const p1 = this.shiftPoint(entry.x1, entry.y1, dx, dy);
      const p2 = this.shiftPoint(entry.x2, entry.y2, dx, dy);
      const p3 = this.shiftPoint(entry.x1, (_a = entry.y3) != null ? _a : entry.y1, dx, dy);
      if (!p1 || !p2 || !p3) return null;
      return {
        ...origin,
        time: p1.time,
        price: p1.price,
        time2: p2.time,
        price2: p2.price,
        time3: p3.time,
        price3: p3.price
      };
    }
    // Body-drag a position: shift the box in time and every price row together.
    shiftPosition(origin, entry, dx, dy) {
      var _a, _b, _c;
      const p = this.shiftPoint(entry.x1, entry.y1, dx, dy);
      if (!p) return null;
      const dPrice = p.price - origin.price;
      const dTime = p.time - ((_a = origin.time) != null ? _a : p.time);
      return {
        ...origin,
        time: ((_b = origin.time) != null ? _b : 0) + dTime,
        time2: ((_c = origin.time2) != null ? _c : 0) + dTime,
        price: p.price,
        stopPrice: origin.stopPrice != null ? origin.stopPrice + dPrice : void 0,
        targetPrice: origin.targetPrice != null ? origin.targetPrice + dPrice : void 0
      };
    }
    shiftPoint(x, y, dx, dy) {
      if (x === null || y === null) return null;
      return this.toPoint({ x: x + dx, y: y + dy });
    }
    endDrag() {
      const drag = this.drag;
      this.drag = null;
      this.applyCursor(null);
      if (!drag.moved) return;
      const ids = [drag.hit.id, ...drag.group.map((g) => g.origin.id)];
      for (const id of ids) {
        const d = this.drawings.find((x) => x.id === id);
        if (d) this.cb.onUpdate({ ...d });
      }
    }
    // ── Snapping (Shift = 45° angle, magnet = OHLC price) ──────────────
    /**
     * Resolve a cursor position to a data point, applying Shift angle-snap
     * (trendlines, when an opposite anchor exists) or magnet OHLC snapping.
     */
    pointFor(pos, shiftKey, angleAnchor) {
      const snapped = this.snapToNearbyAnchor(pos);
      if (snapped) return snapped;
      if (shiftKey && angleAnchor) return this.toPoint(snapAngle(angleAnchor, pos));
      const pt = this.toPoint(pos);
      if (!pt || this.magnetMode === "none") return pt;
      return this.magnetSnap(pt, pos.y);
    }
    // Snap an anchor to a nearby *other* drawing's anchor (object snapping). The
    // currently-selected drawings are skipped so dragging never snaps to itself.
    snapToNearbyAnchor(pos) {
      for (const e of this.resolveAll()) {
        if (this.selectedIds.includes(e.d.id)) continue;
        for (const c of this.anchorPoints(e)) {
          if (dist(pos, { x: c.x, y: c.y }) <= SNAP_ANCHOR_PX)
            return { time: c.time, price: c.price };
        }
      }
      return null;
    }
    anchorPoints(e) {
      const d = e.d;
      const out = [];
      if (e.x1 != null && e.y1 != null && d.time != null) {
        out.push({ x: e.x1, y: e.y1, time: d.time, price: d.price });
      }
      if (e.x2 != null && e.y2 != null && d.time2 != null && d.price2 != null) {
        out.push({ x: e.x2, y: e.y2, time: d.time2, price: d.price2 });
      }
      if (e.x3 != null && e.y3 != null && d.time3 != null && d.price3 != null) {
        out.push({ x: e.x3, y: e.y3, time: d.time3, price: d.price3 });
      }
      return out;
    }
    /** Pixel position of the first anchor while placing a trendline. */
    placementAnchorPx() {
      if (!this.placing || this.tool !== "trendline") return null;
      const x = timeToX(this.ctx(), this.placing.p1.time);
      const y = this.series.priceToCoordinate(this.placing.p1.price);
      return x !== null && y !== null ? { x, y } : null;
    }
    /** Pixel position of the anchor opposite the one being dragged. */
    dragAnchorPx(drag) {
      if (drag.origin.type !== "trendline" || drag.hit.region.kind !== "point") return null;
      const en = drag.originEntry;
      const movingP1 = drag.hit.region.timeKey === "time";
      const x = movingP1 ? en.x2 : en.x1;
      const y = movingP1 ? en.y2 : en.y1;
      return x !== null && y !== null ? { x, y } : null;
    }
    magnetSnap(pt, cursorY) {
      const candle = this.candleAt(pt.time);
      if (!candle) return pt;
      let bestPrice = pt.price;
      let bestDist = this.magnetMode === "strong" ? Number.POSITIVE_INFINITY : MAGNET_THRESHOLD_PX;
      for (const price of [candle.open, candle.high, candle.low, candle.close]) {
        const y = this.series.priceToCoordinate(price);
        if (y === null) continue;
        const d = Math.abs(y - cursorY);
        if (d < bestDist) {
          bestDist = d;
          bestPrice = price;
        }
      }
      return { time: pt.time, price: bestPrice };
    }
    /** Binary search the series data for the bar at the given unix time. */
    candleAt(time) {
      const data = this.series.data();
      let lo = 0;
      let hi = data.length - 1;
      while (lo <= hi) {
        const mid = lo + hi >> 1;
        const t = data[mid].time;
        if (t === time) return asOhlc(data[mid]);
        if (t < time) lo = mid + 1;
        else hi = mid - 1;
      }
      return null;
    }
    // ── State helpers ──────────────────────────────────────────────────
    removeSelected() {
      const ids = this.selectedIds;
      this.select([]);
      this.drawings = this.drawings.filter((d) => !ids.includes(d.id));
      this.primitive.setDrawings(this.drawings);
      for (const id of ids) this.cb.onRemove(id);
    }
    select(ids) {
      var _a, _b;
      const same = ids.length === this.selectedIds.length && ids.every((id, i) => id === this.selectedIds[i]);
      if (same) return;
      this.selectedIds = ids;
      this.primitive.setSelected(ids);
      (_b = (_a = this.cb).onSelectionChange) == null ? void 0 : _b.call(_a, [...ids]);
    }
    hoverHit(hit) {
      var _a;
      const id = (_a = hit == null ? void 0 : hit.id) != null ? _a : null;
      if (id !== this.hoveredId) {
        this.hoveredId = id;
        this.primitive.setHovered(id);
      }
      this.applyCursor(hit ? "pointer" : null);
    }
    applyCursor(interactionCursor) {
      if (interactionCursor) {
        this.container.style.cursor = interactionCursor;
        return;
      }
      this.container.style.cursor = this.tool !== "none" ? "crosshair" : "";
    }
  };

  // constants.ts
  var DRAWING_COLORS = [
    "#2196F3",
    "#f0b90b",
    "#0ecb81",
    "#f6465d",
    "#9c27b0",
    "#ff9800",
    "#787b86",
    "#ffffff"
  ];
  var DRAWING_WIDTHS = [1, 2, 3, 4];
  return __toCommonJS(entry_exports);
})();
