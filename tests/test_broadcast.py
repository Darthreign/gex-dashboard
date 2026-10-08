"""Calcul unique, diffusion à tous (gex/broadcast.py)."""
from __future__ import annotations

import asyncio
import threading
import time

from gex import broadcast


def _canal(cle, interval=0.02):
    appels = {"n": 0}

    def factory():
        def produce():
            appels["n"] += 1
            return f"data: {appels['n']}\n\n"
        return produce
    return broadcast.channel(cle, factory, interval), appels


def test_un_seul_producteur_quel_que_soit_le_nombre_d_abonnes():
    ch, appels = _canal(("t", "multi"))
    gens = [broadcast.sse_events(ch) for _ in range(20)]
    recus = [next(g) for g in gens]          # chaque abonné reçoit un contenu
    assert all(r.startswith("data:") for r in recus)
    time.sleep(0.3)
    n = appels["n"]
    assert n < 40                            # ~15 cycles en 0,3 s, pas 20 x 15
    for g in gens:
        g.close()
    assert ch.subscribers == 0


def test_producteur_s_arrete_sans_abonne_et_redemarre():
    ch, appels = _canal(("t", "idle"))
    ch._idle_stop = 0.05
    g = broadcast.sse_events(ch)
    next(g)
    g.close()
    time.sleep(0.3)
    assert ch._thread is None
    n = appels["n"]
    time.sleep(0.1)
    assert appels["n"] == n                  # plus aucun calcul pour personne
    g = broadcast.sse_events(ch)
    assert next(g).startswith("data:")       # redémarre au premier abonné
    g.close()


def test_keepalive_si_rien_de_neuf():
    def factory():
        return lambda: None
    ch = broadcast.channel(("t", "silence"), factory, 0.01)
    g = broadcast.sse_events(ch, keepalive=0.05)
    assert next(g) == ": keepalive\n\n"
    g.close()


def test_abonne_asynchrone():
    ch, _ = _canal(("t", "async"))

    async def lire():
        agen = broadcast.sse_events_async(ch)
        try:
            return [await agen.__anext__() for _ in range(3)]
        finally:
            await agen.aclose()
    vus = asyncio.run(lire())
    assert all(v.startswith("data:") for v in vus) and len(set(vus)) == 3


def test_shared_calcule_une_fois_pour_n_requetes_concurrentes():
    appels = {"n": 0}

    @broadcast.shared(ttl=5.0)
    def callback(n_intervals, symbole):
        appels["n"] += 1
        time.sleep(0.1)
        return f"fig-{symbole}"

    res = []
    ths = [threading.Thread(target=lambda i=i: res.append(callback(i, "NQ"))) for i in range(10)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert res == ["fig-NQ"] * 10 and appels["n"] == 1
    assert callback(99, "ES") == "fig-ES" and appels["n"] == 2


def test_shared_ne_met_pas_les_exceptions_en_cache():
    appels = {"n": 0}

    @broadcast.shared(ttl=5.0)
    def callback(_, x):
        appels["n"] += 1
        raise ValueError("onglet masqué")

    for _ in range(2):
        try:
            callback(0, 1)
        except ValueError:
            pass
    assert appels["n"] == 2
