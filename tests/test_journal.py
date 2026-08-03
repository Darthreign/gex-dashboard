"""Journal de recherche SQLite (discord_bot/journal.py).

Vérifie que les faits bruts (régimes, votes, contexte) font des allers-retours
fidèles, que les gardes d'idempotence tiennent, et que les agrégats (minutes
par couleur, raison d'un changement) se calculent juste.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# discord_bot n'est pas un package installé : on l'ajoute au path pour importer
# le module tel que le bot le fait (stdlib pure, aucune dépendance).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "discord_bot"))
import journal  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = journal.connect(tmp_path / "j.sqlite")
    yield c
    c.close()


def test_connect_cree_les_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"regime_events", "polls", "heatmaps",
            "market_context", "daily_metrics"} <= tables


def test_regime_roundtrip_et_open_idempotent(conn):
    assert journal.regime_open_done(conn, "2026-08-03") is False
    journal.record_regime(conn, date="2026-08-03",
                          ts="2026-08-03T15:30:00+02:00", kind="open",
                          color="green", confidence="forte", verdict="RAS",
                          families={"S&P": {"statut": "pos"}},
                          market={"nq": {"price": 20000, "dist_open": 0,
                                         "dist_high": -5, "dist_low": 10}})
    assert journal.regime_open_done(conn, "2026-08-03") is True
    tl = journal.regime_timeline(conn, "2026-08-03")
    assert len(tl) == 1
    assert tl[0]["color"] == "green"
    assert tl[0]["nq_price"] == 20000
    assert json.loads(tl[0]["families_json"])["S&P"]["statut"] == "pos"


def test_minutes_par_couleur(conn):
    d = "2026-08-03"
    for ts, col in [("15:30", "green"), ("16:00", "red"), ("17:00", "green")]:
        journal.record_regime(conn, date=d, ts=f"{d}T{ts}:00+02:00",
                              kind="open" if ts == "15:30" else "change", color=col)
    mins = journal.minutes_par_couleur(journal.regime_timeline(conn, d),
                                       fin_ts=f"{d}T22:00:00+02:00")
    assert mins["red"] == 60.0                 # 16h00 -> 17h00
    assert mins["green"] == 30.0 + 300.0       # (15h30->16h) + (17h->22h)


def test_minutes_sans_fin_ignore_dernier_segment(conn):
    d = "2026-08-03"
    journal.record_regime(conn, date=d, ts=f"{d}T15:30:00+02:00", kind="open",
                          color="green")
    # un seul événement, pas de borne de fin -> durée inconnue, 0 partout
    mins = journal.minutes_par_couleur(journal.regime_timeline(conn, d))
    assert mins == {"green": 0.0, "orange": 0.0, "red": 0.0}


def test_compute_reason():
    fam_pos = json.dumps({"Nasdaq": {"statut": "pos"}, "S&P": {"statut": "pos"}})
    fam_neg = json.dumps({"Nasdaq": {"statut": "neg"}, "S&P": {"statut": "pos"}})
    prev = {"color": "green", "confidence": "forte", "families_json": fam_pos}
    # changement de couleur + bascule d'une famille
    r = journal.compute_reason(prev, "orange", "forte",
                               {"Nasdaq": {"statut": "neg"}, "S&P": {"statut": "pos"}})
    assert "couleur green→orange" in r and "Nasdaq pos→neg" in r
    # confiance seule qui bouge (même couleur, mêmes statuts)
    r2 = journal.compute_reason(prev, "green", "moyenne",
                                {"Nasdaq": {"statut": "pos"}, "S&P": {"statut": "pos"}})
    assert r2 == "confiance forte→moyenne"
    # rien ne bouge -> None
    assert journal.compute_reason(prev, "green", "forte",
                                  json.loads(fam_pos)) is None
    # pas de précédent -> None
    assert journal.compute_reason(None, "red", "faible", {}) is None


def test_poll_cycle_et_votes_bruts(conn):
    d = "2026-08-03"
    assert journal.poll_posted(conn, d) is False
    journal.poll_open(conn, date=d, message_id="42",
                      posted_ts=f"{d}T23:05:00+02:00",
                      tally_due_ts="2026-08-04T12:00:00+02:00")
    assert journal.poll_posted(conn, d) is True
    # pas encore l'heure -> rien à dépouiller
    assert journal.polls_a_depouiller(conn, "2026-08-04T11:59:00+02:00") == []
    due = journal.polls_a_depouiller(conn, "2026-08-04T12:00:00+02:00")
    assert [r["message_id"] for r in due] == ["42"]
    # dépouillement : on stocke les votes BRUTS (comptages), pas un booléen
    journal.poll_tally(conn, date=d,
                       counts={"q1_directionnel": 7, "q1_retracement": 2,
                               "q3_b4": 3, "q4_repr_low": 1},
                       tallied_ts="2026-08-04T12:00:00+02:00")
    row = conn.execute("SELECT * FROM polls WHERE date=?", (d,)).fetchone()
    assert row["q1_directionnel"] == 7 and row["q1_retracement"] == 2
    assert row["q3_b4"] == 3 and row["q4_repr_low"] == 1
    assert row["tallied_ts"] is not None
    # une fois dépouillé, il ne ressort plus
    assert journal.polls_a_depouiller(conn, "2026-08-05T12:00:00+02:00") == []


def test_poll_open_ne_duplique_pas(conn):
    d = "2026-08-03"
    journal.poll_open(conn, date=d, message_id="1", posted_ts="x",
                      tally_due_ts="y")
    journal.poll_open(conn, date=d, message_id="2", posted_ts="x",
                      tally_due_ts="y")   # INSERT OR IGNORE : le premier tient
    row = conn.execute("SELECT message_id FROM polls WHERE date=?", (d,)).fetchone()
    assert row["message_id"] == "1"


def test_heatmap_idempotent(conn):
    d = "2026-08-03"
    assert journal.heatmap_done(conn, d, "15h30", "NQ") is False
    journal.record_heatmap(conn, date=d, slot="15h30", symbol="NQ",
                           path="/x/nq.png", ts="t")
    assert journal.heatmap_done(conn, d, "15h30", "NQ") is True
    # même créneau/symbole -> pas de doublon (UNIQUE)
    journal.record_heatmap(conn, date=d, slot="15h30", symbol="NQ",
                           path="/x/nq2.png", ts="t2")
    n = conn.execute("SELECT COUNT(*) c FROM heatmaps WHERE date=? AND slot=? "
                     "AND symbol=?", (d, "15h30", "NQ")).fetchone()["c"]
    assert n == 1


def test_market_context_et_metriques_eav(conn):
    d = "2026-08-03"
    journal.upsert_market_context(conn, date=d, symbol="NQ",
                                  ctx={"open": 20000, "high": 20300, "low": 19900,
                                       "close": 20250, "prev_close": 19980,
                                       "gap": 20, "prev_atr": 280, "weekday": 0})
    row = conn.execute("SELECT * FROM market_context WHERE date=? AND symbol=?",
                       (d, "NQ")).fetchone()
    assert row["gap"] == 20 and row["weekday"] == 0
    # daily_metrics : numérique et textuel, global (symbol NULL) et par symbole
    journal.set_metric(conn, date=d, name="n_changes", value_num=3)
    journal.set_metric(conn, date=d, name="open_regime", value_txt="rouge")
    journal.set_metric(conn, date=d, symbol="NQ", name="range", value_num=400)
    assert journal.get_metric(conn, d, "n_changes") == 3
    assert journal.get_metric(conn, d, "open_regime") == "rouge"
    assert journal.get_metric(conn, d, "range", symbol="NQ") == 400
    # extensibilité : une métrique inventée plus tard s'insère sans ALTER TABLE
    journal.set_metric(conn, date=d, name="metrique_du_futur", value_num=42)
    assert journal.get_metric(conn, d, "metrique_du_futur") == 42


def test_set_metric_globale_ne_duplique_pas(conn):
    """Régression : symbol NULL dans la clé primaire — deux NULL étant distincts
    en SQLite, un upsert naïf dupliquerait. On ré-écrit, on doit remplacer."""
    d = "2026-08-03"
    journal.set_metric(conn, date=d, name="n_changes", value_num=3)
    journal.set_metric(conn, date=d, name="n_changes", value_num=5)
    n = conn.execute("SELECT COUNT(*) c FROM daily_metrics "
                     "WHERE date=? AND symbol IS NULL AND metric_name='n_changes'",
                     (d,)).fetchone()["c"]
    assert n == 1
    assert journal.get_metric(conn, d, "n_changes") == 5


def test_setup_moc_tag(conn):
    d = "2026-08-03"
    assert journal.get_setup(conn, d) is None
    journal.set_setup(conn, date=d, value="MOC A")
    assert journal.get_setup(conn, d) == "MOC A"
    # corrigible : ré-écrire remplace
    journal.set_setup(conn, date=d, value="NONE")
    assert journal.get_setup(conn, d) == "NONE"


def test_research_log(conn):
    # hypothèse liée à une séance passée, avec auteur
    h = journal.add_entry(conn, text="Le Rouge Fort produit un pinning plus fort",
                          created="2026-08-20T23:00:00+02:00", type="hypothesis",
                          author="Emilien", linked_date="2026-08-15")
    journal.add_entry(conn, text="Marche surtout le vendredi",
                      created="2026-08-21T10:00:00+02:00", type="observation",
                      author="Collègue")
    row = next(r for r in journal.list_entries(conn) if r["id"] == h)
    assert row["type"] == "hypothesis" and row["author"] == "Emilien"
    assert row["linked_date"] == "2026-08-15"          # séance ≠ jour d'écriture
    # filtres : par type, par auteur, par statut
    assert [r["id"] for r in journal.list_entries(conn, type="hypothesis")] == [h]
    assert {r["author"] for r in journal.list_entries(conn, author="Collègue")} == {"Collègue"}
    journal.set_entry_status(conn, h, "confirmed", note="180 séances")
    row = next(r for r in journal.list_entries(conn) if r["id"] == h)
    assert row["status"] == "confirmed" and row["note"] == "180 séances"
    assert all(r["status"] == "confirmed"
               for r in journal.list_entries(conn, status="confirmed"))
