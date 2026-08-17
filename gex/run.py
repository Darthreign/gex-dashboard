"""Point d'entrée du dashboard, exposé comme commande ``gex-dashboard``.

Démarre l'ingestion planifiée puis sert le dashboard Dash sur
http://127.0.0.1:8050. Utilisable de trois façons équivalentes :

    gex-dashboard            (après `pip install .`)
    python -m gex.run
    python run.py            (raccourci à la racine du dépôt)
"""
from __future__ import annotations

from gex.app import create_app
from gex.flowtape import TAPE
from gex.logsetup import setup_logging
from gex.rtquote import PUBLIC_QUOTES, QUOTES
from gex.scheduler import start_scheduler
from gex.tickcapture import CAPTURE


def main(host: str = "127.0.0.1", port: int = 8050) -> None:
    # console + logs/gex.log (rotatif) : la trace survit à la fermeture du terminal
    setup_logging()
    start_scheduler()
    # spot temps réel : sans identifiants courtier, l'appel est sans effet
    QUOTES.start()
    # repli gratuit NQ/ES délayé : ne démarre que si QUOTES ne tourne pas
    PUBLIC_QUOTES.start()
    # order flow signé sur options : sans identifiants, l'appel est sans effet
    TAPE.start()
    # capture tick-par-tick continue NQ/ES (24/5) : session dxLink dédiée,
    # sans identifiants l'appel est sans effet
    CAPTURE.start()
    create_app().run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
