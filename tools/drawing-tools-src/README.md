# Moteur de dessin — extrait d'OpenCharts

Source : https://github.com/dylanpersonguy/OpenCharts (MIT). Lightweight
Charts lui-même (dépendance de ce moteur, déjà vendue dans
`gex/assets/lightweight-charts.standalone.production.js`) est Apache 2.0
et exige une attribution visible du lien TradingView — conservée dans
l'interface (lien existant dans le projet).

Ce dossier ne contient QUE la couche "outils de dessin" (manager
d'interaction + primitive de rendu Lightweight Charts) d'OpenCharts, extraite
le 2026-10-04 : trendline, horizontale, verticale, rayon, canal parallèle,
rectangle, ellipse, fibonacci, texte, position long/short, mesure — avec
aimant OHLC, snapping entre objets, multi-sélection, copier/coller, raccourcis
clavier (Alt+T/H/F/R, Ctrl+Z/Y, Suppr, Échap). Le reste d'OpenCharts (terminal
de trading React complet, paper trading, datafeed Binance) n'est **pas**
repris — seuls ces fichiers, sans dépendance React (vérifié : aucun import
`react` dans le moteur).

Fichiers copiés tels quels depuis OpenCharts (seul le chemin d'import vers
`constants.ts` a été réécrit) :
`plugin-base.ts`, `helpers/assertions.ts`, `drawing-tools/{manager,
drawings-primitive,renderers,resolve,hit-test,geometry,types}.ts`.
`constants.ts` est un extrait (types `DrawingLine`/`DrawingTool`/... + 2
constantes de palette), pas le fichier complet d'OpenCharts (375 lignes,
le reste concerne leur app — devises, timeframes, thèmes — sans rapport).

`entry.ts` est écrit pour ce projet (pas dans OpenCharts) : réexporte juste
la surface utile sous `window.GexDrawingTools`.

## Build

Ce dossier n'est PAS une dépendance runtime de l'app (pas de `npm install`
nécessaire pour faire tourner le dashboard) — seulement pour régénérer le
bundle vendu dans `gex/assets/gex-drawing-tools.js` après une modification
ici :

```bash
cd tools/drawing-tools-src
npm install
npm run build
```

Le câblage (barre d'outils, persistance localStorage par symbole,
undo/redo) est écrit directement en JS vanilla dans le clientside_callback
de `gex/app.py` (pas ici) — voir le commentaire "Outils de dessin" dans la
callback qui crée `window._gexLwChart`.
