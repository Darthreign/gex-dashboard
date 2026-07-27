"""Backfill de bougies 1 min historiques via dxFeed (dxLink), optionnel.

Le flux temps réel n'enregistre que ce qu'il voit passer : au démarrage, aucun
historique. Or trois chantiers en ont besoin tout de suite — le backtest de
niveaux (pour mesurer des taux de tenue sur un échantillon crédible), le
parcours du prix des séances passées sur le heatmap, et l'estimation des bêtas
des constituants. Ce module va chercher ce passé au lieu de l'attendre.

Usage :
    python -m gex.pricehist                 # 45 jours, tous les symboles
    python -m gex.pricehist --days 20 --symbols NQ ES NVDA
    python -m gex.pricehist --period 5m     # granularité plus grossière,
                                            # donc davantage de jours couverts

⚠️ Données courtier : NON redistribuables. Écrites dans data/prices/ avec
`source="dxfeed"`, répertoire que l'export ne parcourt pas (cf. gex/export.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import pandas as pd

from . import store
from .config import UNDERLYINGS
from .metrics import ET
from .rtquote import credentials_present, quote_token, resolve_symbols

log = logging.getLogger(__name__)

# dxFeed plafonne une souscription historique à quelques milliers de bougies.
# Observé : ~8 000 par requête, soit environ six semaines de minutes sur un
# sous-jacent liquide. Une granularité plus grossière couvre d'autant plus de
# jours pour le même plafond.
OBSERVED_CAP = 8000
# Silence après lequel on considère que dxFeed a fini d'envoyer l'historique.
IDLE_TIMEOUT_S = 12.0


def candle_symbol(stream_symbol: str, period: str = "m") -> str:
    """Symbole de bougie dxFeed : le symbole du flux suffixé de sa période."""
    return f"{stream_symbol}{{={period}}}"


def candles_to_frame(rows: list[dict]) -> pd.DataFrame:
    """Événements Candle -> table OHLCV horodatée en heure de New York.

    Les bougies sans clôture exploitable sont écartées : dxFeed envoie des
    enregistrements de synchronisation dont tous les champs valent NaN, qui
    créeraient des trous au milieu d'une séance.
    """
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for col in ("time", "open", "high", "low", "close"):
        if col not in df.columns:
            return pd.DataFrame()
    df = df[df["close"].notna() & df["time"].notna()]
    if df.empty:
        return pd.DataFrame()
    ts = pd.to_datetime(df["time"], unit="ms", utc=True)
    out = pd.DataFrame({
        "timestamp": ts.dt.tz_convert(ET).dt.tz_localize(None),
        "open": df["open"].astype(float),
        "high": df["high"].astype(float),
        "low": df["low"].astype(float),
        "close": df["close"].astype(float),
        # `ticks` compte les transactions sur le flux temps réel ; ici dxFeed
        # fournit le volume, plus riche. Colonne distincte pour ne pas mélanger
        # deux grandeurs sous un même nom.
        "volume": df.get("volume", pd.Series(index=df.index, dtype=float)).astype(float),
        "source": "dxfeed",
    })
    return out.dropna(subset=["timestamp"]).sort_values("timestamp")


def write_by_day(symbol: str, df: pd.DataFrame, min_bars: int = 2) -> list[str]:
    """Répartit les bougies dans les fichiers journaliers du stockage.

    Les journées comptant moins de `min_bars` bougies sont écartées : dxFeed
    renvoie une bougie isolée à la borne `fromTime` demandée, séparée de
    plusieurs semaines du reste de la série. La garder fabriquerait une séance
    fantôme dans le sélecteur, inexploitable par ailleurs — le backtest comme
    le tracé du prix exigent au minimum deux points.
    """
    if df.empty:
        return []
    days = []
    for day, part in df.groupby(df["timestamp"].dt.date):
        if len(part) < min_bars:
            log.debug("%s %s : %d bougie(s) isolée(s), ignorée(s)",
                      symbol, day, len(part))
            continue
        rows = part.to_dict("records")
        store.append_prices(symbol, rows, rows[0]["timestamp"])
        days.append(str(day))
    return days


async def fetch(stream_symbols: dict[str, str], days: int,
                period: str = "m") -> dict[str, pd.DataFrame]:
    """Récupère l'historique de bougies pour un ensemble de symboles.

    Tout est demandé sur une seule connexion : dxFeed livre les séries en vrac,
    on les redistribue ensuite par symbole. La collecte s'arrête après
    IDLE_TIMEOUT_S sans nouvel envoi — il n'existe pas de marqueur de fin.
    """
    import websockets

    token, url, _ = quote_token()
    from_ms = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)
    by_candle = {candle_symbol(v, period): k for k, v in stream_symbols.items()}
    rows: dict[str, list[dict]] = defaultdict(list)

    async with websockets.connect(url, max_size=2 ** 24) as ws:
        async def send(m):
            await ws.send(json.dumps(m))

        await send({"type": "SETUP", "channel": 0, "version": "0.1-gex-hist",
                    "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})
        auth_sent = False
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=IDLE_TIMEOUT_S)
            except asyncio.TimeoutError:
                break            # plus rien n'arrive : historique complet
            except websockets.exceptions.ConnectionClosed:
                # dxFeed clôt parfois la connexion une fois l'historique livré,
                # ou quand une autre session utilise le même jeton. Dans les
                # deux cas ce qui est déjà arrivé reste valable : on le garde
                # plutôt que de tout perdre sur une exception.
                log.info("Connexion fermée par dxFeed — %d symboles reçus",
                         len(rows))
                break
            m = json.loads(raw)
            typ = m.get("type")
            if typ == "AUTH_STATE":
                state = m.get("state")
                if state == "UNAUTHORIZED" and not auth_sent:
                    auth_sent = True
                    await send({"type": "AUTH", "channel": 0, "token": token})
                elif state == "UNAUTHORIZED":
                    raise RuntimeError("jeton dxFeed refusé")
                elif state == "AUTHORIZED":
                    # contrat HISTORY : c'est lui qui autorise fromTime
                    await send({"type": "CHANNEL_REQUEST", "channel": 1,
                                "service": "FEED",
                                "parameters": {"contract": "HISTORY"}})
            elif typ == "CHANNEL_OPENED":
                subs = [{"type": "Candle", "symbol": s, "fromTime": from_ms}
                        for s in by_candle]
                await send({"type": "FEED_SUBSCRIPTION", "channel": 1, "add": subs})
                log.info("Historique demandé : %d symboles depuis %s",
                         len(subs), datetime.fromtimestamp(from_ms / 1000, UTC).date())
            elif typ == "FEED_DATA":
                for item in (m.get("data") or []):
                    if isinstance(item, dict) and item.get("eventType") == "Candle":
                        key = by_candle.get(item.get("eventSymbol"))
                        if key:
                            rows[key].append(item)
            elif typ == "ERROR":
                log.warning("dxFeed ERROR : %s", str(m)[:200])

    return {k: candles_to_frame(v) for k, v in rows.items()}


def backfill(symbols: list[str] | None = None, days: int = 45,
             period: str = "m") -> dict[str, int]:
    """Télécharge et enregistre l'historique. Renvoie {symbole: nb de bougies}."""
    if not credentials_present():
        log.error("Identifiants tastytrade absents — backfill impossible")
        return {}
    _, _, access = quote_token()
    resolved = resolve_symbols(access)
    if symbols:
        missing = [s for s in symbols if s not in resolved]
        if missing:
            log.warning("Symboles inconnus, ignorés : %s", ", ".join(missing))
        resolved = {k: v for k, v in resolved.items() if k in symbols}
    if not resolved:
        log.error("Aucun symbole à traiter")
        return {}

    frames = asyncio.run(fetch(resolved, days, period))
    counts: dict[str, int] = {}
    for key, df in sorted(frames.items()):
        if df.empty:
            log.warning("%s : aucune bougie reçue", key)
            continue
        days_written = write_by_day(key, df)
        counts[key] = len(df)
        log.info("%-6s %5d bougies  %s → %s  (%d séances)", key, len(df),
                 df["timestamp"].min().date(), df["timestamp"].max().date(),
                 len(days_written))
        if len(df) >= OBSERVED_CAP:
            log.info("%s : plafond atteint — l'historique remonte probablement "
                     "plus loin, relancer avec --period 5m pour couvrir "
                     "davantage de jours", key)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=45,
                        help="profondeur demandée (défaut : 45)")
    parser.add_argument("--period", default="m",
                        help="granularité dxFeed : m, 5m, 15m, h… (défaut : m)")
    parser.add_argument("--symbols", nargs="*",
                        help=f"sous-ensemble ; défaut : tous "
                             f"({', '.join(sorted(UNDERLYINGS))} + futures)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    counts = backfill(args.symbols, args.days, args.period)
    print(f"\n{sum(counts.values())} bougies écrites sur {len(counts)} symboles.")


if __name__ == "__main__":
    main()
