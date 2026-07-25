"""Valeurs de référence Black-Scholes (Hull, ch. options européennes) :
S=100, K=100, T=1 an, r=5%, sigma=20%.
"""
import numpy as np
import pytest

from gex import greeks

S, K, T, R, SIG = 100.0, 100.0, 1.0, 0.05, 0.20


def test_call_price():
    assert greeks.call_price(S, K, T, R, SIG) == pytest.approx(10.450584, abs=1e-5)


def test_put_price():
    assert greeks.put_price(S, K, T, R, SIG) == pytest.approx(5.573526, abs=1e-5)


def test_put_call_parity():
    c = greeks.call_price(S, K, T, R, SIG)
    p = greeks.put_price(S, K, T, R, SIG)
    assert c - p == pytest.approx(S - K * np.exp(-R * T), abs=1e-10)


def test_call_delta():
    assert greeks.call_delta(S, K, T, R, SIG) == pytest.approx(0.636831, abs=1e-5)


def test_put_delta():
    assert greeks.put_delta(S, K, T, R, SIG) == pytest.approx(-0.363169, abs=1e-5)


def test_gamma():
    assert greeks.gamma(S, K, T, R, SIG) == pytest.approx(0.018762, abs=1e-5)


def test_vega():
    assert greeks.vega(S, K, T, R, SIG) == pytest.approx(37.52403, abs=1e-4)


def test_call_theta():
    assert greeks.call_theta(S, K, T, R, SIG) == pytest.approx(-6.41403, abs=1e-3)


def test_put_theta():
    assert greeks.put_theta(S, K, T, R, SIG) == pytest.approx(-1.65788, abs=1e-3)


def test_vectorized_broadcasting():
    strikes = np.array([90.0, 100.0, 110.0])
    # maturité courte : le drift est négligeable et le pic de gamma est ATM
    # (à maturité longue le pic se décale vers K ≈ S·e^{(r+σ²/2)T})
    g = greeks.gamma(S, strikes, 0.02, R, SIG)
    assert g.shape == (3,)
    assert g[1] > g[0] and g[1] > g[2]


def test_deep_itm_call_delta_near_one():
    assert greeks.call_delta(100.0, 10.0, 0.1, R, SIG) == pytest.approx(1.0, abs=1e-6)


def _num_deriv(f, x, h):
    """Dérivée centrée — référence indépendante des formules analytiques."""
    return (f(x + h) - f(x - h)) / (2 * h)


@pytest.mark.parametrize("k", [85.0, 100.0, 115.0])
@pytest.mark.parametrize("tt", [0.05, 0.5, 2.0])
def test_vanna_matches_numerical_dvega_dsigma(k, tt):
    """vanna == d(delta)/d(sigma), vérifié par dérivation numérique."""
    num = _num_deriv(lambda sig: greeks.call_delta(S, k, tt, R, sig), SIG, 1e-5)
    assert greeks.vanna(S, k, tt, R, SIG) == pytest.approx(num, rel=1e-4)


@pytest.mark.parametrize("k", [85.0, 100.0, 115.0])
@pytest.mark.parametrize("tt", [0.05, 0.5, 2.0])
def test_charm_matches_numerical(k, tt):
    """charm == d(delta)/dt (temps qui passe) == -d(delta)/dT."""
    num_dT = _num_deriv(lambda t_: greeks.call_delta(S, k, t_, R, SIG), tt, 1e-6)
    assert greeks.charm(S, k, tt, R, SIG) == pytest.approx(-num_dT, rel=1e-3)


def test_vanna_charm_identical_call_put():
    """Sans dividende, vanna et charm sont identiques call/put : delta_put =
    delta_call - 1, et la constante disparaît en dérivant."""
    for k in (90.0, 100.0, 110.0):
        num_v = _num_deriv(lambda sig: greeks.put_delta(S, k, 0.5, R, sig), SIG, 1e-5)
        assert greeks.vanna(S, k, 0.5, R, SIG) == pytest.approx(num_v, rel=1e-4)
        num_c = _num_deriv(lambda t_: greeks.put_delta(S, k, t_, R, SIG), 0.5, 1e-6)
        assert greeks.charm(S, k, 0.5, R, SIG) == pytest.approx(-num_c, rel=1e-3)


def test_vanna_sign_flips_around_forward():
    """vanna < 0 côté strikes bas, > 0 côté strikes hauts (d2 change de signe
    au forward) : une détente d'IV ne pousse pas le delta du même côté partout."""
    assert greeks.vanna(S, 80.0, 0.5, R, SIG) < 0
    assert greeks.vanna(S, 130.0, 0.5, R, SIG) > 0


def test_charm_signs_itm_vs_otm():
    """Un call ITM gagne du delta en approchant l'expiration, un OTM en perd."""
    assert greeks.charm_per_day(S, 85.0, 0.25, R, SIG) > 0
    assert greeks.charm_per_day(S, 115.0, 0.25, R, SIG) < 0


def test_implied_vol_round_trip():
    """prix BS -> inversion -> doit retrouver la vol d'origine."""
    # strikes avec valeur temps significative (un deep ITM à vol faible a une
    # valeur temps ~0 : l'IV y est irrécupérable et implied_vol rend NaN, voulu)
    sigmas = np.array([0.10, 0.20, 0.45, 0.90])
    strikes = np.array([95.0, 100.0, 120.0, 150.0])
    prices = greeks.call_price(S, strikes, 0.08, R, sigmas)
    iv = greeks.implied_vol(prices, S, strikes, 0.08, R, np.full(4, True))
    np.testing.assert_allclose(iv, sigmas, atol=1e-4)
    p_prices = greeks.put_price(S, strikes, 0.08, R, sigmas)
    p_iv = greeks.implied_vol(p_prices, S, strikes, 0.08, R, np.full(4, False))
    np.testing.assert_allclose(p_iv, sigmas, atol=1e-4)


def test_implied_vol_below_intrinsic_is_nan():
    iv = greeks.implied_vol(np.array([1.0]), 100.0, 80.0, 0.1, R, np.array([True]))
    assert np.isnan(iv[0])
