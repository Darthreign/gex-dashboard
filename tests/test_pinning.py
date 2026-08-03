"""Pinning de clôture (gex/pinning.py + endpoint /close_context).

Vérifie la mesure directe du pinning (`pin_ratio` : 0 = collé sur un strike,
1 = à mi-chemin), le choix des murs, le comptage des franchissements, et le
câblage de l'endpoint (chaîne ~16h + bougies -> métriques)."""
from __future__ import annotations

import pandas as pd

from gex import pinning


def _chain():
    """Strikes 19900..20100 par 50, gamma max sur 20000 puis 19950."""
    rows = []
    for k in range(19900, 20101, 50):
        g = 5e9 if k == 20000 else (3e9 if k == 19950 else 1e9)
        rows.append({"strike": float(k), "gex": g})
    return pd.DataFrame(rows)


def test_strike_spacing():
    assert pinning.strike_spacing([100, 150, 200, 250]) == 50
    assert pinning.strike_spacing([100]) is None


def test_pin_ratio_colle_sur_strike():
    m = pinning.pin_metrics(_chain(), close_price=20000)
    assert m["nearest_strike"] == 20000
    assert m["dist_nearest_strike"] == 0
    assert m["pin_ratio"] == 0.0            # collé pile
    assert m["strike_spacing"] == 50


def test_pin_ratio_a_mi_chemin():
    m = pinning.pin_metrics(_chain(), close_price=20025)   # entre 20000 et 20050
    assert m["nearest_strike"] == 20000
    assert m["dist_nearest_strike"] == 25
    assert m["pin_ratio"] == 1.0            # à mi-chemin (25 / (50/2))


def test_murs_gex_par_gamma_absolu():
    m = pinning.pin_metrics(_chain(), close_price=20000)
    assert m["gex1_strike"] == 20000 and m["dist_gex1"] == 0
    assert m["gex2_strike"] == 19950 and m["dist_gex2"] == 50


def test_franchissements_preclose():
    m = pinning.pin_metrics(_chain(), close_price=20000,
                            window_closes=[20000, 20030, 20060, 19990])
    # index de strike (spacing 50) : 400, 401, 401, 400 -> 1 + 0 + 1 = 2
    assert m["strike_crossings_preclose"] == 2


def test_close_context_endpoint(monkeypatch):
    from gex import store
    from gex.api import _close_context

    chain = _chain()
    bars = pd.DataFrame({
        "timestamp": pd.to_datetime(
            [f"2026-08-03 15:5{m}:00" for m in range(10)] + ["2026-08-03 16:00:00"]),
        "close": [20010.0] * 10 + [20000.0],
    })
    # snapshot natif (_RT) prioritaire ; bougies présentes
    monkeypatch.setattr(store, "load_snapshot_near",
                        lambda sym, day, **k: chain if sym == "NQ_RT" else None)
    monkeypatch.setattr(store, "load_prices", lambda sym, day: bars)

    ctx = _close_context("NQ", "2026-08-03")
    assert ctx["available"] is True
    assert ctx["close"] == 20000            # bougie la plus proche de 16h00
    assert ctx["nearest_strike"] == 20000 and ctx["pin_ratio"] == 0.0
    assert ctx["gex1_strike"] == 20000
    assert ctx["strike_crossings_preclose"] is not None   # fenêtre pré-clôture vue


def test_close_context_indisponible_sans_chaine(monkeypatch):
    from gex import store
    from gex.api import _close_context
    monkeypatch.setattr(store, "load_snapshot_near", lambda sym, day, **k: None)
    ctx = _close_context("NQ", "2026-08-03")
    assert ctx["available"] is False and "reason" in ctx
