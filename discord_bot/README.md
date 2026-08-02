# Bot Discord — état du gamma

Relaie dans un salon Discord le **verdict** d'état du gamma calculé par le
dashboard GEX. Tes amis voient ta conclusion (« Gamma négatif sur SPX,
trading contrarien risqué ») **sans compte courtier ni accès aux données
brutes** — le bot ne lit que l'API locale du dashboard, qui ne renvoie que des
analyses dérivées.

## Ce qu'il fait

- Poste l'état à **heures fixes** : 8h30 / 15h25 / 17h30 (Paris).
- Poste aussi à **chaque changement de régime** pendant la session US (un
  symbole qui bascule Gamma +/− ou Delta +/−).
- **Silencieux le week-end** : marché fermé = aucun post automatique. Les
  commandes à la demande répondent quand même.
- Répond aux commandes :
  - `!etat` ou `!gamma` — le digest complet ;
  - `!gamma NQ` — les valeurs calculées d'un symbole (GEX net, DEX net, Zero
    Gamma) ;
  - `!niveaux NQ` (ou `!levels NQ`) — les **niveaux GEX en texte** : Gamma
    Flip, HVL, Call Wall, Put Support, 1D min/max, et les 5 murs GEX ;
  - **`!graph NQ heatmap`** — n'importe quel graphique en image, ou les
    raccourcis directs :
    - `!heatmap NQ`, `!gex NQ`, `!delta NQ` (Delta Exposure), `!flow NQ`
      (order flow signé), `!skew SPX`, `!profile SPX`, `!vanna SPX`,
      `!charm SPX`, `!history SPX`, `!positionnement SPX`.

  **Tout graphique du dashboard peut sortir en image** — la même vue que tu
  vois à l'écran.

Le message est un **embed coloré** : vert (peu de risque) / orange (risqué) /
rouge (déconseillé), exactement comme le verdict.

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
