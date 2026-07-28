# GEX Dashboard — analyse Gamma/Delta Exposure (SPX, NDX, SPY, QQQ)

*[English version](README.en.md)* · *[FAQ](FAQ.md)* · *[Avertissement](DISCLAIMER.md)*

[![Tests](https://github.com/Darthreign/gex-dashboard/actions/workflows/tests.yml/badge.svg)](https://github.com/Darthreign/gex-dashboard/actions/workflows/tests.yml)

[Licence MIT](LICENSE) — outil d'**analyse uniquement** : pas de trading,
pas d'exécution, pas de conseil en investissement. Chaque instance tire ses
propres données depuis l'endpoint delayed public de CBOE ; ce projet ne
rediffuse aucune donnée de marché.

> ⚠️ **Le trading d'options et de dérivés comporte un risque élevé de perte.**
> Cet outil est fourni à titre éducatif, sans garantie, et ne constitue pas un
> conseil en investissement. Lisez l'[avertissement complet](DISCLAIMER.md)
> avant toute utilisation.

## Aperçu

| Vue principale | Gamma Profile |
|---|---|
| ![Vue principale](docs/screenshots/01-vue-principale.png) | ![Gamma Profile](docs/screenshots/02-gamma-profile.png) |
| GEX/DEX par strike, niveaux 0DTE, flux delta, historique | Profil de GEX net selon le spot, décomposé par échéance |

| Vanna & Charm | Positionnement |
|---|---|
| ![Vanna et Charm](docs/screenshots/03-vanna-charm.png) | ![Positionnement](docs/screenshots/04-positionnement.png) |
| Grecques de second ordre par strike | Variation d'open interest entre séances |

Dashboard **d'analyse uniquement** (pas de trading) qui reconstruit les métriques
de structure de marché façon SpotGamma à partir des chaînes d'options CBOE :
Gamma Exposure par strike, Delta Exposure, GEX net, niveau Zero Gamma,
put/call ratios, skew IV, et proxy de flux delta intraday.

**Envie de comprendre ce que chaque onglet et chaque chiffre affichent ?** →
[Guide illustré](docs/guide/README.md), un fichier par onglet plus un fichier
qui explique chaque nombre, pensé pour quelqu'un qui découvre le dashboard
sans rien connaître aux options.

## Source de données

Endpoint delayed public CBOE (non documenté officiellement) :
`https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json` (indices
préfixés `_`). Un GET ramène la chaîne complète — bid/ask, IV, open interest,
volume, Greeks — plus le spot. **Délai ~15 min à la source**, régénéré ~toutes
les 60 s (timestamp du feed en UTC). Sous-jacents suivis : SPX, NDX, SPY et
QQQ (`gex/config.py`).

## Installation

**Débutant, jamais installé ce genre d'outil ?** → suis le
**[guide pas à pas illustré](INSTALL.md)** (15 min, aucune connaissance
requise, sans ligne de commande à comprendre).

Sinon, l'[installation assistée par Claude Code](#installation-assistée-claude-code)
ou le [démarrage manuel](#démarrage) ci-dessous.

## Installation assistée (Claude Code)

Si tu utilises [Claude Code](https://claude.com/claude-code), ouvre-le dans un
dossier vide et colle ce prompt — il fait tout, y compris l'enregistrement du
serveur MCP :

```
Installe le dashboard GEX (analyse d'options SPX/NDX) sur ma machine.

Dépôt : https://github.com/Darthreign/gex-dashboard

Étapes :
1. Vérifie que Python 3.11+ et git sont disponibles. S'il en manque un,
   explique-moi comment l'installer et arrête-toi là.
2. Clone le dépôt dans le dossier courant et places-toi dedans.
3. Crée un environnement virtuel .venv et installe requirements.txt.
4. Lance la suite de tests (pytest tests/ -q) pour valider l'installation :
   tous les tests doivent passer.
5. Adapte .mcp.json à mon système : remplace la valeur de "command" par le
   chemin ABSOLU vers le python du venv (Windows : .venv\Scripts\python.exe,
   macOS/Linux : .venv/bin/python). Le fichier livré contient un chemin
   Windows relatif qui ne fonctionne pas ailleurs.
6. Démarre le dashboard (python run.py) et donne-moi l'URL à ouvrir.
7. Explique-moi que je dois redémarrer Claude Code depuis ce dossier pour
   activer le serveur MCP "gex-data", et liste les outils qu'il expose.

Important : aucun compte, aucune clé API ni abonnement n'est nécessaire — les
données proviennent de l'endpoint public gratuit de CBOE. Ne me demande aucun
identifiant. Les modules backfill.py (Databento) et tt_auth.py (tastytrade)
sont optionnels et payants : ignore-les complètement.
```

Le serveur MCP permet ensuite d'interroger tes données en langage naturel
(« analyse la structure gamma actuelle », « où sont les murs sur NDX ? »).

## Démarrage

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py        # dashboard sur http://127.0.0.1:8050
```

Tests : `.venv\Scripts\python -m pytest tests/`

### Installation en package (optionnel)

Le projet est un package Python standard. Installé, il expose deux commandes,
sans avoir à se placer dans le dossier des sources :

```
pip install .                      # ou : pip install -e .  (mode développement)
gex-dashboard                      # démarre le dashboard
gex-mcp                            # démarre le serveur MCP
```

Une fois installé ainsi, `data/` et `logs/` sont créés **dans le dossier
courant** (et non dans les sources) : lance la commande depuis le dossier où
tu veux conserver ton historique.

## Serveur MCP — interroger ses données en langage naturel

C'est ce qui distingue vraiment cet outil d'un dashboard classique : une fois
le serveur MCP actif, tu peux poser tes questions directement à Claude, qui
lit tes fichiers Parquet et te répond sur **tes** données.

```
« Où sont les murs de gamma sur NDX ? »
« Analyse le régime gamma actuel sur SPX »
« Comment le GEX net a-t-il évolué cette semaine ? »
« Montre-moi le flux delta de la dernière séance »
```

⚠️ **Le serveur MCP ne s'active qu'au démarrage de Claude Code, depuis le
dossier du projet.** Si tu viens d'installer l'outil, ferme Claude Code et
relance-le depuis ce dossier — sinon les commandes resteront invisibles.
C'est l'unique étape qui déroute à l'installation.

Le fichier [`.mcp.json`](.mcp.json) enregistre le serveur automatiquement.
Il contient un chemin **Windows relatif** : sous macOS ou Linux, remplace la
valeur de `command` par le chemin absolu vers `.venv/bin/python`, faute de
quoi le serveur échoue sans message explicite.

Outils exposés : `get_gex_summary`, `get_gex_by_strike` (murs de gamma),
`get_flow_delta`, `get_history`, `get_reports`, `get_log_tail`.

## Fonctionnalités

- GEX / DEX par strike (fenêtre réglable ±2/4/10 %), calls/puts au survol
- Niveaux 0DTE tracés : **GEX1-5** (murs de gamma), **Flip** (zero gamma,
  pondéré open interest), **HVL** (bascule pondérée par le volume du jour)
- GEX net, P/C ratios, skew IV par expiration, vue par échéance (0DTE/semaine/mois)
- Flux delta 1 min (proxy Δvolume×δ) avec sélecteur de journée
- Historique GEX net & spot vs zero gamma (s'accumule automatiquement)
- Backfill historique optionnel via Databento (`gex/backfill.py`, payant,
  devis affiché avant tout téléchargement)
- Serveur MCP (`gex/mcp_server.py`) pour interroger les données depuis Claude

## Backfill Databento (optionnel)

Copier `.env.example`, renseigner `DATABENTO_API_KEY`, puis par ex. :
`python -m gex.backfill --daily-days 31 --intraday-days 7 --max-cost 40`.
Les fichiers bruts sont conservés dans `data/databento/` : relancer ne
refacture jamais ce qui est déjà téléchargé. La passerelle Databento peut
renvoyer des 504 sur les grosses requêtes : préférer des tranches d'une
semaine (`--end` + `--daily-days 7`).

## Architecture

- `gex/ingest.py` — fetch + parsing des chaînes (retry/backoff)
- `gex/greeks.py` — Black-Scholes vectorisé (testé sur valeurs Hull)
- `gex/metrics.py` — GEX/DEX par strike, zero gamma, P/C, flux delta
- `gex/store.py` — Parquet : snapshots complets (10 min), flux (1 min), historique
- `gex/scheduler.py` — boucle APScheduler, heures de marché ET (9:30–16:15)
- `gex/app.py` — dashboard Dash (refresh auto 60 s)

## Conventions de calcul

- **GEX** ($ par 1 % de move) = γ × OI × 100 × spot² × 0,01 — calls positifs,
  puts négatifs (convention « naive » SpotGamma : dealers longs calls, courts puts).
- **Zero Gamma** : recalcul du profil de GEX net sur une grille de spots ±8 %
  (IV et maturités figées), interpolation du passage par zéro le plus proche du spot.
- **Flux delta** (proxy) = Δvolume entre deux pulls × δ × 100 × spot. Le sens
  taker n'est pas observable dans ce feed : pression delta-pondérée, pas un
  vrai order-flow signé.
- Échéances posées à 16:00 ET ; contrats expirés exclus ; 0DTE gardé en séance
  avec plancher de 5 min sur t.

## Soutenir le projet

Le dashboard est gratuit, sans publicité et sans collecte de données — et il le
restera. Si tu l'utilises et qu'il te fait gagner du temps, tu peux offrir un
café au développement :

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-dwarfsquirrel-FFDD00?style=flat-square&logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/dwarfsquirrel)

C'est entièrement facultatif. Un don n'ouvre droit à aucun support, aucune
priorité sur les fonctionnalités et aucune garantie — les termes de la
[licence MIT](LICENSE) et de l'[avertissement](DISCLAIMER.md) restent
inchangés. Signaler un bug ou proposer une amélioration aide tout autant.

## Limites connues

- Données délayées 15 min — outil de lecture de structure, pas d'exécution.
- Endpoint CBOE non contractuel : le format peut changer (l'ingestion est
  isolée pour pouvoir brancher une autre source, ex. Tradier).
- **SPY et QQQ** : ces ETF versent un dividende, or le calcul suppose un
  rendement nul (q = 0). L'approximation reste faible sur les échéances
  courtes mais n'est pas nulle — les indices SPX et NDX, eux, n'ont pas ce
  biais. Ils n'ont par ailleurs pas de future associé, donc le sélecteur
  Indice/Futures y est inactif.
