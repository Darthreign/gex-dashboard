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
- Répond aux commandes :
  - `!etat` ou `!gamma` — le digest complet ;
  - `!gamma NQ` — les valeurs calculées d'un symbole (GEX net, DEX net, Zero
    Gamma) ;
  - `!heatmap NQ` — *à venir* (image de la heatmap).

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

# renseigner les variables (cf. .env.example) — le token est un secret
set DISCORD_BOT_TOKEN=...        # Windows
set DISCORD_CHANNEL_ID=...
python bot.py
```

Le **dashboard GEX doit tourner** sur la même machine (par défaut
`http://127.0.0.1:8050`) : c'est lui qui calcule le digest, le bot ne fait que
le relayer.

## Sécurité & licence

- Le **token du bot** est un secret : variable d'environnement, jamais dans
  git.
- Le bot ne diffuse **que des analyses dérivées** (signes, verdicts), jamais
  les chaînes d'options. Pour SPX/NDX/SPY/QQQ la source est de toute façon
  CBOE (publique) ; pour NQ/ES, un signe de gamma est très loin du feed brut.
