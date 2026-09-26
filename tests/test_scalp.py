"""Page Scalp : échelle de niveaux et état de séance (gex/scalp.py)."""
from __future__ import annotations

from datetime import datetime

from gex import scalp


def _ladder(spot=30890.0):
    return scalp.build_ladder(
        "NQ", spot, zg=30653.0, hvl=30713.0,
        keys={"call_wall": 31000.0, "put_support": 30000.0, "d1_min": 30447.0,
              "d1_max": 31026.0},
        walls=[("GEX1", 31000.0, 3e8), ("GEX2", 30000.0, -2e8), ("GEX4", 30850.0, 1e8)])


def test_echelle_triee_du_haut_vers_le_bas_avec_distances():
    r = _ladder()
    prix = [x.price for x in r]
    assert prix == sorted(prix, reverse=True)
    cw = next(x for x in r if x.name == "Call Wall")
    assert cw.dist == 110.0 and cw.kind == "cw"
    assert next(x for x in r if x.name == "Gamma Flip").dist == 30653.0 - 30890.0


def test_niveau_au_contact_selon_le_seuil_du_sous_jacent():
    r = _ladder(30890.0)
    contact = {x.name for x in r if x.near}
    assert contact == set()                         # GEX4 à 40 pts : hors du seuil (15)
    r2 = _ladder(30845.0)
    assert {x.name for x in r2 if x.near} == {"GEX4"}          # 5 pts
    assert scalp.near_threshold("ES") == 4.0 and scalp.near_threshold("XYZ") == 10.0


def test_deux_niveaux_au_meme_prix_restent_deux_lignes():
    r = _ladder()
    a = [x for x in r if x.price == 31000.0]
    assert {x.name for x in a} == {"Call Wall", "GEX1"}


def test_niveaux_absents_omis_pas_inventes():
    r = scalp.build_ladder("NQ", 30000.0, zg=None, hvl=None, keys={}, walls=None)
    assert r == []
    assert scalp.nearest(r) == (None, None)


def test_plus_proche_au_dessus_et_en_dessous():
    haut, bas = scalp.nearest(_ladder(30890.0))
    assert haut.name in ("Call Wall", "GEX1") and haut.price == 31000.0
    assert bas.name == "GEX4" and bas.price == 30850.0


def test_etat_de_seance():
    et = lambda h, m, d=25: datetime(2026, 9, d, h, m)          # 25/09/2026 = vendredi
    assert scalp.session_state(et(9, 0))[0] == "pre"
    code, txt = scalp.session_state(et(9, 45))
    assert code == "open" and "15 min" in txt
    assert scalp.session_state(et(10, 30))[0] == "late"
    assert scalp.session_state(et(16, 5))[0] == "closed"
    assert scalp.session_state(et(11, 0, d=26))[0] == "closed"    # samedi


def test_extension_a_louverture():
    assert scalp.extension_pts(30890.0, 30800.0) == 90.0
    assert scalp.extension_pts(None, 30800.0) is None


def _a(move, net, gross, neg=False, flip=None, sym="NQ"):
    return scalp.assess(sym, move, net, gross, neg, flip)


def test_amplification_flux_dans_le_sens_du_mouvement_meme_en_gamma_positif():
    a = _a(+40.0, +300.0, 400.0)                      # 75 % à sens unique, > 100 M$
    assert a["state"] == "amplification" and a["tone"] == "alert" and a["direction"] == 1
    assert "haussière" in a["title"] and "malgré un gamma positif" in a["title"]
    assert a["lights"] == {"mouvement": True, "flux": True, "gamma": False}


def test_amplification_baissiere_en_gamma_negatif():
    a = _a(-50.0, -250.0, 300.0, neg=True)
    assert a["state"] == "amplification" and a["direction"] == -1
    assert "baissière" in a["title"] and "gamma défavorable" in a["title"]
    assert a["lights"]["gamma"] is True


def test_couverture_a_contre_courant_est_un_frein():
    a = _a(+40.0, -300.0, 400.0)
    assert a["state"] == "brake" and a["tone"] == "ok"


def test_mouvement_sans_soutien_des_dealers():
    a = _a(+40.0, +10.0, 400.0)                       # flux quasi équilibré
    assert a["state"] == "unsupported" and a["tone"] == "ok"
    assert scalp.assess("NQ", 40.0, 50.0, 60.0, False, None)["state"] == "unsupported"  # brut < 100


def test_pas_de_mouvement_directionnel_sous_le_seuil():
    a = _a(+10.0, +300.0, 400.0)
    assert a["state"] == "calm" and a["direction"] == 0
    assert scalp.assess("ES", +8.0, +300.0, 400.0, False, None)["state"] == "amplification"


def test_donnees_insuffisantes():
    a = scalp.assess("NQ", None, 0.0, 0.0, False, None)
    assert a["state"] == "insufficient" and a["tone"] == "neutral"


def test_prix_qui_fonce_vers_le_flip_allume_le_voyant_gamma():
    a = _a(-40.0, -300.0, 400.0, neg=False, flip=-30.0)     # Flip 30 pts SOUS le spot, on baisse
    assert a["lights"]["gamma"] is True and "prix vers le Flip" in a["detail"]
    b = _a(+40.0, +300.0, 400.0, neg=False, flip=-30.0)     # on monte, le Flip est derrière
    assert b["lights"]["gamma"] is False
