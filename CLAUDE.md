# Consignes pour Claude — dashboard GEX

Lu automatiquement au début de chaque session. À suivre AVANT toute modification.

## 1. Avant d'agir

1. Lire `passation.md` : la section du haut décrit l'état le plus récent. En cas de contradiction avec la mémoire du projet ou un commentaire ancien dans le code, **`passation.md` fait foi**. En particulier, les « mesures d'urgence » des 05-06/10/2026 (tape-tick coupé, blocs /scalp masqués, flux du graphique coupé) sont LEVÉES depuis le 08/10 : ne pas les réappliquer.
2. Demande ambiguë : reformuler ce qui est compris et attendre la validation de l'utilisateur avant d'agir.
3. Jamais de changement risqué pendant la séance cash (9h30-16h00 ET) : préparer, tester, déployer hors séance.
4. Données dxFeed / Databento : usage personnel, jamais exportées ni commitées (`source="dxfeed"` / `"databento"` exclus de l'export). Ne jamais commiter `data/`.

## 2. Vérifier une modification

```
.venv\Scripts\python -m pytest tests -q --ignore=tests/test_mcp_market_context.py
```

Tests connus comme dépendants du réseau ou du flux réel : `test_capturebus.py::test_liaison_reelle_relaie_labsorption` et `test_native_targets.py::test_pull_all_ignore_les_cibles_futopt`. Tout autre échec est un vrai problème : le corriger, ne jamais désactiver le test.

Pour une page : lancer le dashboard et regarder la page concernée (`.claude/launch.json`).

## 3. Après une modification

1. Tests verts.
2. Ajouter en haut de `passation.md` ce qui a changé, pourquoi, et comment revenir en arrière.
3. Commit avec un message clair.
4. Redéploiement : arrêter « GEX dashboard » puis « GEX capture » **par PID** (jamais `Stop-Process -Name python`), redémarrer « GEX capture » puis « GEX dashboard ». `GEX_ENGINE=1` sur les deux tâches. Après un changement de `requirements.txt` : `pip install -r requirements.txt` dans le venv.

## 4. Où modifier quoi

| Sujet | Fichiers | À savoir |
|---|---|---|
| Encours des ETF à levier (page /moc) | `gex/letf_aum.py`, `scripts/update_letf_aum.py`, `moc.DEFAULT_LETF` | Mis à jour chaque soir à 18h10 ET par le scheduler d'ingestion (moteur), et au démarrage si périmé. **Si la tuile /moc affiche « PÉRIMÉS »** ou si le log dit « 0 mis à jour » : lancer `python scripts/update_letf_aum.py`. Il affiche, ETF par ETF, la source qui échoue. Corriger `fetch_yahoo` ou `fetch_issuer` (le format du site a changé), ou ajouter une source dans `SOURCES`. **Ne jamais assouplir les garde-fous** (`plausible`) pour faire passer une valeur. Ajouter ou retirer un ETF, ou changer son levier : `moc.DEFAULT_LETF`, avec le levier du prospectus, jamais lu en ligne. Tests : `tests/test_letf_aum.py`. |
| Calcul de la pression MOC | `gex/moc.py` | Convention : flux = −position dealer × Δδ × mult × S, positif = achat. Le débouclage cash des ITM reste HORS total, sauf si `scripts/moc_report.py` montre qu'il améliore la prévision. Tout changement de calcul : relancer ce rapport et comparer. |
| Page /moc | `gex/mocpage.py`, `.moc-*` dans `gex/assets/style.css`, `moc_*` dans `gex/i18n.py` (FR **et** EN) | Calcul toutes les 5 s, uniquement quand la page est ouverte. |
| Page /scalp en temps réel | `gex/app.py` : `scalp_indicators_channel`, `scalp_chart_channel`, `scalp_panels_channel` ; routes dans `gex/asgi.py` **et** routes Flask | Tout passe par SSE, **jamais de `dcc.Interval` à la seconde sur /scalp**. Nouveau bloc : l'ajouter à `scalp_panels_snapshot` et à la table `target` du callback client `/scalp-stream`. Un canal = un calcul partagé par tous les onglets. |
| Lecture normalisée / edge /scalp | `gex/edge.py`, `gex/edge_report.py` | Seuils utilisés seulement si `data/reports/edge_params_<SYM>.json` dit `validated: true` (`scripts/edge_report.py`). |
| Architecture de charge | `gex/broadcast.py`, `gex/livestate.py`, `gex/asgi.py`, `gex/run.py` | Repli serveur : `GEX_SERVER=waitress`. Repli moteur : retirer `GEX_ENGINE`. |
| Conventions de calcul (GEX, IV, AM/PM, horloge de variance…) | `gex/metrics.py`, `gex/greeks.py` | Voir README, « Conventions de calcul ». Ne pas changer une convention sans mettre à jour le README. |
