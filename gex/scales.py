"""Transposition des niveaux vers une autre échelle de prix.

Deux régimes, volontairement distincts :

1. **Indice vers son propre future** (SPX→ES, NDX→NQ) : décalage ADDITIF du
   basis. C'est la conversion exacte — le future converge vers l'indice à
   l'échéance, un strike K se lit donc K + basis sur le future.

2. **Toute autre transposition** (SPX→SPY, SPX→NQ…) : conversion
   PROPORTIONNELLE, qui préserve la distance relative au spot. Un mur situé
   1,2 % au-dessus du spot SPX s'affiche 1,2 % au-dessus du spot cible.

La transposition CROISÉE (entre familles SP et ND) est mathématiquement
valide mais son ratio dérive en permanence : c'est un repère instantané, pas
un niveau stable. `Scale.cross_family()` permet à l'interface de le signaler.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import UNDERLYINGS


@dataclass(frozen=True)
class Scale:
    key: str            # identifiant d'échelle : "SPX", "ES", "QQQ"…
    label: str          # libellé affiché
    family: str         # "SP" ou "ND" — familles d'indices distinctes
    source: str         # sous-jacent fournissant le spot ("SPX" pour "ES")
    is_future: bool = False

    def cross_family(self, src_underlying: str) -> bool:
        """La transposition depuis ce sous-jacent change-t-elle de famille ?"""
        src = UNDERLYINGS.get(src_underlying)
        return src is not None and src.family != self.family


def available_scales() -> list[Scale]:
    """Échelles proposées : chaque sous-jacent actif, plus son future."""
    out: list[Scale] = []
    for u in UNDERLYINGS.values():
        if not u.enabled:
            continue
        out.append(Scale(u.key, u.key, u.family, u.key))
        if u.future:
            out.append(Scale(u.future, u.future, u.family, u.key, is_future=True))
    return out


def scale_by_key(key: str) -> Scale | None:
    return next((s for s in available_scales() if s.key == key), None)


def reference_price(scale: Scale, spots: dict[str, float],
                    bases: dict[str, float | None]) -> float | None:
    """Prix de référence de l'échelle : le spot du sous-jacent, augmenté du
    basis s'il s'agit du future."""
    spot = spots.get(scale.source)
    if spot is None:
        return None
    if scale.is_future:
        basis = bases.get(scale.source)
        if basis is None:
            return None
        return spot + basis
    return spot


def transform(src_underlying: str, target: Scale | None,
              spots: dict[str, float], bases: dict[str, float | None]):
    """Retourne (fonction de conversion, ratio, mode) pour passer des prix du
    sous-jacent source à l'échelle cible.

    mode ∈ {"native", "basis", "ratio"} — "native" = aucune conversion.
    Retourne l'identité si la conversion est impossible (spot cible absent),
    plutôt que d'afficher des niveaux faux.
    """
    identity = (lambda x: x), 1.0, "native"
    if target is None or target.key == src_underlying:
        return identity

    src_spot = spots.get(src_underlying)
    if not src_spot:
        return identity

    # cas exact : l'indice vers son propre future
    if target.is_future and target.source == src_underlying:
        basis = bases.get(src_underlying)
        if basis is None:
            return identity
        return (lambda x: x + basis), 1.0, "basis"

    tgt = reference_price(target, spots, bases)
    if not tgt:
        return identity
    ratio = tgt / src_spot
    return (lambda x: x * ratio), ratio, "ratio"
