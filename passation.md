# Passation — 2026-10-03, session de l'après-midi (mise à jour en cours de route, contexte long)

État au moment de l'écriture : dashboard et capture tournent proprement, aucune erreur connue en cours. Marché fermé (samedi). **Site en maintenance, personne ne l'utilise actuellement** — l'utilisateur a donné carte blanche pour avancer sans validation intermédiaire sur le chantier /scalp v2 ("ne t'arrête que si je te dis de le faire").

## À lire en premier

**La feuille de route /scalp v2 est dans la mémoire du projet, pas dans ce fichier** :
`C:\Users\sk8bo\.claude\projects\D--Gex\memory\roadmap-scalp-v2.md`

Elle a beaucoup changé depuis la version du matin — relire en entier, ne pas se fier à un résumé antérieur. Contient maintenant : routage (`/scalp` v2 remplace, `/scalpv1` = actuelle, DOIT rester vivable pendant tout le chantier — appréciée des scalpeurs testeurs), 7 pistes, audit chiffré, **le bug du spot figé à 30040** (cf. `audit-bug-spot-amplification-30040.md`), **le diagnostic HVL** (problème de design — fenêtre de cumul sur toute la séance, pas de seuil), et **le cas validé du 30/09** qui fusionne les points 1 et 2 du chantier (voir plus bas).

Autres mémoires liées :
- `chantier-scalp-sse-integral.md` — plus un chantier séparé : fondu dans la construction de la v2 (points 2-4 de la roadmap doivent être SSE-natifs dès leur écriture, pas migrés après coup).
- `chantier-analyse-pre-moc.md` — mode MOC : **carve-out explicite, à discuter avec l'utilisateur avant de coder**, même sous mandat d'autonomie général (vérifier faisabilité hedge, piste max pain à construire de zéro si retenue).
- `audit-bug-spot-amplification-30040.md` — détail du bug spot=30040.

## Travail fait cette session (dans l'ordre)

1. **Vérifié les outcomes de `scalp_signals` contre les vrais ticks** (`data/ticks/`) — ce n'était jamais fait, l'audit du matin s'était fié à la colonne `outcome` stockée.
2. **Bug trouvé** : 46/131 signaux `amplification` (29/09→01/10) ont `spot` figé à 30040.00 (repli `app.py` vers `QUOTES.last()`/`ctx["snap_spot"]` périmé sous saturation 1-thread Werkzeug). Ça gonflait artificiellement le taux de réussite d'`amplification` affiché dans l'audit du matin (64% → en fait 44,7% une fois nettoyé, identique à `unsupported`).
3. **Migration DB appliquée** (non destructive) : colonne `excluded_reason` ajoutée à `scalp_signals` (`discord_bot/journal.py`, suit le pattern `_ensure_*_columns` existant). Les 46 lignes marquées `excluded_reason='spot_fallback_30040_2026-10-03'`. Sauvegarde de la base AVANT modif : `data/journal/journal.sqlite.bak-20261003-avant-recalibrage`. Tests `.venv/Scripts/python.exe -m pytest tests/test_journal.py tests/test_resolve_scalp_signals.py` : 25/25 verts (⚠️ utiliser `.venv/Scripts/python.exe`, pas `python` nu — flask absent de l'interpréteur système).
4. **HVL recalibré — diagnostic, pas de fix appliqué** : reconstruit le volume profile réel au moment de chacune des 257 salves d'absorption depuis les ticks bruts. `delta_fraction` médian = 3,4%, 95e percentile = 19,4% — très loin du seuil `HVL_MIN_DELTA_FRACTION=0.35`. Ce n'est pas un seuil mal réglé : cumuler le delta sur TOUTE la séance lisse presque toujours le déséquilibre directionnel. Le design (fenêtre de cumul) doit changer, pas juste le seuil. **Laissé à l'utilisateur** (il compte affiner l'absorption lui-même, piste 3 de la roadmap).
5. **Seuils `unsupported`/`amplification` — analyse faite, PAS appliquée au code.** `GROSS_MIN_MUSD` plus élevé améliore réellement `amplification` (44,7%→63,6% continued au 75e percentile de flux, n=22 seulement) mais ne change rien à `unsupported` (reste ~44% peu importe le seuil — pas d'edge, le postulat contrarien n'est pas soutenu).
6. **Cas concret validé, remonté par l'utilisateur de mémoire, vérifié dans les données** : mercredi 30/09, ids 71-89 (15h25-16h04 CEST), poussée haussière réelle 30691→30871. Le bandeau dit `unsupported` quasi à chaque étape DE LA MONTÉE (alors qu'il y avait du soutien réel), puis passe à `amplification`/soutenu pile au sommet (ids 83/85/87, ~16h02-16h04) — ces 3 signaux se résolvent tous `reversed`. **Les deux lectures sont inversées aux deux moments clés.**
7. **Conclusion qui change l'ordre du chantier** : le problème n'est pas (que) les seuils, c'est que `net_musd`/`gross_musd` est calculé sur une fenêtre fixe de 5 min glissante (`TAPE.live_points`, `scalp_inputs`) qui peut capter du market-making réactif (pas une vraie conviction directionnelle). **Les points 1 (recalibrage) et 2 (swing H/L) de la roadmap sont fusionnés** — ne pas figer de seuils sur la base 5 min actuelle avant d'avoir testé des bases alternatives (barres-volume, barres-ticks, barres-range).
8. **Utilisateur a explicitement autorisé le backtest** ("le sur-apprentissage n'est pas une fatalité") et donné carte blanche totale sur les points 1-6 de la roadmap (MOC excepté, carve-out ci-dessus).

## Module `gex/bars.py` livré (soir du 2026-10-03)

**Fait et testé** : `gex/bars.py` (barres tick/volume/range + `zigzag` + `trend_move`), `tests/test_bars.py` (12 tests). Suite complète du projet : **563/563 verts** (`.venv/Scripts/python.exe -m pytest tests/`). Rien dans `app.py`/`scalp.py` n'a encore été touché — module autonome, pas branché.

**Validation faite sur le cas du 30/09 (ids 71-89, détail dans roadmap-scalp-v2.md)** :
- Win structurel net : le zigzag (toute base confondue) montre une tendance propre là où le signal 5 min disait l'inverse aux deux moments clés.
- MAIS testé plus largement sur les 119 signaux résolus du 30/09 : `swing_dir` seul prédit le signe du mouvement suivant à 38,2%, quasi identique aux 38,5% de la direction 5 min actuelle — **aucun edge seul, sur ce jour**.
- Piste qui mérite d'être creusée mais PAS validée (un seul jour, petits échantillons) : filtrer sur l'ACCORD entre swing et 5 min change les taux de réussite (ex. `unsupported` en accord : 48,2% reversed vs ~35% sans filtre). `amplification` en accord : 87,5% reversed sur seulement 16 signaux — trop peu pour conclure.

## Trouvaille structurelle (soir, après le module bars.py) — la plus solide de la session

Lecture directe de `scalp.assess()` (if/elif chain) + vérification sur les 321 lignes `unsupported` propres : **`unsupported` ne peut STRUCTURELLEMENT jamais représenter un flux qui s'oppose au mouvement** — ce cas (`flow_dir==-direction`) retourne toujours `brake`, jamais `unsupported`. Les 321 lignes `unsupported` ont 100% un flux insignifiant (`flow_dir==0`, sous les seuils), 0% un flux opposant. Donc `unsupported` ("sans soutien des dealers — extension à corriger ?", ton contrarien) ne mesure en réalité jamais "pas de soutien" — il mesure "aucune lecture de flux", qui n'a aucune raison d'avoir un edge contrarien. Ça explique le 44% continued observé = taux de base, pas un signal.

Exploré (négatif) : baisser `RATIO_MIN` pour faire émerger plus de vrais `brake`/`amplification` depuis ce pool ne valide rien de propre (sous-échantillons 21-91, résultats pas cohérents avec la thèse de `brake`). Ne pas creuser plus loin par seuils sans nouvelles données — risque de sur-apprentissage déjà signalé par l'utilisateur.

**Tranché et appliqué (soir)** : `tone` passé de `"ok"` à `"neutral"` dans `gex/scalp.py`, titre reformulé dans `gex/i18n.py` (FR/EN) pour ne plus laisser croire à un edge contrarien inexistant. Tests mis à jour. 564/564 verts.

## Routage `/scalp` vs `/scalpv1` — rendu explicite (soir, après les trouvailles brake/flux)

`/scalpv1` fonctionnait déjà par accident (`"/scalpv1".startswith("/scalp")` est vrai), donc rien ne cassait, mais c'était fragile pour la suite. Rendu explicite :
- `gex/app.py` : nouvelle fonction `is_scalp_path(path)` (juste après `_SC_KIND_COLOR`, avant `scalp_inputs`) — reconnaît `/scalp` ET `/scalpv1` explicitement. Les 3 endroits qui testaient `.startswith("/scalp")` (callback clientside JS `scalp-page`, `scalp_symbol`, `refresh_scalp`) l'utilisent maintenant.
- **Aucun changement de comportement aujourd'hui** : les deux URLs affichent toujours exactement le même rendu (pas de contenu v2 distinct pour l'instant) — c'est juste le point de branchement qui est prêt pour quand `/scalp` divergera.
- Test ajouté : `tests/test_scalp_banner.py::test_is_scalp_path_reconnait_v2_et_v1`.
- Suite complète : 563/563 verts après ce changement (`.venv/Scripts/python.exe -m pytest tests/`).

**Autre ajout en mémoire (soir)** : piste de Noé (Discord 01/10) — widgets de page ajoutables/enlevables/redimensionnables (ex. graphiques empilés verticalement), distincte de la personnalisation par identité (Cloudflare). Les deux sont maintenant pistes 6 et 7 séparées dans roadmap-scalp-v2.md (renumérotées, MOC et indicateurs maison décalés à 8/9).

## Bug spot=30040 — CORRIGÉ dans le code (soir, pas juste documenté)

En retravaillant `refresh_scalp` pour le routage, je suis retombé sur le code exact du bug diagnostiqué plus tôt (`gex/app.py`, maintenant ~ligne 2762). **Corrigé** : `spot` utilise maintenant `_futures_last_price()` (tick-accurate, gex/api.py — même source que la résolution des outcomes côté scheduler) comme source primaire, repli sur `QUOTES.last()` puis `ctx["snap_spot"]` seulement si `None`. Avant : `QUOTES.last()` (conflaté) en premier avec repli direct sur un snapshot pouvant être périmé — exactement le chemin qui avait pollué 46 signaux fin septembre/début octobre. Tests : 564/564 verts (`.venv/Scripts/python.exe -m pytest tests/`). Détail dans `audit-bug-spot-amplification-30040.md`.

Ne règle pas tout (la cible reste "tout vient de la capture", cf. chantier SSE), mais élimine la cause immédiate sans attendre cette migration plus large.

## `gex/bars.py` complété : `swing_move()` prêt à l'emploi pour le bandeau v2

Ajout d'une fonction de convenance qui assemble `volume_bars`+`zigzag`+`trend_move` en un seul appel (`bars.swing_move(ticks, current_price, bar_volume=60.0, min_move=15.0)`) — c'est l'équivalent swing-ancré de ce que `scalp_inputs` fait avec la bougie 5 min dans `gex/app.py`. Pas encore appelé nulle part en production (pas de bandeau v2 à qui le donner) — prêt pour quand ce bandeau existera. Défauts (60/15) choisis par cohérence avec la validation du cas du 30/09, pas calibrés formellement. Tests ajoutés, suite complète 566/566 verte.

## Prototype Lightweight Charts — validé visuellement (soir)

Socle de la piste 4 (graphique TradingView) testé hors Dash, hors production (jamais touché le port 8050 de la tâche planifiée — port déjà occupé, deux process dessus d'ailleurs, pas investigué, ne pas y toucher sans comprendre pourquoi il y en a deux). Prototype HTML autonome + serveur statique jetable (arrêté après coup), données réelles du cas 30/09 exportées depuis `gex/bars.py`.

**Résultat : ça marche**, confirmé visuellement dans le navigateur intégré (bougies + pivots swing H/L correctement superposés).

**Piège trouvé, à ne pas refaire lors de la vraie intégration** : `createChart()` sur un conteneur en `100vw/100vh` sans `width`/`height` explicites capture un canvas à largeur 0 — invisible, SANS aucune erreur JS. Fix : passer les dimensions mesurées explicitement + listener `resize`. Détail complet dans roadmap-scalp-v2.md.

Pas encore fait : l'intégration réelle dans `gex/app.py` (composant Dash, mise à jour live via `series.update()` plutôt que `setData()` à chaque cycle, thème clair/sombre, remplacement de `scalp_price_fig`). Le prototype de ce soir valide l'approche, pas encore l'implémentation en place.

## Lightweight Charts INTÉGRÉ (pas juste prototypé) et vérifié EN LIVE

Suite du prototype : câblé pour de vrai dans `gex/app.py` (`scalp_v2_chart_data`, carte `#scalp-lw-card`, classe `body.scalp-v2-page`, callback `refresh_scalp_lw`, clientside callback de rendu, lib vendue dans `gex/assets/`). Détail complet dans roadmap-scalp-v2.md.

**Dashboard redémarré proprement pour charger le code** (`Stop-ScheduledTask "GEX dashboard"` → tuer les 2 PID pythonw restants → `Start-ScheduledTask`, procédure de `redemarrer-dashboard-tuer-pythonw.md`). Un seul listener sur 8050 après coup (le PID Tailscale sur le port est normal, déjà là avant, sans rapport). **Vérifié dans le navigateur** : `/scalp` affiche la nouvelle carte (vide, normal un samedi sans tick), `/scalpv1` affiche toujours l'ancienne figure Plotly intacte. Zéro erreur console, zéro erreur serveur sur plusieurs cycles.

**Pas encore fait** : mise à jour incrémentale (`series.update()`), test en séance réelle (marché ouvert).

## Bandeau swing câblé sur /scalp v2 (soir, après le graphique)

Le bandeau de `/scalp` utilise maintenant `scalp_inputs_swing` (mouvement swing-ancré) au lieu de `scalp_inputs` (fenêtre 5 min) — `scalp_banner(..., swing=True)` seulement sur `/scalp` exact, `/scalpv1` inchangée. Détail complet dans roadmap-scalp-v2.md : `(symbole, basis)` au lieu de juste `symbole` pour le dédup journal (évite que deux onglets se marchent dessus), nouvelle colonne `basis` en base, 4 nouveaux tests, 573/573 verts, dashboard redémarré et vérifié sans erreur.

**Limite connue, à vérifier lundi** : impossible de voir le bandeau swing avec de vraies données ce soir (`ctx is None` le samedi, pas de snapshot NQ frais → `refresh_scalp` s'arrête avant `scalp_banner` quel que soit `swing`). La logique est vérifiée par test bout-en-bout, pas par un oeil humain sur des vraies données de marché.

## Niveaux GEX/HVL/Flip sur le graphique v2 (soir, après le bandeau swing)

`scalp_v2_chart_data` renvoie maintenant aussi `levels` (en plus de `candles`/`markers`), dessinés en `priceLine` sur le graphique. `_scalp_live_spot` factorisée (le fix du bug 30040, un seul endroit). 573/573 verts, 3e redémarrage du dashboard ce soir, vérifié : 11 price lines créées côté `/scalp` (correspond à l'échelle affichée), invisibles seulement parce qu'il n'y a aucune bougie pour ancrer l'échelle un samedi — normal, pas un bug. `/scalpv1` revérifiée intacte.

## Pour reprendre si la session s'arrête ici

1. `gex/bars.py` existe et fonctionne — ne pas le recréer, l'étendre.
2. **Prochaine étape logique** : refaire la comparaison accord/désaccord swing vs 5 min sur les 4 jours (29/09→02/10), pas juste le 30/09, avant de songer à toucher `scalp.py`. Scripts de référence (non committés, dans le scratchpad de cette session, à reproduire si besoin) : construction de barres-volume=60 sur une journée entière + comparaison avec `scalp_signals.direction`/`outcome`.
3. Si la validation 4 jours confirme la piste "accord swing/5min" : proposer une implémentation dans `scalp.py`/`app.py`, en gardant `/scalpv1` intacte (nouveau code isolé, pas de refactor partagé prématuré).
4. Ne pas committer/pousser sans vérifier `git status` d'abord — plusieurs fichiers non suivis existent déjà en dehors de ce chantier (`Env.txt`, `GEX_formules.pdf`, `icone.png`, `scripts/nightly_migrate.py`, etc., présents dès le début de session, pas liés à ce travail). Rien n'a encore été commité par cette session.
5. Mode MOC (point 5) : ne pas commencer sans en reparler avec l'utilisateur, même en autonomie.
6. Chantier SSE : pas une étape séparée, cf. ci-dessus — s'applique au fur et à mesure de la construction de la v2, pas en bloc à part.
7. **L'utilisateur a dit de ne pas s'arrêter sauf instruction explicite** — cette mise à jour de passation est un checkpoint de sécurité (contexte long), pas une pause. Continuer sur le point 2 ci-dessus si la session reprend sans nouvelle instruction.
