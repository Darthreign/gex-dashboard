/* Regroupement réglable des zones de confluence du graphique /scalp —
 * même algorithme que gex/confluence.py::cluster_levels + le filtre
 * d'affichage de app.scalp_confluence_zones, appliqué dans le navigateur aux
 * niveaux transposés envoyés par le serveur (`confluence_levels`), selon les
 * réglages du panneau ⚙ (2026-10-26) :
 *   - écart : deux niveaux voisins à au plus `gap` points rejoignent la même
 *     zone (chaînage de proche en proche) ; défaut serveur 50 NQ / 15 ES ;
 *   - minimum de niveaux par zone (défaut 2 : un niveau seul n'est pas une
 *     confluence) ;
 *   - indices inclus (SPX, NDX, QQQ, SPY, NQ, ES) ;
 *   - largeur max 3 × l'écart (une grappe chaînée de plusieurs centaines de
 *     points ne dit plus « ici ») ; zones classées CL1 (plus de niveaux) à CL10.
 * Avec les réglages par défaut, le résultat est celui du serveur.
 */
(function () {
  "use strict";
  window.GexClusterConfluence = function (levels, gap, minN, families, max) {
    const pts = (levels || [])
      .filter((l) => !families || families.includes(l.family))
      .slice().sort((a, b) => a.price - b.price);
    const zones = [];
    let cur = null, prev = null;
    pts.forEach((l) => {
      if (cur && l.price - prev <= gap + 1e-9) {
        cur.push(l);
      } else {
        cur = [l];
        zones.push(cur);
      }
      prev = l.price;
    });
    const kept = zones
      .map((z) => {
        const lo = z[0].price, hi = z[z.length - 1].price;
        return { price: (lo + hi) / 2, width: hi - lo, n: z.length,
                 names: z.map((l) => l.name).sort() };
      })
      .filter((z) => z.n >= (minN || 2) && z.width <= gap * 3 + 1e-9);
    kept.sort((a, b) => b.n - a.n);           // tri stable : à égalité, ordre des prix
    return kept.slice(0, max || 10).map((z, i) => Object.assign(z, { rank: i + 1 }));
  };
})();
