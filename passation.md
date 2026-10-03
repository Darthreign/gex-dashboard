# Passation — 2026-10-03, avant clear de session

État au moment de l'écriture : dashboard et capture tournent proprement (`GEX dashboard` + `GEX capture`, tâches planifiées, toutes deux "Running"), aucune erreur connue en cours. Marché fermé (samedi).

## À lire en premier

**La feuille de route /scalp v2 est dans la mémoire du projet, pas dans ce fichier** :
`C:\Users\sk8bo\.claude\projects\D--Gex\memory\roadmap-scalp-v2.md`

Elle contient : la décision de routage (nouvelle page prend `/scalp`, l'actuelle devient `/scalpv1`), les 8 pistes retenues (TradingView Lightweight Charts + indicateurs maison, swing high/low sur mèches, vraie absorption, bulles de volume, accessibilité, personnalisation, mode MOC, outils order flow/niveaux combinés), l'audit chiffré des 4 derniers jours de trading (flapping du bandeau confirmé, signal `unsupported` à 44 % de réussite seulement, 0 confirmation HVL sur 257 salves), et un ordre de priorité proposé **pas encore validé définitivement** par l'utilisateur.

Lire aussi, liées à ce chantier :
- `chantier-scalp-sse-integral.md` — plan SSE en 3 phases, prévu pour un dimanche, **ensemble avec l'utilisateur, jamais en session autonome** (instruction explicite).
- `chantier-analyse-pre-moc.md` — détail du mode MOC (charm déjà calculé via `cex`, GTBR à construire, leçon de pondération tirée d'un cas test réel du 2026-09-30).

## Ce qui a été fait le 2026-10-01 (journée de debug intense, tout en production)

Dans l'ordre, chaque point corrigé, testé (suite complète verte à chaque étape), déployé :

1. **Bougies NQ/ES passées du flux conflaté (rtquote.QUOTES) au tick-à-tick** (`TickCapture`) — écart constaté de 10 pts sur un plus bas face à un relevé Tradovate réel.
2. **Filtre du contrat SUIVANT** exclu des agrégats par sous-jacent (spot, absorption, volume profile, bougie) — un print du contrat non-dominant avait fait bondir une bougie de ~370 pts.
3. **Prints à côté agresseur "UNDEFINED" ignorés** pour le spot et les bougies (toujours bufferisés en brut) — un seul print aberrant avait fait exploser une bougie de 166 pts.
4. **Traduction EN du bandeau /scalp complétée** — `_GAMMA_EN` existait mais n'était jamais appelé ; gamma/VIX/"depuis l'open" traduits.
5. **Volume profile de séance (HVL) + `zero_vanna`/`vanna_profile`** ajoutés à `gex/metrics.py` — ce dernier pour tenter d'expliquer un "VFlip" partagé par un tiers, hypothèse écartée après test sur données réelles (profil vanna toujours positif sur la plage plausible), gardé quand même pour ce qu'il vaut.
6. **Incident de production en soirée, résolu en plusieurs étapes** (⚠️ le plus important à comprendre avant de retoucher `gex/run.py` ou `tape-tick`) :
   - Cache 2s sur la lecture des bougies, puis cache 5s sur `load_history` — corrige un verrou disque Windows (`os.replace` vs lecture concurrente) qui faisait planter `/scalp` ("Error loading layout").
   - `_read_parquet_retry` ajouté comme filet de sécurité général.
   - **`threaded=True` essayé puis RETIRÉ** : avec le GIL de Python, plus de threads pour du travail CPU (reconstruire des figures Plotly) n'apporte aucun parallélisme réel, ça a aggravé la contention sous charge réelle.
   - **`tape-tick` repassé plusieurs fois entre 250ms et 1000ms**, mesuré proprement en dernier (sans confondus) : **250ms sature le serveur de dev Werkzeug, mono-thread — figé à 1000ms**, ne pas repasser à 250ms sans un vrai serveur multi-worker (gunicorn/waitress).
   - Cause initiale du tout premier symptôme du jour (page lente) : un process `find` orphelin (lancé par erreur dans une session Claude précédente, jamais tué) qui saturait le CPU/disque — **toujours vérifier les process orphelins avant de chercher un bug de code** quand une lenteur système apparaît sans rapport évident.

## Audit du 2026-10-03 (résumé, détail dans roadmap-scalp-v2.md)

Fait sur `data/journal/journal.sqlite` (470 signaux, 4 séances) :
- Bandeau : 46 % des transitions à moins de 150s de la précédente, même sur une séance propre (02/10) — flapping réel, pas une impression. Cause : pas de délai minimum avant de reconnaître un changement d'état, juste un cooldown anti-répétition.
- `unsupported` (68 % des signaux) : seulement 44 % "continued" — thèse contrarien faible, seuils à recalibrer.
- Absorption : 0 confirmation HVL sur 257 salves en 3 jours — seuils jamais calibrés sur données réelles.
- Détection directionnelle structurellement en retard (fenêtre fixe 5 min) — justifie la piste "swing high/low sur mèches".

## Pour reprendre proprement

1. Lire `roadmap-scalp-v2.md` en entier avant de coder quoi que ce soit sur /scalp v2.
2. Valider/réordonner les priorités avec l'utilisateur (pas encore fait définitivement).
3. Le recalibrage des seuils (HVL, `unsupported`) peut se faire indépendamment de la v2, rapide, données déjà là.
4. Ne pas toucher à `tape-tick`/`threaded` sans relire la section 6 ci-dessus — déjà testé dans les deux sens aujourd'hui.
5. Chantier SSE (phases bandeau → prints → graphes) : attendre que l'utilisateur donne le signal, ne pas démarrer seul.
