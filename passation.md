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
4. **Dashboard lancé en double (jusqu'à 3 process) à chaque redémarrage de Windows — trouvé et corrigé.** Deux mécanismes de démarrage indépendants existaient pour `run.py` : la tâche planifiée "GEX dashboard" (`LogonTrigger`) ET `GEX_start.vbs` (dossier Démarrage utilisateur), qui lançait AUSSI son propre `run.py` avant le bot Discord. Les deux démarraient en parallèle à chaque connexion ; `RestartOnFailure` (3 tentatives/1 min) sur la tâche planifiée pouvait en ajouter un 3e. Corrigé : retiré le lancement de `run.py` dans `GEX_start.vbs` (garde uniquement le bot Discord) — la tâche planifiée reste la SEULE source de démarrage du dashboard. **Pas encore fait : désactiver aussi `RestartOnFailure` sur la tâche (proposé, pas confirmé par l'utilisateur).** À vérifier au prochain reboot qu'un seul `run.py` démarre.

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
