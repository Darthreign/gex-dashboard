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
