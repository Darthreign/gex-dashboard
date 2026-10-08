"""Serveur ASGI du dashboard : flux SSE en asynchrone, Dash/Flask derrière.

Pourquoi : sous waitress, chaque flux SSE ouvert immobilisait un thread du
pool pour toute la vie de l'onglet — d'où les relèvements successifs
(8 -> 256 threads, connection_limit 500) et les pannes « connection limit
reached » en séance. Ici :

- les flux SSE (/stream, /scalp-indicators-stream, /chart-stream,
  /scalp-stream) sont
  servis par la boucle asyncio d'uvicorn : une connexion ne coûte qu'une
  coroutine endormie, des milliers tiennent sur un seul thread. Elles lisent
  les mêmes canaux partagés (gex/broadcast.py) que la version WSGI ;
- tout le reste (Dash, API JSON, images) passe inchangé par a2wsgi, dans un
  pool de threads BORNÉ (`WSGI_THREADS`) réservé aux vraies requêtes courtes.

uvicorn, Starlette et a2wsgi sont en Python pur et tournent sous Windows.
Repli possible sur waitress : GEX_SERVER=waitress (cf. gex/run.py).
"""
from __future__ import annotations

from a2wsgi import WSGIMiddleware
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route

from . import broadcast

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
# Requêtes WSGI simultanées (callbacks Dash, API). Les flux SSE n'en
# consomment plus aucune : ce pool n'a plus à absorber les onglets ouverts.
WSGI_THREADS = 32


def _sse(ch: broadcast.Channel) -> StreamingResponse:
    return StreamingResponse(broadcast.sse_events_async(ch),
                             media_type="text/event-stream", headers=SSE_HEADERS)


async def price_stream(request):
    from .api import price_channel
    symbol = request.path_params["symbol"].upper()
    if symbol not in ("NQ", "ES"):
        return JSONResponse({"error": "symbole non couvert (NQ/ES seulement)"}, 404)
    return _sse(price_channel(symbol))


async def scalp_indicators_stream(request):
    from .app import SCALP_SCHED_SYMBOLS, scalp_indicators_channel
    symbol = request.path_params["symbol"].upper()
    if symbol not in SCALP_SCHED_SYMBOLS:
        return JSONResponse({"error": "symbole non couvert (NQ/ES seulement)"}, 404)
    lang = request.query_params.get("lang", "fr")
    swing = request.query_params.get("swing") == "1"
    return _sse(scalp_indicators_channel(symbol, lang, swing))


async def scalp_chart_stream(request):
    from .app import CHART_TF_DEFAULT, SCALP_SCHED_SYMBOLS, scalp_chart_channel
    symbol = request.path_params["symbol"].upper()
    if symbol not in SCALP_SCHED_SYMBOLS:
        return JSONResponse({"error": "symbole non couvert (NQ/ES seulement)"}, 404)
    tf = request.query_params.get("tf") or CHART_TF_DEFAULT
    return _sse(scalp_chart_channel(symbol, tf))


async def scalp_panels_stream(request):
    from .app import SCALP_SCHED_SYMBOLS, scalp_panels_channel, scalp_panels_params
    symbol = request.path_params["symbol"].upper()
    if symbol not in SCALP_SCHED_SYMBOLS:
        return JSONResponse({"error": "symbole non couvert (NQ/ES seulement)"}, 404)
    return _sse(scalp_panels_channel(symbol, *scalp_panels_params(request.query_params)))


async def lw_stream(request):
    from .app import LW_CHARTS, lw_channel, lw_params
    name = request.path_params["name"]
    params = lw_params(request.query_params)
    if name not in LW_CHARTS or params is None:
        return JSONResponse({"error": "graphique ou symbole inconnu"}, 404)
    return _sse(lw_channel(name, params))


async def lw_multi_stream(request):
    from .app import lw_multi_channels
    chs = lw_multi_channels(request.query_params.get("q"))
    if not chs:
        return JSONResponse({"error": "aucun graphique valide"}, 404)
    return StreamingResponse(broadcast.multi_events_async(chs),
                             media_type="text/event-stream", headers=SSE_HEADERS)


def build(dash_app, wsgi_threads: int = WSGI_THREADS) -> Starlette:
    """Application ASGI : routes SSE asynchrones, puis tout le reste vers
    l'application Flask de Dash."""
    return Starlette(routes=[
        Route("/api/v1/{symbol}/stream", price_stream),
        Route("/api/v1/{symbol}/scalp-indicators-stream", scalp_indicators_stream),
        Route("/api/v1/{symbol}/chart-stream", scalp_chart_stream),
        Route("/api/v1/{symbol}/scalp-stream", scalp_panels_stream),
        Route("/api/v1/lw-multi", lw_multi_stream),
        Route("/api/v1/lw/{name}", lw_stream),
        Mount("/", app=WSGIMiddleware(dash_app.server, workers=wsgi_threads)),
    ])
