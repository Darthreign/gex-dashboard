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


def test_orange_une_seule_famille_negative():
    """UNE famille négative (Nasdaq) pendant que l'autre est positive (S&P)
    → orange. Le S&P reste sain, donc pas rouge."""
    rows = [_row(s, +1e9, +1e9) for s in ("SPX", "SPY", "ES")]     # S&P positif
    rows += [_row("NDX", -1e9, -1e9), _row("QQQ", -1e9, -1e9),      # Nasdaq négatif
             _row("NQ", -1e9, +1e9)]
    d = digest.build_digest(rows, vix=13.0)
    assert d.color == "orange"
    assert d.verdict == "Trading contrarient risqué sur session US."
    # NDX et QQQ (delta−) regroupés sur la ligne short
    assert any("Delta Négatif (Dealers short gamma) sur NDX et QQQ" in ln for ln in d.lines)


def test_deux_familles_negatives_rouge():
    """Les DEUX familles en gamma net négatif (sans même être 'fort') → rouge :
    tout le marché est short gamma. (C'était 'orange' sous l'ancien comptage
    par symbole ; le modèle par familles le juge plus sévèrement.)"""
    rows = [_row(s, -1e9, +1e9) for s in ("SPX", "SPY", "ES", "NQ")]
    rows += [_row("QQQ", -1e9, -1e9), _row("NDX", -1e9, -1e9)]
    d = digest.build_digest(rows, vix=13.0)
    assert d.color == "red"
    assert "déconseillé" in d.verdict.lower()


def test_exemple_3_rouge_fort_gamma_negatif():
    """3 symboles en Fort Gamma Négatif → rouge, déconseillé."""
    hist_fort = [-1e8] * 25          # historique faible : |−5e9| écrase tout → fort
    rows = [_row(s, -1e8, +1e9, hist=[-1e8] * 25) for s in ("SPX", "SPY", "ES")]
    rows += [_row(s, -5e9, +1e9, hist=hist_fort) for s in ("QQQ", "NDX", "NQ")]
    d = digest.build_digest(rows, vix=15.0)
    assert d.color == "red"
    assert "déconseillé" in d.verdict.lower()
    assert any("Fort Gamma Négatif" in ln and "NDX, QQQ et NQ" in ln for ln in d.lines)


def test_vix_entre_16_et_20_reste_vert_si_structure_saine():
    """VIX 16-20 : ligne d'alerte affichée (fin du confort), MAIS un verdict sain
    reste VERT — le VIX ne force la couleur qu'à partir de 20."""
    rows = [_row(s, +1e9, +1e9) for s in ("SPX", "SPY", "NDX", "ES", "NQ")]
    rows.append(_row("QQQ", +1e9, -1e9))
    d = digest.build_digest(rows, vix=18.5)
    assert d.color == "green"
    assert "peu de risque" in d.verdict
    assert d.vix_line == "VIX supérieur à 16 ! (actuellement 18.50)"   # info conservée


def test_vix_au_dessus_de_20_force_orange():
    """VIX ≥ 20 (vraiment élevé) → orange + forte amplitude, même tout Gamma+."""
    rows = [_row(s, +1e9, +1e9) for s in ("SPX", "SPY", "NDX", "ES", "NQ")]
    rows.append(_row("QQQ", +1e9, -1e9))
    d = digest.build_digest(rows, vix=22.0)
    assert d.color == "orange"
    assert "forte amplitude" in d.verdict.lower()
    assert any("Delta Négatif (Dealers short gamma) sur QQQ" in ln for ln in d.lines)


def test_fort_exige_de_l_historique():
    """Sans 20 points d'historique, pas de 'Fort' deviné — juste 'Gamma
    Négatif'."""
    d = digest.build_digest([_row("SPX", -5e9, +1e9, hist=[-1e8] * 5)], vix=12.0)
    assert d.lines[0].startswith("Gamma Négatif -")


def test_vix_grade_paliers():
    """Le VIX est gradé : 16 (= seuil) est 'Calme', 'Élevé' commence à 20."""
    assert digest.vix_grade(10)["label"] == "Complaisance"
    assert digest.vix_grade(14)["label"] == "Calme"
    assert digest.vix_grade(16)["label"] == "Normal-haut"   # 16 = fin du confort
    assert digest.vix_grade(22)["label"] == "Élevé"
    assert digest.vix_grade(30)["label"] == "Stress"
    assert digest.vix_grade(50)["label"] == "Panique"
    assert digest.vix_grade(None) is None


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


# --- Verdict par familles (S&P / Nasdaq) ---

def test_indice_principal_fort_suffit_pour_rouge():
    """SPX seul en FORT gamma négatif fait basculer toute la famille S&P en
    fort négatif → rouge, même si SPY/ES sont positifs et le Nasdaq sain.
    C'est le choix (a) : 'le cash index commande'."""
    hist_fort = [-1e8] * 25
    rows = [_row("SPX", -5e9, +1e9, hist=hist_fort),     # SPX fort négatif
            _row("SPY", +1e9, +1e9), _row("ES", +1e9, +1e9)]
    rows += [_row(s, +1e9, +1e9) for s in ("NDX", "QQQ", "NQ")]   # Nasdaq positif
    d = digest.build_digest(rows, vix=12.0)
    assert d.color == "red"


def test_ponderation_famille_pas_dominee_par_le_future():
    """NDX− (poids 3), QQQ− (2), NQ+ (1) : la famille reste négative, le future
    NQ positif ne renverse pas le verdict de l'indice cash."""
    rows = [_row(s, +1e9, +1e9) for s in ("SPX", "SPY", "ES")]    # S&P positif
    rows += [_row("NDX", -1e9, +1e9), _row("QQQ", -1e9, +1e9), _row("NQ", +1e9, +1e9)]
    d = digest.build_digest(rows, vix=12.0)
    assert d.color == "orange"          # une seule famille (Nasdaq) négative
    # inversement, si seul NQ est négatif (poids 1) et l'indice cash positif,
    # la famille reste positive → pas d'orange.
    rows2 = [_row(s, +1e9, +1e9) for s in ("SPX", "SPY", "ES", "NDX", "QQQ")]
    rows2.append(_row("NQ", -1e9, +1e9))
    assert digest.build_digest(rows2, vix=12.0).color == "green"


def test_normalisation_symbole_manquant():
    """Le score de famille est normalisé par les poids présents : NQ absent ne
    change pas le verdict tiré de NDX/QQQ."""
    complet = digest.build_digest(
        [_row(s, +1e9, +1e9) for s in ("SPX", "SPY", "ES")] +
        [_row("NDX", -1e9, +1e9), _row("QQQ", -1e9, +1e9), _row("NQ", -1e9, +1e9)],
        vix=12.0)
    sans_nq = digest.build_digest(
        [_row(s, +1e9, +1e9) for s in ("SPX", "SPY", "ES")] +
        [_row("NDX", -1e9, +1e9), _row("QQQ", -1e9, +1e9)],
        vix=12.0)
    assert complet.color == sans_nq.color == "orange"


def test_confiance_forte_quand_famille_complete_et_concordante():
    rows = [_row(s, +1e9, +1e9) for s in ("SPX", "SPY", "ES", "NDX", "QQQ", "NQ")]
    d = digest.build_digest(rows, vix=12.0)
    assert d.confidence == "forte"
    assert "Confiance : Forte" in d.to_text()


def test_confiance_faible_si_indice_principal_absent():
    """Sans SPX ni NDX (indices cash), on n'a pas la référence → confiance
    faible, même si des symboles sont présents."""
    rows = [_row("SPY", +1e9, +1e9), _row("ES", +1e9, +1e9),
            _row("QQQ", +1e9, +1e9), _row("NQ", +1e9, +1e9)]
    d = digest.build_digest(rows, vix=12.0)
    assert d.confidence == "faible"


def test_confiance_faible_si_signes_contradictoires():
    """Indice principal présent mais un pair le contredit en signe → la
    confiance de cette famille tombe, et la globale suit (maillon faible)."""
    rows = [_row(s, +1e9, +1e9) for s in ("SPX", "SPY", "ES", "NDX", "QQQ")]
    rows.append(_row("NQ", -1e9, +1e9))          # NQ contredit le Nasdaq positif
    d = digest.build_digest(rows, vix=12.0)
    assert d.confidence == "faible"


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
