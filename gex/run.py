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
    # ⚠️ threaded=True RETIRÉ le 2026-10-01 : testé plus tôt dans la même
    # journée pour débloquer /scalp derrière un callback lent, mais sous
    # charge réelle il a empiré la situation — Python a un GIL, donc plus de
    # threads pour du travail CPU (reconstruire des figures Plotly) n'apporte
    # aucun parallélisme réel, juste du changement de contexte, et risque de
    # tomber sur un verrou partagé (cache, journal). Constaté : la file de
    # requêtes _dash-update-component en attente grossissait sans se vider
    # (57 -> 216 en 1 min) même après avoir réglé la vraie cause du jour
    # (un process `find` orphelin qui saturait le CPU/disque). Revenir à
    # threaded=True un jour suppose d'abord de vérifier qu'aucun chemin
    # chaud (callbacks /scalp) ne fait de travail CPU lourd synchrone.
    create_app().run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
