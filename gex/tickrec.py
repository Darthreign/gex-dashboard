"""Enregistreur de ticks sur la FENÊTRE DE CLÔTURE (15h45-16h05 ET).

But : capturer le brut à la seconde autour de la clôture cash (22h Paris), la
seule source qui permette de répondre plus tard à « un stop aurait-il été
balayé ? » — impossible avec des bougies 1 min. Hors fenêtre, l'enregistreur est
totalement inerte (`active = False`), de sorte que le tap dans le chemin temps
réel (`rtquote._ingest`) ne coûte qu'une lecture d'attribut.

On garde le BRUT (fenêtre ~20 min/jour, quelques Mo) et on calcule les métriques
à la demande (cf. gex/tickstats) — conforme à « recalculable → pas stocké,
perdu si non saisi → stocké ». Un tick ne se rejoue pas s'il n'a pas été capté.

Le buffer est en mémoire ; l'écriture disque se fait à la fermeture de la
fenêtre (job planifié), jamais dans le chemin d'ingestion.
"""
from __future__ import annotations

import threading


class TickWindowRecorder:
    """Tampon mémoire des ticks des symboles suivis, actif seulement pendant la
    fenêtre de clôture. `record()` est appelé depuis le flux temps réel : il
    doit rester le plus léger possible (garde `active` en tête)."""

    def __init__(self, symbols: tuple[str, ...] = ("NQ", "ES")) -> None:
        self.symbols = set(symbols)
        self.active = False
        self._buf: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def arm(self) -> None:
        """Ouvre la fenêtre : les ticks des symboles suivis seront bufferisés."""
        with self._lock:
            self.active = True
            self._buf = {}

    def record(self, symbol: str, ts: float, price: float | None,
               bid: float | None = None, ask: float | None = None) -> None:
        """Enregistre un tick (appelé sur chaque Trade). Négligeable hors
        fenêtre : sort immédiatement si inactif ou symbole non suivi."""
        if not self.active or symbol not in self.symbols or price is None:
            return
        with self._lock:
            self._buf.setdefault(symbol, []).append(
                {"ts": ts, "price": float(price),
                 "bid": bid, "ask": ask, "source": "dxfeed"})

    def disarm_and_drain(self) -> dict[str, list[dict]]:
        """Ferme la fenêtre et récupère le buffer (à écrire sur disque)."""
        with self._lock:
            self.active = False
            out, self._buf = self._buf, {}
        return out


# Singleton partagé : le flux (rtquote) alimente, le scheduler arme/vide.
TICKREC = TickWindowRecorder()
