# Passation — 2026-10-04 soir → 2026-10-05 nuit, avant compactage

**État au moment de l'écriture : dashboard UP mais fragile.** Plusieurs correctifs appliqués ce soir, mais **une fuite de connexions/threads reste active** — confirmée déclenchée par un usage normal de l'utilisateur (pas seulement mes propres tests). **Un audit de code est nécessaire en priorité à la reprise**, avant tout nouveau chantier fonctionnel.

Marché fermé (week-end) pendant toute la session — rien de ce qui suit n'a été revu en séance réelle.

## À lire en premier

1. **Ce fichier** (vue d'orientation de cette session, très longue et dense).
2. `C:\Users\sk8bo\.claude\projects\D--Gex\memory\roadmap-scalp-v2.md` pour le contexte de fond (sessions précédentes).
3. Section "Fuite de threads — PAS RÉSOLUE" ci-dessous avant de toucher au serveur.

## ⚠️ Priorité absolue à la reprise : fuite de connexions/threads non résolue

**5 pannes serveur ce soir**, malgré plusieurs correctifs. La dernière est survenue sur un **rechargement de page normal par l'utilisateur** (pas mes tests) — preuve qu'il reste une vraie fuite active, pas juste "trop de trafic de test".

**Correctifs déjà appliqués** (tous commités, tous utiles mais **insuffisants seuls**) :
- `gex/api.py::_last_trade_stream` et `gex/app.py::_scalp_indicators_stream` : un **vrai bug** trouvé et corrigé — le `yield` (écriture) était à l'intérieur du `try/except` qui protège le calcul, donc une erreur d'écriture (client déconnecté) était avalée silencieusement et la boucle `while True` ne s'arrêtait **jamais**. Corrigé : le yield est sorti du try/except.
- Heartbeat SSE toutes les ~15s sur les deux flux (force une écriture régulière, détecte les connexions mortes plus vite).
- `channel_timeout=90` ajouté à waitress (ferme un canal sans octet échangé depuis ce délai).
- `threads` waitress 48→128.
- `connection_limit` waitress 100→300 (paramètre **séparé** de `threads`, jamais touché avant ce soir — c'est lui qui donnait "connection limit reached").
- Intervalle du moteur planifié `_refresh_scalp_indicators` (gex/app.py) 8s→20s — mesuré en régime stable à 0,2-1,2s/cycle (large marge), mais le premier cycle après chaque redémarrage prend 14-15s (caches froids), et sous charge réelle (contention GIL) ce cycle peut déborder bien au-delà, provoquant des dépassements en cascade (confirmé dans les logs : `apscheduler ... skipped: maximum number of running instances reached` en boucle).

**Ce qui N'EST PAS expliqué/résolu** : la dernière panne (après tous ces correctifs) montrait 88 connexions ESTABLISHED + 48 CloseWait pour un usage très léger (l'utilisateur + moi). **Il reste une source de fuite non identifiée.** Pistes à auditer en priorité :
- Tout autre endpoint avec un pattern `while True` / générateur long-lived (grep `while True` dans gex/*.py) — vérifier CHAQUE écriture est bien hors d'un try/except trop large, comme le bug déjà trouvé.
- Les callbacks Dash avec `dcc.Interval` très fréquents (`tape-tick`) — combien de requêtes par seconde par onglet réellement, est-ce que `_dash-update-component` lui-même peut rester bloqué/non libéré dans certains cas (le fameux `IndexError: list index out of range` dans `dash._prepare_grouping`, vu en boucle ce soir — artefact documenté comme transitoire, mais **jamais vérifié si ce n'est PAS aussi une source de thread qui ne se libère pas**).
- Les 2 flux SSE par onglet (ticker + indicateurs) × plusieurs symboles/onglets réels : combien un utilisateur normal en ouvre-t-il en pratique (changement de symbole NQ/ES = nouvel EventSource sans fermer l'ancien côté client ?) — vérifier le JS qui gère `EventSource` (recherché `new EventSource` dans gex/app.py) ferme proprement l'ancien avant d'en ouvrir un nouveau.
- `gex/scheduler.py` (le scheduler CRITIQUE, séparé de celui des indicateurs scalp) — jamais audité ce soir, pourrait avoir son propre pattern à risque.

**Migration gevent** (la vraie solution long-terme, cf. plus bas) reste la correction structurelle recommandée, mais un blocage réel a été trouvé (`gevent.monkey.patch_all()` par défaut bloque la vraie connexion `wss://` vers dxFeed) — `patch_all(thread=False)` contourne le blocage mais laisse une friction résiduelle (exceptions `LoopExit` du résolveur DNS gevent) pas assez éprouvée pour la prod. **Ne pas migrer sans une session dédiée avec plus de marge de test.**

**Ne pas committer de nouveau correctif serveur sans d'abord auditer** — on a déjà patché 5 symptômes différents ce soir (threads, connection_limit, channel_timeout, heartbeat, intervalle scheduler) sans trouver la cause racine complète.

## Autre bug repéré, PAS corrigé

**Chevauchement de texte dans le panneau "Niveaux" en vue mobile** (`/scalp`, viewport ≤375px) — le texte des niveaux (ex. "30,500" et "+0.5 Bn") se superpose verticalement. Repéré en toute fin de session, pas encore investigué. Probablement un problème de `line-height`/hauteur de ligne fixe trop petite pour le texte qui wrap sur plusieurs lignes à cette largeur.

## Recherche Hedge Pressure (toute la soirée, chantier de fond)

Exploration longue, méthodique, sur 5 jours de données disponibles (28/09→02/10, limite dure : les prints OPRA bruts ne sont persistés que depuis le 27/09). **Verdict global : rien d'exploitable comme signal directionnel/de fading**, sauf UN signal gardé :

- **`gex/expansion_regime.py`** (module de recherche isolé, **non branché**, testé 15/15) : `EXPANSION_REGIME = |price_z_30s|>2 ET |flow_z_0dte_30s|>2` (z-scores strictement causaux, lookback 60 fenêtres). Validé comme **détecteur de régime d'expansion/volatilité** (29,7% vs 23,4% baseline de retracement ≥10pts à 30s, IC qui ne se chevauchent pas, cohérent sur 4/5 jours) — mais **PAS un prédicteur directionnel** (testé explicitement : aligné vs divergent ne discrimine rien).
- Tout le reste testé et **invalidé** : signe/magnitude du gamma, niveau/pente du notionnel (5 résolutions), skew (niveau semblait prometteur puis s'est effondré au test prédictif en avant), réaccélération après repli, absence de retracement ≥3pts (dégénéré, 99,5% d'occurrence).
- **`gex/hedge_pressure.py`** (module antérieur, lui aussi non branché, testé) : formules Γ·dS+Vanna·dσ+Charm·dt, convention de signe vérifiée par tests — reste disponible pour un futur backtest, pas de piste trouvée dessus cette nuit au-delà du signal A.
- Détail complet des méthodologies, pièges évités (hindsight via `leg_len`, confusion continuation/retracement, biais de sélection par jour) dans l'historique de conversation — trop long pour ce fichier, redemander si besoin de le reconstruire.

**Prochaine étape suggérée par l'utilisateur, pas commencée** : chercher quelles infos disponibles à l'instant t prédisent qu'un retracement significatif NE va PAS apparaître — tenté sur une définition dégénérée (≥3pts), à refaire avec une vraie définition si repris.

## Fonctionnalités livrées ce soir

- **Bouton V1/V2 du bandeau** (`scalp-banner-version-toggle`) : bascule entre le calcul V1 (fenêtre fixe 5 min) et V2 (swing H/L 60V) du bandeau d'amplification. **Défaut changé à V1** ce soir (retour des testeurs : "V1 était la plus juste en live") — V2 reste dispo en opt-in.
- **Swing H/L étendu à tous les TF du graphique** (pas seulement 60 vol) — même moteur `gex/bars.py::zigzag`, même seuil partout. Brièvement reverté puis restauré après avoir déterminé que la vraie cause des pannes était l'intervalle du scheduler, pas cette fonctionnalité (testée isolément sur les 6 TF temps, aucun blocage reproduit).
- **Profil gamma par strike plafonné à la largeur d'un strike** (`gex/assets/gex-profile-overlay.js`) : la hauteur de zone est maintenant recalculée à chaque zoom/pan, plafonnée à l'écart réel (en pixels, au zoom courant) entre deux strikes voisins — fini le chevauchement au dézoom.

## Pour reprendre proprement

1. **Auditer la fuite de connexions AVANT tout nouveau chantier** (cf. section dédiée ci-dessus) — c'est la priorité n°1, le site n'est pas fiable pour plusieurs utilisateurs tant que ce n'est pas trouvé.
2. **Corriger le chevauchement de texte mobile** (panneau Niveaux) — mineur, mais rapide.
3. **Procédure de redémarrage inchangée** : `Stop-ScheduledTask "GEX dashboard"` → tuer les PID `pythonw run.py` restants → `Start-ScheduledTask "GEX dashboard"` → attendre 20-25s → vérifier **un seul** listener sur le port 8050. Onglet FRAIS (jamais réutilisé à travers un redémarrage) pour vérifier la console — un onglet réutilisé accumule des `ERR_CONNECTION_RESET/REFUSED` résiduels qui ne sont PAS de vrais bugs (reconfirmé plusieurs fois ce soir).
4. **`plotly.min.js` (4,7 Mo) est lent au premier accès après chaque redémarrage** (5-9s, cache disque froid) — explique un chargement qui semble figé juste après un restart ; devient rapide (<1s) une fois le cache OS chaud. Pas un bug à corriger, juste à savoir.
5. **Migration gevent** : piste validée en partie (`patch_all(thread=False)` contourne le blocage wss/asyncio trouvé), mais pas assez testée — prévoir une session dédiée avec marge, pas un correctif de dernière minute.
6. **`/scalpv1` n'a jamais régressé** à aucune étape de cette session — revérifié après chaque redémarrage, toujours intact.
