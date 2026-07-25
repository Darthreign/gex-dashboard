"""Journalisation sur disque, partagée par le dashboard, le backfill et les
tâches planifiées.

Deux fichiers, deux usages :
- logs/gex.log      : log technique rotatif (INFO), écrit par le code
- logs/reports.md   : rapports lisibles append-only, écrits par les tâches
                      planifiées (qui tournent dans une conversation séparée
                      et dont la sortie serait sinon perdue)
"""
from __future__ import annotations

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import DATA_DIR

# logs/ à côté de data/ : racine du dépôt en développement, dossier courant
# après un pip install (cf. gex.config._default_data_dir).
LOG_DIR = DATA_DIR.parent / "logs"
LOG_FILE = LOG_DIR / "gex.log"
REPORTS_FILE = LOG_DIR / "reports.md"

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(level: int = logging.INFO, console: bool = True) -> None:
    """Configure le logging racine : console + fichier rotatif (5 Mo × 3)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    # évite les doublons si appelé deux fois (dashboard + backfill importé)
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(fh)
    if console and not any(isinstance(h, logging.StreamHandler)
                           and not isinstance(h, RotatingFileHandler)
                           for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(sh)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def write_report(title: str, body: str, source: str = "tâche planifiée") -> Path:
    """Ajoute un rapport horodaté à logs/reports.md (append-only).

    Destiné aux tâches planifiées : leur sortie vit dans une conversation
    séparée, ce fichier est le seul canal pour la retrouver ensuite.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n## {stamp} — {title}\n\n*source : {source}*\n\n{body.strip()}\n"
    with REPORTS_FILE.open("a", encoding="utf-8") as f:
        f.write(entry)
    return REPORTS_FILE


def read_reports(last_n: int = 5) -> str:
    """Retourne les derniers rapports (pour relecture rapide)."""
    if not REPORTS_FILE.exists():
        return "Aucun rapport enregistré."
    blocks = REPORTS_FILE.read_text(encoding="utf-8").split("\n## ")
    tail = blocks[-last_n:] if len(blocks) > last_n else blocks
    return "\n## ".join(b for b in tail if b.strip())
