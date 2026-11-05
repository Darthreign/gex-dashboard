"""API de ticks en lecture seule pour String X Chart (repli de Rithmic).

Contrat : docs/api-ticks.md du dépôt String X Chart. Adresse de base à régler
dans l'application : https://<hôte>/api/v1

    GET {base}/ticks/{PRODUIT}/days
    GET {base}/ticks/{PRODUIT}/{AAAA-MM-JJ}.parquet
    GET {base}/ticks/stream?products=NQ,ES      (niveaux et profil de /scalp, en SSE)

Sécurité : jeton `Authorization: Bearer` obligatoire (variable GEX_TICKS_TOKEN).
Sans cette variable (ni GEX_TICKS_SECRET), tout est refusé : les routes restent inertes
tant qu'on ne l'a pas définie. Données personnelles et non redistribuables.

Jetons personnels : la page d'invités de Squirrel X délivre à chacun un jeton signé
`v1.<charge>.<signature>` (charge = e-mail et échéance, signature = HMAC-SHA256 avec
GEX_TICKS_SECRET). Il est vérifié ici sans base de données. Un e-mail listé dans le fichier
GEX_TICKS_REVOKED (un par ligne ; par défaut data/ticks_revoked.txt) est refusé aussitôt.
"""
from __future__ import annotations

import asyncio
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
# Garde-fou contre le partage de clé : flux ouverts en même temps par clé.
STREAM_MAX_CONN = 3
STREAM_RECHECK_S = 60.0
STREAM_RETRY_S = 15
STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

_hits: dict[str, deque[float]] = {}
_conns: dict[str, int] = {}
# RLock : StreamSlot.__del__ peut se déclencher pendant que le verrou est tenu.
_hits_lock = threading.RLock()
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


def _personal_email(given: str) -> str | None:
    """E-mail d'un jeton personnel valide : signature exacte, non expiré, non révoqué."""
    secret = _env("GEX_TICKS_SECRET") or ""
    parts = given.split(".")
    if not secret or len(parts) != 3 or parts[0] != "v1":
        return None
    expected = _b64(hmac.new(secret.encode(), parts[1].encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(parts[2].encode(), expected.encode()):
        return None
    try:
        claims = json.loads(_unb64(parts[1]))
        email, expires = str(claims["e"]), int(claims["exp"])
    except (ValueError, KeyError, TypeError):
        return None
    return email.lower() if expires > time.time() and not _revoked(email) else None


def key_of(header: str) -> str | None:
    """Identité de la clé d'un en-tête Authorization, None si elle est refusée.
    Sans Flask : sert aussi au flux asynchrone de gex/asgi.py."""
    expected = _env("GEX_TICKS_TOKEN") or ""
    given = header[7:] if header.startswith("Bearer ") else ""
    if expected and hmac.compare_digest(given.encode(), expected.encode()):
        return "partage"
    email = _personal_email(given)
    return f"perso:{email}" if email else None


def _over_quota(key: str) -> bool:
    """Compte une requête pour `key`, True si sa limite est atteinte. Par clé : un
    utilisateur ne doit pas pouvoir bloquer les autres."""
    now = time.monotonic()
    with _hits_lock:
        hits = _hits.setdefault(key, deque())
        while hits and now - hits[0] > RATE_WINDOW_S:
            hits.popleft()
        if len(hits) >= RATE_MAX:
            return True
        hits.append(now)
    return False


def _refused() -> Response | None:
    key = key_of(request.headers.get("Authorization", ""))
    if key is None:
        return Response(status=401)
    if _over_quota(key):
        return Response(status=429, headers={"Retry-After": str(int(RATE_WINDOW_S))})
    return None


class StreamSlot:
    """Place d'un flux ouvert. Libérée une seule fois, y compris par le ramasse-miettes
    si le client part avant le démarrage du flux (son `finally` ne s'exécute alors pas)."""

    def __init__(self, key: str) -> None:
        self.key = key
        self._held = True

    def release(self) -> None:
        with _hits_lock:
            if not self._held:
                return
            self._held = False
            _conns[self.key] -= 1
            if not _conns[self.key]:
                del _conns[self.key]

    __del__ = release


def open_stream(header: str) -> tuple[StreamSlot | None, tuple[int, dict] | None]:
    """(place, None) si le flux peut s'ouvrir, sinon (None, (statut, en-têtes)). Seule
    l'ouverture compte dans le quota, jamais les événements."""
    key = key_of(header)
    if key is None:
        return None, (401, {})
    if _over_quota(key):
        return None, (429, {"Retry-After": str(int(RATE_WINDOW_S))})
    with _hits_lock:
        if _conns.get(key, 0) >= STREAM_MAX_CONN:
            return None, (429, {"Retry-After": str(STREAM_RETRY_S)})
        _conns[key] = _conns.get(key, 0) + 1
        return StreamSlot(key), None


def guarded_stream(events, header: str, slot: StreamSlot):
    """Flux WSGI : la clé est recontrôlée régulièrement (le keepalive garantit un
    passage ici), et la connexion se ferme si elle a expiré ou été révoquée."""
    next_check = time.monotonic() + STREAM_RECHECK_S
    try:
        for chunk in events:
            if time.monotonic() >= next_check:
                if key_of(header) != slot.key:
                    return
                next_check = time.monotonic() + STREAM_RECHECK_S
            yield chunk
    finally:
        slot.release()
        events.close()


async def guarded_stream_async(events, header: str, slot: StreamSlot):
    """Même contrôle en asynchrone. `key_of` peut attendre un fichier verrouillé :
    hors de la boucle, qui sert toutes les connexions SSE."""
    next_check = time.monotonic() + STREAM_RECHECK_S
    try:
        async for chunk in events:
            if time.monotonic() >= next_check:
                if await asyncio.to_thread(key_of, header) != slot.key:
                    return
                next_check = time.monotonic() + STREAM_RECHECK_S
            yield chunk
    finally:
        slot.release()
        await events.aclose()


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
    _register_unknown_paths(server)

    def _ticks_stream():
        # Repli GEX_SERVER=waitress : sous ASGI, la route de gex/asgi.py passe avant.
        from . import broadcast
        from .app import scalp_feed_channels
        if request.method != "GET":
            return Response(status=405, headers={"Allow": "GET"})
        header = request.headers.get("Authorization", "")
        slot, refusal = open_stream(header)
        if slot is None:
            return Response(status=refusal[0], headers=refusal[1])
        chs = scalp_feed_channels(request.args.get("products"))
        if not chs:
            slot.release()
            return Response(status=404)
        return Response(guarded_stream(broadcast.multi_events(chs), header, slot),
                        mimetype="text/event-stream", headers=STREAM_HEADERS)
    server.add_url_rule("/api/v1/ticks/stream", endpoint="_ticks_stream", view_func=_ticks_stream,
                        methods=["GET"], provide_automatic_options=False)

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


def _register_unknown_paths(server: Flask) -> None:
    """Ce chemin passe le Bypass Cloudflare Access : sans cette règle, une adresse
    inconnue tomberait sur la page du dashboard (route attrape-tout de Dash) ou sur
    une route `/api/v1/<symbol>/...` avec « ticks » pour symbole, sans aucun jeton."""
    def _ticks_unknown(rest=""):
        return Response(status=404)
    for rule in ("/api/v1/ticks/", "/api/v1/ticks/<path:rest>"):
        server.add_url_rule(rule, endpoint="_ticks_unknown", view_func=_ticks_unknown,
                            methods=["GET"], provide_automatic_options=False)


def _route(server: Flask, rule: str):
    def deco(view):
        guarded = _guard(view)
        server.add_url_rule(rule, endpoint=view.__name__, view_func=guarded,
                            methods=["GET"], provide_automatic_options=False)
        return view
    return deco
