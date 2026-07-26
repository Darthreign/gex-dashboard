"""Séparation entre sous-jacents analysés et constituants.

Les constituants (NVDA, SMH…) sont collectés pour alimenter les niveaux de
confluence. Ils ne doivent apparaître ni dans le sélecteur de sous-jacent ni
dans celui des échelles : un utilisateur qui tomberait sur « NVDA » comme
échelle d'affichage n'y comprendrait rien.
"""
from __future__ import annotations

from gex import scales
from gex.config import SETTINGS, UNDERLYINGS, constituents, targets
from gex.scheduler import _Cadence


def test_cibles_et_constituants_disjoints():
    t = {u.key for u in targets()}
    c = {u.key for u in constituents()}
    assert t & c == set()
    assert t == {"SPX", "NDX", "SPY", "QQQ"}


def test_constituants_absents_des_echelles():
    """Une échelle d'affichage doit rester un indice ou son future."""
    keys = {s.key for s in scales.available_scales()}
    assert keys == {"SPX", "ES", "NDX", "NQ", "SPY", "QQQ"}
    for u in constituents():
        assert u.key not in keys


def test_liens_vers_les_cibles():
    """Un constituant n'informe que les indices qui le contiennent : pas de
    financières ni d'énergie dans le Nasdaq-100."""
    ndx = {u.key for u in constituents("NDX")}
    spx = {u.key for u in constituents("SPX")}
    assert "NVDA" in ndx and "NVDA" in spx
    assert "XLF" not in ndx and "XLF" in spx
    assert "XLE" not in ndx and "XLE" in spx


def test_tout_constituant_declare_une_cible():
    for u in constituents():
        assert u.links, f"{u.key} n'informe aucune cible"
        for target in u.links:
            assert target in UNDERLYINGS


def test_cadence_constituants_plus_lente():
    """Leurs murs reposent sur l'open interest, publié une fois par jour :
    les puller au rythme des cibles chargerait la source pour rien."""
    assert SETTINGS.constituent_interval_s > SETTINGS.flow_interval_s
    c = _Cadence(SETTINGS.constituent_interval_s)
    declenches = [i for i in range(30) if c.tick()]
    assert declenches == [0, 10, 20]


def test_snapshots_constituants_tres_espaces():
    """L'open interest ne bougeant qu'une fois par jour, en garder une photo
    toutes les 10 min ferait des centaines de Mo de doublons quotidiens."""
    assert (SETTINGS.constituent_snapshot_interval_s
            > SETTINGS.snapshot_interval_s * 5)
    c = _Cadence(SETTINGS.constituent_snapshot_interval_s)
    # ~6h30 de séance à 60 s : une poignée de snapshots, pas des dizaines
    assert sum(c.tick() for _ in range(390)) <= 6


def test_cadence_par_defaut_inchangee():
    """Sans argument, la cadence reste celle des snapshots complets."""
    a, b = _Cadence(), _Cadence(SETTINGS.snapshot_interval_s)
    assert a.every == b.every
