"""Point d'entrée du dashboard, exposé comme commande ``gex-dashboard``.

Démarre l'ingestion planifiée puis sert le dashboard Dash sur
http://127.0.0.1:8050. Utilisable de trois façons équivalentes :

    gex-dashboard            (après `pip install .`)
    python -m gex.run
    python run.py            (raccourci à la racine du dépôt)
"""
from __future__ import annotations

from gex import flowtape
from gex.app import create_app
from gex.capturebus import RemoteTape, remote_url
from gex.flowtape import TAPE
from gex.logsetup import setup_logging
from gex.rtquote import PUBLIC_QUOTES, QUOTES
from gex.scheduler import start_scheduler
from gex.tickcapture import CAPTURE


def main(host: str = "127.0.0.1", port: int = 8050) -> None:
    # console + logs/gex.log (rotatif) : la trace survit à la fermeture du terminal
    setup_logging()
    url = remote_url()
    # Mode SÉPARÉ (GEX_CAPTURE_URL défini) : ticks, order flow et bougies vivent
    # dans le process `gex.capture`, que redémarrer le dashboard ne coupe plus.
    # Le dashboard s'y abonne et lit un miroir (RemoteTape, même interface).
    start_scheduler(embedded_capture=url is None)
    # spot temps réel pour l'AFFICHAGE : sans identifiants courtier, sans effet
    QUOTES.start()
    # repli gratuit NQ/ES délayé : ne démarre que si QUOTES ne tourne pas
    PUBLIC_QUOTES.start()
    if url:
        # les vues du dashboard font `from .flowtape import TAPE` à l'appel : on
        # remplace l'attribut du module AVANT de servir, elles lisent le miroir
        flowtape.TAPE = RemoteTape(url)
        flowtape.TAPE.start()
    else:
        # order flow signé sur options : sans identifiants, sans effet
        TAPE.start()
        # capture tick-par-tick continue NQ/ES (24/5) : session dxLink dédiée
        CAPTURE.start()
    # `threaded=True` sur le serveur de dev Werkzeug a été essayé le
    # 2026-10-03 et retiré dans la minute (observé pire en direct). Diagnostic
    # confirmé le 2026-10-04 par un test isolé (app Flask jouet, hors
    # gex/app.py) : Werkzeug `threaded=True` lève bien le blocage HTTP de
    # base, MAIS spawn un thread PAR CONNEXION, sans aucune limite — sous
    # rafale (plusieurs onglets, callbacks ~1s chacun), ça peut lancer des
    # dizaines de threads qui se contentent le GIL en même temps pendant du
    # travail pandas synchrone (confluence/order_flow), d'où le ressenti
    # "pire". waitress règle ce point précis : pool de threads BORNÉ (ici 8),
    # jamais plus de 8 requêtes traitées en parallèle quelle que soit la
    # rafale — testé le 2026-10-04 (même scénario jouet) : lève le blocage
    # HTTP de base sans le risque d'explosion de threads de Werkzeug.
    from waitress import serve
    serve(create_app().server, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
