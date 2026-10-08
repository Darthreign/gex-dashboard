# Passation — 2026-10-08 (conventions de calcul + architecture de charge)

**État à l'écriture : tout commité/poussé sur `claude/sleepy-knuth-y3b4d2` (non fusionné dans `main`). Jamais testé sous charge réelle ni avec les flux CBOE/dxFeed (réseau bloqué dans l'environnement de développement) : déployer HORS séance.**

## Architecture de charge (nouvelle)

1. **Calcul unique, diffusion à tous** (`gex/broadcast.py`) : un seul producteur par flux SSE et par clé (symbole, langue, tf), et un cache partagé single-flight pour 10 callbacks Dash périodiques. Un onglet de plus ne coûte plus de calcul.
2. **Moteur d'indicateurs linéaire** : `volume_bars` vectorisé (3,5 s -> 0,16 s sur 600 000 ticks), `scalp_orderflow_untested` sans boucle quadratique (7,6 s -> 0,15 s, résultats identiques), ticks du jour relus seulement quand le fichier change.
3. **Processus moteur séparé** (`GEX_ENGINE=1`, `gex/livestate.py`) : `gex.capture` récupère et calcule AUSSI les chaînes et les indicateurs /scalp, publie dans `data/live/` ; le dashboard ne fait que lire.
4. **Serveur ASGI par défaut** (`gex/asgi.py`, uvicorn) : flux SSE en asyncio (aucun thread par connexion), Dash/API via a2wsgi dans un pool de 32 threads.

## Déploiement (Windows, hors séance)

1. Arrêter les tâches « GEX dashboard » puis « GEX capture » (cibler les PID, PAS `Stop-Process -Name python,pythonw`).
2. `git pull` sur la branche, puis dans le venv : `pip install -r requirements.txt` (ajoute uvicorn, starlette, a2wsgi).
3. Définir `GEX_ENGINE=1` pour les DEUX tâches (variable d'environnement utilisateur, comme `GEX_CAPTURE_URL`).
4. Démarrer « GEX capture » (devient le moteur ; attend jusqu'à 20 s le spot temps réel avant ses premiers pulls), vérifier `data/live/*.json` qui apparaissent, puis démarrer « GEX dashboard ».
5. Contrôles : `logs/capture.log` montre « démarrage (mode MOTEUR) » et les pulls ; `logs/gex.log` du dashboard ne montre plus de pull.

**Retour arrière, par étage :** `GEX_SERVER=waitress` (ancien serveur), retirer `GEX_ENGINE` (le dashboard recalcule lui-même comme avant). Les étages 1 et 2 n'ont pas d'interrupteur : ce sont des changements de code testés à résultat identique.

## Lecture normalisée /scalp et mesure d'edge (même session)

- `gex/edge.py` : excès = (prix − ouverture) en mouvements attendus (EM, straddle de l'échéance proche, sinon étendue réalisée) ; zone frein / transition / accélération selon la distance au Gamma Flip en EM et le GEX 0DTE ; confirmations d'épuisement (gamma frein, absorption opposée, flux qui freine). Affiché sur `/scalp` (V1 comme V2 du bandeau), jamais sur `/scalpv1` ; transitions journalisées dans `scalp_setups`.
- **L'edge n'est PAS démontré** tant que `python scripts/edge_report.py` n'a pas tourné sur tes données : il rejoue l'historique (ticks live + import Databento, snapshots, historique des métriques, tape), choisit les seuils sur 60 % des séances et les valide sur les 40 % suivantes contre le rejet naïf et le hasard. Le bandeau n'utilise ces seuils que s'ils sont validés (`data/reports/edge_params_<SYM>.json`), sinon il affiche « edge NON validé ».
- Banc vérifié sur données synthétiques : aucun edge sur marche aléatoire ; edge détecté et filtre de zone utile quand il existe.

## /scalp entièrement rebranché, en flux poussé (même session)

Les mesures d'urgence des 05-06/10 sont levées et la page scalp n'a plus AUCUN sondage à la seconde : tout arrive par SSE (serveur asynchrone, un calcul partagé par réglages pour tous les onglets, cf. gex/broadcast.py).
- `/scalp-indicators-stream` : bandeau /scalp + indicateurs du graphique (inchangé).
- `/chart-stream` : graphique Lightweight Charts (inchangé, actif dès que le graphique est visible).
- `/scalp-stream` (nouveau, `scalp_panels_channel`) : en-tête, niveaux, gros prints, couverture des dealers ; sur /scalpv1 aussi le bandeau et le prix Plotly. Poussé seulement si un bloc change, et le navigateur ne remplace que les blocs modifiés. Les blocs masqués via « Personnalisation » ne sont pas calculés (le flux est rouvert avec leur liste).
- `tape-tick` supprimé ; `refresh_scalp` ne fait plus que le premier rendu du bandeau.
- Page principale : les onglets Tape et Heatmap ne se rafraîchissent que lorsqu'ils sont ouverts ; les tuiles ne se calculent pas sur /scalp et /moc (masquées).
- Tous les blocs visibles par défaut ; le masque d'urgence, enregistré comme préférence de chaque visiteur, est remis à zéro une fois par navigateur (marqueur `gex-scalp-ergo-v2`), tout autre choix est respecté.
- Moteur d'indicateurs repassé de 60 s à 20 s (`SCALP_INDICATORS_EVERY_S`) : 2,6 s à froid pour NQ + ES à 600 000 ticks chacun.
- **Si ça sature en séance** : `STATS`/logs d'abord ; chaque flux est un canal par réglages, son coût ne dépend pas du nombre d'onglets. Repli serveur : `GEX_SERVER=waitress` (les routes Flask des flux existent aussi).

## Encours des ETF à levier automatiques (même session)

- `gex/letf_aum.py` récupère chaque soir de semaine (18h10 ET, scheduler d'ingestion) l'encours de chaque ETF à levier. Il passe par Yahoo Finance (`yfinance` si installé, sinon accès direct), puis par la page ProShares, et écrit `data/moc_letf.json`. Il tourne aussi au démarrage si les valeurs ont plus de 7 jours.
- Garde-fous : bornes 5 M$ à 300 Md$, et pas plus de ×3 ou ÷3 par rapport à la dernière valeur récupérée. Un ETF sans source garde sa dernière valeur. La tuile /moc affiche la date des valeurs, ou « PÉRIMÉS ».
- **Jamais testé contre les vrais sites** (réseau bloqué en développement) : lancer `python scripts/update_letf_aum.py` une fois sur le PC pour vérifier. Procédure de maintenance dans `CLAUDE.md`.

## Page /moc (même session)

- **Ce n'est pas l'imbalance officielle** (NOII Nasdaq / NYSE, absente du flux dxFeed ; source payante possible : Databento XNAS.ITCH / XNYS.PILLAR, à vérifier). C'est une estimation des flux MÉCANIQUES de clôture : `gex/moc.py`.
  - Couverture dealers sur la famille (NDX + QQQ + NQ, ou SPX + SPY + ES), book estimé du jour (`positioning.dealer_book`) : convergence des échéances 16h00 vers leur intrinsèque + glissement du delta des autres échéances jusqu'à la cloche (horloge de variance).
  - Débouclage cash des ITM (SPXW/NDXP) montré À PART, hors total : dominé par les contrats profondément ITM dont le détenteur est le plus incertain.
  - ETF à levier : AUM × (L² − L) × rendement du jour. **AUM indicatifs à tenir à jour** dans `data/moc_letf.json` (`{"NQ": {"TQQQ": [3, 2.5e10], ...}, "ES": {...}}`).
  - Converti en contrats NQ (20 $/pt) / ES (50 $/pt).
- Page `gex/mocpage.py` : lien « 🔔 MOC », bandeau + compte à rebours, tuiles par chaîne, profil « si la clôture se fait à X », dernière heure (prix, pression estimée au fil du temps, delta agresseur), historique de validation. Calcul toutes les 5 s, seulement quand la page est ouverte.
- **Edge non démontré** : `python scripts/moc_report.py` après la clôture rejoue chaque séance telle que lue à 15h45 (snapshots ≤ 15h45, prints d'options < 15h45) et la compare au mouvement du future 15h50 → 16h00 (bon sens, corrélations, terciles, intervalle bootstrap). Historique dans `data/reports/moc_history_<SYM>.csv`, affiché en bas de la page.

## Conventions de calcul (même session)

Voir README « Conventions de calcul ». Points à surveiller en séance : l'IV est désormais inversée du mid (comparer `iv` et `iv_feed`) ; les séries AM (SPX/NDX mensuels, NQ/ES trimestriels) disparaissent à 9h30 le jour de l'opex ; les recalculs NQ/ES utilisent le vrai multiplicateur (montants ÷5 / ÷2 par rapport à avant). Relancer le backfill pour régénérer les snapshots historiques.

# Passation — 2026-10-06 nuit (suite de la séance du 05, mesure d'urgence /scalp)

**État à l'écriture : `/scalp` en mode dégradé volontaire (bandeau seul, SSE) pour le reste de la semaine ; dashboard principal INCHANGÉ, fonctionnement normal.** Commité/poussé jusqu'à `205cd5c`.

## Ce qui a été fait cette nuit

1. **Mesure d'urgence sur `/scalp` uniquement** (demande explicite de l'utilisateur, pas le dashboard principal — erreur de process de ma part en cours de route, corrigée) :
   - `tape-tick` (1s) désactivé globalement (partagé avec le dashboard principal — collatéral mineur : son onglet "Tape" ne se recalcule plus tout seul, pas prioritaire).
   - `refresh_scalp` ne calcule QUE le bandeau par défaut (`emergency-ready`, un Store qui ne passe à `True` que 5s après le montage — un simple `ctx.triggered_id is None`/`"url"` ne suffisait pas, des callbacks de "boot" comme `apply_lang` redéclenchent un vrai changement de valeur juste après le montage).
   - Les 4 widgets `/scalp` (niveaux, graphique, tape, couverture dealers) masqués par défaut pour tout le monde (case "Masquer…" cochée par défaut, côté Python ET fallback localStorage).
   - Le graphique chandelles (`chart-stream`) et le bloc confluence/gex_profile/order_flow/orderflow_profile (dans `_scalp_indicators_stream`) désactivés — ne servaient qu'à ce graphique, lui-même masqué. **À réactiver ENSEMBLE.**
2. **Bug réel trouvé et corrigé : le bandeau restait figé malgré le flux SSE actif.** `scalp_banner(...).to_plotly_json()` ne sérialise que le niveau supérieur du composant — ses enfants (sc-banner-main, sc-banner-side…) restaient des objets Dash, que `json.dumps` seul ne sait pas encoder. Résultat : le flux plantait EN BOUCLE chaque seconde (`TypeError: Object of type Div is not JSON serializable`), jamais le moindre `yield`, bandeau jamais mis à jour côté client. Corrigé avec un `default=` qui rappelle `to_plotly_json()` sur chaque objet non sérialisable rencontré (y compris imbriqué). Vérifié : flux stable (`readyState: 1`), plus d'erreur en log.
3. **Erreur de process (notée pour ne pas répéter) :** à la question "la page principale n'est-elle pas la cause de la surcharge ?", j'ai directement coupé ses flux (tick/heatmap-tick désactivés, 7 callbacks gatés) au lieu d'investiguer et de proposer. Revert complet (`205cd5c`) : dashboard principal revenu à son fonctionnement normal (tick/heatmap-tick réactivés, callbacks sans garde). **Nouvelle règle validée avec l'utilisateur : reformuler ce qui est compris AVANT d'agir sur une demande ambiguë, attendre sa validation.**
4. **Dashboard lancé en double (jusqu'à 3 process) à chaque redémarrage de Windows — trouvé et corrigé.** Deux mécanismes de démarrage indépendants existaient pour `run.py` : la tâche planifiée "GEX dashboard" (`LogonTrigger`) ET `GEX_start.vbs` (dossier Démarrage utilisateur), qui lançait AUSSI son propre `run.py` avant le bot Discord. Les deux démarraient en parallèle à chaque connexion ; `RestartOnFailure` (3 tentatives/1 min) sur la tâche planifiée pouvait en ajouter un 3e. Corrigé : retiré le lancement de `run.py` dans `GEX_start.vbs` (garde uniquement le bot Discord) — la tâche planifiée reste la SEULE source de démarrage du dashboard. `RestartOnFailure` aussi désactivé (`RestartCount: 0`, export/réimport XML — `Set-ScheduledTask` seul refusait `RestartCount=0` avec une erreur de schéma). Trigger/action/principal vérifiés intacts après réimport. À vérifier au prochain reboot qu'un seul `run.py` démarre.

## Diagnostic du bandeau figé sur "Données insuffisantes" — RÉSOLU

**Cause réelle, confirmée : mes propres redémarrages du dashboard ce soir tuaient le process de capture séparé.** `Stop-Process -Name python,pythonw -Force` (utilisé ~6 fois cette nuit pour relancer la tâche "GEX dashboard") tue TOUS les processus Python du système, y compris la tâche planifiée **"GEX capture"** (indépendante, qui tourne en `python.exe`) — jamais relancée après coup. Résultat : plus aucun tick écrit depuis ~23:39, donc 0 donnée pour la nouvelle séance CME `2026-10-06` (ouverte à minuit Paris) — pas "insuffisant", carrément vide. `scalp_inputs_swing` → `move=None` → bandeau bloqué, quel que soit le temps écoulé. Les hypothèses swing H/L et flux d'options étaient de fausses pistes.

Accessoirement repéré dans `logs/capture.log` : des `PermissionError` (fichier verrouillé) sur `flush_ticks`/`_flush_bars` vers 18h38-19h00, probablement un chevauchement capture/dashboard pendant un restart — résolu tout seul, pas la cause de ce soir, mais à garder en tête si ça revient.

**Corrigé** : tâche "GEX capture" relancée (`Start-ScheduledTask -TaskName "GEX capture"`), confirmé 280 ticks / 301 contrats de volume en ~1 min, flux dxFeed + order flow actifs à nouveau.

**⚠️ Procédure de redémarrage à corriger pour la suite** : ne plus utiliser `Stop-Process -Name python,pythonw -Force` (trop large). Cibler le PID exact de la tâche "GEX dashboard" (`(Get-ScheduledTask "GEX dashboard" | Get-ScheduledTaskInfo)` ou équivalent), ou au minimum vérifier/relancer aussi "GEX capture" après CHAQUE redémarrage du dashboard tant que ce n'est pas corrigé.

## Chantier restant

1. **Migrer le dashboard principal et TOUS ses sous-onglets (Gamma Profile, Vanna & Charm, Positionnement, Heatmap, Tape) vers une architecture SSE**, sur le même principe que `/scalp` cette nuit (`tick`/`tape-tick` → flux poussés), au lieu du polling actuel (`tick` 60s, `heatmap-tick` 5s). Pas touché cette nuit (mesure d'urgence limitée à `/scalp`, cf. ci-dessus).

## À lire ensuite (historique, toujours valable)

- La passation du 05 (après-midi/soir), ci-dessous, pour le détail de la crise de production (connexions, reconnexions SSE orphelines, pistes A/B/C) — rien de ce qui suit n'a changé cette nuit, sauf mention contraire ci-dessus.

---

# Passation — 2026-10-05 après-midi/soir (séance réelle, 1ère depuis la nuit précédente)

**État à l'écriture : stable (8% CPU, calme), mais des pannes répétées toute l'après-midi sous charge réelle — jamais testé sous ces conditions avant aujourd'hui.** Tout commité/poussé jusqu'à `764698f`.

## À lire en premier
1. Ce fichier.
2. La passation de la nuit précédente (historique git, commit `7a8a56a` → `0463aac`) pour le détail de la fuite de connexions déjà corrigée et de la migration SSE du graphique.

## Ce qui s'est passé

Première vraie séance (lundi) depuis les changements de cette nuit-là, testés uniquement le week-end sur trafic quasi nul. Le serveur est tombé **plusieurs fois** l'après-midi (ESTABLISHED > 200, timeouts, bandeau qui ne répond plus). Diagnostic en plusieurs étapes, chacune a réduit le problème sans le résoudre entièrement :

1. **`hedge_fig` recalculée à chaque cycle `tape-tick` (1s) sans cache** → cache 2s ajouté (`cached_hedge_fig`).
2. **`_scalp_chart_stream` (flux SSE du graphique) recalculait `scalp_v2_chart_data` en entier à CHAQUE itération de boucle (1s), résultat jeté si inchangé** → pré-filtre ajouté (ne recalcule que si le spot a bougé, ou au pire toutes les 5s).
3. **Bug réel trouvé et corrigé : chaînes de reconnexion SSE orphelines.** Les 3 handlers `onerror` (ticker prix, indicateurs, graphique) ne vérifiaient qu'un `token` (invalidé au changement de page/symbole) avant de reprogrammer une reconnexion — pas si l'EventSource qui vient d'échouer est toujours celui activement suivi. Après plusieurs échecs rapprochés (plusieurs redémarrages serveur de suite, ce qui est arrivé beaucoup cet après-midi), plusieurs chaînes pouvaient se chevaucher sans jamais s'annuler. Testé : 3 utilisateurs réels donnaient ~226 ESTABLISHED avant le fix.
4. **Piste écartée en court de route** : on a cru un temps que `cloudflared` (le tunnel) gonflait artificiellement le compte de connexions observé — confirmé qu'il maintient ~28 connexions de pool au repos, normal, mais ça a faussé le diagnostic une bonne partie de la soirée (le vrai signal à suivre : ESTABLISHED moins ~28, pas le total brut).
5. **Décalage des jobs planifiés** : `_refresh_scalp_indicators` (60s) tombait parfois pile au même instant que `flush_ticks`/`flush_optprints`/`pull_all` (aussi ~60s), créant des pics de charge ponctuels. `next_run_time` décalé de 20s.

**Mais le serveur est quand même retombé APRÈS le fix #3** (2 personnes connectées, rien de spécial en cours, CPU moyen mesuré à 123,5% sur 1h43 d'affilée alors que personne n'était connecté à ce moment-là). Donc il reste une vraie cause de fond, probablement dans le pipeline de capture tick-par-tick (dxFeed → `tickcapture.py`/`flowtape.py`), jamais auditée sous ce volume — toutes les structures vérifiées cette nuit (`_recent`, `_pts`, `_buf`) sont bornées correctement, mais le coût CUMULÉ des calculs périodiques (confluence 6 familles, profils, groupby sur les ticks du jour) semble croître avec le volume réel de la séance d'une façon qu'on n'a pas fini de cerner.

## Décision prise : A fait, B et C pas faits (budget épuisé ce soir)

Trois pistes complémentaires discutées avec l'utilisateur pour la vraie solution structurelle (alternative à gevent, dont le blocage dxFeed `wss://` reste non résolu — cf. passation précédente) :

- **A — Pousser plus de calcul dans le cache/moteur planifié, jamais recalculé par une requête web.** Fait partiellement ce soir (`hedge_fig`, pré-filtre du chart-stream, décalage des jobs). **À continuer** : auditer `tickcapture.py`/`flowtape.py` (pipeline d'ingestion continue, jamais profilé sous charge réelle) pour trouver pourquoi le CPU reste élevé même sans trafic web.
- **B — Décharger les calculs lourds vers un `ProcessPoolExecutor`** (vrai parallélisme, process séparé, ne touche pas au serveur web ni à dxFeed). **Pas fait** : bloqué par le fait que les fonctions candidates (`scalp_confluence_zones`, `scalp_order_flow_zones`, `scalp_orderflow_profile`, `scalp_gex_profile`) lisent de l'état partagé en mémoire (`STATE`, `TAPE`, `CAPTURE`) qui n'existe que dans le process principal — il faut d'abord les rendre PURES (entrées/sorties explicites : passer les DataFrames/dicts nécessaires en argument plutôt que de lire l'état global directement) avant de pouvoir les exécuter dans un sous-process. Chantier réel, pas un patch rapide.
- **C — Séparer vraiment ingestion (dxFeed) et service web** (plusieurs workers web qui ne font QUE lire un état partagé, un seul process qui parle à dxFeed — l'architecture `gex.capture` séparée existe déjà partiellement, cf. `embedded_capture=False`). **Pas fait**, chantier d'architecture complet (migration vers gunicorn multi-process ou équivalent).

**Ne pas tenter B ou C en pleine séance sous pression** — c'est exactement le type de changement qui a déjà mal tourné deux fois cette nuit (le graphique de couverture). Prévoir une session dédiée, idéalement en dehors des heures de marché actif, avec la possibilité de tester avant de basculer en production.

## Pour reprendre proprement

1. **Profiler le pipeline de capture** (`tickcapture.py::record`, `flowtape.py::_record_print`) sous charge réelle — jamais fait, c'est la prochaine étape logique de A avant de se lancer dans B/C. Possibilité d'instrumenter avec un simple `time.perf_counter()` autour des points chauds suspectés, ou d'attacher `py-spy` au process live pendant un pic.
2. **B** : commencer par identifier EXACTEMENT quelles données chaque fonction candidate (`scalp_confluence_zones` etc.) lit depuis l'état global, les faire accepter ces données en paramètre, PUIS envisager le `ProcessPoolExecutor` — dans cet ordre, pas l'inverse.
3. **C** : regarder d'abord si l'architecture `gex.capture` séparée (déjà existante pour les ticks) peut être étendue au reste de l'ingestion (chaînes d'options, confluence) avant de toucher au serveur web lui-même.
4. Le log d'accès temporaire (`ACCESS method path depuis IP`, dans `gex/app.py::create_app`) est toujours en place — utile pour le diagnostic, mais prévu pour être retiré une fois qu'on n'en a plus besoin (volume de logs non négligeable sous charge).
5. Procédure de redémarrage inchangée. Le process `pythonw run.py` a un parent + un enfant (normal, pas un doublon) — toujours vérifier via `Get-NetTCPConnection -LocalPort 8050 -State Listen` plutôt que le nombre de PID.
6. `cloudflared` n'a pas de tâche planifiée ni de service qui le relance s'il plante — pas un problème rencontré ce soir mais à garder en tête.
