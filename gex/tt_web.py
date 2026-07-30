"""Connexion tastytrade depuis le dashboard, sans copier-coller.

Remplace la gymnastique de `python -m gex.tt_auth` (ouvrir une URL, atterrir
sur une page d'erreur, recopier le `code=` de la barre d'adresse, puis `setx`)
par deux clics : « Connecter », approuver.

Ce qui rend ça possible : l'URI de redirection enregistrée côté tastytrade est
`http://localhost:8050/oauth/callback`, donc le navigateur revient sur le
dashboard lui-même — qui peut alors lire le code et faire l'échange.

⚠️ Portée. Ces routes ne servent QUE l'autorisation, en scope `read`. Ce
serveur n'écoute qu'en local (cf. gex/api.py) et le projet n'exécute aucun
ordre : un jeton obtenu ici ne peut pas trader, par construction.

Sécurité de l'échange :
- un `state` aléatoire à usage unique est vérifié au retour. Sans lui, une
  page tierce pourrait déclencher notre callback avec SON code
  d'autorisation, et le dashboard enregistrerait le jeton de quelqu'un
  d'autre (« OAuth code injection ») ;
- le `code` et le `client_secret` ne transitent que vers l'API tastytrade ;
- le refresh token obtenu n'est jamais affiché ni journalisé, seulement
  rangé là où `rtquote._env` le lit déjà (cf. tt_auth.store_refresh).
"""
from __future__ import annotations

import logging
import secrets
import threading

from flask import Flask, redirect, request

from . import tt_auth
from .rtquote import credentials_present

log = logging.getLogger(__name__)

# `state` en attente, à usage unique. Un dict plutôt qu'une valeur simple :
# rien n'interdit à l'utilisateur de cliquer deux fois puis de terminer la
# première autorisation. Borné pour ne pas croître indéfiniment si des
# tentatives sont abandonnées.
_PENDING: dict[str, float] = {}
_PENDING_LOCK = threading.Lock()
_MAX_PENDING = 8


def _page(titre: str, message: str, ok: bool) -> str:
    """Page de retour minimale, aux couleurs du dashboard."""
    couleur = "#22c55e" if ok else "#ef4444"
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>{titre}</title></head>
<body style="background:#0d0d0d;color:#e5e5e5;font-family:system-ui,sans-serif;
             display:flex;align-items:center;justify-content:center;
             height:100vh;margin:0">
  <div style="max-width:34rem;padding:2rem;border-left:3px solid {couleur};
              background:#151515">
    <h1 style="margin:0 0 .6rem;font-size:1.1rem;color:{couleur}">{titre}</h1>
    <p style="margin:0 0 1.2rem;line-height:1.5">{message}</p>
    <a href="/" style="color:#22d3ee">Retour au dashboard</a>
  </div>
</body></html>"""


def _remember_state() -> str:
    import time

    state = secrets.token_urlsafe(24)
    with _PENDING_LOCK:
        if len(_PENDING) >= _MAX_PENDING:
            # purge le plus ancien : une tentative abandonnée ne doit pas
            # bloquer les suivantes
            plus_vieux = min(_PENDING, key=_PENDING.get)
            _PENDING.pop(plus_vieux, None)
        _PENDING[state] = time.time()
    return state


def _consume_state(state: str | None) -> bool:
    """Vrai si le state était bien en attente. À usage unique : un même state
    ne peut pas servir deux fois."""
    if not state:
        return False
    with _PENDING_LOCK:
        return _PENDING.pop(state, None) is not None


def connection_status() -> tuple[str, str]:
    """(état, message) de la connexion courtier, pour l'affichage.

    `credentials_present` exige les trois valeurs : sans identifiants d'appli,
    il n'y a même pas de quoi lancer une autorisation — le message doit dire
    laquelle manque plutôt qu'un « non connecté » indifférencié.
    """
    from .rtquote import _env

    cid = _env("TASTYTRADE_CLIENT_ID")
    secret = _env("TASTYTRADE_CLIENT_SECRET")
    refresh = _env("TT_REFRESH")
    if not cid or not secret:
        return "absent", ("Identifiants d'application manquants "
                          "(TASTYTRADE_CLIENT_ID / _SECRET).")
    if not refresh:
        return "deconnecte", "Application configurée — reste à autoriser l'accès."
    return "connecte", "Compte tastytrade connecté (lecture seule)."


def register_oauth(app) -> None:
    """`app` : instance Dash (on grimpe à `.server`) ou Flask directement —
    comme `api.register_api`, pour que les tests n'aient pas à monter tout le
    dashboard."""
    server: Flask = app.server if hasattr(app, "server") else app

    @server.route("/oauth/start")
    def _oauth_start():
        from .rtquote import _env

        cid = _env("TASTYTRADE_CLIENT_ID")
        if not cid:
            return _page("Configuration incomplète",
                         "TASTYTRADE_CLIENT_ID est introuvable. Crée une "
                         "application OAuth chez tastytrade "
                         "(Manage → My Profile → API), puis renseigne "
                         "TASTYTRADE_CLIENT_ID et TASTYTRADE_CLIENT_SECRET.",
                         ok=False), 400
        return redirect(tt_auth.authorize_url(cid, state=_remember_state()))

    @server.route("/oauth/callback")
    def _oauth_callback():
        erreur = request.args.get("error")
        if erreur:
            # refus explicite côté tastytrade : ce n'est pas une panne
            return _page("Autorisation refusée",
                         f"tastytrade a renvoyé : {erreur}. "
                         "Rien n'a été enregistré.", ok=False), 400

        if not _consume_state(request.args.get("state")):
            log.warning("OAuth : state invalide ou expiré, échange refusé")
            return _page("Demande non reconnue",
                         "Cette autorisation ne correspond à aucune demande "
                         "partie de ce dashboard. Par sécurité, rien n'a été "
                         "enregistré — relance depuis le bouton Connecter.",
                         ok=False), 400

        code = request.args.get("code")
        if not code:
            return _page("Code absent",
                         "tastytrade n'a pas renvoyé de code d'autorisation.",
                         ok=False), 400

        try:
            cid, secret = tt_auth.credentials()
            data = tt_auth.exchange_code(cid, secret, code)
        except SystemExit as exc:      # tt_auth signale ses échecs ainsi
            log.warning("OAuth : échange refusé (%s)", exc)
            return _page("Échange refusé", str(exc), ok=False), 400
        except Exception:              # noqa: BLE001 — réseau, JSON malformé…
            log.exception("OAuth : échec de l'échange du code")
            return _page("Échec de l'échange",
                         "Impossible de contacter tastytrade. "
                         "Vérifie la connexion et réessaie.", ok=False), 502

        refresh = data.get("refresh_token")
        if not refresh:
            return _page("Réponse inattendue",
                         "Aucun refresh_token dans la réponse de tastytrade.",
                         ok=False), 502

        note = tt_auth.store_refresh(refresh)
        # le jeton lui-même n'est JAMAIS journalisé
        log.info("OAuth tastytrade : connexion réussie. %s", note)
        _demarrer_les_flux()
        return _page("Connecté à tastytrade",
                     f"{note} Les flux temps réel démarrent — les données "
                     "apparaîtront dans la minute.", ok=True)


def _demarrer_les_flux() -> None:
    """Démarre les flux qui refusaient de tourner faute d'identifiants.

    Sans cela, il faudrait redémarrer le dashboard juste après s'être
    connecté — ce qui reviendrait à remplacer un copier-coller par un
    redémarrage. `start()` est idempotent des deux côtés.
    """
    try:
        if credentials_present():
            from .flowtape import TAPE
            from .rtquote import QUOTES

            QUOTES.start()
            TAPE.start()
    except Exception:  # noqa: BLE001 — un flux qui refuse de démarrer ne doit
        log.exception("Démarrage des flux après connexion")
