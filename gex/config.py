"""Configuration centrale du dashboard GEX."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

def _default_data_dir() -> Path:
    """data/ à la racine du dépôt si on tourne depuis les sources, sinon dans
    le dossier courant (cas d'un `pip install` : le code vit dans
    site-packages, où l'on n'écrit pas)."""
    root = Path(__file__).resolve().parent.parent
    if (root / ".git").exists() or (root / "pyproject.toml").exists():
        return root / "data"
    return Path.cwd() / "data"


DATA_DIR = _default_data_dir()

# Taux sans risque annualisé utilisé dans Black-Scholes (approx T-bills 3M).
RISK_FREE_RATE = 0.045

# Multiplicateur de contrat (SPX, NDX, SPY, QQQ : 100).
CONTRACT_MULTIPLIER = 100


@dataclass(frozen=True)
class Underlying:
    key: str            # identifiant interne ("SPX")
    cboe_symbol: str    # symbole endpoint CBOE ("_SPX")
    label: str          # libellé affiché ("SPX / ES")
    future: str | None = None   # future CME associé, pour la conversion de basis
    # Famille d'indice : la transposition entre familles est possible mais son
    # ratio dérive dans le temps (cf. gex/scales.py).
    family: str = "SP"
    enabled: bool = True


UNDERLYINGS: dict[str, Underlying] = {
    u.key: u
    for u in [
        # libellés = simples tickers : l'échelle d'affichage (ES/NQ) se choisit
        # dans son propre sélecteur, la mentionner ici ferait doublon
        Underlying("SPX", "_SPX", "SPX", future="ES", family="SP"),
        Underlying("NDX", "_NDX", "NDX", future="NQ", family="ND"),
        # ETF : pas de future associé (le sélecteur Indice/Futures est donc
        # désactivé), options américaines, et sous-jacents versant un dividende
        # — voir la note sur l'approximation q=0 dans les limites du README.
        Underlying("SPY", "SPY", "SPY", family="SP"),
        Underlying("QQQ", "QQQ", "QQQ", family="ND"),
    ]
}


@dataclass
class Settings:
    # Intervalle de pull des flux (secondes). 60 s = résolution max utile
    # (le feed CBOE est délayé 15 min à la source, ceci ne change que la
    # résolution d'échantillonnage, pas le délai).
    flow_interval_s: int = 60
    # Intervalle de persistance d'un snapshot complet de chaîne (secondes).
    snapshot_interval_s: int = 600
    # Fenêtre de strikes affichée autour du spot (fraction).
    strike_window: float = 0.10
    # Grille de recherche du zero gamma autour du spot (fraction, pas).
    zg_range: float = 0.08
    zg_steps: int = 161
    # Ne puller que pendant les heures de marché US (ET).
    market_hours_only: bool = True
    # Commit+push automatique du repo git data/ (historique+flux) après la
    # clôture (16:20 ET). Sans effet si data/ n'est pas un repo git avec remote.
    auto_push_data: bool = True
    data_dir: Path = field(default_factory=lambda: DATA_DIR)


SETTINGS = Settings()
