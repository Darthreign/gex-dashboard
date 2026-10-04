# Passation — 2026-10-04, avant compactage

État au moment de l'écriture : dashboard et capture tournent proprement, dernier redémarrage vérifié sans erreur (`/scalp` et `/scalpv1` revérifiées en direct). Marché fermé (dimanche). Tout le travail de cette session est **commité et poussé** (`git log` : de `5348c9c` à `cc86462`, 15 commits). `git status` propre — seuls des fichiers non suivis préexistants traînent (`Env.txt`, `GEX_formules.pdf`, `icone.png`, `scripts/nightly_migrate.py`, `gex/scheduler.py.bak-20260818`, `graphify-out/`, `.claude/settings.json`), présents avant cette session, sans rapport.

## À lire en premier

**La feuille de route /scalp v2 est dans la mémoire du projet, pas dans ce fichier** :
`C:\Users\sk8bo\.claude\projects\D--Gex\memory\roadmap-scalp-v2.md`

Ce fichier-ci est un résumé d'orientation de **cette session** (2026-10-04, journée) — la session précédente (nuit du 03/10) a sa propre trace dans l'historique git et dans la roadmap. Mémoires liées : `audit-bug-spot-amplification-30040.md`, `chantier-analyse-pre-moc.md` (MOC, carve-out explicite).

## État de /scalp v2 — ce qui tourne réellement en production

`/scalpv1` n'a **jamais régressé** à aucune étape de cette session — revérifié en direct après chaque redémarrage (une douzaine), toujours Plotly inchangée.

Sur `/scalp` (v2), livré et vérifié en live cette session :

**Outils de dessin** (moteur extrait d'OpenCharts, MIT — `github.com/dylanpersonguy/OpenCharts`) : tendance, horizontale, verticale, rayon, canal, rectangle, ellipse, Fibonacci, texte, aimant OHLC, annuler/rétablir, tout effacer. Tracés persistés en localStorage par symbole. Compilé en bundle vanilla (`gex/assets/gex-drawing-tools.js`, sources dans `tools/drawing-tools-src/`). **Pas fait** : dialogue de style (couleur/épaisseur par défaut), outil position long/short (jugé inutile par l'utilisateur — "on est des scalpeurs").

**Widgets déplaçables/redimensionnables** (GridStack.js, MIT, `gex/assets/gridstack-all.js`) : remplace les anciens dropdowns "Ordre". 4 widgets (niveaux/graphique/couverture/prints) glissables par poignée, redimensionnables par le coin. Masquage via `removeWidget`/`makeWidget` (API v11+, jamais `display:none` — laisserait un trou dans la grille). Disposition sauvegardée comme les autres préférences (localStorage + serveur si identifié). **Pas testé à la souris par l'utilisateur** (testé par API de mon côté, l'utilisateur a dit "je ferai les tests plus tard").

**Indicateurs du graphique, 6 bascules individuelles** (bouton dans la barre, retour visuel instantané, persisté) :
- Niveaux GEX/HVL/Flip/murs
- Confluence multi-familles
- Order flow (ancien, zones HVL cumulées sur la séance — pas retiré, voir plus bas)
- **Profil de GEX par strike** (porté de la page heatmap — OI + volume du jour superposés, seule vraie fonctionnalité de la heatmap qui manquait ici)
- Swing H/L (pivots zigzag)
- **Profil de volume par jambe de swing** (nouveau, voir ci-dessous)

**Profil order-flow par jambe de swing** (`scalp_orderflow_profile`/`scalp_orderflow_untested`) — premier jalon du chantier "order flow sur Lightweight Chart" demandé explicitement par l'utilisateur, qui absorbe aussi le recalibrage HVL (même mesure, mieux ancrée que le cumul sur toute la séance déjà diagnostiqué cassé) :
- **2 jambes pleines** (en cours + dernière confirmée, PAS que celle qui se dessine — sinon le contexte d'un retracement se perd) avec POC/VAH/VAL (zone de valeur à 70%, définition standard) et delta acheteur/vendeur par palier. Fenêtré à 90 min (rapide, ces 2 jambes sont par nature récentes).
- **Zones HVN/LVN non revisitées** (High/Low Volume Node — PAS "HVL", qui désigne autre chose dans ce projet, gex/iceberg.py ; renommé après une remarque directe de l'utilisateur). Scan sur la **séance complète** en arrière-plan (son propre cache 60s, pas 10s), affichage plafonné aux 20 plus récentes (le scan complet remontait 150+ zones, illisible sur un graphique).
- Rendu via une nouvelle primitive Lightweight Charts (`gex/assets/gex-orderflow-profile.js`), ancrée sur la plage temporelle de chaque jambe (pas tout le pane, contrairement au profil gamma).
- **Pas fait** (annoncé comme prochain jalon par l'utilisateur) : bulles de delta.

**Moteur planifié + poussée SSE** (demande explicite : "un moteur qui les calcule et remplit un fichier de données envoyé à tous les onglets", "ça peut tourner sur la plage complète en arrière-plan") :
- `_refresh_scalp_indicators` : scheduler APScheduler DÉDIÉ (8s, séparé de `gex/scheduler.py` qui pilote l'ingestion critique), rafraîchit en continu niveaux/confluence/order-flow/profils pour NQ/ES. Les requêtes ne font plus jamais que LIRE un cache chaud.
- `/api/v1/<symbol>/scalp-indicators-stream` (SSE, même principe que le ticker de prix) : pousse dès que ça change, plus de polling ~1s par onglet pour la partie PARTAGÉE entre tous les traders sur un symbole. Les bougies (par TF, propres à chaque onglet) restent sur le canal Dash existant.
- Démarré depuis `gex/run.py::main()`, PAS depuis `create_app()` (comme `start_scheduler` — les tests construisent l'app en boucle sans vouloir de vrai travail de fond).

**Préférences par utilisateur via Cloudflare Access** — confirmé en production (connexion par code à usage unique par email, header `Cf-Access-Authenticated-User-Email` bien reçu, testé en vrai par l'utilisateur) :
- Table `user_prefs` dans le même journal SQLite que le bot Discord (`discord_bot/journal.py`), routes `/api/v1/prefs` (GET/POST).
- Scope actuel : disposition verticale, ergonomie/mots-clés, disposition GridStack. Sans identité (accès local) : repli silencieux sur localStorage, comportement inchangé.
- Panneau "⚙ Personnalisation" replié en **menu déroulant** (icône à côté de dxFeed) au lieu d'un panneau toujours ouvert — demande explicite, gagne de la place verticale.

**Fuseau horaire des graphiques** : corrigé — Lightweight Charts n'utilise PAS le fuseau du navigateur par défaut (hypothèse fausse de la session précédente), formate en UTC pur sauf formateur explicite. `tickMarkFormatter`/`localization.timeFormatter` ajoutés sur les deux graphiques.

**Serveur** : waitress (pool de 48 threads) au lieu de Werkzeug mono-thread. `threaded=True` (Werkzeug) définitivement écarté — spawn un thread par connexion sans limite, risque de contention GIL sous charge CPU, confirmé pire en direct deux fois.

Suite de tests : **578/578 verts**. Un test (`test_graphe_sous_jacent_comble_le_trou_si_le_disque_a_du_retard`) reste **flaky** (frontière de minute), repasse seul en isolation à chaque fois — pas lié au code de cette session.

## Bugs trouvés et corrigés cette session

1. **Epoch divisé par 1000** (bougies datées 21/01/1970) : `.astype("int64")` sur une Series datetime tz-aware suppose des nanosecondes, mais pandas 3.0.5 ici produit des microsecondes. `_epoch_seconds()` lit la résolution réelle (`ts.dt.unit`) au lieu de la supposer.
2. **Reflow CSS cassé** : `.sc-grid` utilisait des `grid-template-areas` nommées — masquer un bloc laissait un trou au lieu de faire remonter le reste. Passé en auto-placement pur.
3. **Graphique ne suivait pas le redimensionnement** : `.sc-lw-chart` avait une hauteur fixe en px au lieu de `flex:1 1 auto` — un `ResizeObserver` sur le conteneur (pas juste `window.resize`) + le flex fix règlent le problème pour GridStack.
4. **Panne serveur critique** (~10h03) : pool waitress à 8 threads épuisé par le flux SSE du ticker de prix (chaque onglet garde un thread ouvert en permanence). Relevé à 48 — les threads SSE sont endormis l'essentiel du temps (I/O), pas de risque de contention GIL contrairement à `threaded=True` Werkzeug.
5. **`scalp_orderflow_profile` trop lent pour le moteur planifié** : `volume_bars()` sur la séance entière (jusqu'à 1,4M ticks pour ES) prenait 6-8s. Fenêtré à 90 min pour les jambes "pleines" ; le scan complet (zones non testées) déplacé dans une fonction séparée avec son propre cache 60s.
6. **En-tête Cache-Control manquant** sur l'export PNG des graphiques (`/api/v1/<symbol>/chart/<name>.png`) — un graphique qui change en continu ne doit jamais être mis en cache par un intermédiaire.
7. **HVN et LVN affichés au même prix** (contradictoire) — déduplication par (prix, nature) au lieu de prix seul, laissait passer les deux labels empilés au même niveau quand ils venaient de jambes différentes. Corrigé : dédup par prix seul + espacement minimum (2x la taille de palier) entre zones gardées, pour éviter l'effet escalier des jambes courtes/serrées.

## Décisions explicites de l'utilisateur, pas encore exécutées

- **MOC** : carve-out confirmé, à démarrer seulement une fois l'order-flow sur Lightweight Chart opérationnel (dit explicitement par l'utilisateur ce jour).
- **HVL / recalibrage des seuils d'absorption** : même chantier que l'order-flow sur Lightweight Chart (le profil par jambe remplace la mesure cumulée sur séance, diagnostiquée cassée) — pas un chantier séparé.
- **GoCharting/TradingView Advanced Charts** : abandonné — "pas utile si Lightweight Chart suffit", confirmé par l'utilisateur maintenant que les outils de dessin + les profils sont en place.
- **Bulles de delta** : prochain jalon annoncé du chantier order-flow, pas commencé.

## Pour reprendre proprement

1. **Lire `roadmap-scalp-v2.md`** pour le contexte de fond (sessions précédentes).
2. **Vérifier en séance réelle (lundi, marché ouvert)** : tout ce qui n'a pu être testé qu'avec des données historiques/weekend — bandeau swing, profils order-flow, moteur planifié sous vraie charge, flux SSE avec plusieurs onglets réels.
3. **Glisser-déposer GridStack à la souris** : testé par API seulement, l'utilisateur doit confirmer que ça glisse bien en vrai.
4. **Ne jamais committer sans `git status` d'abord** — fichiers non suivis préexistants à ne pas embarquer.
5. **Procédure de redémarrage** : `Stop-ScheduledTask "GEX dashboard"` → tuer les PID `pythonw run.py` restants (`Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where CommandLine -like "*run.py*"` puis `Stop-Process`) → `Start-ScheduledTask "GEX dashboard"` → attendre 20-25s avant de vérifier le listener sur le port 8050 → `Get-NetTCPConnection -LocalPort 8050 -State Listen` doit donner **un seul** process sur `127.0.0.1` (le PID Tailscale est normal, sans rapport). Après redémarrage, un onglet FRAIS (pas réutilisé) pour vérifier la console — un onglet réutilisé across plusieurs redémarrages accumule des erreurs `ERR_CONNECTION_RESET`/`REFUSED` qui sont des résidus, pas de vrais bugs.
6. **Disque C: surveillé** : était à 100% plein ce jour (0 octet libre), nettoyé par l'utilisateur (fichiers de cache/update Windows, PAS lié au dashboard). Repasser un œil si ça recommence.
7. **MOC et recalibrage HVL** : ne rien commencer sans l'utilisateur, cf. ci-dessus.
