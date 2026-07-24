# GEX Dashboard — analyse Gamma/Delta Exposure (SPX/ES, NDX/NQ)

Dashboard **d'analyse uniquement** (pas de trading) qui reconstruit les métriques
de structure de marché façon SpotGamma à partir des chaînes d'options CBOE :
Gamma Exposure par strike, Delta Exposure, GEX net, niveau Zero Gamma,
put/call ratios, skew IV, et proxy de flux delta intraday.

## Source de données

Endpoint delayed public CBOE (non documenté officiellement) :
`https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json` (indices
préfixés `_`). Un GET ramène la chaîne complète — bid/ask, IV, open interest,
volume, Greeks — plus le spot. **Délai ~15 min à la source**, régénéré ~toutes
les 60 s (timestamp du feed en UTC). Sous-jacents : SPX et NDX actifs par
défaut, SPY/QQQ en fallback désactivé (`gex/config.py`).

## Démarrage

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py        # dashboard sur http://127.0.0.1:8050
```

Tests : `.venv\Scripts\python -m pytest tests/`

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

## Limites connues

- Données délayées 15 min — outil de lecture de structure, pas d'exécution.
- Endpoint CBOE non contractuel : le format peut changer (l'ingestion est
  isolée pour pouvoir brancher une autre source, ex. Tradier).
- Niveaux exprimés en points d'indice (SPX/NDX), pas convertis en ES/NQ.
