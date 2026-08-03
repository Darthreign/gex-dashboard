# Bot Discord — état du gamma

Relaie dans un salon Discord le **verdict** d'état du gamma calculé par le
dashboard GEX. Tes amis voient ta conclusion (« Gamma négatif sur SPX,
trading contrarien risqué ») **sans compte courtier ni accès aux données
brutes** — le bot ne lit que l'API locale du dashboard, qui ne renvoie que des
analyses dérivées.

## Ce qu'il fait

- Poste l'état à **heures fixes** : 8h30 / 15h25 / 15h35 / 17h30 (Paris).
- Poste aussi à **chaque changement de régime** pendant la session US (un
  symbole qui bascule Gamma +/− ou Delta +/−).
- **Silencieux le week-end** : marché fermé = aucun post automatique. Les
  commandes à la demande répondent quand même.
- Répond aux commandes :
  - `!help` (ou `!aide`) — la liste des commandes, regroupées par thème ;
  - `!etat` ou `!gamma` — le digest complet ;
  - `!gamma NQ` — les valeurs calculées d'un symbole (GEX net, DEX net, Zero
    Gamma) ;
  - `!niveaux NQ` (ou `!levels NQ`) — les **niveaux GEX en texte** : Gamma
    Flip, HVL, Call Wall, Put Support, 1D min/max, et les 5 murs GEX ;
  - `!niveaux NDX NQ` — les niveaux NDX **transposés en prix NQ** ;
  - **`!graph NQ heatmap`** — n'importe quel graphique en image, ou les
    raccourcis directs :
    - `!heatmap NQ`, `!gex NQ`, `!delta NQ` (Delta Exposure), `!flow NQ`
      (order flow signé), `!skew SPX`, `!profile SPX`, `!vanna SPX`,
      `!charm SPX`, `!history SPX`, `!positionnement SPX`.
  - `!sondage` — (re)poster le sondage de séance à la demande.
  - `!pin QQQ` (ou `!pin SPX 2026-08-01`) — **pinning de clôture** : le prix
    s'est-il collé sur un strike / un mur GEX à 22h ?
  - `!tick NQ` (ou `!tick ES 2026-08-01`) — **fenêtre de clôture au tick**
    (21h45-22h05) : range avant/après 22h, expansion, excursions.
  - `!setup MOC A` (ou `!setup NONE`) — tag ton **setup MOC du jour** (recherche).
  - `!hypo` / `!note` — consigner une hypothèse / observation (mémoire du labo).

  **Tout graphique du dashboard peut sortir en image** — la même vue que tu
  vois à l'écran.

Le message est un **embed coloré** : vert (peu de risque) / orange (risqué) /
rouge (déconseillé), exactement comme le verdict.

## Comment se lit le verdict

Le régime n'est **pas** jugé symbole par symbole (SPX, SPY, ES… sont trois vues
d'un même S&P 500 ; NDX, QQQ, NQ d'un même Nasdaq — les compter à égalité
reviendrait à compter trois fois le même sous-jacent). Il est jugé par
**famille indépendante** :

- **S&P** : SPX / SPY / ES · **Nasdaq** : NDX / QQQ / NQ.
- Chaque famille agrège l'intensité de ses symboles avec des **poids** selon
  l'importance du marché d'options : **indice cash ×3** (SPX, NDX) > **ETF ×2**
  (SPY, QQQ) > **future ×1** (ES, NQ). Un future négatif ne renverse donc pas
  le signal de l'indice cash.
- L'**indice cash est l'indice principal** : s'il passe en *fort* gamma
  négatif, toute sa famille l'est (« le cash index commande »).

Couleur du verdict à partir des deux familles + le VIX :

| Couleur | Condition |
|---|---|
| 🔴 Rouge | 2 familles négatives, **ou** une famille en fort négatif |
| 🟠 Orange | 1 famille négative, **ou** VIX au-dessus du seuil |
| 🟢 Vert | sinon |

Le digest affiche aussi une **confiance** (forte / moyenne / faible) selon la
couverture des données : *forte* = indice principal présent, les 3 symboles de
la famille, signes concordants ; *faible* = indice cash manquant ou symboles
qui se contredisent ; *moyenne* entre les deux. Deux verdicts identiques
n'ont pas la même valeur selon les sources disponibles.

## Collecte pour le backtest (base de recherche)

En plus de diffuser, le bot **accumule des données** pour étudier plus tard
quels scénarios se sont joués et comment. Tout est rangé en local dans
`data/journal/` (base **SQLite** + images), à côté des données du dashboard.
Voir [`journal.py`](journal.py) pour le schéma.

Ce qui est capturé :

- **Sondage de séance** posté à **23h05** (Lun-Ven), **dépouillé le lendemain à
  12h** (temps de voter le matin). Quatre questions à réactions : journée
  directionnelle (😰/🧘), ouverture haussière/baissière (📈/📉), ampleur du
  mouvement (1️⃣-4️⃣, échelles NQ **et** ES), et **représentativité du régime**
  (🎯/😐/🤷 — pour distinguer « mauvaise journée = marché » de « = moteur qui a
  mal classé »). Les **votes bruts** sont conservés (pas un booléen) : tu
  pourras redéfinir un seuil et recalculer.
- **Régimes** : snapshot à l'ouverture (15h30), **chaque changement** (avec la
  *raison* : couleur / famille / confiance) et un **heartbeat** toutes les 10
  min (pour prouver que le bot tournait). Chaque événement stocke aussi l'état
  du marché à l'instant T (prix NQ/ES, distance à l'open/high/low).
- **Heatmaps** des 6 symboles aux créneaux **15h30 / 16h00 / 18h00 / 22h00**
  (PNG). Le numérique par strike est déjà dans les Parquet du dashboard — les
  PNG sont le complément visuel.
- **Contexte de marché objectif** (OHLC, gap, ATR veille, excursions,
  retournements) par séance, calculé par le dashboard, à confronter au ressenti
  du sondage.
- **Tag métier `setup_MOC`** (`!setup`) : est-ce qu'un setup MOC existait ce
  jour-là, selon tes règles ? Info **non recalculable** — c'est ce qui permet
  d'étudier « quand *ma stratégie* marche », pas seulement le marché.
- **`research_log`** : la mémoire du labo (`!hypo`, `!note`) — hypothèses,
  observations, conclusions, décisions, bugs. Colonnes `type`, `status`,
  `author` (capté automatiquement) et `linked_date` (la séance concernée, ≠ le
  jour où c'est écrit). Dans un an, ça dira *pourquoi* telle donnée existe et si
  elle a été tranchée.
- **`daily_metrics`** : quelques agrégats au format long (extensible sans
  migration) pour une lecture facile.

**La règle d'architecture, en une phrase :**

> **Recalculable → pas stocké. Perdu si non saisi → stocké.**

Trois catégories, gérées différemment :

1. **Sources immuables** (à conserver, non recréables) : snapshots GEX,
   régimes horodatés, bougies, **ticks de la fenêtre de clôture** (21h45-22h05,
   capturés à la seconde), résultats du sondage, heatmaps, `setup_MOC`,
   `research_log`.
2. **Dérivés** (calculables, *non* stockés sauf coût prohibitif) : durée d'un
   régime, `pin_ratio`, range 21h50-22h00, impulsion, minutes depuis le dernier
   changement… recalculés **à la demande** depuis les sources. Le **pinning**
   (commande `!pin`, endpoint `/api/v1/<sym>/close_context`) en est l'exemple :
   il se recalcule à partir du snapshot de chaîne ~16h et des bougies, tous deux
   déjà conservés — donc rien n'est figé. Idem pour la **fenêtre de clôture au
   tick** (`!tick`, endpoint `/api/v1/<sym>/tick_context`) : les excursions et
   surtout le rejeu **« un stop aurait-il été balayé ? »** (`?entry=&stop=&dir=`)
   se calculent depuis les ticks bruts capturés 21h45-22h05. Cette capture
   **nécessite le flux temps réel** (compte courtier) : sans lui, le repli
   public est délayé ~15 min et ne dirait rien du vrai comportement à 22h.
3. **Données humaines** (irremplaçables, grande valeur) : `setup_MOC`, notes,
   votes du sondage — ce qu'aucun calcul ne retrouvera jamais.

> Le bot lit ces analyses via l'API locale du dashboard — il ne voit toujours
> aucune donnée brute d'options. La base reste **strictement locale** (usage
> personnel), comme le reste des données du projet.

## Installation (une fois)

1. **Créer le bot** : <https://discord.com/developers/applications> → *New
   Application* → onglet **Bot** → *Add Bot*.
2. Activer **MESSAGE CONTENT INTENT** (même onglet) — sinon les commandes `!`
   ne marchent pas.
3. Copier le **token** du bot.
4. **Inviter** le bot : onglet *OAuth2 → URL Generator*, cocher le scope
   `bot` et la permission **Send Messages**, ouvrir l'URL générée, choisir ton
   serveur.
5. Récupérer l'**ID du salon** cible (Discord → Paramètres → Avancé → activer
   le *Mode développeur*, puis clic droit sur le salon → *Copier l'identifiant*).

## Lancer

```bash
cd discord_bot
python -m venv .venv && .venv/Scripts/activate   # ou source .venv/bin/activate
pip install -r requirements.txt
```

Puis renseigner le token et l'ID du salon — **deux voies au choix** :

**A. Fichier `.env`** (le plus simple, rien à toucher côté système) :

```bash
copy .env.example .env
```

…puis éditer `.env`. Le bot le lit automatiquement. Il est déjà gitignoré.

**B. Variables d'environnement Windows** — en **User** (pas Système : un token
perso ne doit pas être machine-wide), via PowerShell (`set` ne persiste pas) :

```bash
[Environment]::SetEnvironmentVariable("DISCORD_BOT_TOKEN", "ton-token", "User")
```
```bash
[Environment]::SetEnvironmentVariable("DISCORD_CHANNEL_ID", "123456789012345678", "User")
```

Rouvrir un terminal ensuite (les variables sont lues au démarrage du process).

Enfin :

```bash
python bot.py
```

Le **dashboard GEX doit tourner** sur la même machine (par défaut
`http://127.0.0.1:8050`) : c'est lui qui calcule le digest et rend les
graphiques, le bot ne fait que les relayer.

> Pour les commandes graphiques, le dashboard a besoin de **kaleido** (export
> Plotly → PNG) : sur sa machine, `pip install ".[charts]"`. Sans lui, les
> digests texte marchent quand même, seules les images sont indisponibles.

## Sécurité & licence

- Le **token du bot** est un secret : variable d'environnement, jamais dans
  git.
- Le bot ne diffuse **que nos analyses calculées** — signes, verdicts,
  graphiques d'agrégats — jamais les chaînes d'options. Ces données viennent
  du flux **dxFeed** (compte courtier) : c'est tout l'intérêt du projet, le
  temps réel. Un signe de gamma ou une heatmap d'agrégats est une conclusion
  que nous produisons, très loin du feed brut par contrat. À toi de rester
  dans le cadre « usage personnel » de dxFeed : partage des conclusions, pas
  une rediffusion du flux.
