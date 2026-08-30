"""Roll de contrat AU VOLUME (gex/roll) : c'est ce qui décide quel contrat
compose la série continue écrite sur disque.

L'enjeu : coller au `NQ.v.0` de Databento (roll au VOLUME) plutôt qu'au
contrat « active-month » du courtier (roll au CALENDRIER) — les deux diffèrent
de quelques jours autour du roll, précisément là où les prix sautent.
"""
from __future__ import annotations

import pytest

from gex import roll
from gex.config import SETTINGS

FRONT, NEXT = "/NQU6", "/NQZ6"


@pytest.fixture(autouse=True)
def _isole(tmp_path, monkeypatch):
    """Chaque test écrit son état de roll dans un dossier jetable."""
    monkeypatch.setattr(SETTINGS, "data_dir", tmp_path)


def test_sans_historique_replie_sur_le_contrat_actif():
    # premier démarrage : aucun volume de veille connu -> convention calendaire
    assert roll.dominant("NQ", "2026-09-10", [FRONT, NEXT]) == FRONT


def test_dominant_suit_le_volume_de_la_veille():
    roll.record_volumes("NQ", "2026-09-10", {FRONT: 900_000, NEXT: 50_000})
    assert roll.dominant("NQ", "2026-09-11", [FRONT, NEXT]) == FRONT
    # le suivant passe devant : la séance d'APRÈS bascule
    roll.record_volumes("NQ", "2026-09-11", {FRONT: 300_000, NEXT: 700_000})
    assert roll.dominant("NQ", "2026-09-12", [FRONT, NEXT]) == NEXT


def test_decision_figee_pour_la_seance():
    """Le volume de la séance EN COURS ne doit pas changer la décision : sinon
    la série se couperait en deux au milieu d'une séance."""
    roll.record_volumes("NQ", "2026-09-10", {FRONT: 900_000, NEXT: 50_000})
    roll.record_volumes("NQ", "2026-09-11", {FRONT: 10, NEXT: 999_999})
    # on décide pour le 11 : seule la veille (le 10) compte -> FRONT
    assert roll.dominant("NQ", "2026-09-11", [FRONT, NEXT]) == FRONT


def test_volumes_cumules_entre_flushes():
    roll.record_volumes("NQ", "2026-09-10", {FRONT: 100, NEXT: 10})
    roll.record_volumes("NQ", "2026-09-10", {FRONT: 50, NEXT: 5})
    assert roll.load_state()["NQ"]["2026-09-10"] == {FRONT: 150, NEXT: 15}


def test_seance_vide_ignoree_on_remonte_plus_loin():
    """Un jour férié à volume nul ne doit pas figer le choix : on remonte à la
    dernière séance réellement traitée."""
    roll.record_volumes("NQ", "2026-09-10", {FRONT: 100, NEXT: 900})
    roll.record_volumes("NQ", "2026-09-11", {FRONT: 0, NEXT: 0})
    assert roll.dominant("NQ", "2026-09-12", [FRONT, NEXT]) == NEXT


def test_etat_borne_en_taille():
    for j in range(1, 25):
        roll.record_volumes("NQ", f"2026-09-{j:02d}", {FRONT: 10})
    assert len(roll.load_state()["NQ"]) == roll.KEEP_SESSIONS


def test_etat_illisible_ne_bloque_pas(tmp_path):
    p = roll._state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ ceci n'est pas du json", encoding="utf-8")
    assert roll.load_state() == {}
    assert roll.dominant("NQ", "2026-09-11", [FRONT, NEXT]) == FRONT


def test_aucun_contrat():
    assert roll.dominant("NQ", "2026-09-11", []) is None


def test_flush_ticks_n_ecrit_que_le_dominant(monkeypatch, tmp_path):
    """Chemin complet : la capture livre DEUX contrats, seul le dominant est
    écrit — mais les volumes des DEUX sont mémorisés pour la séance suivante."""
    from datetime import datetime, timedelta, UTC
    from gex import scheduler, store
    from gex.metrics import ET

    # tick à 10:00 ET le 2026-09-11 -> séance 2026-09-11
    ts = (datetime(2026, 9, 11, 10, 0, tzinfo=ET)).timestamp()
    def row(px, vol):
        return {"ts": ts, "price": px, "volume": vol, "bid": None,
                "ask": None, "side": None, "source": "dxfeed"}

    monkeypatch.setattr(scheduler.CAPTURE, "drain", lambda: {
        "NQ": {FRONT: [row(100.0, 5)], NEXT: [row(200.0, 7)]}})
    monkeypatch.setattr(scheduler.CAPTURE, "contract_order", lambda s: [FRONT, NEXT])

    # la veille, FRONT dominait -> c'est lui qu'on écrit aujourd'hui
    roll.record_volumes("NQ", "2026-09-10", {FRONT: 900, NEXT: 100})
    scheduler.flush_ticks()

    df = store.load_ticks("NQ", "2026-09-11")
    assert list(df["price"]) == [100.0]          # NEXT écarté du disque
    # ... mais son volume est bien mémorisé pour la décision de demain
    assert roll.load_state()["NQ"]["2026-09-11"] == {FRONT: 5, NEXT: 7}
    assert roll.dominant("NQ", "2026-09-12", [FRONT, NEXT]) == NEXT
