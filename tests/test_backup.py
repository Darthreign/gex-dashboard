"""Sauvegarde rclone : commande construite et garde-fous.

Rien n'est exécuté ici — on vérifie que la commande est correcte et que
l'absence de configuration ne fait tomber ni le planificateur ni la collecte.
"""
from __future__ import annotations

from gex import backup


def test_copie_et_non_synchronisation():
    """`sync` répercuterait les suppressions locales sur la sauvegarde : une
    fausse manœuvre effacerait le distant. `copy` ajoute et met à jour."""
    cmd = backup.build_command()
    assert cmd[1] == "copy"
    assert "sync" not in cmd


def test_depot_git_exclu():
    """data/.git est déjà répliqué sur GitHub — le copier ferait doublon."""
    cmd = backup.build_command()
    assert ".git/**" in cmd


def test_destination():
    cmd = backup.build_command(remote="autre")
    assert cmd[3] == f"autre:{backup.REMOTE_PATH}"


def test_dry_run():
    assert "--dry-run" in backup.build_command(dry_run=True)
    assert "--dry-run" not in backup.build_command()


def test_limite_de_debit_pour_drive():
    """Drive étrangle les clients trop bavards ; sans plafond la passe
    s'éternise en réessais."""
    cmd = backup.build_command()
    assert "--tpslimit" in cmd


def test_sans_remote_configure_ne_leve_pas(monkeypatch):
    """Appelée par le planificateur : une sauvegarde impossible ne doit pas
    interrompre la collecte."""
    monkeypatch.setattr(backup, "remote_configured", lambda *a, **k: False)
    assert backup.run() is False


def test_sans_rclone_ne_leve_pas(monkeypatch):
    monkeypatch.setattr(backup, "rclone_path", lambda: None)
    assert backup.run() is False
