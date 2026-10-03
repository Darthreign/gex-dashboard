// Extrait minimal de src/pages/trading/constants.ts (OpenCharts,
// https://github.com/dylanpersonguy/OpenCharts, MIT) — uniquement les types
// et constantes dont le moteur de dessin (drawing-tools/*) a besoin. Le reste
// de ce fichier (couleurs de thème, devises, timeframes de leur app) est
// volontairement omis : non utilisé par le moteur.

/**
 * Armable tools. Some are placement-only aliases that commit a different stored
 * `type`: "ray"/"extended" store a "trendline" with extend flags;
 * "long-position"/"short-position" store a "position" with `side`; "measure" is
 * a transient gesture that never commits.
 */
export type DrawingTool =
  | "none"
  | "trendline"
  | "horizontal"
  | "fibonacci"
  | "rectangle"
  | "vertical"
  | "ray"
  | "extended"
  | "channel"
  | "text"
  | "fibextension"
  | "ellipse"
  | "arrow"
  | "triangle"
  | "position"
  | "long-position"
  | "short-position"
  | "measure";

/** Stored drawing kinds (excludes placement-only aliases). */
export type DrawingType = Exclude<
  DrawingTool,
  "none" | "ray" | "extended" | "long-position" | "short-position" | "measure"
>;

export type DrawingLineStyle = "solid" | "dashed" | "dotted";

/** Magnet snapping mode: off, weak (snap within a few px), or strong (always). */
export type MagnetMode = "none" | "weak" | "strong";

export interface DrawingLine {
  id: string;
  type: DrawingType;
  price: number;
  price2?: number;
  time?: number;
  time2?: number;
  /** Third anchor — parallel-channel offset line. */
  price3?: number;
  time3?: number;
  color: string;
  // v2 style/behavior fields — all optional so v1 drawings load unchanged
  /** Line width in px (default 2). */
  width?: number;
  lineStyle?: DrawingLineStyle;
  /** Locked drawings can be selected but not moved or resized. */
  locked?: boolean;
  /** Trendline only: extend the line to the pane edges. */
  extendLeft?: boolean;
  extendRight?: boolean;
  /** Hidden drawings stay in the object tree but don't render or hit-test. */
  hidden?: boolean;
  /** Render order — higher draws on top. Defaults to creation order. */
  zIndex?: number;
  /** Timeframe the drawing was created on (used by visibility "tf"). */
  createdTf?: string;
  /** "all" (default) shows on every timeframe; "tf" only on createdTf. */
  visibility?: "all" | "tf";
  // ── Styling depth (Tier 4) ──
  /** Fill colour for shapes / channel / fib bands (defaults to `color`). */
  fillColor?: string;
  /** Fill opacity 0–1 (defaults per drawing kind). */
  fillOpacity?: number;
  /** Arrowheads on line ends (trendline / arrow). */
  arrowStart?: boolean;
  arrowEnd?: boolean;
  /** Text content + size for text/callout drawings and attachable labels. */
  text?: string;
  fontSize?: number;
  /** Bold / italic styling for text drawings. */
  bold?: boolean;
  italic?: boolean;
  /** Optional text-box background (text drawings). */
  textBg?: boolean;
  textBgColor?: string;
  /** Optional text-box border (text drawings). */
  textBorder?: boolean;
  textBorderColor?: string;
  /** Custom fibonacci levels (fractions); defaults applied when absent. */
  fibLevels?: number[];
  // ── Position tool (long/short risk-reward) ──
  side?: "long" | "short";
  /** Stop-loss price (position tool). */
  stopPrice?: number;
  /** Take-profit price (position tool). */
  targetPrice?: number;
  /** % of account equity risked — drives size/$ readout (default 1). */
  riskPct?: number;
  // ── Price alerts on lines ──
  /** When set, the platform alerts when price crosses this line. */
  alertEnabled?: boolean;
  alertMessage?: string;
}

/** Swatch palette for the floating drawing toolbar (TradingView-style). */
export const DRAWING_COLORS = [
  "#2196F3",
  "#f0b90b",
  "#0ecb81",
  "#f6465d",
  "#9c27b0",
  "#ff9800",
  "#787b86",
  "#ffffff",
] as const;

export const DRAWING_WIDTHS = [1, 2, 3, 4] as const;
