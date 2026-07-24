"""Black-Scholes vectorisé (numpy/scipy).

Conventions :
- t en années (365 jours), sigma en volatilité annualisée (ex: 0.20),
  r taux sans risque continu.
- Les fonctions acceptent scalaires ou ndarrays (broadcasting numpy).
- Pas de dividende (q=0) : acceptable pour l'agrégation GEX où le gamma
  est dominé par les échéances courtes.
"""
from __future__ import annotations

import numpy as np
from scipy.stats import norm

_EPS = 1e-12


def _d1_d2(s, k, t, r, sigma):
    s = np.asarray(s, dtype=float)
    k = np.asarray(k, dtype=float)
    t = np.maximum(np.asarray(t, dtype=float), _EPS)
    sigma = np.maximum(np.asarray(sigma, dtype=float), _EPS)
    d1 = (np.log(s / k) + (r + 0.5 * sigma**2) * t) / (sigma * np.sqrt(t))
    d2 = d1 - sigma * np.sqrt(t)
    return d1, d2


def call_price(s, k, t, r, sigma):
    d1, d2 = _d1_d2(s, k, t, r, sigma)
    return s * norm.cdf(d1) - k * np.exp(-r * t) * norm.cdf(d2)


def put_price(s, k, t, r, sigma):
    d1, d2 = _d1_d2(s, k, t, r, sigma)
    return k * np.exp(-r * t) * norm.cdf(-d2) - s * norm.cdf(-d1)


def call_delta(s, k, t, r, sigma):
    d1, _ = _d1_d2(s, k, t, r, sigma)
    return norm.cdf(d1)


def put_delta(s, k, t, r, sigma):
    return call_delta(s, k, t, r, sigma) - 1.0


def gamma(s, k, t, r, sigma):
    """Gamma, identique calls et puts."""
    d1, _ = _d1_d2(s, k, t, r, sigma)
    t = np.maximum(np.asarray(t, dtype=float), _EPS)
    sigma = np.maximum(np.asarray(sigma, dtype=float), _EPS)
    return norm.pdf(d1) / (np.asarray(s, dtype=float) * sigma * np.sqrt(t))


def vega(s, k, t, r, sigma):
    """Vega pour 1 point de vol (non divisé par 100)."""
    d1, _ = _d1_d2(s, k, t, r, sigma)
    t = np.maximum(np.asarray(t, dtype=float), _EPS)
    return np.asarray(s, dtype=float) * norm.pdf(d1) * np.sqrt(t)


def call_theta(s, k, t, r, sigma):
    """Theta annualisé (diviser par 365 pour le theta/jour)."""
    d1, d2 = _d1_d2(s, k, t, r, sigma)
    t = np.maximum(np.asarray(t, dtype=float), _EPS)
    return (
        -np.asarray(s, dtype=float) * norm.pdf(d1) * sigma / (2 * np.sqrt(t))
        - r * np.asarray(k, dtype=float) * np.exp(-r * t) * norm.cdf(d2)
    )


def put_theta(s, k, t, r, sigma):
    d1, d2 = _d1_d2(s, k, t, r, sigma)
    t = np.maximum(np.asarray(t, dtype=float), _EPS)
    return (
        -np.asarray(s, dtype=float) * norm.pdf(d1) * sigma / (2 * np.sqrt(t))
        + r * np.asarray(k, dtype=float) * np.exp(-r * t) * norm.cdf(-d2)
    )
