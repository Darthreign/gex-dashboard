"""Qui verrouille un fichier ? Diagnostic automatique des echecs d'ecriture.

Sous Windows, `os.replace` echoue (WinError 5) tant qu'un autre processus tient
la destination ouverte. Le message d'erreur ne dit JAMAIS lequel : le
2026-08-31, trois heures de bougies 1 min ont ete perdues sans qu'on puisse
identifier le coupable apres coup (MCP et moteur de backtest tous deux mis hors
de cause, sans que le vrai responsable apparaisse). D'ou ce module : au moment
ou l'ecriture echoue — le seul instant ou le verrou est observable — on demande
a Windows qui tient le fichier, et on l'ecrit dans logs/lockdiag.log.

Repose sur l'API Restart Manager (`rstrtmgr.dll`), celle qu'utilisent les
installeurs pour dire « fermez Word avant de continuer ». Elle est faite
exactement pour cette question, vit dans le systeme (aucun paquet a installer,
aucun binaire a telecharger) et n'exige pas de droits administrateur.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

# Un diagnostic par fenetre de temps : l'incident du 2026-08-31 a produit 3 659
# echecs : sans ce garde-fou on lancerait 3 659 diagnostics.
COOLDOWN_S = 300.0
_last_run = 0.0
_guard = threading.Lock()

CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63
RM_REBOOT_REASON_NONE = 0
_APP_TYPES = {0: "inconnu", 1: "application", 2: "application en arriere-plan",
              3: "explorateur", 4: "console", 5: "service", 6: "session Windows"}


class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD),
                ("ProcessStartTime", wintypes.FILETIME)]


class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [("Process", _RM_UNIQUE_PROCESS),
                ("strAppName", wintypes.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
                ("strServiceShortName", wintypes.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
                ("ApplicationType", ctypes.c_uint),
                ("AppStatus", ctypes.c_ulong),
                ("TSSessionId", wintypes.DWORD),
                ("bRestartable", wintypes.BOOL)]


def who_locks(path: Path) -> list[dict]:
    """Processus tenant `path` ouvert, via le Restart Manager.

    Renvoie une liste de {pid, nom, type, cmdline}. Liste vide si personne ne
    le tient (ou si l'API n'est pas disponible) — ne leve jamais : un
    diagnostic qui plante serait pire que pas de diagnostic.
    """
    if sys.platform != "win32":
        return []
    try:
        rm = ctypes.WinDLL("rstrtmgr")
        session = wintypes.DWORD()
        key = (wintypes.WCHAR * 33)()
        if rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
            return []
        try:
            files = (wintypes.LPCWSTR * 1)(str(path))
            if rm.RmRegisterResources(session, 1, files, 0, None, 0, None) != 0:
                return []
            besoin = ctypes.c_uint(0)
            n = ctypes.c_uint(0)
            raison = wintypes.DWORD()
            # premier appel : combien de processus ? (ERROR_MORE_DATA attendu)
            rm.RmGetList(session, ctypes.byref(besoin), ctypes.byref(n),
                         None, ctypes.byref(raison))
            if besoin.value == 0:
                return []
            infos = (_RM_PROCESS_INFO * besoin.value)()
            n = ctypes.c_uint(besoin.value)
            if rm.RmGetList(session, ctypes.byref(besoin), ctypes.byref(n),
                            infos, ctypes.byref(raison)) != 0:
                return []
            return [{"pid": infos[i].Process.dwProcessId,
                     "nom": infos[i].strAppName,
                     "type": _APP_TYPES.get(infos[i].ApplicationType, "?"),
                     "cmdline": _cmdline(infos[i].Process.dwProcessId)}
                    for i in range(n.value)]
        finally:
            rm.RmEndSession(session)
    except Exception:  # noqa: BLE001 — le diagnostic ne doit jamais casser l'appelant
        log.debug("Restart Manager indisponible", exc_info=True)
        return []


def _cmdline(pid: int) -> str:
    """Ligne de commande du processus — c'est elle qui identifie vraiment le
    coupable (« python.exe » seul ne dit rien quand cinq python tournent)."""
    try:
        import subprocess
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\")"
             ".CommandLine"],
            capture_output=True, text=True, timeout=15)
        return (r.stdout or "").strip() or "?"
    except Exception:  # noqa: BLE001
        return "?"


def _log_path() -> Path:
    from .config import SETTINGS
    p = SETTINGS.data_dir.parent / "logs" / "lockdiag.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def report(path: Path) -> None:
    """Identifie et JOURNALISE qui verrouille `path`. Limite par COOLDOWN_S."""
    global _last_run
    with _guard:
        if time.monotonic() - _last_run < COOLDOWN_S:
            return
        _last_run = time.monotonic()

    holders = who_locks(path)
    horo = time.strftime("%Y-%m-%d %H:%M:%S")
    if holders:
        lignes = [f"{horo} | VERROU sur {path}"]
        for h in holders:
            lignes.append(f"    PID {h['pid']} — {h['nom']} ({h['type']})")
            lignes.append(f"        {h['cmdline']}")
        log.error("Fichier verrouillé par : %s",
                  ", ".join(f"{h['nom']} (PID {h['pid']})" for h in holders))
    else:
        lignes = [f"{horo} | {path} : échec d'écriture mais AUCUN processus "
                  f"détenteur signalé (antivirus/indexeur, ou verrou déjà relâché)"]
        log.error("Échec d'écriture sur %s sans détenteur identifiable "
                  "(antivirus ou indexeur ?)", path.name)
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write("\n".join(lignes) + "\n")
    except Exception:  # noqa: BLE001
        log.debug("Écriture de lockdiag.log impossible", exc_info=True)


def report_async(path: Path) -> None:
    """`report` dans un thread : l'appel peut prendre quelques secondes et ne
    doit pas retarder la boucle d'ingestion."""
    threading.Thread(target=report, args=(path,), name="lockdiag",
                     daemon=True).start()
