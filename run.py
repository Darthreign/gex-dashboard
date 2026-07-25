"""Point d'entrée : démarre l'ingestion planifiée + le dashboard Dash.

Usage : python run.py  (dashboard sur http://127.0.0.1:8050)
"""
from gex.app import create_app
from gex.logsetup import setup_logging
from gex.scheduler import start_scheduler

# console + logs/gex.log (rotatif) : la trace survit à la fermeture du terminal
setup_logging()

if __name__ == "__main__":
    start_scheduler()
    create_app().run(host="127.0.0.1", port=8050, debug=False)
