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
import time

from apscheduler.schedulers.background import BackgroundScheduler

from .capturebus import DEFAULT_PORT, bind_hosts, serve
from .flowtape import TAPE
from .logsetup import setup_logging
from .rtquote import QUOTES
from .scheduler import add_flush_jobs
from .tickcapture import CAPTURE

log = logging.getLogger(__name__)


def attendre_spot(quotes=QUOTES, symboles=("SPX", "NDX", "SPY", "QQQ"),
                  timeout: float = 20.0, pas: float = 0.5,
                  horloge=time.monotonic, dormir=time.sleep) -> bool:
    """Attend que le flux spot ait un prix pour CHACUN des `symboles` (ou que
    `timeout` s'écoule). L'univers du tape se construit autour du spot : sans
    cette attente, il retombait sur le spot CBOE délayé (~15 min) au démarrage,
    parce que `QUOTES` n'avait pas encore reçu son premier prix. Renvoie True si
    tout est prêt ; sinon le tape démarre quand même (repli CBOE, recentrage
    ensuite) plutôt que de rester bloqué."""
    fin = horloge() + timeout
    while horloge() < fin:
        if all(quotes.price(s) for s in symboles):
            return True
        dormir(pas)
    manquants = [s for s in symboles if not quotes.price(s)]
    log.warning("Process capture : spot temps réel absent pour %s après %.0f s — "
                "le tape démarre sur le repli CBOE", ", ".join(manquants), timeout)
    return False


def main(host=None, port: int = DEFAULT_PORT) -> None:
    setup_logging(filename="capture.log")
    log.info("Process capture : démarrage")
    sched = BackgroundScheduler(timezone="America/New_York")
    add_flush_jobs(sched)
    sched.start()
    QUOTES.start()          # requis par la construction de l'univers du tape (spots)
    CAPTURE.start()         # les ticks ne dépendent pas du spot : on ne les fait pas attendre
    attendre_spot()         # le tape, lui, a besoin du premier prix pour son univers
    TAPE.start()
    threading.current_thread().name = "capture-main"
    serve(TAPE, host or bind_hosts(), port)  # bloque : c'est ce qui garde le process en vie


if __name__ == "__main__":
    main()
