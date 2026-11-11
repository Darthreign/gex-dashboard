"""Black-Scholes-Merton vectorisé (numpy/scipy).

Conventions :
- t en années (365 jours), sigma en volatilité annualisée (ex: 0.20),
  r taux sans risque continu.
- q : taux de portage continu du sous-jacent (dividendes pour un indice ou un
  ETF, q = r pour une option sur future). Il n'est pas supposé : il se déduit
  du forward implicite de chaque échéance (cf. metrics.implied_forwards),
  F = S·e^((r−q)t). Avec q ainsi calibré, ce BSM est exactement du Black-76
  sur F, exprimé en sensibilités au spot. q=0 par défaut (ancien comportement).
- Les fonctions acceptent scalaires ou ndarrays (broadcasting numpy).
"""
from __future__ import annotations

import numpy as np
from scipy.stats import norm

_EPS = 1e-12
_INV_SQRT_2PI = 1.0 / np.sqrt(2.0 * np.pi)


def _pdf(x):
    """Densité de la loi normale centrée réduite. Même valeur que
    `scipy.stats.norm.pdf`, sans son coût fixe de validation : sur les
    profils de gamma (dizaines de milliers de points, ~40 appels par chaîne)
    ce coût dominait le calcul du Gamma Flip."""
    x = np.asarray(x, dtype=float)
    return _INV_SQRT_2PI * np.exp(-0.5 * x * x)


def gex_dollars(sign, gamma, weight, multiplier, spot):
    """GEX en $ par 1 % de move : sign × gamma × poids × multiplicateur × spot² × 0.01.

    Seul endroit où la formule est écrite. `weight` = open interest, volume ou
    taille d'un print selon l'appelant ; `sign` encode l'hypothèse de
    positionnement dealer (call +1, put −1), le gamma BS étant toujours positif.
    """
    return sign * gamma * weight * multiplier * spot ** 2 * 0.01


def _arr(x):
    return np.asarray(x, dtype=float)


def _t(t):
    return np.maximum(_arr(t), _EPS)


def _sig(sigma):
    return np.maximum(_arr(sigma), _EPS)


def _d1_d2(s, k, t, r, sigma, q=0.0):
    t, sigma = _t(t), _sig(sigma)
    d1 = (np.log(_arr(s) / _arr(k)) + (r - _arr(q) + 0.5 * sigma**2) * t) / (sigma * np.sqrt(t))
    d2 = d1 - sigma * np.sqrt(t)
    return d1, d2


def _dq(t, q):
    return np.exp(-_arr(q) * _t(t))


def call_price(s, k, t, r, sigma, q=0.0):
    d1, d2 = _d1_d2(s, k, t, r, sigma, q)
    return _arr(s) * _dq(t, q) * norm.cdf(d1) - _arr(k) * np.exp(-r * _arr(t)) * norm.cdf(d2)


def put_price(s, k, t, r, sigma, q=0.0):
    d1, d2 = _d1_d2(s, k, t, r, sigma, q)
    return _arr(k) * np.exp(-r * _arr(t)) * norm.cdf(-d2) - _arr(s) * _dq(t, q) * norm.cdf(-d1)


def call_delta(s, k, t, r, sigma, q=0.0):
    d1, _ = _d1_d2(s, k, t, r, sigma, q)
    return _dq(t, q) * norm.cdf(d1)


def put_delta(s, k, t, r, sigma, q=0.0):
    return call_delta(s, k, t, r, sigma, q) - _dq(t, q)


def gamma(s, k, t, r, sigma, q=0.0):
    """Gamma, identique calls et puts."""
    d1, _ = _d1_d2(s, k, t, r, sigma, q)
    return _dq(t, q) * _pdf(d1) / (_arr(s) * _sig(sigma) * np.sqrt(_t(t)))


def vega(s, k, t, r, sigma, q=0.0):
    """Vega pour 1 point de vol (non divisé par 100)."""
    d1, _ = _d1_d2(s, k, t, r, sigma, q)
    return _arr(s) * _dq(t, q) * _pdf(d1) * np.sqrt(_t(t))


def call_theta(s, k, t, r, sigma):
    """Theta annualisé (diviser par 365 pour le theta/jour)."""
    d1, d2 = _d1_d2(s, k, t, r, sigma)
    t = _t(t)
    return (
        -_arr(s) * _pdf(d1) * sigma / (2 * np.sqrt(t))
        - r * _arr(k) * np.exp(-r * t) * norm.cdf(d2)
    )


def put_theta(s, k, t, r, sigma):
    d1, d2 = _d1_d2(s, k, t, r, sigma)
    t = _t(t)
    return (
        -_arr(s) * _pdf(d1) * sigma / (2 * np.sqrt(t))
        + r * _arr(k) * np.exp(-r * t) * norm.cdf(-d2)
    )


def vanna(s, k, t, r, sigma, q=0.0):
    """∂delta/∂sigma = ∂vega/∂S — identique calls et puts.

    Positif sous le strike ATM-forward, négatif au-dessus (d2 change de signe).
    Multiplier par 0.01 pour l'effet d'un point de volatilité.
    """
    d1, d2 = _d1_d2(s, k, t, r, sigma, q)
    return -_dq(t, q) * _pdf(d1) * d2 / _sig(sigma)


def charm(s, k, t, r, sigma, q=0.0, is_call=True):
    """Décroissance du delta avec le TEMPS QUI PASSE : ∂delta/∂t = -∂delta/∂T
    (convention trader). Identique calls et puts quand q=0 ; avec un portage,
    ils diffèrent du terme q·e^(−qt)·N(±d1).

    Signe intuitif : un call ITM gagne du delta en approchant de l'expiration
    (charm > 0), un call OTM en perd (charm < 0). C'est ce flux mécanique que
    les dealers doivent hedger, d'où les dérives de fin de séance.
    """
    d1, d2 = _d1_d2(s, k, t, r, sigma, q)
    t, sigma, q = _t(t), _sig(sigma), _arr(q)
    dq = np.exp(-q * t)
    common = -dq * _pdf(d1) * (2 * (r - q) * t - d2 * sigma * np.sqrt(t)) \
        / (2 * t * sigma * np.sqrt(t))
    carry = np.where(is_call, q * dq * norm.cdf(d1), -q * dq * norm.cdf(-d1))
    return common + carry


def charm_per_day(s, k, t, r, sigma, q=0.0, is_call=True):
    """Variation de delta pour une journée écoulée."""
    return charm(s, k, t, r, sigma, q, is_call) / 365.0


def implied_vol(price, s, k, t, r, is_call, tol=1e-6, max_iter=60, q=0.0):
    """IV par Newton-Raphson vectorisé (fallback bisection implicite via clip).

    Retourne NaN quand le prix est sous la valeur intrinsèque actualisée ou
    quand la convergence échoue — l'appelant exclut ces contrats comme les
    iv<=0 du feed live.
    """
    price = np.asarray(price, dtype=float)
    shape = price.shape
    s = np.broadcast_to(_arr(s), shape).copy()
    k = np.broadcast_to(_arr(k), shape).copy()
    t = np.broadcast_to(_arr(t), shape).copy()
    q = np.broadcast_to(_arr(q), shape).copy()
    is_call = np.broadcast_to(np.asarray(is_call, dtype=bool), shape)

    fwd_s = s * np.exp(-q * t)
    disc_k = k * np.exp(-r * t)
    intrinsic = np.where(is_call, np.maximum(fwd_s - disc_k, 0.0),
                         np.maximum(disc_k - fwd_s, 0.0))
    valid = (price > intrinsic + 1e-10) & (t > 0)

    def _model(sig):
        return np.where(is_call, call_price(s, k, t, r, sig, q), put_price(s, k, t, r, sig, q))

    sigma = np.full(shape, 0.5)
    for _ in range(max_iter):
        v = vega(s, k, t, r, sigma, q)
        step = np.where(v > 1e-12, (_model(sigma) - price) / np.maximum(v, 1e-12), 0.0)
        sigma = np.clip(sigma - np.clip(step, -0.5, 0.5), 1e-4, 10.0)
        if np.all(np.abs(step) < 1e-8):
            break
    converged = np.abs(_model(sigma) - price) < np.maximum(tol, 1e-4 * price)
    return np.where(valid & converged, sigma, np.nan)
