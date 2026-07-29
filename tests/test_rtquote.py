"""État du flux spot temps réel.

Le voyant de l'interface repose entièrement sur `status()` : s'il ment, on
croit lire un prix de l'instant alors qu'on regarde une donnée de 15 minutes.
Aucun de ces tests ne touche le réseau.
"""
from __future__ import annotations

import time
from datetime import date

from gex.rtquote import (
    PUBLIC_DEMO_URL,
    PublicDelayedQuotes,
    RealtimeQuotes,
    Tick,
    decode_compact_feed_data,
    front_quarterly_code,
)


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


def test_decode_compact_reconstruit_le_format_full():
    """Format COMPACT (cf. commentaire au-dessus de COMPACT_FIELDS) : un
    tableau à plat par type d'événement, décodé en dicts pour ne rien changer
    au code consommateur (_ingest, futopt._collect_one) qui attendait le
    format FULL implicite avant ce correctif du 2026-07-28."""
    data = [
        "Quote", ["Quote", "SPY", 559.30, 559.40, "Quote", "AAPL", 190.0, 190.05],
        "Trade", ["Trade", "SPY", 559.36, 1_250_000.0],
    ]
    out = decode_compact_feed_data(data)
    assert out == [
        {"eventType": "Quote", "eventSymbol": "SPY", "bidPrice": 559.30, "askPrice": 559.40},
        {"eventType": "Quote", "eventSymbol": "AAPL", "bidPrice": 190.0, "askPrice": 190.05},
        {"eventType": "Trade", "eventSymbol": "SPY", "price": 559.36,
         "dayVolume": 1_250_000.0},
    ]


def test_decode_compact_ignore_un_type_non_declare():
    """Un type qu'on n'a pas demandé dans COMPACT_FIELDS (Profile, TimeAndSale…)
    ne doit pas faire échouer le décodage du reste du message."""
    data = ["Profile", ["Profile", "SPY", "desc"],
           "Trade", ["Trade", "SPY", 100.0, 42.0]]
    out = decode_compact_feed_data(data)
    assert out == [{"eventType": "Trade", "eventSymbol": "SPY", "price": 100.0,
                    "dayVolume": 42.0}]


def test_decode_compact_vide():
    assert decode_compact_feed_data([]) == []


def test_decode_compact_greeks_et_summary():
    """Champs utilisés par futopt.enrich_native : IV (Greeks), OI (Summary) et
    volume du jour (Trade) — ceux-là seuls sont déclarés dans COMPACT_FIELDS,
    pas tout ce que l'exemple officiel tastytrade propose
    (delta/gamma/theta/rho/vega, dayOpenPrice…).

    Le volume est bien sur Trade et NON sur Summary : vérifié le 2026-07-29
    contre le flux réel en format FULL, sur options d'indice comme sur
    options sur future (cf. COMPACT_FIELDS)."""
    data = ["Greeks", ["Greeks", "./NQU26C28000:XCME", 0.18],
           "Summary", ["Summary", "./NQU26C28000:XCME", 1200.0],
           "Trade", ["Trade", "./NQU26C28000:XCME", 51.25, 45.0]]
    out = decode_compact_feed_data(data)
    assert out == [
        {"eventType": "Greeks", "eventSymbol": "./NQU26C28000:XCME", "volatility": 0.18},
        {"eventType": "Summary", "eventSymbol": "./NQU26C28000:XCME",
         "openInterest": 1200.0},
        {"eventType": "Trade", "eventSymbol": "./NQU26C28000:XCME",
         "price": 51.25, "dayVolume": 45.0},
    ]


def test_front_quarterly_code_mois_normal():
    """Bien avant le roulement : le contrat du trimestre en cours."""
    assert front_quarterly_code(date(2026, 1, 15)) == "H26"   # mars
    assert front_quarterly_code(date(2026, 3, 10)) == "H26"   # encore mars (3j avant le 3e vendredi)


def test_front_quarterly_code_roule_avant_expiration():
    """Régime CME standard : on bascule sur le trimestre suivant ~1 semaine
    avant l'expiration, pas le jour même — sans ça le spot afficherait un
    contrat sur le point d'expirer plutôt que celui qui compte."""
    # 3e vendredi de mars 2026 = 20/03 ; expiry-7 = 13/03
    assert front_quarterly_code(date(2026, 3, 14)) == "M26"  # juin


def test_front_quarterly_code_bascule_decembre_annee_suivante():
    """Après le roulement de décembre, plus aucun trimestre de l'année en
    cours ne convient : on retombe sur mars de l'année suivante."""
    assert front_quarterly_code(date(2026, 12, 15)) == "H27"


def test_public_delayed_quotes_symboles_sans_reseau():
    """_resolve_symbols ne doit faire AUCUN appel réseau (contrairement à
    resolve_symbols côté courtier) — c'est tout l'intérêt du repli."""
    q = PublicDelayedQuotes()
    code = front_quarterly_code()
    assert q._resolve_symbols("") == {"NQ": f"/NQ{code}:XCME", "ES": f"/ES{code}:XCME"}
    assert q._quote_token() == ("demo", PUBLIC_DEMO_URL, "")


def test_public_delayed_quotes_ne_demarre_pas_si_compte_reel(monkeypatch):
    """Le repli n'a de sens QUE sans identifiants — un compte réel doit
    garder l'exclusivité du flux temps réel, pas tourner les deux en même
    temps pour rien."""
    import gex.rtquote as rtq

    monkeypatch.setattr(rtq, "credentials_present", lambda: True)
    q = PublicDelayedQuotes()
    q.start()
    assert q._started is False
