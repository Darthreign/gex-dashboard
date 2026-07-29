"""Order flow signé (gex/flowtape.py).

Le réseau n'est pas testé ici — ce qui casse silencieusement et fausserait
une lecture de marché, c'est la logique de signe, l'exclusion des jambes de
combos et la pondération par la taille. Tout cela est purement calculatoire
et vit dans `ingest_print`.
"""
from __future__ import annotations

import pytest

from gex.flowtape import FlowTape, option_type_of


def _tape() -> FlowTape:
    t = FlowTape()
    t._by_stream = {
        ".SPXW260729C7400": "SPX",
        ".SPXW260729P7400": "SPX",
        "./EWN26C7500:XCME": "ES",
    }
    return t


def _print(sym, side, size, price=10.0, spread=False):
    return {"eventSymbol": sym, "aggressorSide": side, "size": size,
            "price": price, "spreadLeg": spread}


def test_signe_du_point_de_vue_de_lagresseur():
    t = _tape()
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 10), now=60.0)
    t.ingest_print(_print(".SPXW260729C7400", "SELL", 4), now=60.0)
    bar = t.bars["SPX"]
    assert bar.net_contracts == pytest.approx(6.0)      # 10 achetés - 4 vendus
    assert bar.buy_contracts == pytest.approx(10.0)
    assert bar.sell_contracts == pytest.approx(4.0)
    assert bar.prints == 2


def test_jambes_de_spread_isolees_du_flux_net():
    """23 % des prints SPX sont des jambes de combos : les compter comme
    directionnels fausserait le signal d'un quart (cf. docstring du module)."""
    t = _tape()
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 5), now=60.0)
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 100, spread=True), now=60.0)
    bar = t.bars["SPX"]
    assert bar.net_contracts == pytest.approx(5.0)       # le combo n'entre pas
    assert bar.spread_contracts == pytest.approx(100.0)  # mais il est conservé
    assert bar.spread_prints == 1
    assert bar.prints == 2                                # compté dans le total


def test_agresseur_inconnu_ni_compte_ni_cache():
    t = _tape()
    t.ingest_print(_print(".SPXW260729C7400", "UNDEFINED", 7), now=60.0)
    bar = t.bars["SPX"]
    assert bar.net_contracts == 0.0
    assert bar.undefined_prints == 1


def test_ponderation_par_la_taille_pas_par_le_nombre_de_prints():
    """2,1 contrats de taille moyenne sur SPX contre 10,9 sur ES : compter
    les prints donnerait le même poids à un lot de 1 et à un bloc de 500."""
    t = _tape()
    for _ in range(10):
        t.ingest_print(_print(".SPXW260729C7400", "BUY", 1), now=60.0)
    t.ingest_print(_print(".SPXW260729C7400", "SELL", 500), now=60.0)
    assert t.bars["SPX"].net_contracts == pytest.approx(-490.0)


def test_separation_calls_puts():
    t = _tape()
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 8), now=60.0)
    t.ingest_print(_print(".SPXW260729P7400", "SELL", 3), now=60.0)
    bar = t.bars["SPX"]
    assert bar.net_calls == pytest.approx(8.0)
    assert bar.net_puts == pytest.approx(-3.0)
    assert bar.net_contracts == pytest.approx(5.0)


def test_prime_en_dollars_avec_le_multiplicateur():
    t = _tape()
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 2, price=18.5), now=60.0)
    # indice : multiplicateur 100
    assert t.bars["SPX"].net_premium == pytest.approx(2 * 18.5 * 100)


def test_changement_de_minute_cloture_la_barre():
    t = _tape()
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 5), now=60.0)
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 3), now=125.0)
    done = t.drain_bars()
    assert len(done) == 1 and done[0][0] == "SPX"
    assert done[0][1].net_contracts == pytest.approx(5.0)
    assert t.bars["SPX"].net_contracts == pytest.approx(3.0)   # barre en cours


def test_symbole_non_souscrit_ignore():
    t = _tape()
    t.ingest_print(_print(".AAPL260729C200", "BUY", 5), now=60.0)
    assert t.bars == {}


def test_taille_absente_ou_nulle_ignoree():
    t = _tape()
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 0), now=60.0)
    t.ingest_print({"eventSymbol": ".SPXW260729C7400", "aggressorSide": "BUY"}, now=60.0)
    assert t.bars == {}


@pytest.mark.parametrize("sym,attendu", [
    (".SPXW260729C7400", "C"),
    (".SPXW260729P7400", "P"),
    ("./EWN26C7500:XCME", "C"),
    ("./Q5CN26P27960:XCME", "P"),
    (".SPX260821C200", "C"),
])
def test_type_lu_dans_le_symbole(sym, attendu):
    assert option_type_of(sym) == attendu


def test_drain_flush_sort_la_barre_en_cours():
    t = _tape()
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 5), now=60.0)
    assert t.drain_bars() == []              # rien d'achevé
    out = t.drain_bars(flush=True)
    assert len(out) == 1 and out[0][1].net_contracts == pytest.approx(5.0)


def test_flush_ecrit_dans_tape_pas_dans_flows(tmp_path, monkeypatch):
    """`flows/` porte le proxy NON signé calculé sur CBOE (redistribuable),
    `tape/` le flux réellement signé du courtier. Les confondre rendrait
    impossible de savoir, en relisant un fichier, si le signe est observé ou
    déduit."""
    import time as _time

    from gex import scheduler, store
    from gex.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    t = _tape()
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 12), now=_time.time())
    monkeypatch.setattr(scheduler.flowtape, "TAPE", t)

    scheduler.flush_tape()

    day = __import__("pandas").Timestamp.now().strftime("%Y-%m-%d")
    # la barre en cours n'est pas encore achevée : rien ne doit être écrit
    assert store.load_tape("SPX", day).empty

    t.ingest_print(_print(".SPXW260729C7400", "BUY", 1), now=_time.time() + 120)
    scheduler.flush_tape()
    out = store.load_tape("SPX", day)
    assert not out.empty
    assert out["net_contracts"].iloc[0] == pytest.approx(12.0)
    assert out["source"].iloc[0] == "dxfeed"
    assert not (tmp_path / "flows").exists()


def test_row_porte_la_provenance():
    """Garde-fou de licence : ces barres viennent du courtier et ne doivent
    jamais devenir exportables par oubli d'étiquette."""
    t = _tape()
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 5), now=60.0)
    row = t.bars["SPX"].as_row("SPX", "2026-07-29 10:00")
    assert row["source"] == "dxfeed"
