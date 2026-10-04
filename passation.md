# Passation — 2026-10-05 nuit (suite directe de la session 2026-10-04→05)

**État au moment de l'écriture : dashboard UP, fuite de connexions auditée et un vrai correctif trouvé + testé sous stress (pas seulement théorique).** Marché encore calme (dimanche soir/nuit, futures NQ/ES en séance continue mais faible volume) — **rien de ce qui suit n'a été revu sous charge réelle de séance US active**. Tout commité et poussé (`7a8a56a`).

## À lire en premier

1. **Ce fichier.**
2. `C:\Users\sk8bo\.claude\projects\D--Gex\memory\roadmap-scalp-v2.md` pour le contexte de fond.
3. La passation précédente (2026-10-04→05, dans l'historique git si besoin) pour le détail des 5 pannes serveur de la veille et des correctifs qui avaient déjà été posés (yield hors try/except, heartbeat SSE, channel_timeout, threads/connection_limit waitress) — tous toujours en place, pas reconduits ici en détail.

## Fuite de connexions : cause supplémentaire trouvée, corrigée, testée sous stress

En reprenant l'audit là où la veille s'était arrêtée (grep `while True`, vérif `gex/scheduler.py`, gestion JS des `EventSource`), tout était déjà correct SAUF un point jamais couvert par les correctifs de la veille :

**Aucun des deux flux `EventSource`** (`_last_trade_stream` côté ticker de prix, `_scalp_indicators_stream` côté indicateurs) **n'avait de `onerror`**. Par spec, un `EventSource` dont la connexion se ferme pour n'importe quelle raison (timeout, erreur réseau, contention serveur) **se reconnecte automatiquement côté navigateur, sans aucune limite de tentatives**, sauf si le JS appelle `.close()` lui-même. Hypothèse retenue comme cause plausible de la fuite résiduelle de la veille (88 ESTABLISHED + 48 CloseWait pour un usage léger) : une coupure sous contention → reconnexion automatique non bornée → nouvelle contention → nouvelle coupure, boucle qui s'auto-entretient.

**Corrigé** (`gex/app.py`, les deux `clientside_callback` d'ouverture de flux, ~ligne 4126 et 4165) : `onerror` ferme désormais explicitement le flux et reconnecte à **délai croissant borné (3s → 30s max)**, avec un token qui invalide la reconnexion si le flux a été remplacé entre-temps (changement de symbole/page).

**Testé sous stress réel par l'utilisateur** : une trentaine de rechargements de page consécutifs sans aucune panne. C'est la première fois cette nuit-là qu'un vrai test de stress (pas juste une mesure statique) passe sans incident. Reste à confirmer sur une durée plus longue et sous charge de séance US active — un test de 30 reloads en quelques minutes ne couvre pas plusieurs heures d'usage réel.

## tape-tick : 1000ms → 500ms (250ms essayé et écarté)

Demande explicite de l'utilisateur : rendre le graphique /scalp plus réactif. `tape-tick` (`dcc.Interval` qui pilote la quasi-totalité des rafraîchissements de /scalp) avait déjà été testé et écarté à 250ms le 2026-10-01, mais pour deux raisons précises, **toutes deux couvertes par des caches ajoutés depuis** (`_load_prices_cached` 2s, `SCALP_CACHE_S`/`SCALP_HEAVY_CACHE_S` 10s) et par la migration vers waitress multi-thread (le blocage d'origine était spécifique au serveur de dev Werkzeug, mono-thread).

- **250ms réessayé** : latence par requête individuelle bonne (11-37ms), **mais CPU mesuré à ~194% d'un cœur en continu pour un usage léger** — le coût fixe par requête (routage Flask, regroupement Dash, sérialisation JSON) × 6 Outputs partagés sur `tape-tick` × 4Hz s'additionne même quand chaque calcul individuel est un cache-hit.
- **Retombé à 500ms** : ~80% CPU moyen mesuré juste après redémarrage (à prendre avec prudence, les toutes premières secondes d'un process sont dominées par les tâches de démarrage, donc pas une mesure stabilisée).
- Ne pas redescendre à 250ms sans alléger le coût fixe par cycle d'abord (piste proposée mais pas creusée : fusionner les callbacks qui partagent déjà `tape-tick` en un seul, pour ne payer qu'une seule fois le routage/regroupement Dash par cycle au lieu de 5).

## Bug du graphique /scalp v2 : bougie figée — trouvé et corrigé (en 2 temps)

Après le passage à 500ms, l'utilisateur a signalé que la bougie en cours restait figée plusieurs secondes, "comme si ça attendait la clôture pour la dessiner". Deux bugs distincts, corrigés dans l'ordre :

1. **Bougie live jamais injectée** : `scalp_v2_chart_data` (graphique Lightweight Charts, branche bougies-temps) ne lisait que les bougies déjà clôturées et écrites sur disque — contrairement à l'ancien graphique Plotly (`scalp_price_fig`, /scalpv1) qui complète avec `_update_live_bar` (bougie reconstruite à partir du spot vu par CE process). Porté le même mécanisme, + passage de `store.load_prices` brut à `_load_prices_cached` (nécessaire à 500ms pour éviter la collision disque avec l'écriture atomique du process capture, déjà documentée le 2026-10-01).
2. **Bug introduit par le correctif n°1, trouvé et corrigé dans la foulée** : la condition qui décidait d'injecter la bougie live comparait `_session_day()` (convention séance CME, décalage +6h) à la date utilisée pour charger les bougies — alors que `store.append_prices`/`load_prices` range les fichiers par **date calendaire ET simple**. Entre 18h et minuit ET (donc pile l'heure à laquelle ça a été testé), `_session_day()` pointe déjà sur la date calendaire de DEMAIN → la condition était systématiquement fausse → bougie live jamais injectée, malgré le correctif n°1. Remplacé par une comparaison à la date calendaire ET directe (`datetime.now(ET).strftime("%Y-%m-%d")`), même convention que `scalp_price_fig`.

**Confirmé fonctionnel par l'utilisateur après le 2e correctif** ("Graphique en direct maintenant bien jouer").

## Nettoyage au passage

`refresh_scalp` (callback qui alimente entre autres `scalp-price`/`scalp-hedge`, les figures Plotly) calculait ces deux figures sur **toutes** les pages, y compris `/scalp` v2 où elles étaient masquées en CSS (remplacées par les graphiques Lightweight Charts) — travail dupliqué pour rien à chaque cycle de `tape-tick`. `no_update` renvoyé pour `scalp-price` quand `/scalp` v2 est actif (ça, ça reste vrai). **`scalp-hedge` ne suit PLUS cette règle** depuis la fin de soirée (cf. section suivante) — il est recalculé et affiché sur `/scalp` v2 aussi maintenant.

## Clignotement de "Couverture des dealers" (/scalp v2) : cause identifiée, corrigé en revenant sur Plotly

Signalé par l'utilisateur via une vidéo (visible aussi en observant ensemble le même onglet) : le graphique "Couverture des dealers" en Lightweight Charts clignotait **au rythme exact de `tape-tick`** (confirmé par l'utilisateur).

**Diagnostic** (double instrumentation JS en direct, deux sessions de 10-30s chacune) : aucune anomalie de LOGIQUE trouvée — `setData()` jamais appelé avec un tableau vide, conteneur jamais recréé (`MutationObserver` muet), `chart.resize()` jamais appelé pendant le clignotement observé. Conclusion retenue : le thread principal du navigateur n'a pas le temps de repeindre le canvas entre deux cycles de redessin trop rapprochés (5 callbacks Dash distincts déclenchent leurs mises à jour quasi simultanément à chaque `tape-tick`) — un problème de **rendu/performance**, pas de code JS cassé.

**Deux correctifs tentés cette nuit-là, TOUS DEUX ONT RÉGRESSÉ et ont été annulés (`git checkout`) :**
1. Intervalle `dcc.Interval` dédié (1000ms, détaché de `tape-tick`) → a provoqué une **page entièrement figée** côté navigateur (lien de cause à effet pas formellement prouvé, mais la régression est apparue immédiatement après ce changement, et un hard-reload a résolu le gel — donc probablement un état JS corrompu plutôt qu'un vrai deadlock serveur).
2. Flux SSE poussé (nouvelle route `/api/v1/<symbol>/hedge-live-stream`, même principe que le ticker de prix) → le graphique s'est retrouvé **sans aucune courbe** (juste les étiquettes de prix affichées, aucune ligne tracée) — cause exacte non investiguée, abandonné après ce constat.

**Solution finale retenue** (suggestion de l'utilisateur, "il fonctionnait dessus avant") : **repasser ce graphique sur Plotly** (`hedge_fig`/`scalp-hedge`) sur `/scalp` v2 aussi, au lieu de la version Lightweight Charts (`scalp-lw-hedge-card`, désormais masquée inconditionnellement par CSS — plus de bascule par page). Plotly n'a jamais eu ce problème de clignotement sur `/scalpv1`. **Confirmé fonctionnel par l'utilisateur** ("parfait").

Le graphique de PRIX (bougies) sur `/scalp` v2 reste sur Lightweight Charts, inchangé — seul le graphique de couverture est repassé sur Plotly. Si quelqu'un veut reprendre la migration Lightweight Charts de ce graphique precis un jour, repartir de zéro plutôt que de réappliquer les deux tentatives ci-dessus telles quelles.

## Cases "Masquer..." (niveaux/graphique prix/couverture dealers/tape) qui ne masquaient plus rien

Repéré par l'utilisateur en toute fin de soirée, pas lié aux chantiers ci-dessus : cocher une case "Masquer…" dans le panneau Personnalisation changeait bien l'état interne (`_gexGridVisible`) et le détachait de GridStack (`grid.removeWidget(el, false)`, compteur interne qui retombe à 0), mais l'élément restait **affiché à l'écran** — `removeWidget(..., false)` détache du moteur GridStack sans jamais appliquer `display:none`. Corrigé en ajoutant explicitement `el.style.display = 'none'`/`''` dans les deux chemins qui masquent/affichent un widget (callback des cases à cocher, restauration de l'état au chargement de la grille). Validé dans les deux sens (cocher/décocher) et sous rechargement complet. Cause probablement ancienne (pas liée aux changements de ce soir), juste jamais remarquée avant.

## Couleur LVN (profil de volume par jambe)

Demande cosmétique : les zones LVN (creux de volume) étaient rendues dans la même teinte violette que les HVN (pics), seule l'opacité différait. LVN passe à un bleu distinct (`#3987e5`, même bleu que "Calls achetés" ailleurs dans le dashboard) — dégradé du canvas ET étiquette d'axe, dans `gex/assets/gex-orderflow-profile.js`.

## Pas touché / pas résolu ce soir

- **Chevauchement de texte mobile** (panneau "Niveaux", /scalp ≤375px) — toujours pas corrigé, signalé la veille.
- **Migration gevent** — toujours en attente d'une session dédiée (cf. passation précédente pour le détail du blocage wss/dxFeed déjà identifié).
- **Recherche Hedge Pressure** — rien repris ce soir, cf. passation précédente pour l'état complet (`gex/expansion_regime.py` validé comme détecteur de régime, pas de signal directionnel trouvé).
- **`IndexError: list index out of range` dans `dash._prepare_grouping`** (`/_dash-update-component`) — toujours présente dans les logs, récurrente sur les rechargements de page. Confirmé cette nuit qu'elle n'est PAS amplifiée par `tape-tick` à 500ms (fréquence comparable à 1000ms) — semble liée aux rechargements/navigation, pas à la cadence. Toujours non investiguée en profondeur, toujours documentée comme transitoire/sans impact connu.
- **NOUVEAU repéré ce soir, pas corrigé** : `Error: Value is null` dans la console navigateur, levée par Lightweight Charts (`createPriceLine`) au chargement de `/scalp` v2, de façon reproductible (confirmé sur plusieurs rechargements complets, PAS un artefact d'instrumentation). Vient très probablement d'une entrée de `d.levels` (niveaux GEX/HVL/Flip/murs envoyés au graphique de prix) avec un `price` null/undefined — un niveau pas encore calculable dans l'état actuel du marché (séance calme/fermée). Ne semble pas bloquant (le graphique de prix fonctionne quand même, les lignes de niveaux valides s'affichent), mais pollue la console et mérite un `.filter(lv => lv.price != null)` côté `applyIndicators` (JS, ~ligne 3630) ou côté Python (`_scalp_indicator_snapshot`/`scalp.build_ladder`) à l'occasion.

## Pour reprendre proprement

1. **Valider la fuite de connexions sur une vraie séance US active**, pas juste un test de 30 reloads en quelques minutes — le correctif `onerror` + reconnexion bornée est solide en théorie et a passé son premier test de stress, mais la nuit dernière aussi semblait calme avant de craquer sous usage normal.
2. **Procédure de redémarrage inchangée** : `Stop-ScheduledTask "GEX dashboard"` → tuer les PID `pythonw run.py` restants (le process a un parent + un enfant, les deux nommés `run.py` — normal, pas un doublon de serveur) → `Start-ScheduledTask "GEX dashboard"` → attendre 20-25s → vérifier **un seul** listener sur le port 8050 (`Get-NetTCPConnection -LocalPort 8050 -State Listen` ; les entrées sur les IP Tailscale appartiennent à `tailscaled.exe`, pas au dashboard — ignorer).
3. Si le graphique redevient lent après un changement futur : vérifier d'abord le CPU moyen du process (`Get-Process` + `StartTime`) avant de soupçonner une fuite de connexions — ce soir, deux symptômes qui se ressemblaient ("c'est lent") avaient deux causes totalement différentes (charge CPU réelle à 250ms vs bug fonctionnel de date à 500ms).
4. `/scalpv1` non retouché ce soir, toujours sur l'ancien pipeline Plotly inchangé.
