# Passation — chantier extendTraces (graphes live qui gardent le zoom)

**État au 2026-09-29 ~14h45 Paris : tentative interrompue, code DÉSACTIVÉ,
dashboard restauré en état sain (rendu Python normal, testé, déployé).**
Ne PAS réactiver `const live = false && ...` sans relire ce fichier en entier.

## Ce qui a été tenté

Backend construit et testé (garde en place, inoffensif tant que non appelé) :
- `gex/api.py` : route SSE `/api/v1/<symbol>/hedge_stream`, pousse les
  nouveaux points de `flowtape.TAPE.live_points` (commentaire `: ouverture`
  immédiat + heartbeat 15s ajoutés en cours de route, cf. plus bas).
- `gex/scalp.py` : `hedge_live_key`/`should_render_hedge` (pure, testées) —
  décident quand Python doit reconstruire la figure vs s'effacer.
- `gex/app.py` `refresh_scalp` : la logique de saut (`no_update` en mode live
  déjà rendu) a été ÉCRITE puis RETIRÉE (cf. « État actuel » plus bas) — le
  rendu Python est repassé à INCONDITIONNEL pour la courbe de couverture.
- `gex/app.py` clientside_callback (sink `scalp-hedge-stream-sink`) : ouvre
  un EventSource sur `/hedge_stream` et fait `Plotly.extendTraces` — ÉCRIT
  mais NEUTRALISÉ par `const live = false && ...` (code gardé pour reprise,
  jamais exécuté).

## Deux bugs RÉELS trouvés, à corriger avant de réessayer

1. **Mauvaise cible DOM (confirmé par inspection directe).** Dans Dash,
   `dcc.Graph(id="scalp-hedge")` crée un DIV WRAPPER portant cet id — l'objet
   Plotly réel (`.data`, l'API `Plotly.extendTraces` etc.) vit sur un enfant
   `.js-plotly-plot` À L'INTÉRIEUR. `document.getElementById('scalp-hedge')`
   ne porte PAS `.data`. Le code désactivé ciblait directement l'id — à
   corriger : `document.getElementById('scalp-hedge').querySelector('.js-plotly-plot')`.
   Vérifié en direct :
   ```js
   document.getElementById('scalp-hedge').querySelector('.js-plotly-plot').data.length // => 5, marche
   document.getElementById('scalp-hedge').data // => undefined
   ```

2. **Le flux SSE `/hedge_stream` ne délivrait aucune donnée en test réel**
   (readyState resté à `0` CONNECTING plusieurs secondes, malgré du flux
   d'options réel confirmé par l'utilisateur au même moment). Cause NON
   confirmée. Un correctif a été ajouté (commentaire `: ouverture\n\n` émis
   immédiatement à l'ouverture + heartbeat `: ping` toutes les 15 s, cf.
   `gex/api.py` `_hedge_stream`) pour éviter qu'EventSource reste bloqué en
   CONNECTING tant qu'aucun point n'arrive — mais ce correctif n'a PAS été
   revérifié en conditions réelles avant l'arrêt du chantier (voir ci-dessous
   pourquoi). À valider en premier à la reprise, AVANT de réactiver le JS.

## Une fausse piste écartée, à ne pas re-suivre

Pendant le test, une tempête `ERR_CONNECTION_REFUSED`/`ERR_CONNECTION_RESET`
est apparue dans la console du navigateur, y compris APRÈS avoir désactivé le
déclenchement JS (`live = false`) — ce qui semblait indiquer une instabilité
du serveur de dev Flask/Werkzeug avec plusieurs connexions SSE permanentes.
**Écarté** : `curl` vers `http://127.0.0.1:8050/` a répondu 200 OK sans
interruption pendant TOUTE la période où le navigateur affichait ces erreurs,
et fermer/rouvrir le pane du navigateur intégré (outil de test, pas le
dashboard) a fait disparaître les erreurs instantanément. Conclusion la plus
probable : instabilité du PANE DE NAVIGATEUR DE TEST lui-même (après une
succession rapide de fermetures/ouvertures d'onglets), pas du dashboard. Le
dashboard n'a jamais cessé de répondre correctement selon `curl`.
**Mais** : ça n'exclut pas totalement un vrai souci de concurrence côté
Werkzeug avec 2 connexions SSE permanentes par onglet (prix + couverture) —
à surveiller quand même à la reprise, avec un test PLUS PROPRE (onglet frais,
pas de churn d'onglets pendant le test, DevTools réseau natif si besoin
plutôt que l'outil de navigateur intégré).

## État actuel du code (2026-09-29, redéployé et vérifié sain)

- `refresh_scalp` (gex/app.py) : rendu Python INCONDITIONNEL du graphe de
  couverture (comportement normal d'avant ce chantier — le zoom se
  réinitialise à chaque cycle de 2 s, MAIS les données s'affichent
  correctement, ce qui est le point non négociable).
- clientside_callback du flux de couverture : présent mais `live = false &&
  ...` — n'ouvre jamais de connexion.
- Route `/hedge_stream`, `scalp.hedge_live_key`/`should_render_hedge` :
  présentes, testées, inoffensives (jamais appelées par le client).
- Vérifié en direct après redéploiement : prix live OK, graphe de couverture
  se remplit normalement au fil des cycles Python, aucune erreur console,
  514 tests passent.

## Pour reprendre proprement

1. Corriger le bug DOM (point 1 ci-dessus) dans le JS déjà écrit.
2. Retester `/hedge_stream` SEUL (sans le JS extendTraces, juste avec
   `curl -N http://127.0.0.1:8050/api/v1/NQ/hedge_stream` pendant que le
   marché a du flux réel) pour confirmer que le correctif de heartbeat
   règle bien le problème de connexion bloquée, AVANT de toucher au JS.
3. Réactiver `live = ...` (enlever le `false &&`) seulement après 1 et 2
   validés.
4. Reprendre la checklist de vérification complète (zoom, pan, légende,
   changement de symbole, reconnexion) — pas encore faite du tout,
   le chantier s'est arrêté avant.
5. Envisager aussi la courbe de couverture en modes 15/30 min et les
   bougies du sous-jacent — non commencés.

## Rappel sans lien avec ce chantier

Rappel programmé à 23h10 (Paris, 29/09, pause CME) pour redémarrer la
capture et vérifier le recalibrage de l'absorption + le flux poussé du prix
(déjà en production, fonctionne). Ne pas confondre avec ce fichier.

## Après reprise complète

Supprimer ce fichier une fois le chantier terminé et validé. Mettre à jour
`chantier-extendtraces-graphes-live.md` dans la mémoire du projet.
