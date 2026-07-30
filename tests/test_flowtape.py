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


def test_delta_pondere_limpact_pas_le_nombre_de_contrats():
    """La pondération par le delta reste : 100 calls très hors-monnaie
    (delta 0,05) pèsent dix fois moins que 100 à la monnaie (delta 0,50). Le
    signe est en convention DEALER — un achat de calls par le preneur laisse
    le dealer COURT delta, donc net_delta négatif (comme la contribution −δ
    d'un call dans la tuile DEX)."""
    t = _tape()
    t._spot["SPX"] = 7400.0
    t._delta[".SPXW260729C7400"] = 0.50
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 100), now=60.0)
    proche = t.bars["SPX"].net_delta

    t2 = _tape()
    t2._spot["SPX"] = 7400.0
    t2._delta[".SPXW260729C7400"] = 0.05
    t2.ingest_print(_print(".SPXW260729C7400", "BUY", 100), now=60.0)
    loin = t2.bars["SPX"].net_delta

    assert proche == pytest.approx(-100 * 0.50 * 100 * 7400.0)  # dealer court
    assert proche == pytest.approx(loin * 10)


def test_achat_de_puts_rend_les_dealers_longs_delta():
    """Convention dealer, cohérente avec la tuile DEX (dex = −δ·oi) : un preneur
    qui achète des puts laisse le dealer LONG delta (net_delta positif), et le
    put acheté descend sur la ligne des contrats — comme un put sur le graphe
    GEX par strike."""
    t = _tape()
    t._spot["SPX"] = 7400.0
    t._delta[".SPXW260729P7400"] = -0.45
    t.ingest_print(_print(".SPXW260729P7400", "BUY", 10), now=60.0)
    bar = t.bars["SPX"]
    assert bar.net_puts < 0             # put acheté -> négatif (convention GEX)
    assert bar.net_delta > 0            # dealers LONGS delta


def test_print_sans_delta_connu_exclu_pas_estime():
    t = _tape()
    t._spot["SPX"] = 7400.0            # delta jamais reçu pour ce contrat
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 10), now=60.0)
    bar = t.bars["SPX"]
    assert bar.net_delta == 0.0
    assert bar.no_delta_prints == 1
    assert bar.net_contracts == pytest.approx(10.0)   # le reste compte quand même


def test_ingest_greeks_ignore_les_symboles_non_suivis():
    t = _tape()
    t.ingest_greeks({"eventSymbol": ".AAPL260729C200", "delta": 0.5})
    t.ingest_greeks({"eventSymbol": ".SPXW260729C7400", "delta": 0.42})
    assert t._delta == {".SPXW260729C7400": 0.42}


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
    """Convention GEX (call +, put −) appliquée à la direction du preneur :
    un call ACHETÉ monte (+8), un put VENDU monte aussi (+3, car put −1 fois
    vente −1). Le net est leur somme."""
    t = _tape()
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 8), now=60.0)
    t.ingest_print(_print(".SPXW260729P7400", "SELL", 3), now=60.0)
    bar = t.bars["SPX"]
    assert bar.net_calls == pytest.approx(8.0)     # call acheté -> +
    assert bar.net_puts == pytest.approx(3.0)      # put vendu -> + (−1×−1)
    assert bar.net_contracts == pytest.approx(11.0)


def test_prime_en_dollars_avec_le_multiplicateur():
    """+ quand le preneur achète (le dealer encaisse la prime)."""
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

    # jour lu en ET comme flush_tape, qui convertit avant d'écrire : entre
    # minuit local et minuit ET, la date locale n'est pas celle du marché
    from datetime import datetime

    from gex.metrics import ET
    day = datetime.now(ET).strftime("%Y-%m-%d")
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


def test_repli_cboe_si_la_colonne_dxfeed_manque(tmp_path, monkeypatch):
    """Le choix de source se fait colonne par colonne, pas sur l'existence du
    fichier. Une journée collectée avant l'ajout d'une mesure a bien un
    fichier tape/, mais sans la colonne : sans ce test, le graphique recevait
    le tableau dxFeed puis y cherchait des colonnes CBOE et s'affichait VIDE
    (constaté le 2026-07-30 sur le gamma échangé)."""
    import pandas as pd

    from gex import store
    from gex.app import flow_source
    from gex.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)
    ts = pd.Timestamp("2026-07-29 10:00")
    # tape SANS les colonnes de gamma (ancien schéma)
    store.append_tape("SPX", [{"timestamp": ts, "net_delta": 1.0,
                               "source": "dxfeed"}], ts)
    store.append_daily("flows", "SPX", {"timestamp": ts, "gflow_calls": 2.0,
                                        "gflow_puts": -1.0, "source": "cboe"}, ts)

    d, src = flow_source("SPX", "2026-07-29", ("net_gamma_calls", "net_gamma_puts"))
    assert src == "cboe", "colonne absente -> repli, pas un tableau inexploitable"
    assert "gflow_calls" in d.columns

    d2, src2 = flow_source("SPX", "2026-07-29", ("net_delta",))
    assert src2 == "dxfeed"      # celle-là est bien présente
    assert "net_delta" in d2.columns


def test_derive_detectee_au_dela_du_seuil(monkeypatch):
    """Recentrage anticipé : dès que le spot live s'éloigne du centre de plus
    de la moitié de la demi-fenêtre, l'univers doit être reconstruit. Sinon un
    mouvement rapide fait sortir le prix de la bande souscrite (NQ, 3 min sur
    550 le 2026-07-29)."""
    import gex.flowtape as ft

    t = ft.FlowTape()
    t._center = {"SPX": 7400.0}
    seuil = ft.STRIKE_WINDOW * ft.RECENTER_FRACTION      # 0.0075 -> 55,5 pts

    # juste en deçà du seuil : pas de recentrage
    monkeypatch.setattr(ft.QUOTES, "price", lambda k: 7400.0 + 50)
    assert t._drifted() is None

    # au-delà : recentrage
    monkeypatch.setattr(ft.QUOTES, "price", lambda k: 7400.0 + 70)
    assert t._drifted() == "SPX"

    # symétrique vers le bas
    monkeypatch.setattr(ft.QUOTES, "price", lambda k: 7400.0 - 70)
    assert t._drifted() == "SPX"


def test_derive_ignore_un_spot_live_absent(monkeypatch):
    """Un sous-jacent sans cotation live (price() = None) ne doit jamais
    déclencher de recentrage — sinon un flux muet reconstruirait en boucle."""
    import gex.flowtape as ft

    t = ft.FlowTape()
    t._center = {"SPX": 7400.0, "NQ": 27000.0}
    monkeypatch.setattr(ft.QUOTES, "price",
                        lambda k: None if k == "NQ" else 7400.0)
    assert t._drifted() is None


def test_build_universe_fixe_le_centre(monkeypatch):
    """Le centre est REMPLACÉ à chaque construction, pas cumulé : c'est le
    référentiel de la fenêtre courante."""
    import gex.flowtape as ft
    from gex import futopt, idxopt

    t = ft.FlowTape()
    t._center = {"SPX": 1.0, "OBSOLETE": 2.0}      # ancien état à écraser
    monkeypatch.setattr(ft, "quote_token", lambda: ("tok", "wss://x", "acc"))
    monkeypatch.setattr(idxopt, "reference_spot",
                        lambda s: 7400.0 if s == "SPX" else None)
    monkeypatch.setattr(futopt, "_reference_spot", lambda s, a: None)
    monkeypatch.setattr(ft, "build_index_universe",
                        lambda s, spot, acc: [".SPXW260729C7400"])

    t._build_universe()
    assert t._center.get("SPX") == 7400.0
    assert "OBSOLETE" not in t._center      # remplacé, pas fusionné


# --- Tape : transactions individuelles ---

@pytest.mark.parametrize("sym,strike,typ", [
    (".SPXW260729C7400", 7400.0, "C"),
    (".SPXW260729P7350", 7350.0, "P"),
    ("./EWN26C7500:XCME", 7500.0, "C"),
    ("./Q5CN26P27960:XCME", 27960.0, "P"),
])
def test_strike_lu_dans_le_symbole(sym, strike, typ):
    from gex.flowtape import strike_of
    assert strike_of(sym) == strike


def _tape_prints() -> FlowTape:
    t = FlowTape()
    t._by_stream = {".SPXW260729C7400": "SPX", ".SPXW260729P7350": "SPX"}
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 5, price=20.0), now=1000.0)
    t.ingest_print(_print(".SPXW260729P7350", "SELL", 50, price=3.0), now=1001.0)
    t.ingest_print(_print(".SPXW260729C7400", "BUY", 1, price=21.0, spread=True), now=1002.0)
    return t


def test_recent_prints_plus_recent_dabord():
    t = _tape_prints()
    rows = t.recent_prints("SPX")
    assert [r["t"] for r in rows] == [1002.0, 1001.0, 1000.0]
    assert rows[0]["strike"] == 7400.0 and rows[0]["type"] == "C"


def test_recent_prints_filtre_par_taille():
    """Le firehose est illisible sans filtre : min_size isole les blocs."""
    t = _tape_prints()
    gros = t.recent_prints("SPX", min_size=10)
    assert [r["size"] for r in gros] == [50.0]


def test_recent_prints_peut_masquer_les_combos():
    t = _tape_prints()
    sans = t.recent_prints("SPX", include_combos=False)
    assert all(not r["combo"] for r in sans)
    assert len(sans) == 2
    # combo bien présent quand on ne filtre pas
    assert any(r["combo"] for r in t.recent_prints("SPX"))


def test_recent_prints_notional_en_dollars():
    """Le notionnel (prix×taille×mult) classe les blocs par poids réel, pas par
    seul nombre de contrats."""
    t = _tape_prints()
    vente = next(r for r in t.recent_prints("SPX") if r["side"] == "SELL")
    assert vente["notional"] == pytest.approx(3.0 * 50 * 100)   # indice mult 100


def test_recent_prints_cote_indetermine_marque():
    t = FlowTape()
    t._by_stream = {".SPXW260729C7400": "SPX"}
    t.ingest_print(_print(".SPXW260729C7400", "UNKNOWN", 3, price=5.0), now=1.0)
    assert t.recent_prints("SPX")[0]["side"] == "?"


def test_recent_prints_borne_au_tampon():
    from gex.flowtape import PRINT_BUFFER
    t = FlowTape()
    t._by_stream = {".SPXW260729C7400": "SPX"}
    for i in range(PRINT_BUFFER + 50):
        t.ingest_print(_print(".SPXW260729C7400", "BUY", 1, price=1.0), now=float(i))
    # jamais plus que le tampon, et ce sont les plus RÉCENTS qui restent
    rows = t.recent_prints("SPX", limit=PRINT_BUFFER + 100)
    assert len(rows) == PRINT_BUFFER
    assert rows[0]["t"] == float(PRINT_BUFFER + 49)


def test_recent_prints_symbole_vide():
    assert FlowTape().recent_prints("SPX") == []
