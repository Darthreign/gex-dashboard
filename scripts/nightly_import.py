"""Job RÉCURRENT — chaque soir Lun-Ven 23h05 Paris (après la clôture CME).

Exporte la SÉANCE CME du jour (définie en ET : 18:00 ET -> 16:59 ET, cf.
_session_date) depuis data/ticks live vers import/ticks_full/NQ,
sans jamais écraser un fichier existant :
  - séance live complète  -> dxfeed, bid/ask CONSERVÉS ;
  - séance live incomplète -> repli Databento (schéma trades, sans bid/ask,
    ~0,35 $), choix validé par l'utilisateur.

Ne touche NI au code du capteur NI au serveur (déjà en place) : uniquement
l'import du jour. Journalisé dans logs/nightly_import.log.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(r"D:/Gex")
TICKS = ROOT / "data" / "ticks" / "NQ"
IMPORT = ROOT / "data" / "import" / "ticks_full" / "NQ"
RAW = ROOT / "data" / "import" / "databento_raw"
LOG = ROOT / "logs" / "nightly_import.log"
PARIS = ZoneInfo("Europe/Paris")   # affichage/log seulement
ET = ZoneInfo("America/New_York")   # référence des séances CME


def log(msg: str) -> None:
    line = f"{dt.datetime.now(PARIS):%Y-%m-%d %H:%M:%S} | {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def target_day() -> dt.date | None:
    """Séance à importer = celle qui vient de clôturer (16:59 ET). Déterminée en
    ET pour rester correcte même pendant les semaines de bascule DST décalée :
    après la clôture on prend le jour ET courant, avant on prend la veille.
    Week-end (pas de séance) -> None."""
    now = dt.datetime.now(ET)
    d = now.date() if now.hour >= 17 else now.date() - dt.timedelta(days=1)
    return None if d.weekday() >= 5 else d


def _session_date(ts: pd.Series) -> pd.Series:
    """Date de SÉANCE CME, définie en heure de New York : 18:00 ET -> 16:59 ET
    du lendemain, soit (ET + 6 h).date().

    ⚠️ Jamais l'heure de Paris : Paris ne vaut ET+6 que quand les deux zones
    sont en heure d'été ensemble. Sur les ~3 semaines/an de bascules décalées
    (mi-mars, fin octobre) l'écart tombe à 5 h et la séance serait coupée au
    mauvais endroit. L'ET est la référence du marché."""
    et = pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert(ET)
    return (et + pd.Timedelta(hours=6)).dt.date.astype(str)


def _live_session(day: str) -> pd.DataFrame:
    """Ticks live (dxfeed) de la séance CME `day`. Lit le fichier du jour et ses
    voisins (une séance peut chevaucher 2 fichiers journaliers), puis filtre sur
    la date de séance en ET."""
    d0 = dt.date.fromisoformat(day)
    names = {(d0 + dt.timedelta(days=k)).isoformat() for k in (-1, 0, 1)}
    frames = []
    for f in sorted(TICKS.glob("*.parquet")):
        if f.stem in names:
            try:
                frames.append(pd.read_parquet(f))
            except Exception as e:  # noqa: BLE001
                log(f"   (live) illisible {f.name}: {e}")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df[_session_date(df["ts"]) == day]
    # ⚠️ Surtout PAS de drop_duplicates ici : sur du tick, des transactions
    # réellement distinctes partagent couramment (ts, price, volume) — plusieurs
    # lots d'un contrat à la même milliseconde et au même prix. Dédupliquer là
    # -dessus avait supprimé 27 % d'une séance (135 942 trades le 2026-08-18).
    # Le risque de doublon n'existe pas : chaque tick n'est écrit qu'une fois,
    # dans le fichier de sa séance.
    # tri STABLE : 42 % des prints partagent leur horodatage (jusqu'a 271 sur
    # une meme milliseconde), et le moteur de backtest rejoue tick par tick a la
    # ms — l'ordre AU SEIN d'une ms est donc porteur d'information (sequence
    # reelle du marche). Un tri non stable (defaut pandas) pourrait le permuter.
    return df.sort_values("ts", kind="stable").reset_index(drop=True)


# Recoupement avec Databento : notre flux dxFeed produit ~1,29 fois plus de
# prints que Databento pour le MEME volume — dxFeed livre les fills un par un
# la ou Databento agrege ceux d'un meme ordre agresseur (mesure le 2026-08-31
# sur une fenetre continue : ratio volume 0,997, ratio prints 1,294 ; ratio
# stable 1,25-1,30 sur six seances). Une seance saine est donc largement
# au-dessus de 1 ; sous ce seuil, il manque des donnees.
MIN_RATIO = 0.9
# Repli hors ligne : trou interne au-dela duquel la seance est jugee incomplete.
# Genereux a dessein — une nuit de veille de jour ferie peut legitimement ne
# rien traiter pendant des heures (Thanksgiving 2025 : 28 trades en 10,8 h).
MAX_GAP_S = 6 * 3600


def _expected_count(day: str) -> int | None:
    """Nombre de trades de la seance chez Databento. `get_record_count` est un
    appel de metadonnees : il ne facture rien et ne telecharge rien."""
    try:
        import databento as db
        c = db.Historical(_db_key())
        start, end = _session_window(day)
        return c.metadata.get_record_count(
            dataset="GLBX.MDP3", symbols=["NQ.v.0"], stype_in="continuous",
            schema="trades", start=start.isoformat(), end=end.isoformat())
    except Exception as e:  # noqa: BLE001 — le controle ne doit jamais bloquer
        log(f"   (controle) comptage Databento indisponible : {type(e).__name__}: {e}")
        return None


def _complete(df: pd.DataFrame, day: str) -> bool:
    """La seance capturee est-elle exploitable telle quelle ?

    Verifier le seul premier tick ne suffit pas : le 2026-08-28 demarrait bien
    a 18:00 ET mais avait un trou de 14,5 h au milieu (serveur eteint la nuit),
    et il a ete ecrit comme « complet ». On recoupe donc le NOMBRE de prints
    avec Databento, ce qui detecte un manque ou qu'il soit dans la seance.
    """
    if df.empty:
        return False
    first = pd.to_datetime(df["ts"].min(), unit="s", utc=True).tz_convert(ET)         + pd.Timedelta(hours=6)
    if (first.hour * 60 + first.minute) > 10:      # demarrage apres 18:10 ET
        return False

    attendu = _expected_count(day)
    if attendu:
        ratio = len(df) / attendu
        if ratio < MIN_RATIO:
            log(f"   (controle) {len(df):,} prints captures pour {attendu:,} "
                f"attendus (ratio {ratio:.2f}) -> seance incomplete")
            return False
        log(f"   (controle) {len(df):,} prints vs {attendu:,} Databento "
            f"(ratio {ratio:.2f}) -> seance complete")
        return True

    # Sans le comptage de reference : au moins verifier l'absence de gros trou.
    trou = df["ts"].sort_values().diff().max()
    if trou and trou > MAX_GAP_S:
        log(f"   (controle) trou interne de {trou / 3600:.1f} h -> seance incomplete")
        return False
    return True


def _session_window(day: str) -> tuple[dt.datetime, dt.datetime]:
    """Bornes UTC de la seance CME `day` : 18:00 ET la veille -> 17:00 ET le
    jour meme. Jamais en heure de Paris (cf. _session_date)."""
    d0 = dt.date.fromisoformat(day)
    start = dt.datetime.combine(d0 - dt.timedelta(days=1), dt.time(18, 0), ET)         .astimezone(dt.timezone.utc)
    end = dt.datetime.combine(d0, dt.time(17, 0), ET).astimezone(dt.timezone.utc)
    return start, end


def _db_key() -> str | None:
    import os
    k = os.environ.get("DATABENTO_API_KEY")
    if k:
        return k
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
            return winreg.QueryValueEx(h, "DATABENTO_API_KEY")[0]
    except OSError:
        return None


def _databento_session(day: str) -> pd.DataFrame:
    import databento as db
    key = _db_key()
    client = db.Historical(key) if key else db.Historical()
    start, end = _session_window(day)
    RAW.mkdir(parents=True, exist_ok=True)
    params = dict(dataset="GLBX.MDP3", symbols=["NQ.v.0"], stype_in="continuous",
                  schema="trades", start=start.isoformat(), end=end.isoformat())
    log(f"   (databento) {day} : coût estimé {client.metadata.get_cost(**params):.4f} USD")
    store = client.timeseries.get_range(**params, path=str(RAW / f"NQ_v0_trades_{day}.dbn.zst"))
    d = store.to_df(price_type="float").reset_index()
    ts = pd.to_datetime(d["ts_event"], utc=True)
    side_map = {"B": "BUY", "A": "SELL", "N": None}
    out = pd.DataFrame({
        "ts": ts.astype("int64") / 1e9,
        "price": d["price"].astype(float),
        "volume": d["size"].astype("int64"),
        "side": d["side"].map(side_map),
        "source": "databento",
    })
    return out[_session_date(out["ts"]) == day].reset_index(drop=True)


def _write_if_new(df: pd.DataFrame, day: str, origin: str) -> None:
    IMPORT.mkdir(parents=True, exist_ok=True)
    out = IMPORT / f"{day}.parquet"
    if out.exists():
        log(f"{day} : déjà présent dans import — NON réécrit.")
        return
    base = [c for c in ["ts", "price", "volume", "side", "source"] if c in df.columns]
    extra = [c for c in ["bid", "ask"] if c in df.columns]
    df = df[base + extra].sort_values("ts", kind="stable").reset_index(drop=True)
    df.to_parquet(out, index=False)
    log(f"{day} : ÉCRIT ({origin}) — {len(df):,} lignes, colonnes {list(df.columns)}")


def main() -> None:
    day = target_day()
    if day is None:
        log("Week-end / pas de séance — rien à importer.")
        return
    day = day.isoformat()
    log(f"===== Import nocturne {day} — DÉBUT =====")
    try:
        if (IMPORT / f"{day}.parquet").exists():
            log(f"{day} : déjà présent dans import — NON réécrit.")
        else:
            live = _live_session(day)
            if _complete(live, day):
                _write_if_new(live, day, "dxfeed live, bid/ask conservés")
            else:
                where = "" if live.empty else (
                    f" (live commence à "
                    f"{pd.to_datetime(live['ts'].min(), unit='s', utc=True).tz_convert(ET):%H:%M} ET)")
                log(f"{day} : capture live incomplète{where} -> repli Databento.")
                _write_if_new(_databento_session(day), day, "databento, sans bid/ask")
    except Exception as e:  # noqa: BLE001
        log(f"{day} : ÉCHEC import — {type(e).__name__}: {e}")
    log(f"===== Import nocturne {day} — FIN =====")


if __name__ == "__main__":
    main()
