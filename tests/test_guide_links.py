"""Les titres de graphiques renvoient au guide : les ancres doivent exister.

Sans ce test, renommer une section du guide ou ajouter un graphique casserait
les liens en silence — le titre resterait cliquable et mènerait à une page
sans ancre, ce qui est pire qu'un titre non cliquable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gex.app import GUIDE_ANCHORS, guided

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "guide"


@pytest.mark.parametrize("key", sorted(GUIDE_ANCHORS))
def test_ancre_existe_dans_le_guide(key):
    page, anchor = GUIDE_ANCHORS[key]
    fichier = GUIDE / page
    assert fichier.exists(), f"page du guide absente : {page}"
    # ancres posées explicitement (<a id="...">), pas déduites du titre :
    # GitHub dérive les siennes du libellé, or nos titres sont traduits
    assert f'id="{anchor}"' in fichier.read_text(encoding="utf-8"), (
        f"ancre #{anchor} absente de {page}")


def test_titre_reste_affiche_si_la_cle_est_inconnue():
    """Un lien manquant ne doit jamais faire disparaître un titre."""
    assert guided("Mon titre", "cle-qui-nexiste-pas") == "Mon titre"


def test_le_lien_ouvre_un_nouvel_onglet():
    """Le dashboard tourne en local : remplacer la page en cours par GitHub
    ferait perdre l'état de l'interface (onglet, échéance, fenêtre…)."""
    html = guided("Titre", next(iter(GUIDE_ANCHORS)))
    assert 'target="_blank"' in html
    assert html.startswith("<a href=")
