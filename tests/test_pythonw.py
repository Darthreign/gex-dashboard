"""Lancement sans console (pythonw.exe, tâches planifiées) : sys.stdout et
sys.stderr valent None. Le démarrage ne doit pas planter."""
import sys

import uvicorn
from starlette.applications import Starlette

from gex import logsetup


def test_sans_console_les_flux_standard_sont_rediriges(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(logsetup, "LOG_DIR", tmp_path)
    logsetup._ensure_std_streams()
    assert sys.stdout is not None and not sys.stdout.isatty()
    assert sys.stderr is not None


def test_uvicorn_sans_console_avec_log_config_none(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    # configuration utilisée par gex/run.py : ne doit pas lever
    uvicorn.Config(Starlette(), log_level="warning", log_config=None).configure_logging()
