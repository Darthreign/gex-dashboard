"""Connexion tastytrade depuis le dashboard (gex/tt_web.py).

Le point sensible est le `state` anti-CSRF : sans lui, une page tierce peut
faire aboutir SON code d'autorisation sur notre callback, et le dashboard
enregistrerait le refresh token de quelqu'un d'autre. Les tests ci-dessous
vérifient qu'aucun chemin ne mène à un enregistrement sans state valide.
"""
from __future__ import annotations

import pytest
from flask import Flask

from gex import tt_auth, tt_web


@pytest.fixture()
def client(monkeypatch):
    tt_web._PENDING.clear()
    app = Flask(__name__)
    tt_web.register_oauth(app)
    return app.test_client()


def _identifiants(monkeypatch, cid="cid", secret="sec", refresh=None):
    valeurs = {"TASTYTRADE_CLIENT_ID": cid, "TASTYTRADE_CLIENT_SECRET": secret,
               "TT_REFRESH": refresh}
    lire = lambda n: valeurs.get(n)
    # DEUX _env à patcher : rtquote._env (status, /oauth/start) ET tt_auth._env
    # (credentials(), appelé dans le callback). Sans le second, le callback lit
    # les VRAIES variables d'environnement — vert sur un poste où un compte
    # tastytrade est configuré, rouge en CI (env vierge). Le test doit être
    # hermétique.
    monkeypatch.setattr("gex.rtquote._env", lire)
    monkeypatch.setattr("gex.tt_auth._env", lire)


def test_start_redirige_vers_tastytrade_avec_un_state(client, monkeypatch):
    _identifiants(monkeypatch)
    r = client.get("/oauth/start")
    assert r.status_code == 302
    assert r.headers["Location"].startswith(tt_auth.AUTH_URL)
    assert "state=" in r.headers["Location"]
    assert "scope=read" in r.headers["Location"]     # jamais "trade"


def test_start_sans_client_id_explique_au_lieu_de_planter(client, monkeypatch):
    _identifiants(monkeypatch, cid=None)
    r = client.get("/oauth/start")
    assert r.status_code == 400
    assert b"TASTYTRADE_CLIENT_ID" in r.data


def test_callback_refuse_un_state_inconnu(client, monkeypatch):
    """Le cœur de la protection : un code venu d'ailleurs ne doit RIEN
    enregistrer."""
    _identifiants(monkeypatch)
    appels = []
    monkeypatch.setattr(tt_auth, "store_refresh", lambda t: appels.append(t))
    monkeypatch.setattr(tt_auth, "exchange_code",
                        lambda *a: {"refresh_token": "voler"})

    r = client.get("/oauth/callback?code=abc&state=inconnu")
    assert r.status_code == 400
    assert appels == [], "aucun jeton ne doit être enregistré"


def test_callback_refuse_un_state_absent(client, monkeypatch):
    _identifiants(monkeypatch)
    appels = []
    monkeypatch.setattr(tt_auth, "store_refresh", lambda t: appels.append(t))
    r = client.get("/oauth/callback?code=abc")
    assert r.status_code == 400
    assert appels == []


def test_state_est_a_usage_unique(client, monkeypatch):
    """Rejouer une redirection capturée ne doit pas refaire un échange."""
    _identifiants(monkeypatch)
    stockes = []
    monkeypatch.setattr(tt_auth, "store_refresh",
                        lambda t: stockes.append(t) or "ok")
    monkeypatch.setattr(tt_auth, "exchange_code",
                        lambda *a: {"refresh_token": "jeton"})
    monkeypatch.setattr(tt_web, "_demarrer_les_flux", lambda: None)

    state = tt_web._remember_state()
    assert client.get(f"/oauth/callback?code=abc&state={state}").status_code == 200
    assert stockes == ["jeton"]
    # rejeu du même state
    assert client.get(f"/oauth/callback?code=abc&state={state}").status_code == 400
    assert stockes == ["jeton"], "pas de second enregistrement"


def test_callback_nominal_enregistre_et_demarre_les_flux(client, monkeypatch):
    _identifiants(monkeypatch)
    stockes, demarres = [], []
    monkeypatch.setattr(tt_auth, "store_refresh",
                        lambda t: stockes.append(t) or "enregistré")
    monkeypatch.setattr(tt_auth, "exchange_code",
                        lambda *a: {"refresh_token": "jeton-frais"})
    monkeypatch.setattr(tt_web, "_demarrer_les_flux",
                        lambda: demarres.append(True))

    state = tt_web._remember_state()
    r = client.get(f"/oauth/callback?code=abc&state={state}")
    assert r.status_code == 200
    assert stockes == ["jeton-frais"]
    assert demarres == [True], "les flux doivent démarrer sans redémarrage"
    # le jeton ne doit jamais apparaître dans la page rendue
    assert b"jeton-frais" not in r.data


def test_refus_utilisateur_nest_pas_traite_comme_une_panne(client, monkeypatch):
    _identifiants(monkeypatch)
    r = client.get("/oauth/callback?error=access_denied")
    assert r.status_code == 400
    assert "refus" in r.data.decode().lower()


def test_pending_borne(client, monkeypatch):
    """Des tentatives abandonnées ne doivent pas faire croître la mémoire."""
    for _ in range(tt_web._MAX_PENDING + 5):
        tt_web._remember_state()
    assert len(tt_web._PENDING) <= tt_web._MAX_PENDING


@pytest.mark.parametrize("cid,secret,refresh,attendu", [
    (None, None, None, "absent"),
    ("cid", "sec", None, "deconnecte"),
    ("cid", "sec", "tok", "connecte"),
])
def test_statut_de_connexion(monkeypatch, cid, secret, refresh, attendu):
    _identifiants(monkeypatch, cid, secret, refresh)
    etat, message = tt_web.connection_status()
    assert etat == attendu
    assert message


def test_redirect_uri_en_http_sur_localhost():
    """Le dashboard sert en clair sur 127.0.0.1 : une URI https ne serait
    jamais atteinte par le navigateur, et le code ne reviendrait pas."""
    assert tt_auth.REDIRECT_URI.startswith("http://localhost:8050")


def test_scope_reste_en_lecture_seule():
    """Garde-fou : ce projet n'exécute pas d'ordres. Un jeton sans le scope
    trade ne peut pas mal trader, même en cas de bug."""
    assert tt_auth.SCOPE == "read"


def test_fmt_notional_jamais_zero_k():
    """Un petit ticket vaut quelques centaines de dollars, pas « 0 k$ »."""
    from gex.app import _fmt_notional
    assert _fmt_notional(370) == "370 $"
    assert _fmt_notional(9500) == "10 k$"
    assert _fmt_notional(2_300_000) == "2.3 M$"
    assert _fmt_notional(0) == "—"
    assert _fmt_notional(None) == "—"


def test_apply_user_zoom_conserve_la_plage_manuelle():
    """La heatmap se régénère sur tick : un zoom manuel de l'axe des prix doit
    survivre au rafraîchissement (cf. _apply_user_zoom)."""
    from gex.app import _apply_user_zoom
    lay = {"yaxis": {}, "xaxis": {"range": [0, 1]}}
    _apply_user_zoom(lay, {"yaxis.range[0]": 7360.6, "yaxis.range[1]": 7509.4})
    assert lay["yaxis"]["range"] == [7360.6, 7509.4]
    assert lay["yaxis"]["autorange"] is False


def test_apply_user_zoom_double_clic_repart_en_auto():
    """Un double-clic renvoie axis.autorange=true : on ne fige aucune plage,
    la vue complète revient."""
    from gex.app import _apply_user_zoom
    lay = {"yaxis": {}, "xaxis": {}}
    _apply_user_zoom(lay, {"yaxis.autorange": True})
    assert "range" not in lay["yaxis"]


def test_apply_user_zoom_sans_interaction_ne_touche_rien():
    from gex.app import _apply_user_zoom
    lay = {"yaxis": {"title": "x"}, "xaxis": {"range": [0, 1]}}
    _apply_user_zoom(lay, None)
    assert lay["yaxis"] == {"title": "x"}
    assert lay["xaxis"]["range"] == [0, 1]
