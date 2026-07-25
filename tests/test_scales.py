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
