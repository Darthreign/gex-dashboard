"""Transposition d'échelle : la conversion doit être exacte vers le future
d'un indice, proportionnelle sinon, et neutre quand elle est impossible."""
import pytest

from gex import scales

SPOTS = {"SPX": 7412.0, "NDX": 28128.0, "SPY": 738.93, "QQQ": 683.90}
BASES = {"SPX": 33.0, "NDX": 157.0, "SPY": None, "QQQ": None}


def _xf(src, tgt_key, spots=None, bases=None):
    return scales.transform(src, scales.scale_by_key(tgt_key),
                            spots if spots is not None else SPOTS,
                            bases if bases is not None else BASES)


def test_native_scale_is_identity():
    xf, ratio, mode = _xf("SPX", "SPX")
    assert mode == "native"
    assert xf(7450.0) == 7450.0


def test_index_to_own_future_is_additive():
    """SPX -> ES : décalage exact du basis, pas un ratio."""
    xf, _, mode = _xf("SPX", "ES")
    assert mode == "basis"
    assert xf(7450.0) == pytest.approx(7483.0)
    assert xf(7400.0) == pytest.approx(7433.0)
    # l'écart entre deux niveaux est préservé
    assert xf(7450.0) - xf(7400.0) == pytest.approx(50.0)


def test_index_to_etf_is_proportional():
    """SPX -> SPY : ratio, qui capte le tracking réel (~1/10)."""
    xf, ratio, mode = _xf("SPX", "SPY")
    assert mode == "ratio"
    assert ratio == pytest.approx(738.93 / 7412.0)
    assert xf(7412.0) == pytest.approx(738.93)      # le spot se transpose sur le spot
    assert xf(7450.0) == pytest.approx(742.72, abs=0.01)


def test_cross_family_preserves_relative_distance():
    """SPX -> NQ : un niveau à +0,5 % du spot SPX ressort à +0,5 % du spot NQ."""
    xf, _, mode = _xf("SPX", "NQ")
    assert mode == "ratio"
    level = 7412.0 * 1.005
    nq_ref = 28128.0 + 157.0
    assert xf(level) == pytest.approx(nq_ref * 1.005)


def test_cross_family_is_flagged():
    assert scales.scale_by_key("NQ").cross_family("SPX") is True
    assert scales.scale_by_key("QQQ").cross_family("SPX") is True
    assert scales.scale_by_key("ES").cross_family("SPX") is False
    assert scales.scale_by_key("SPY").cross_family("SPX") is False
    assert scales.scale_by_key("NQ").cross_family("NDX") is False


def test_missing_target_spot_falls_back_to_identity():
    """Plutôt qu'afficher des niveaux faux, on n'applique aucune conversion."""
    xf, _, mode = _xf("SPX", "NQ", spots={"SPX": 7412.0}, bases={"SPX": 33.0})
    assert mode == "native"
    assert xf(7450.0) == 7450.0


def test_missing_basis_falls_back_to_identity():
    xf, _, mode = _xf("SPX", "ES", bases={"SPX": None})
    assert mode == "native"
    assert xf(7450.0) == 7450.0


def test_available_scales_include_futures():
    keys = {s.key for s in scales.available_scales()}
    assert {"SPX", "ES", "NDX", "NQ", "SPY", "QQQ"} <= keys
    es = scales.scale_by_key("ES")
    assert es.is_future and es.source == "SPX"


def test_basis_mesure_en_seance(monkeypatch):
    """En séance, indice et future cotent ensemble : leur écart EST le basis,
    et le spot transposé retombe exactement sur le prix du future."""
    from gex import app as A
    from gex.rtquote import QUOTES

    live = {"NDX": 28128.34, "NQ": 28306.25, "SPX": 7407.68, "ES": 7444.12}
    monkeypatch.setattr(QUOTES, "price", lambda k: live.get(k))
    monkeypatch.setattr(A, "market_is_open", lambda *a, **k: True)

    for index, future in (("NDX", "NQ"), ("SPX", "ES")):
        xf, _, mode = A._transform_for(index, future)
        assert mode == "basis"
        assert xf(live[index]) == pytest.approx(live[future])


def test_etf_au_prix_temps_reel_en_seance(monkeypatch):
    """QQQ n'a pas de future : en séance son spot doit être le prix temps réel,
    pas celui de sa chaîne différée, sinon le ratio marie un future actuel à un
    QQQ en retard (HVL 82 points trop bas sur NQ le 16/11/2026)."""
    from types import SimpleNamespace

    from gex import app as A
    from gex.rtquote import QUOTES

    chaine = {"QQQ": 666.75, "NDX": 27990.0}
    monkeypatch.setattr(A.STATE, "get", lambda k: SimpleNamespace(
        summary=SimpleNamespace(spot=chaine[k], basis=None) if k in chaine else None))
    live = {"QQQ": 664.655, "NDX": 27390.0, "NQ": 27405.25}
    monkeypatch.setattr(QUOTES, "price", lambda k: live.get(k))
    monkeypatch.setattr(A, "market_is_open", lambda *a, **k: True)
    xf, ratio, mode = A._transform_for("QQQ", "NQ")
    assert mode == "ratio" and ratio == pytest.approx(27405.25 / 664.655)
    assert xf(664.655) == pytest.approx(27405.25)

    del live["QQQ"]                                  # sans cotation : repli sur la chaîne
    assert A._transform_for("QQQ", "NQ")[1] == pytest.approx(27405.25 / 666.75)

    monkeypatch.setattr(A, "market_is_open", lambda *a, **k: False)
    live["QQQ"] = 664.655                            # hors séance : rien ne change
    monkeypatch.setattr(A.STATE, "get", lambda k: SimpleNamespace(
        summary=SimpleNamespace(spot=chaine[k], basis=180.0) if k in chaine else None))
    assert A._transform_for("QQQ", "NQ")[1] == pytest.approx((27990.0 + 180.0) / 666.75)


def test_basis_fige_hors_seance(monkeypatch):
    """Hors séance l'indice ne cote plus : son écart au future absorbe tout le
    mouvement overnight. L'utiliser ferait dériver TOUS les niveaux transposés
    avec le future — un gap de 330 pts sur NQ décalerait les murs d'autant,
    alors qu'ils décrivent des positions arrêtées la veille.
    """
    from gex import app as A
    from gex.rtquote import QUOTES

    monkeypatch.setattr(A, "market_is_open", lambda *a, **k: False)
    appels = []
    monkeypatch.setattr(QUOTES, "price", lambda k: appels.append(k))
    A._transform_for("NDX", "NQ")
    assert appels == [], "le flux temps réel ne doit pas servir de basis hors séance"


def test_repli_sur_le_basis_de_la_chaine_sans_flux(monkeypatch):
    """Sans flux temps réel, la transposition doit continuer de fonctionner."""
    from gex import app as A
    from gex.rtquote import QUOTES

    monkeypatch.setattr(QUOTES, "price", lambda k: None)
    xf, _, mode = A._transform_for("NDX", "NQ")
    # pas de flux et pas d'état chargé : identité plutôt que niveaux faux
    assert mode in {"native", "basis"}
