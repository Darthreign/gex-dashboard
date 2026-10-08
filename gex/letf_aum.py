"""Mise à jour automatique des encours (AUM) des ETF à levier — page /moc.

Le rééquilibrage de clôture d'un ETF à levier vaut AUM × (L² − L) × rendement
du jour (cf. gex/moc.py) : l'encours doit être celui de la veille, à quelques
pour cent près. Ce module le récupère chaque soir, gratuitement, et l'écrit
dans `data/moc_letf.json` (même format que la saisie à la main).

Sources, dans l'ordre, pour chaque ETF :
1. Yahoo Finance (`totalAssets`), via la bibliothèque `yfinance` si elle est
   installée (elle suit les changements d'accès de Yahoo), sinon par
   requêtes directes (cookie + « crumb ») ;
2. la page produit de l'émetteur (ProShares), « Net Assets » lu dans le HTML.

Garde-fous — une valeur douteuse ne remplace jamais une bonne :
- bornes absolues (5 M$ à 300 Md$) ;
- écart à la dernière valeur récupérée borné (÷3 à ×3 : un ETF ne perd ni ne
  gagne les deux tiers de son encours en une journée) ;
- un ETF qu'aucune source ne donne garde sa dernière valeur, avec sa date.
Le levier n'est jamais lu en ligne : il est fixé par le prospectus
(moc.DEFAULT_LETF).

    python scripts/update_letf_aum.py        # à la main
    (et chaque soir de semaine à 18h10 ET par le scheduler d'ingestion)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .config import SETTINGS
from .metrics import ET

log = logging.getLogger(__name__)

MIN_AUM, MAX_AUM = 5e6, 3e11
MAX_DAILY_RATIO = 3.0
STALE_DAYS = 7
TIMEOUT_S = 15
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PROSHARES = {"TQQQ", "SQQQ", "QLD", "QID", "PSQ", "UPRO", "SPXU", "SSO", "SDS", "SH"}


def path() -> Path:
    return SETTINGS.data_dir / "moc_letf.json"


# --- sources --------------------------------------------------------------------

def fetch_yahoo(tickers: list[str]) -> dict[str, float]:
    try:
        import yfinance as yf          # optionnel
    except ImportError:
        yf = None
    out: dict[str, float] = {}
    if yf is not None:
        for t in tickers:
            try:
                v = (yf.Ticker(t).info or {}).get("totalAssets")
                if v:
                    out[t] = float(v)
            except Exception as e:  # noqa: BLE001 — une source qui échoue passe la main
                log.info("yfinance %s : %s", t, e)
        return out
    s = requests.Session()
    s.headers["User-Agent"] = UA
    try:
        s.get("https://fc.yahoo.com", timeout=TIMEOUT_S)          # dépose le cookie
        crumb = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb",
                      timeout=TIMEOUT_S).text.strip()
    except requests.RequestException as e:
        log.info("Yahoo injoignable : %s", e)
        return out
    if not crumb or "<" in crumb or len(crumb) > 64:
        log.info("Yahoo : crumb refusé")
        return out
    for t in tickers:
        try:
            r = s.get(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{t}",
                      params={"modules": "summaryDetail", "crumb": crumb}, timeout=TIMEOUT_S)
            res = r.json()["quoteSummary"]["result"][0]["summaryDetail"]
            v = (res.get("totalAssets") or {}).get("raw")
            if v:
                out[t] = float(v)
        except Exception as e:  # noqa: BLE001
            log.info("Yahoo %s : %s", t, e)
    return out


_NET_ASSETS = re.compile(
    r"Net\s+Assets[^$]{0,200}?\$\s*([\d.,]+)\s*(billion|million|thousand|[BMK])?\b",
    re.IGNORECASE | re.DOTALL)
_SCALE = {"billion": 1e9, "b": 1e9, "million": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}


def parse_net_assets(html: str) -> float | None:
    """« Net Assets … $27.41 billion » / « $27,410,123,456 » -> dollars."""
    text = re.sub(r"<[^>]+>", " ", html)
    m = _NET_ASSETS.search(text)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return v * _SCALE.get((m.group(2) or "").lower(), 1.0)


def fetch_issuer(tickers: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in tickers:
        if t not in PROSHARES:
            continue
        try:
            r = requests.get(f"https://www.proshares.com/our-etfs/leveraged-and-inverse/{t.lower()}",
                             headers={"User-Agent": UA}, timeout=TIMEOUT_S)
            v = parse_net_assets(r.text) if r.ok else None
            if v:
                out[t] = v
        except requests.RequestException as e:
            log.info("ProShares %s : %s", t, e)
    return out


SOURCES = (("yahoo", fetch_yahoo), ("proshares", fetch_issuer))


# --- mise à jour ----------------------------------------------------------------

def plausible(new: float, prev: float | None) -> bool:
    if not (MIN_AUM <= new <= MAX_AUM):
        return False
    if prev:
        r = new / prev
        return 1 / MAX_DAILY_RATIO <= r <= MAX_DAILY_RATIO
    return True


def load(p: Path | None = None) -> dict:
    p = p or path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def update(sources=SOURCES, now: datetime | None = None, p: Path | None = None) -> dict:
    """Récupère les AUM, applique les garde-fous et réécrit le fichier.
    Renvoie {"updated": [...], "kept": [...], "rejected": {...}}."""
    from .moc import DEFAULT_LETF
    p = p or path()
    now = now or datetime.now(ET)
    old = load(p)
    meta = old.get("_meta", {}).get("etf", {})
    tickers = [t for fam in DEFAULT_LETF.values() for t in fam]
    got: dict[str, tuple[float, str]] = {}
    rejected: dict[str, str] = {}
    for name, fetch in sources:
        todo = [t for t in tickers if t not in got]
        if not todo:
            break
        try:
            vals = fetch(todo)
        except Exception as e:  # noqa: BLE001
            log.warning("Source %s en échec : %s", name, e)
            continue
        for t, v in vals.items():
            prev = meta.get(t, {}).get("aum")
            if plausible(v, prev):
                got[t] = (v, name)
            else:
                rejected[t] = f"{name}: {v:,.0f} $ (précédent {prev or 0:,.0f} $)"
    out: dict = {}
    new_meta: dict = {}
    updated, kept = [], []
    for sym, fam in DEFAULT_LETF.items():
        out[sym] = {}
        for t, (lev, default_aum) in fam.items():
            if t in got:
                aum, src = got[t]
                new_meta[t] = {"aum": aum, "source": src, "asof": now.isoformat(timespec="minutes")}
                updated.append(t)
            elif t in meta:
                aum = meta[t]["aum"]
                new_meta[t] = meta[t]
                kept.append(t)
            else:
                aum = default_aum
                new_meta[t] = {"aum": aum, "source": "défaut", "asof": None}
                kept.append(t)
            out[sym][t] = [lev, aum]
    out["_meta"] = {"updated": now.isoformat(timespec="minutes"), "etf": new_meta}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    log.info("AUM ETF à levier : %d mis à jour, %d conservés, %d rejetés",
             len(updated), len(kept), len(rejected))
    return {"updated": updated, "kept": kept, "rejected": rejected}


def freshness(symbol: str, now: datetime | None = None) -> tuple[str | None, bool]:
    """(date de la plus ancienne valeur RÉCUPÉRÉE de la famille, périmée ?).
    (None, True) si aucune valeur n'a jamais été récupérée."""
    from .moc import DEFAULT_LETF
    now = now or datetime.now(ET)
    meta = load().get("_meta", {}).get("etf", {})
    dates = [meta.get(t, {}).get("asof") for t in DEFAULT_LETF.get(symbol, {})]
    if not dates or any(d is None for d in dates):
        return None, True
    oldest = min(datetime.fromisoformat(d) for d in dates)
    return oldest.strftime("%Y-%m-%d"), now - oldest > timedelta(days=STALE_DAYS)


def is_stale(now: datetime | None = None) -> bool:
    from .moc import DEFAULT_LETF
    return any(freshness(s, now)[1] for s in DEFAULT_LETF)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    r = update()
    print(f"Mis à jour : {', '.join(r['updated']) or 'aucun'}")
    print(f"Conservés (aucune source) : {', '.join(r['kept']) or 'aucun'}")
    for t, why in r["rejected"].items():
        print(f"Rejeté {t} : {why}")
    print(f"-> {path()}")
