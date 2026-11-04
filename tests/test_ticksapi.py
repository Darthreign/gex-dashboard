"""API de ticks pour String X Chart : contrat de docs/api-ticks.md."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from flask import Flask

from gex import ticksapi
from gex.config import SETTINGS

TOKEN = "jeton-de-test"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _write(root, product, day, side=True, volume=7):
    d = root / product
    d.mkdir(parents=True, exist_ok=True)
    row = {"ts": 1.5, "price": 25000.25, "volume": volume, "bid": 1.0}
    if side:
        row["side"] = "BUY"
    pd.DataFrame([row]).to_parquet(d / f"{day}.parquet", index=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    monkeypatch.setenv("GEX_TICKS_TOKEN", TOKEN)
    ticksapi._hits.clear()
    ticksapi._schema_ok.clear()
    app = Flask(__name__)
    ticksapi.register_ticks_api(app)
    return app.test_client(), tmp_path / "ticks", tmp_path / "import" / "ticks_full"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer faux"}, {"Authorization": TOKEN}])
def test_jeton_absent_ou_faux_refuse(env, headers):
    client, live, _ = env
    _write(live, "NQ", "2026-10-30")
    assert client.get("/api/v1/ticks/NQ/days", headers=headers).status_code == 401
    assert client.get("/api/v1/ticks/NQ/2026-10-30.parquet", headers=headers).status_code == 401


def test_sans_variable_tout_est_refuse(env, monkeypatch):
    client, live, _ = env
    monkeypatch.delenv("GEX_TICKS_TOKEN")
    _write(live, "NQ", "2026-10-30")
    assert client.get("/api/v1/ticks/NQ/days", headers={"Authorization": "Bearer "}).status_code == 401


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch", "head", "options"])
def test_lecture_seule(env, method):
    client, live, _ = env
    _write(live, "NQ", "2026-10-30")
    assert getattr(client, method)("/api/v1/ticks/NQ/days", headers=AUTH).status_code == 405


def test_days_fusionne_et_ecarte_les_fichiers_incomplets(env):
    client, live, full = env
    _write(full, "NQ", "2024-08-06", side=False)     # ancien : pas de côté agresseur
    _write(full, "NQ", "2026-10-29")
    _write(live, "NQ", "2026-10-29")
    _write(live, "NQ", "2026-10-30")
    r = client.get("/api/v1/ticks/NQ/days", headers=AUTH)
    assert r.status_code == 200 and r.get_json() == {"days": ["2026-10-29", "2026-10-30"]}


@pytest.mark.parametrize("path", ["MNQ/days", "nq/days", "TROPLONGPRODUIT/days",
                                  "NQ/2026-13-45.parquet", "NQ/2026-10-31.parquet",
                                  "NQ/hier.parquet"])
def test_inconnu_404(env, path):
    client, live, _ = env
    _write(live, "NQ", "2026-10-30")
    assert client.get(f"/api/v1/ticks/{path}", headers=AUTH).status_code == 404


def test_fichier_incomplet_404(env):
    client, _, full = env
    _write(full, "NQ", "2024-08-06", side=False)
    assert client.get("/api/v1/ticks/NQ/2024-08-06.parquet", headers=AUTH).status_code == 404


def test_fichier_servi_tel_quel_et_export_prefere(env):
    client, live, full = env
    _write(live, "NQ", "2026-10-29", volume=1)
    _write(full, "NQ", "2026-10-29", volume=2)
    r = client.get("/api/v1/ticks/NQ/2026-10-29.parquet", headers=AUTH)
    assert r.status_code == 200 and r.mimetype == "application/octet-stream"
    assert r.data == (full / "NQ" / "2026-10-29.parquet").read_bytes()


def test_incomplet_dans_l_export_repli_sur_le_live(env):
    client, live, full = env
    _write(full, "NQ", "2026-10-29", side=False)
    _write(live, "NQ", "2026-10-29")
    r = client.get("/api/v1/ticks/NQ/2026-10-29.parquet", headers=AUTH)
    assert r.data == (live / "NQ" / "2026-10-29.parquet").read_bytes()


def test_etag_304_puis_nouvelle_version(env):
    client, live, _ = env
    _write(live, "NQ", "2026-10-30")
    etag = client.get("/api/v1/ticks/NQ/2026-10-30.parquet", headers=AUTH).headers["ETag"]
    r = client.get("/api/v1/ticks/NQ/2026-10-30.parquet", headers={**AUTH, "If-None-Match": etag})
    assert r.status_code == 304 and r.data == b"" and r.headers["ETag"] == etag
    _write(live, "NQ", "2026-10-30", volume=123456)
    r = client.get("/api/v1/ticks/NQ/2026-10-30.parquet", headers={**AUTH, "If-None-Match": etag})
    assert r.status_code == 200 and r.headers["ETag"] != etag


def test_le_fichier_est_referme_avant_l_envoi(env):
    """Sous Windows, un fichier resté ouvert bloquerait le remplacement par la capture."""
    client, live, _ = env
    _write(live, "NQ", "2026-10-30")
    r = client.get("/api/v1/ticks/NQ/2026-10-30.parquet", headers=AUTH, buffered=False)
    _write(live, "NQ", "2026-10-30", volume=2)   # remplace pendant la réponse
    assert r.status_code == 200
    r.close()


def test_limite_de_debit(env, monkeypatch):
    client, live, _ = env
    monkeypatch.setattr(ticksapi, "RATE_MAX", 2)
    _write(live, "NQ", "2026-10-30")
    assert [client.get("/api/v1/ticks/NQ/days", headers=AUTH).status_code for _ in range(3)] == [200, 200, 429]


def test_historique_reconstruit_servi_si_l_export_n_a_pas_side(env, tmp_path):
    client, _, full = env
    side_root = tmp_path / "import" / "ticks_full_side"
    _write(full, "NQ", "2024-08-06", side=False)
    _write(side_root, "NQ", "2024-08-06")
    assert client.get("/api/v1/ticks/NQ/days", headers=AUTH).get_json() == {"days": ["2024-08-06"]}
    r = client.get("/api/v1/ticks/NQ/2024-08-06.parquet", headers=AUTH)
    assert r.data == (side_root / "NQ" / "2024-08-06.parquet").read_bytes()


SECRET = "secret-de-test"


def _signed(email="ami@example.com", delta=3600, secret=SECRET):
    import time
    return ticksapi.sign_token(email, int(time.time()) + delta, secret)


@pytest.fixture
def perso(env, monkeypatch):
    client, live, _ = env
    monkeypatch.setenv("GEX_TICKS_SECRET", SECRET)
    monkeypatch.setenv("GEX_TICKS_REVOKED", str(live.parent / "revoques.txt"))
    _write(live, "NQ", "2026-10-30")
    return client


def test_jeton_personnel_valide_accepte(perso):
    assert perso.get("/api/v1/ticks/NQ/days", headers={"Authorization": f"Bearer {_signed()}"}).status_code == 200


@pytest.mark.parametrize("jeton", ["", "v1.x.y", "v1.a.b.c", "v2.abc.def"])
def test_jeton_personnel_mal_forme_refuse(perso, jeton):
    assert perso.get("/api/v1/ticks/NQ/days", headers={"Authorization": f"Bearer {jeton}"}).status_code == 401


def test_jeton_personnel_expire_ou_mal_signe_refuse(perso):
    assert perso.get("/api/v1/ticks/NQ/days", headers={"Authorization": f"Bearer {_signed(delta=-10)}"}).status_code == 401
    assert perso.get("/api/v1/ticks/NQ/days", headers={"Authorization": f"Bearer {_signed(secret='autre')}"}).status_code == 401


def test_charge_modifiee_refusee(perso):
    jeton = _signed()
    _, payload, sig = jeton.split(".")
    forged = ticksapi._b64(b'{"e":"ami@example.com","exp":9999999999}')
    assert perso.get("/api/v1/ticks/NQ/days", headers={"Authorization": f"Bearer v1.{forged}.{sig}"}).status_code == 401


def test_email_revoque_refuse_et_les_autres_restent_servis(perso, tmp_path, monkeypatch):
    import os
    (tmp_path / "revoques.txt").write_text("# départ\nAmi@Example.com\n", encoding="utf-8")
    monkeypatch.setenv("GEX_TICKS_REVOKED", str(tmp_path / "revoques.txt"))
    assert perso.get("/api/v1/ticks/NQ/days", headers={"Authorization": f"Bearer {_signed()}"}).status_code == 401
    assert perso.get("/api/v1/ticks/NQ/days", headers={"Authorization": f"Bearer {_signed('autre@example.com')}"}).status_code == 200


def test_liste_de_revocation_illisible_refuse(perso, tmp_path, monkeypatch):
    monkeypatch.setattr(ticksapi, "REPLACE_DELAY_S", 0)
    # Un dossier : ouverture refusée (PermissionError sous Windows, comme un fichier verrouillé).
    monkeypatch.setenv("GEX_TICKS_REVOKED", str(tmp_path))
    jeton = {"Authorization": f"Bearer {_signed()}"}
    assert perso.get("/api/v1/ticks/NQ/days", headers=jeton).status_code == 401
    mauvais = tmp_path / "invalide.txt"
    mauvais.write_bytes(b"\xff\xfe\xff")
    monkeypatch.setenv("GEX_TICKS_REVOKED", str(mauvais))
    assert perso.get("/api/v1/ticks/NQ/days", headers=jeton).status_code == 401
    assert perso.get("/api/v1/ticks/NQ/days", headers=AUTH).status_code == 200  # le jeton partagé reste servi


def test_verrou_passager_la_revocation_reste_appliquee(perso, tmp_path, monkeypatch):
    monkeypatch.setattr(ticksapi, "REPLACE_DELAY_S", 0)
    liste = tmp_path / "revoques.txt"
    liste.write_text("ami@example.com\n", encoding="utf-8")
    monkeypatch.setenv("GEX_TICKS_REVOKED", str(liste))
    vrai, essais = Path.read_text, []

    def verrouille_une_fois(self, *a, **k):
        if self == liste and not essais:
            essais.append(1)
            raise PermissionError("verrouillé")
        return vrai(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", verrouille_une_fois)
    assert perso.get("/api/v1/ticks/NQ/days", headers={"Authorization": f"Bearer {_signed()}"}).status_code == 401
    assert essais == [1]
    assert perso.get("/api/v1/ticks/NQ/days",
                     headers={"Authorization": f"Bearer {_signed('autre@example.com')}"}).status_code == 200


def test_sans_secret_aucun_jeton_personnel(env, monkeypatch):
    client, live, _ = env
    monkeypatch.delenv("GEX_TICKS_SECRET", raising=False)
    _write(live, "NQ", "2026-10-30")
    assert client.get("/api/v1/ticks/NQ/days", headers={"Authorization": f"Bearer {_signed()}"}).status_code == 401
    assert client.get("/api/v1/ticks/NQ/days", headers=AUTH).status_code == 200  # le jeton partagé existant marche toujours
