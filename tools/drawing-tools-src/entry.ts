// Point d'entrée du bundle. Expose le moteur de dessin (extrait de
// OpenCharts, MIT, https://github.com/dylanpersonguy/OpenCharts) sous
// `window.GexDrawingTools`. Le câblage (barre d'outils, persistance
// localStorage, instanciation sur le chart LW existant) reste côté
// gex/assets/*.js, en JS vanilla, pas ici.
export { DrawingToolsManager } from "./drawing-tools/manager";
export type { DrawingToolsManagerOptions } from "./drawing-tools/manager";
export { DRAWING_COLORS, DRAWING_WIDTHS } from "./constants";
export type {
  DrawingLine,
  DrawingLineStyle,
  DrawingTool,
  DrawingType,
  MagnetMode,
} from "./constants";
