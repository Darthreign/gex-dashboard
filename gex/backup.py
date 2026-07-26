"""Sauvegarde de data/ vers un stockage distant via rclone.

Complète le dépôt git de données, qui ne peut pas tout porter : GitHub rejette
tout fichier de plus de 100 Mo, or les archives Databento en dépassent. rclone
n'a pas cette limite et parle indifféremment à Drive, OneDrive, B2 ou S3.

Deux choix de conception :

**`copy` et non `sync`.** rclone `sync` reflète les suppressions locales sur le
distant ; une erreur de manipulation effacerait donc la sauvegarde. `copy`
ajoute et met à jour, sans jamais supprimer — le comportement qu'on attend
d'une sauvegarde.

**Programmée, jamais continue.** Un client de synchronisation de bureau
re-téléverserait le fichier de prix du jour à chaque écriture, soit toutes les
30 secondes et par symbole. Une passe quotidienne après la clôture évite ce
gaspillage.

Configuration préalable (une fois, dans un terminal) :

    rclone config

Créer un remote nommé `gexbackup` — l'autorisation passe par le navigateur.
Vérifier ensuite avec `python -m gex.backup --check`.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess

from .config import SETTINGS

log = logging.getLogger(__name__)

REMOTE = "gexbackup"
REMOTE_PATH = "gex-data"

# Le dépôt git local est déjà répliqué sur GitHub : le copier ferait doublon,
# pour un contenu compressé qui se re-téléverse mal.
EXCLUDES = [".git/**", "*.tmp", "*.lock"]


def rclone_path() -> str | None:
    return shutil.which("rclone")


def remote_configured(remote: str = REMOTE) -> bool:
    exe = rclone_path()
    if not exe:
        return False
    try:
        out = subprocess.run([exe, "listremotes"], capture_output=True,
                             text=True, timeout=30)
        return f"{remote}:" in out.stdout
    except Exception:  # noqa: BLE001
        return False


def build_command(remote: str = REMOTE, dry_run: bool = False) -> list[str]:
    exe = rclone_path() or "rclone"
    cmd = [exe, "copy", str(SETTINGS.data_dir), f"{remote}:{REMOTE_PATH}",
           # les Parquet sont déjà compressés : vérifier la taille et la date
           # suffit, et évite de relire des centaines de Mo pour rien
           "--size-only",
           "--transfers", "4",
           "--checkers", "8",
           # Drive limite le débit de requêtes ; au-delà rclone se fait
           # étrangler et la passe s'éternise
           "--tpslimit", "10",
           "--retries", "3",
           "--stats", "30s",
           "--stats-one-line",
           "--log-level", "INFO"]
    for pattern in EXCLUDES:
        cmd += ["--exclude", pattern]
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def run(remote: str = REMOTE, dry_run: bool = False) -> bool:
    """Lance la sauvegarde. Renvoie True si elle s'est terminée proprement.

    Ne lève jamais : appelée depuis le planificateur, une sauvegarde ratée ne
    doit pas interrompre la collecte.
    """
    if not rclone_path():
        log.warning("rclone introuvable — sauvegarde ignorée")
        return False
    if not remote_configured(remote):
        log.warning("Remote rclone '%s' non configuré — lancer `rclone config`",
                    remote)
        return False
    cmd = build_command(remote, dry_run)
    log.info("Sauvegarde vers %s: — %s", remote, SETTINGS.data_dir)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3 * 3600)
    except Exception:  # noqa: BLE001
        log.exception("Sauvegarde interrompue")
        return False
    tail = (res.stderr or res.stdout or "").strip().splitlines()[-4:]
    for line in tail:
        log.info("  %s", line)
    if res.returncode != 0:
        log.error("rclone a échoué (code %d)", res.returncode)
        return False
    log.info("Sauvegarde terminée")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", default=REMOTE)
    parser.add_argument("--dry-run", action="store_true",
                        help="montre ce qui serait envoyé, sans rien envoyer")
    parser.add_argument("--check", action="store_true",
                        help="vérifie l'installation et la configuration")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.check:
        exe = rclone_path()
        print(f"rclone           : {exe or 'ABSENT'}")
        print(f"remote '{args.remote}' : "
              f"{'configuré' if remote_configured(args.remote) else 'NON CONFIGURÉ'}")
        print(f"source           : {SETTINGS.data_dir}")
        print(f"destination      : {args.remote}:{REMOTE_PATH}")
        if not remote_configured(args.remote):
            print(f"\nÀ faire : `rclone config`, créer un remote nommé "
                  f"'{args.remote}' (Google Drive = option `drive`).")
        return

    run(args.remote, args.dry_run)


if __name__ == "__main__":
    main()
