"""API de ticks en lecture seule pour String X Chart (repli de Rithmic).

Contrat : docs/api-ticks.md du dépôt String X Chart. Adresse de base à régler
dans l'application : https://<hôte>/api/v1

    GET {base}/ticks/{PRODUIT}/days
    GET {base}/ticks/{PRODUIT}/{AAAA-MM-JJ}.parquet

Sécurité : jeton `Authorization: Bearer` obligatoire (variable GEX_TICKS_TOKEN).
Sans cette variable (ni GEX_TICKS_SECRET), tout est refusé : les routes restent inertes
tant qu'on ne l'a pas définie. Données personnelles et non redistribuables.

Jetons personnels : la page d'invités de Squirrel X délivre à chacun un jeton signé
`v1.<charge>.<signature>` (charge = e-mail et échéance, signature = HMAC-SHA256 avec
GEX_TICKS_SECRET). Il est vérifié ici sans base de données. Un e-mail listé dans le fichier
GEX_TICKS_REVOKED (un par ligne ; par défaut data/ticks_revoked.txt) est refusé aussitôt.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
from flask import Flask, Response, jsonify, request

from .config import SETTINGS
from .rtquote import _env
from .store import REPLACE_DELAY_S, REPLACE_RETRIES

log = logging.getLogger(__name__)

PRODUCT_RE = re.compile(r"^[A-Z0-9]{1,8}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Colonnes exigées : String X Chart refuse un fichier auquel l'une d'elles manque.
REQUIRED = {"ts", "price", "volume", "side"}
RATE_MAX = 120
RATE_WINDOW_S = 60.0

_hits: deque[float] = deque()
_hits_lock = threading.Lock()
_schema_ok: dict[Path, tuple[int, int, bool]] = {}


def _roots(product: str) -> tuple[Path, Path, Path]:
    """Dans l'ordre de préférence : l'export de fin de séance, définitif (repli
    Databento si le live était incomplet) ; sa copie avec le côté agresseur
    reconstruit pour l'historique ancien (scripts/rebuild_ticks_side.py) ; puis
    la capture live, seule à avoir la séance en cours."""
    imp = SETTINGS.data_dir / "import"
    return (imp / "ticks_full" / product, imp / "ticks_full_side" / product,
            SETTINGS.data_dir / "ticks" / product)


def _retry_locked(fn):
    """Windows refuse l'ouverture pendant que la capture remplace le fichier."""
    for essai in range(REPLACE_RETRIES):
        try:
            return fn()
        except PermissionError:
            if essai == REPLACE_RETRIES - 1:
                raise
            time.sleep(REPLACE_DELAY_S * (essai + 1))
    raise AssertionError("unreachable")  # pragma: no cover


def _usable(path: Path) -> bool:
    st = path.stat()
    hit = _schema_ok.get(path)
    if hit and hit[:2] == (st.st_mtime_ns, st.st_size):
        return hit[2]
    ok = REQUIRED <= set(_retry_locked(lambda: pq.read_schema(path)).names)
    _schema_ok[path] = (st.st_mtime_ns, st.st_size, ok)
    return ok


def _session_file(product: str, day: str) -> Path | None:
    for root in _roots(product):
        path = root / f"{day}.parquet"
        if path.exists() and _usable(path):
            return path
    return None


def available_days(product: str) -> list[str]:
    stems = {p.stem for root in _roots(product) if root.exists() for p in root.glob("*.parquet")}
    return sorted(d for d in stems if DAY_RE.match(d) and _session_file(product, d))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_token(email: str, expires: int, secret: str) -> str:
    """Jeton personnel `v1.<charge>.<signature>` (même format que la page d'invités)."""
    payload = _b64(json.dumps({"e": email.lower(), "exp": int(expires)}, separators=(",", ":")).encode())
    return f"v1.{payload}.{_b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())}"


def _revoked(email: str) -> bool:
    """Sans fichier, personne n'est révoqué. Un fichier illisible refuse tous les jetons
    personnels : mieux vaut un refus de trop qu'un e-mail bloqué qui passe."""
    path = Path(_env("GEX_TICKS_REVOKED") or SETTINGS.data_dir / "ticks_revoked.txt")
    try:
        lines = _retry_locked(lambda: path.read_text(encoding="utf-8")).splitlines()
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError) as exc:
        log.warning("Liste de révocation illisible, jetons personnels refusés : %s", type(exc).__name__)
        return True
    return email.lower() in {l.strip().lower() for l in lines if l.strip() and not l.startswith("#")}


def _personal_ok(given: str) -> bool:
    """Jeton personnel valide : signature exacte, non expiré, e-mail non révoqué."""
    secret = _env("GEX_TICKS_SECRET") or ""
    parts = given.split(".")
    if not secret or len(parts) != 3 or parts[0] != "v1":
        return False
    expected = _b64(hmac.new(secret.encode(), parts[1].encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(parts[2].encode(), expected.encode()):
        return False
    try:
        claims = json.loads(_unb64(parts[1]))
        email, expires = str(claims["e"]), int(claims["exp"])
    except (ValueError, KeyError, TypeError):
        return False
    return expires > time.time() and not _revoked(email)


def _refused() -> Response | None:
    expected = _env("GEX_TICKS_TOKEN") or ""
    header = request.headers.get("Authorization", "")
    given = header[7:] if header.startswith("Bearer ") else ""
    shared = bool(expected) and hmac.compare_digest(given.encode(), expected.encode())
    if not shared and not _personal_ok(given):
        return Response(status=401)
    now = time.monotonic()
    with _hits_lock:
        while _hits and now - _hits[0] > RATE_WINDOW_S:
            _hits.popleft()
        if len(_hits) >= RATE_MAX:
            return Response(status=429, headers={"Retry-After": str(int(RATE_WINDOW_S))})
        _hits.append(now)
    return None


def _guard(view):
    def wrapped(*args, **kwargs):
        # Flask ajoute HEAD à toute route GET : le contrat n'admet que GET.
        if request.method != "GET":
            return Response(status=405, headers={"Allow": "GET"})
        return _refused() or view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def _read_for_response(path: Path) -> tuple[str, bytes | None]:
    """(ETag, contenu), contenu à None si le client a déjà cette version. Le
    fichier est lu en mémoire puis refermé AVANT l'envoi : sous Windows, un
    fichier ouvert empêche la capture de le remplacer, et un client lent lui
    ferait perdre son écriture du cycle (cf. store._replace_with_retry)."""
    def read() -> tuple[str, bytes | None]:
        with path.open("rb") as f:
            st = os.fstat(f.fileno())
            etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
            if _matches(etag):
                return etag, None
            return etag, f.read()
    return _retry_locked(read)


def _matches(etag: str) -> bool:
    sent = request.headers.get("If-None-Match", "")
    return sent.strip() == "*" or etag in [t.strip().removeprefix("W/") for t in sent.split(",")]


def register_ticks_api(server: Flask) -> None:
    @_route(server, "/api/v1/ticks/<product>/days")
    def _ticks_days(product):
        if not PRODUCT_RE.match(product):
            return Response(status=404)
        days = available_days(product)
        return jsonify({"days": days}) if days else Response(status=404)

    @_route(server, "/api/v1/ticks/<product>/<day>.parquet")
    def _ticks_file(product, day):
        if not PRODUCT_RE.match(product) or not DAY_RE.match(day):
            return Response(status=404)
        try:
            date.fromisoformat(day)
        except ValueError:
            return Response(status=404)
        path = _session_file(product, day)
        if path is None:
            return Response(status=404)
        etag, data = _read_for_response(path)
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if data is None:
            return Response(status=304, headers=headers)
        return Response(data, mimetype="application/octet-stream", headers=headers)


def _route(server: Flask, rule: str):
    def deco(view):
        guarded = _guard(view)
        server.add_url_rule(rule, endpoint=view.__name__, view_func=guarded,
                            methods=["GET"], provide_automatic_options=False)
        return view
    return deco
