"""Écritures concurrentes sur les fichiers Parquet.

history/metrics.parquet a TROIS producteurs, chacun dans son propre thread
APScheduler : pull_all (CBOE, 60 s), pull_native_options (NQ/ES, 15 min) et
pull_native_index (SPX/NDX, 3 min). Le 2026-07-29, ce troisième producteur a
suffi à rendre les collisions quasi certaines : `_write_atomic` dérivait le
nom de son fichier temporaire de la seule destination, donc deux threads
écrivaient dans le MÊME `.tmp`, entrelaçaient leurs octets, et `os.replace`
publiait le résultat. Fichier illisible (« Page was smaller than expected »),
alors que chaque écriture était correcte prise isolément.
"""
from __future__ import annotations

import threading

import pandas as pd
import pytest

from gex import store
from gex.config import SETTINGS


def test_ecritures_concurrentes_ne_corrompent_pas(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    erreurs: list[BaseException] = []

    def writer(n: int) -> None:
        try:
            for i in range(15):
                store.append_history({
                    "timestamp": pd.Timestamp("2026-07-29 10:00") + pd.Timedelta(minutes=i),
                    "symbol": f"T{n}", "spot": 100.0 + i, "net_gex": 1e9,
                    "zero_gamma": 101.0, "pc_oi": 1.0, "pc_volume": 1.0,
                    "net_gex_0dte": 0.0, "basis": None, "source": "cboe",
                    "net_dex": 0.0,
                })
        except BaseException as e:  # noqa: BLE001 — remonté au test
            erreurs.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not erreurs, f"écriture en échec : {erreurs[:1]}"
    out = store.load_history()          # doit rester LISIBLE
    # et aucune ligne perdue : 4 producteurs x 15 lignes
    assert len(out) == 60
    assert set(out["symbol"]) == {"T0", "T1", "T2", "T3"}


def test_temporaire_unique_par_ecriture(tmp_path, monkeypatch):
    """Deux écritures simultanées ne doivent jamais viser le même fichier
    temporaire — c'est la cause directe de la corruption."""
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    vus: list[str] = []
    vrai_to_parquet = pd.DataFrame.to_parquet

    def espion(self, path, *a, **k):
        vus.append(str(path))
        return vrai_to_parquet(self, path, *a, **k)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", espion)
    cible = tmp_path / "x.parquet"
    for _ in range(5):
        store._write_atomic(pd.DataFrame([{"a": 1}]), cible)

    assert len(set(vus)) == 5, "le nom du temporaire doit varier à chaque écriture"
    assert all(v != str(cible) for v in vus)


def test_pas_de_temporaire_orphelin_apres_echec(tmp_path, monkeypatch):
    """Une écriture qui échoue ne doit pas laisser de `.tmp` derrière elle."""
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)

    def boum(self, path, *a, **k):
        raise OSError("disque plein")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", boum)
    with pytest.raises(OSError):
        store._write_atomic(pd.DataFrame([{"a": 1}]), tmp_path / "y.parquet")

    assert list(tmp_path.glob("*.tmp")) == []
