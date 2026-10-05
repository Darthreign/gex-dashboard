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
