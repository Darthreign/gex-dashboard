"""Process CAPTURE : les flux temps réel et leurs écritures sur disque, séparés
du dashboard pour que redémarrer celui-ci ne coupe plus rien.

Ce process porte :
- la capture tick-par-tick NQ/ES (`tickcapture.CAPTURE`) + son flush disque ;
- l'order flow signé des options (`flowtape.TAPE`) + son flush disque ;
- le flux spot temps réel (`rtquote.QUOTES`), dont les bougies 1 min sont
  écrites ici (le dashboard garde sa propre connexion pour AFFICHER le spot,
  mais n'écrit plus de bougies) ;
- la liaison WebSocket locale vers le dashboard (`capturebus`).

Lancement (tâche planifiée « GEX capture », sans fenêtre) :
    pythonw.exe -m gex.capture

Le dashboard s'y branche si `GEX_CAPTURE_URL` est défini (cf. gex/run.py).
Le redémarrer, ou l'arrêter, n'interrompt ni la capture ni les écritures.
"""
from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from .capturebus import DEFAULT_HOST, DEFAULT_PORT, serve
from .flowtape import TAPE
from .logsetup import setup_logging
from .rtquote import QUOTES
from .scheduler import add_flush_jobs
from .tickcapture import CAPTURE

log = logging.getLogger(__name__)


def main(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    setup_logging(filename="capture.log")
    log.info("Process capture : démarrage")
    sched = BackgroundScheduler(timezone="America/New_York")
    add_flush_jobs(sched)
    sched.start()
    QUOTES.start()          # requis par la construction de l'univers du tape (spots)
    TAPE.start()
    CAPTURE.start()
    threading.current_thread().name = "capture-main"
    serve(TAPE, host, port)  # bloque : c'est ce qui garde le process en vie


if __name__ == "__main__":
    main()
