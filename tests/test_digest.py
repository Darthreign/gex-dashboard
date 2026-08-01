"""Digest d'état du gamma (gex/digest.py).

Le cœur : reproduire EXACTEMENT le format demandé par l'utilisateur (4
exemples du 2026-07-30), y compris le décodage subtil — la glose
« (Dealers long/short gamma) » suit le signe du DELTA, pas du gamma.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gex import digest

PARIS = ZoneInfo("Europe/Paris")


def _row(sym, gex, dex, hist=None):
    return {"symbol": sym, "net_gex": gex, "net_dex": dex, "hist": hist}


def test_glose_suit_le_delta_pas_le_gamma():
    """Décodage clé : Delta+ → 'long gamma', Delta− → 'short gamma', quel que
    soit le signe du gamma."""
    assert classify_gloss(gex=+1, dex=+1) == "Dealers long gamma"
    assert classify_gloss(gex=-1, dex=+1) == "Dealers long gamma"   # gamma−, delta+ → long
    assert classify_gloss(gex=-1, dex=-1) == "Dealers short gamma"
    assert classify_gloss(gex=+1, dex=-1) == "Dealers short gamma"  # gamma+, delta− → short


def classify_gloss(gex, dex):
    return digest.classify(gex, dex)["gloss"]


def test_exemple_1_vert():
    """SPX/SPY/NDX/ES/NQ Gamma+ Delta+, QQQ Gamma− Delta+, VIX calme → vert."""
    rows = [_row(s, +1e9, +1e9) for s in ("SPX", "SPY", "NDX", "ES", "NQ")]
    rows.append(_row("QQQ", -1e9, +1e9))
    d = digest.build_digest(rows, vix=14.0)
    assert d.color == "green"
    assert "peu de risque" in d.verdict
    assert d.lines[0] == "Gamma Positif - Delta Positif (Dealers long gamma) sur SPX, SPY, NDX, ES et NQ"
    assert d.lines[1] == "Gamma Négatif - Delta Positif (Dealers long gamma) sur QQQ"
    assert d.vix_line is None


def test_exemple_2_orange_majorite_negative():
    rows = [_row(s, -1e9, +1e9) for s in ("SPX", "SPY", "ES", "NQ")]
    rows += [_row("QQQ", -1e9, -1e9), _row("NDX", -1e9, -1e9)]
    d = digest.build_digest(rows, vix=13.0)
    assert d.color == "orange"
    assert d.verdict == "Trading contrarient risqué sur session US."
    # QQQ et NDX regroupés sur la ligne short
    assert any("Delta Négatif (Dealers short gamma) sur NDX et QQQ" in ln for ln in d.lines)


def test_exemple_3_rouge_fort_gamma_negatif():
    """3 symboles en Fort Gamma Négatif → rouge, déconseillé."""
    hist_fort = [-1e8] * 25          # historique faible : |−5e9| écrase tout → fort
    rows = [_row(s, -1e8, +1e9, hist=[-1e8] * 25) for s in ("SPX", "SPY", "ES")]
    rows += [_row(s, -5e9, +1e9, hist=hist_fort) for s in ("QQQ", "NDX", "NQ")]
    d = digest.build_digest(rows, vix=15.0)
    assert d.color == "red"
    assert "déconseillé" in d.verdict.lower()
    assert any("Fort Gamma Négatif" in ln and "NDX, QQQ et NQ" in ln for ln in d.lines)


def test_exemple_4_orange_vix_haut_malgre_gamma_positif():
    """Tout Gamma+ mais VIX>17 → orange + forte amplitude."""
    rows = [_row(s, +1e9, +1e9) for s in ("SPX", "SPY", "NDX", "ES", "NQ")]
    rows.append(_row("QQQ", +1e9, -1e9))
    d = digest.build_digest(rows, vix=18.5)
    assert d.color == "orange"
    assert "forte amplitude" in d.verdict.lower()
    assert d.vix_line == "VIX supérieur à 17 ! (actuellement 18.5)"
    assert any("Delta Négatif (Dealers short gamma) sur QQQ" in ln for ln in d.lines)


def test_fort_exige_de_l_historique():
    """Sans 20 points d'historique, pas de 'Fort' deviné — juste 'Gamma
    Négatif'."""
    d = digest.build_digest([_row("SPX", -5e9, +1e9, hist=[-1e8] * 5)], vix=12.0)
    assert d.lines[0].startswith("Gamma Négatif -")


def test_header_paris_avec_offset():
    now = datetime(2026, 7, 30, 6, 30, tzinfo=ZoneInfo("UTC"))   # 08h30 Paris (CEST)
    d = digest.build_digest([_row("SPX", +1e9, +1e9)], vix=12.0, now=now)
    assert d.header == "État du gamma à 8h30 GMT+2 (Paris)"


def test_signature_change_sur_bascule_de_regime():
    """La signature doit changer quand un symbole flippe de régime — c'est ce
    qui déclenche un post 'changement de régime'."""
    a = digest.build_digest([_row("SPX", +1e9, +1e9)], vix=12.0).signature
    b = digest.build_digest([_row("SPX", -1e9, +1e9)], vix=12.0).signature
    assert a != b
    c = digest.build_digest([_row("SPX", +2e9, +1e9)], vix=12.0).signature
    assert a == c   # même régime (gamma+, delta+), magnitude différente → pas de post


def test_symbole_absent_ignore():
    """Un symbole sans données ne casse pas le digest."""
    d = digest.build_digest([_row("SPX", +1e9, +1e9), _row("AAPL", -1e9, +1e9)], vix=12.0)
    assert "AAPL" not in d.to_text()


def test_pas_de_recommandation_directionnelle():
    """Garde-fou : le verdict qualifie le RISQUE, jamais une direction."""
    d = digest.build_digest([_row("SPX", -5e9, +1e9)], vix=20.0)
    txt = d.to_text().lower()
    for interdit in ("achète", "vends", "acheter", "vendre", "prends un", "pose un"):
        assert interdit not in txt


# --- Export générique des graphiques ---

def test_chart_names_uniques_et_non_vide():
    from gex.app import CHART_NAMES
    assert len(CHART_NAMES) >= 10
    assert len(set(CHART_NAMES)) == len(CHART_NAMES)


def test_figure_for_nom_inconnu_renvoie_none():
    """Garde-fou : un nom de graphique inconnu ne rend rien (pas d'exception,
    pas de kaleido). L'endpoint renverra 404."""
    from gex.app import _figure_for
    assert _figure_for("SPX", "pas-un-graphe") is None
