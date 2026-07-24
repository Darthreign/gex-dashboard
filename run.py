"""Point d'entrée : démarre l'ingestion planifiée + le dashboard Dash.

Usage : python run.py  (dashboard sur http://127.0.0.1:8050)
"""
import logging

from gex.app import create_app
from gex.scheduler import start_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

if __name__ == "__main__":
    start_scheduler()
    create_app().run(host="127.0.0.1", port=8050, debug=False)
