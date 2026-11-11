"""Bougies 1 minute cash pour Squirrel X : routes d'historique et événement `bar:`."""
from __future__ import annotations

import io
import json
from datetime import datetime

import pandas as pd
import pytest
from flask import Flask

from gex import cashbars, ticksapi
from gex.config import SETTINGS
from gex.rtquote import Bar, RealtimeQuotes

TOKEN = "jeton-de-test"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _write(root, symbol, day, times):
    d = root / "prices" / symbol
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"timestamp": datetime.fromisoformat(f"{day} {t}"), "open": 1.0, "high": 3.0,
             "low": 0.5, "close": 2.0, "ticks": 12, "source": "dxfeed"} for t in times]
    pd.DataFrame(rows).to_parquet(d / f"{day}.parquet", index=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    monkeypatch.setenv("GEX_TICKS_TOKEN", TOKEN)
    ticksapi._hits.clear()
    ticksapi._conns.clear()
    cashbars._has_session.clear()
    cashbars._built.clear()
    app = Flask(__name__)
    ticksapi.register_ticks_api(app)
    return app.test_client(), tmp_path


def test_days_sans_week_end_ni_jour_sans_seance(env):
    client, root = env
    _write(root, "QQQ", "2026-11-06", ["09:30", "15:59"])      # vendredi
    _write(root, "QQQ", "2026-11-07", ["10:00"])               # samedi : valeurs figées
    _write(root, "QQQ", "2026-09-07", ["05:00", "17:00"])      # férié : rien en séance
    r = client.get("/api/v1/ticks/bars/QQQ/days", headers=AUTH)
    assert r.status_code == 200 and r.get_json() == {"days": ["2026-11-06"]}


def test_fichier_borne_a_la_seance_en_secondes_utc(env):
    client, root = env
    _write(root, "SPX", "2026-11-10", ["09:29", "09:30", "15:59", "16:00"])   # heure d'hiver
    _write(root, "SPX", "2026-07-15", ["09:30"])                              # heure d'été
    r = client.get("/api/v1/ticks/bars/SPX/2026-11-10.parquet", headers=AUTH)
    df = pd.read_parquet(io.BytesIO(r.data))
    assert list(df.columns) == ["ts", "open", "high", "low", "close"]
    assert df["ts"].tolist() == [int(pd.Timestamp("2026-11-10 14:30", tz="UTC").timestamp()),
                                 int(pd.Timestamp("2026-11-10 20:59", tz="UTC").timestamp())]
    ete = pd.read_parquet(io.BytesIO(
        client.get("/api/v1/ticks/bars/SPX/2026-07-15.parquet", headers=AUTH).data))
    assert ete["ts"].tolist() == [int(pd.Timestamp("2026-07-15 13:30", tz="UTC").timestamp())]


def test_etag_304_puis_nouvelle_version(env):
    client, root = env
    _write(root, "SPY", "2026-11-10", ["09:30"])
    url = "/api/v1/ticks/bars/SPY/2026-11-10.parquet"
    etag = client.get(url, headers=AUTH).headers["ETag"]
    assert client.get(url, headers={**AUTH, "If-None-Match": etag}).status_code == 304
    _write(root, "SPY", "2026-11-10", ["09:30", "09:31"])
    r = client.get(url, headers={**AUTH, "If-None-Match": etag})
    assert r.status_code == 200 and len(pd.read_parquet(io.BytesIO(r.data))) == 2


@pytest.mark.parametrize("path", ["NQ/days", "AAPL/days", "qqq/days", "QQQ/2026-11-08.parquet",
                                  "QQQ/2026-11-09.parquet", "QQQ/pas-une-date.parquet"])
def test_symbole_ou_jour_inconnu_404(env, path):
    client, root = env
    _write(root, "QQQ", "2026-11-06", ["10:00"])
    _write(root, "QQQ", "2026-11-08", ["10:00"])               # dimanche
    _write(root, "NQ", "2026-11-06", ["10:00"])                # un future n'est pas servi ici
    assert client.get(f"/api/v1/ticks/bars/{path}", headers=AUTH).status_code == 404


def test_jeton_obligatoire_et_lecture_seule(env):
    client, root = env
    _write(root, "QQQ", "2026-11-06", ["10:00"])
    assert client.get("/api/v1/ticks/bars/QQQ/days").status_code == 401
    assert client.get("/api/v1/ticks/bars/QQQ/2026-11-06.parquet").status_code == 401
    assert client.post("/api/v1/ticks/bars/QQQ/days", headers=AUTH).status_code == 405


T0 = int(pd.Timestamp("2026-11-10 10:00", tz="America/New_York").timestamp())


def _producer(quotes, now, fichier=None):
    """`fichier` : minute -> Bar, ce que la capture a déjà écrit."""
    fichier = {} if fichier is None else fichier
    produce = cashbars.bar_producer("QQQ", quotes=quotes, clock=lambda: now[0],
                                    file_bar=lambda s, m: fichier.get(m))
    return lambda: (lambda p: p and json.loads(p[len("data: "):]))(produce())


def test_bougie_en_cours_emise_sur_changement():
    q, now = RealtimeQuotes(), [T0 + 5]
    read = _producer(q, now)
    assert read() is None                                      # aucun prix encore
    q._accumulate("QQQ", 687.0, T0)
    assert read() == {"symbol": "QQQ", "t": T0, "o": 687.0, "h": 687.0, "l": 687.0, "c": 687.0}
    assert read() is None                                      # inchangée : rien
    q._accumulate("QQQ", 687.5, T0)
    assert read()["c"] == 687.5


def test_cloture_emise_avec_les_valeurs_du_fichier_quand_il_arrive():
    q, now, fichier = RealtimeQuotes(), [T0 + 5], {}
    read = _producer(q, now, fichier)
    q._accumulate("QQQ", 687.0, T0)
    read()
    now[0] = T0 + 62                                           # minute suivante, premier prix
    q._accumulate("QQQ", 688.0, T0 + 60)
    suivante = {"symbol": "QQQ", "t": T0 + 60, "o": 688.0, "h": 688.0, "l": 688.0, "c": 688.0}
    assert read() == suivante                                  # fichier pas encore écrit
    assert read() is None
    fichier[T0] = Bar(T0, 687.01, 687.6, 686.9, 687.4)         # la capture a vidé ses bougies
    now[0] = T0 + 80
    assert read() == {"symbol": "QQQ", "t": T0, "o": 687.01, "h": 687.6, "l": 686.9, "c": 687.4,
                      "closed": True}
    assert read() is None                                      # une seule fois


def test_sans_fichier_la_bougie_locale_part_apres_l_attente():
    q, now = RealtimeQuotes(), [T0 + 5]
    read = _producer(q, now)
    q._accumulate("QQQ", 687.0, T0)
    read()
    now[0] = T0 + 61                                           # la minute a tourné, pas de prix
    q.drain_bars(now=now[0])                                   # le vidage périodique est passé
    assert read() is None
    now[0] = T0 + 60 + cashbars.CLOSED_WAIT_S
    assert read() == {"symbol": "QQQ", "t": T0, "o": 687.0, "h": 687.0, "l": 687.0, "c": 687.0,
                      "closed": True}
    assert read() is None


def test_cloture_lue_dans_le_vrai_fichier(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    cashbars._file_bars.clear()
    assert cashbars._file_bar("QQQ", T0) is None               # fichier absent
    _write(tmp_path, "QQQ", "2026-11-10", ["09:59"])
    assert cashbars._file_bar("QQQ", T0) is None               # minute pas encore écrite
    _write(tmp_path, "QQQ", "2026-11-10", ["09:59", "10:00"])
    bar = cashbars._file_bar("QQQ", T0)
    assert (bar.minute, bar.open, bar.high, bar.low, bar.close) == (T0, 1.0, 3.0, 0.5, 2.0)


def test_hors_seance_ou_bougie_close_ancienne_rien_n_est_emis():
    q = RealtimeQuotes()
    soir = int(pd.Timestamp("2026-11-10 18:00", tz="America/New_York").timestamp())
    q._accumulate("QQQ", 687.0, soir)
    assert _producer(q, [soir + 5])() is None
    q2 = RealtimeQuotes()
    q2._closed["QQQ"] = Bar(T0 - 600, 1.0, 1.0, 1.0, 1.0)     # flux resté sans abonné
    assert _producer(q2, [T0 + 5])() is None


def test_canaux_du_flux(monkeypatch):
    monkeypatch.setattr("gex.app.scalp_feed_channels",
                        lambda products: {"levels:NQ": object()} if products != "CL" else {})
    assert set(ticksapi.stream_channels(None, None)) == {"levels:NQ"}          # inchangé
    assert set(ticksapi.stream_channels(None, "QQQ,SPX,NQ,x")) == {"bar:QQQ", "bar:SPX"}
    assert set(ticksapi.stream_channels("NQ", "QQQ")) == {"levels:NQ", "bar:QQQ"}
    assert ticksapi.stream_channels("CL", "AAPL") == {}
