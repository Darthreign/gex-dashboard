# Passation — 2026-10-03/04 (nuit), avant clear de session

État au moment de l'écriture : dashboard et capture tournent proprement, dernier redémarrage vérifié sans erreur. Marché fermé (week-end). Tout le travail de cette session est **commité et poussé** (`git log` : de `9f743f7` à `92c17c2`, 9 commits). `git status` propre — seuls des fichiers non suivis préexistants traînent (`Env.txt`, `GEX_formules.pdf`, `icone.png`, `scripts/nightly_migrate.py`, `gex/scheduler.py.bak-20260818`, `graphify-out/`, `.claude/settings.json`), présents dès le début de session, sans rapport avec ce chantier.

## À lire en premier

**La feuille de route /scalp v2 est dans la mémoire du projet, pas dans ce fichier** :
`C:\Users\sk8bo\.claude\projects\D--Gex\memory\roadmap-scalp-v2.md`

Elle a énormément grossi cette session (14+ sections ajoutées au fil des retours de l'utilisateur) — c'est la source de vérité détaillée, ce fichier-ci n'en est qu'un résumé d'orientation. Mémoires liées : `audit-bug-spot-amplification-30040.md` (bug corrigé), `chantier-scalp-sse-integral.md` (fondu dans la v2, pas un chantier à part), `chantier-analyse-pre-moc.md` (MOC, carve-out explicite — ne pas commencer sans en reparler à l'utilisateur).

## État de /scalp v2 — ce qui tourne réellement en production

Routage explicite (`is_scalp_path`) : `/scalp` (v2) et `/scalpv1` (gelée, Plotly inchangée) partagent le même DOM, le CSS/les callbacks bifurquent par chemin exact. **`/scalpv1` n'a jamais régressé à aucune étape** — vérifié en live après chaque redémarrage (une dizaine ce soir).

Sur `/scalp` (v2), livré et vérifié en live :
- **Graphique Lightweight Charts** (lib vendue en local, `gex/assets/`) avec **sélecteur de TF** (1/5/10/15min, 1h, 4h en ré-échantillonnage des bougies 1 min ; 6/60/600 vol en barres-volume avec pivots swing). Niveaux GEX/HVL/Flip/murs + confluence multi-familles (SPX/NDX/QQQ/SPY/NQ/ES) + zones order flow (HVL) en overlay. Repli sur le dernier jour disponible si la séance en cours est vide. Zoom utilisateur préservé (`fitContent()` une seule fois, plus jamais écrasé).
- **Graphique "Couverture des dealers"** migré en Lightweight Charts lui aussi (LineSeries), même logique que le prix.
- **Bandeau swing-ancré** (`scalp_inputs_swing`, `bar_volume=60` fixe en interne) au lieu de la fenêtre 5 min — séparé du sélecteur de TF du graphique (qui ne change que l'affichage, jamais le moteur de signal).
- **Panneau "⚙ Personnalisation"** (carte visible, pas une ligne discrète) : disposition verticale, ordre des blocs (Niveaux/Graphique/Couverture/Prints, CSS `order`, pas de drag-and-drop réel — risque de désync React/Dash), masquage par bloc (niveaux/graphique prix/couverture/tape), mode mots-clés. **Étendu à `/scalpv1` aussi** à la demande de l'utilisateur (préférences partagées via localStorage).
- **`unsupported`** : ton neutre + titre honnête (ne mesure jamais un flux opposé au mouvement, vérifié sur 321 signaux réels — l'ancien titre/ton laissait croire à un edge contrarien inexistant).

Suite de tests : **578/578 verts** (`.venv/Scripts/python.exe -m pytest tests/`). Un test (`test_graphe_sous_jacent_comble_le_trou_si_le_disque_a_du_retard`) est **flaky** (dépend de la frontière de la minute en cours) — repasse seul en isolation, sans rapport avec le code de cette session.

## Bugs trouvés et corrigés cette session

1. **Spot figé à 30040** (46/131 signaux `amplification` pollués, 29/09→01/10) — `_scalp_live_spot` utilise maintenant `_futures_last_price()` (tick-accurate) en source primaire. Détail : `audit-bug-spot-amplification-30040.md`.
2. **`scalp_order_flow_zones` lent** (`.groupby().apply(lambda)` = appel Python par groupe malgré l'air vectorisé) — a saturé le serveur mono-thread dès que le repli historique a commencé à traiter de vraies séances (500k+ ticks). Réécrit en agrégation pandas pure (0,076s pour 531k ticks). Cache 10s ajouté sur confluence/order_flow en prévention.
3. **Confluence "x58"** : le chaînage de `cluster_levels` avec 6 familles peut produire une zone de plusieurs centaines de points — plafond d'affichage à 3x le seuil de clustering (ne touche pas à `cluster_levels`).
4. **Chevauchement CSS** en disposition verticale (`grid-template-areas: none` sur le parent ne suffit pas, `grid-area: auto` à forcer explicitement sur les enfants).
5. **`threaded=True` retesté puis re-retiré** dans la même minute — observé pire en direct par l'utilisateur, confirme la leçon du 2026-10-01. Ne pas retenter sans d'abord alléger le travail CPU synchrone ou passer à un vrai serveur multi-worker (waitress — pas installé).

## Décisions explicites de l'utilisateur, pas encore exécutées

- **Licence TradingView Advanced Charts** : Lightweight Charts (la lib utilisée) n'a pas d'outils de dessin par design — l'utilisateur veut les vrais outils (lignes de tendance, Fibonacci) via Advanced Charts, mais ça nécessite une demande de licence FAITE PAR LUI auprès de TradingView. Pas commencé — attendre sa démarche.
- **MOC** : carve-out confirmé, ne pas commencer sans discussion explicite.
- **HVL / vraie détection d'absorption** : seuils diagnostiqués cassés par design (cumul sur toute la séance lisse le déséquilibre), mais l'utilisateur a dit vouloir affiner ça lui-même — ne pas improviser de nouveaux seuils.

## Point en suspens, pas bloquant

Bruit de log (`IndexError: list index out of range` dans `dash._prepare_grouping`) observé pendant les transitions de page juste après un redémarrage — diagnostiqué comme transitoire (requêtes en vol d'un ancien onglet), stable (n'augmente plus) une fois la page stabilisée sur un onglet propre. Pas de bug fonctionnel constaté, mais à surveiller si ça redevient un flux continu plutôt qu'un pic au moment des transitions.

## Pour reprendre proprement

1. **Lire `roadmap-scalp-v2.md` en entier** avant de retoucher `/scalp` v2 — énormément de contexte et de décisions y sont consignées, pas ici.
2. **Vérifier en séance réelle (marché ouvert)** ce qui n'a pu être testé qu'avec des données vides/historiques ce week-end : le bandeau swing avec de vraies données live, le graphique en conditions de marché actif.
3. **Ne jamais committer sans vérifier `git status` d'abord** — fichiers non suivis préexistants à ne pas embarquer par erreur.
4. **Procédure de redémarrage du dashboard** : `Stop-ScheduledTask "GEX dashboard"` → tuer les PID `pythonw run.py` restants (`Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where CommandLine -like "*run.py*"` puis `Stop-Process`) → `Start-ScheduledTask "GEX dashboard"` → attendre 15-20s avant de vérifier le listener sur le port 8050 (parfois plus lent que d'habitude ce soir) → vérifier `Get-NetTCPConnection -LocalPort 8050 -State Listen` donne **un seul** process sur `127.0.0.1` (le PID sur l'adresse Tailscale est normal, sans rapport).
5. **Mode MOC et licence TradingView** : ne rien commencer sans l'utilisateur, cf. ci-dessus.
