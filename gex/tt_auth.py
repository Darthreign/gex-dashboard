"""Autorisation OAuth2 tastytrade — à exécuter UNE FOIS.

Le SDK tastytrade s'authentifie avec un refresh token de longue durée, qu'il
échange contre des access tokens courts. Ce module réalise l'étape initiale
(authorization code -> refresh token), qui exige une approbation navigateur.

Usage :
    python -m gex.tt_auth

Le script affiche une URL à ouvrir, tu approuves, ton navigateur est redirigé
vers https://localhost:8050/oauth/callback (page d'erreur attendue : rien
n'écoute en HTTPS sur ce port). Le paramètre `code=` de la barre d'adresse est
à recoller ici. Le refresh token obtenu est ensuite à stocker en variable
d'environnement TT_REFRESH.
"""
from __future__ import annotations

import os
import sys
import urllib.parse

import requests

AUTH_URL = "https://my.tastytrade.com/auth.html"
TOKEN_URL = "https://api.tastyworks.com/oauth/token"
# HTTP et non HTTPS : le dashboard sert en clair sur 127.0.0.1, donc c'est la
# SEULE forme que son navigateur puisse réellement atteindre. tastytrade
# n'exige HTTPS que pour les URI publiques et accepte http sur localhost, ce
# qui permet de récupérer le code automatiquement (cf. gex/tt_web.py) au lieu
# de le faire recopier depuis une page d'erreur.
REDIRECT_URI = "http://localhost:8050/oauth/callback"
# Lecture seule, volontairement : ce projet n'exécute pas d'ordres et n'a
# aucune raison de demander le scope "trade" (cf. README, « analyse
# uniquement »). Un jeton qui ne peut pas trader ne peut pas mal trader.
SCOPE = "read"


def _env(name: str) -> str | None:
    """Variable d'environnement, avec repli sur le registre utilisateur Windows
    (une session ouverte avant `setx` ne voit pas la nouvelle valeur)."""
    val = os.environ.get(name)
    if not val and sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                val = winreg.QueryValueEx(k, name)[0]
        except OSError:
            pass
    return val


def credentials() -> tuple[str, str]:
    cid = _env("TASTYTRADE_CLIENT_ID")
    secret = _env("TASTYTRADE_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit(
            "TASTYTRADE_CLIENT_ID / TASTYTRADE_CLIENT_SECRET introuvables "
            "(variables d'environnement ou registre HKCU)."
        )
    return cid, secret


def authorize_url(client_id: str, state: str | None = None) -> str:
    """URL d'autorisation à ouvrir dans le navigateur.

    `state` : jeton anti-CSRF, renvoyé tel quel par tastytrade sur la
    redirection. Le vérifier empêche qu'une page tierce fasse aboutir SON code
    d'autorisation sur notre callback — ce qui enregistrerait le jeton de
    quelqu'un d'autre à la place du tien.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
    }
    if state:
        params["state"] = state
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def store_refresh(token: str) -> str:
    """Enregistre le refresh token là où `rtquote._env` sait déjà le lire.

    Sur Windows : variable d'environnement utilisateur (HKCU\\Environment),
    c'est-à-dire exactement ce que faisait `setx` manuellement — aucun nouveau
    mécanisme, aucun fichier de secret ajouté au dépôt. La valeur est aussi
    posée dans `os.environ` du processus courant, sinon le dashboard ne la
    verrait qu'après redémarrage (une session héritant de son environnement au
    lancement).

    Renvoie une phrase décrivant ce qui a été fait, à afficher à l'utilisateur.
    """
    os.environ["TT_REFRESH"] = token
    if sys.platform != "win32":
        return ("Jeton actif pour cette session. Pour le rendre permanent, "
                'ajoute TT_REFRESH="…" à ton profil shell.')
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "TT_REFRESH", 0, winreg.REG_SZ, token)
        return "Jeton enregistré (variable utilisateur TT_REFRESH)."
    except OSError as exc:
        return (f"Jeton actif pour cette session, mais non enregistré ({exc}). "
                "Il faudra se reconnecter au prochain démarrage.")


def exchange_code(client_id: str, secret: str, code: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": secret,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(f"Échec de l'échange ({resp.status_code}) : {resp.text[:400]}")
    return resp.json()


def main() -> None:
    cid, secret = credentials()
    print("\n1) Ouvre cette URL dans ton navigateur et approuve l'accès :\n")
    print(authorize_url(cid))
    print(
        "\n2) Tu seras redirigé vers une page d'ERREUR (normal : rien n'écoute"
        f"\n   sur {REDIRECT_URI}). Dans la barre d'adresse, copie la valeur"
        "\n   du paramètre code=... (tout ce qui suit 'code=', avant un '&')\n"
    )
    code = input("3) Colle le code ici : ").strip()
    if not code:
        raise SystemExit("Aucun code fourni.")

    data = exchange_code(cid, secret, code)
    refresh = data.get("refresh_token")
    if not refresh:
        raise SystemExit(f"Pas de refresh_token dans la réponse : {data}")

    print("\n" + "=" * 62)
    print("Refresh token obtenu. Enregistre les DEUX variables ci-dessous")
    print("(noms imposés par le SDK tastytrade), puis relance ta session :\n")
    print(f'  setx TT_REFRESH "{refresh}"')
    print('  setx TT_SECRET "%TASTYTRADE_CLIENT_SECRET%"')
    print("=" * 62)
    print("\nCe token est un secret de longue durée : ne le partage pas et ne")
    print("le committe jamais dans le repo.\n")


if __name__ == "__main__":
    main()
