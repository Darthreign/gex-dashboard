"""Les tests ne dépendent pas de la configuration de la machine.

Sur le PC de production, les variables du dashboard (GEX_CAPTURE_URL,
GEX_ENGINE, identifiants tastytrade…) sont définies dans l'environnement
utilisateur Windows, et `rtquote._env` les relit même dans le registre.
Sans cette isolation, les tests y voient un mode séparé ou moteur qu'ils ne
simulent pas. Chaque test part donc d'un environnement vierge ; ceux qui
testent ces modes les définissent eux-mêmes (monkeypatch.setenv).
"""
import os

import pytest

MACHINE_VARS = ("GEX_CAPTURE_URL", "GEX_ENGINE", "GEX_SERVER", "TT_REFRESH",
                "TASTYTRADE_CLIENT_ID", "TASTYTRADE_CLIENT_SECRET", "DATABENTO_API_KEY")


def _env_sans_registre(name: str) -> str | None:
    return os.environ.get(name)


@pytest.fixture(autouse=True)
def _environnement_isole(monkeypatch):
    for v in MACHINE_VARS:
        monkeypatch.delenv(v, raising=False)
    from gex import capturebus, rtquote, tt_auth
    monkeypatch.setattr(rtquote, "_env", _env_sans_registre)
    monkeypatch.setattr(capturebus, "_env", _env_sans_registre)
    if hasattr(tt_auth, "_env"):
        monkeypatch.setattr(tt_auth, "_env", _env_sans_registre)
