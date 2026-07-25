"""État du flux spot temps réel.

Le voyant de l'interface repose entièrement sur `status()` : s'il ment, on
croit lire un prix de l'instant alors qu'on regarde une donnée de 15 minutes.
Aucun de ces tests ne touche le réseau.
"""
from __future__ import annotations

import time

from gex.rtquote import RealtimeQuotes, Tick


def _connected(age_s: float = 0.0) -> RealtimeQuotes:
    q = RealtimeQuotes()
    q._state = "connected"
    q.ticks["ES"] = Tick(bid=7443.75, ask=7444.5, ts=time.time() - age_s)
    return q


def test_inactif_sans_identifiants():
    """Installation par défaut : la fonction n'existe pas, pas de voyant rouge."""
    assert RealtimeQuotes().status() == ("off", "")


def test_flux_frais_est_connecte():
    assert _connected().status(market_open=True)[0] == "connected"


def test_silence_en_seance_est_degrade():
    state, detail = _connected(age_s=45).status(market_open=True)
    assert state == "degraded"
    assert "45" in detail


def test_silence_hors_seance_reste_connecte():
    """Marché fermé, aucun tick n'est attendu : signaler une dégradation serait
    un faux positif permanent chaque nuit et chaque week-end."""
    assert _connected(age_s=3600).status(market_open=False)[0] == "connected"


def test_socket_coupee_est_deconnectee():
    q = _connected()
    q._state = "disconnected"
    q._detail = "socket fermée"
    state, detail = q.status(market_open=True)
    assert state == "disconnected"
    assert detail == "socket fermée"


def test_connecte_sans_aucune_cotation_est_degrade():
    """Connexion établie mais rien reçu : on ne peut rien afficher."""
    q = RealtimeQuotes()
    q._state = "connected"
    assert q.status(market_open=True)[0] == "degraded"


def test_prix_prefere_le_milieu_de_fourchette():
    """Le mid ne saute pas d'un bord à l'autre du spread selon le sens de la
    dernière transaction, contrairement au last."""
    assert Tick(bid=7443.75, ask=7444.5, last=7443.75).price == 7444.125


def test_prix_retombe_sur_le_dernier_echange():
    # cas d'un indice sans carnet (NDX) : pas de bid/ask exploitable
    assert Tick(last=28128.34).price == 28128.34
    assert Tick().price is None


def test_ingest_ignore_les_nan():
    """dxFeed renvoie NaN sur les indices sans carnet : écraser un prix connu
    avec NaN ferait disparaître le spot de l'affichage."""
    q = RealtimeQuotes()
    q._by_stream = {"NDX": "NDX"}
    q._ingest([{"eventType": "Trade", "eventSymbol": "NDX", "price": 28128.34}])
    q._ingest([{"eventType": "Quote", "eventSymbol": "NDX",
                "bidPrice": float("nan"), "askPrice": float("nan")}])
    assert q.price("NDX") == 28128.34


def test_ingest_ignore_les_symboles_inconnus():
    q = RealtimeQuotes()
    q._by_stream = {"/ESU26:XCME": "ES"}
    q._ingest([{"eventType": "Trade", "eventSymbol": "AUTRE", "price": 1.0}])
    assert q.price("ES") is None
