"""Regénère les captures d'écran du guide depuis le dashboard qui tourne.

Outillage hors du paquet : Playwright n'est PAS une dépendance du projet.
Le dashboard doit tourner sur http://127.0.0.1:8050 avant de lancer ce script.

    pip install playwright        # le binaire chromium est déjà en cache
    python tools/screenshots.py
    pip uninstall playwright -y   # on ne le laisse pas dans l'environnement

Les onglets sont capturés en pleine page ; les « chiffres » sont des crops
d'éléments précis. Le seul piège rencontré : la .topbar est collante et se
dessine par-dessus un crop quand Playwright fait défiler l'élément dans la
vue — d'où le masquage ciblé de CET élément (tout mettre en position:static
casse la mise en page interne de Plotly, crops noirs).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
URL = "http://127.0.0.1:8050"

# onglet -> (valeur du dcc.Tab, id du panneau, nom de fichier)
ONGLETS = [
    ("main", "pane-main", "onglet-vue-principale.png"),
    ("profile", "pane-profile", "onglet-gamma-profile.png"),
    ("greeks2", "pane-greeks2", "onglet-vanna-charm.png"),
    ("heat", "pane-heat", "onglet-heatmap.png"),
    ("pos", "pane-pos", "onglet-positionnement.png"),
]

# crops ciblés : id de l'élément -> nom de fichier
CROPS = [
    ("cards", "chiffre-tuiles.png"),
    ("pc-gauge", "chiffre-jauge.png"),
    ("regime-banner", "chiffre-regime.png"),
    ("levels", "chiffre-niveaux.png"),
    ("tape", "chiffre-order-flow.png"),
    ("gflow", "chiffre-gamma-echange.png"),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000},
                          device_scale_factor=2)
        page.goto(URL, wait_until="networkidle", timeout=90_000)
        # laisser les callbacks peupler les graphiques
        time.sleep(12)

        for value, pane, nom in ONGLETS:
            try:
                page.click(f"#tabh-{value}")
            except Exception as e:  # noqa: BLE001
                print(f"  ! clic onglet {value} : {e}")
            time.sleep(6)
            page.screenshot(path=str(OUT / nom), full_page=True)
            print(f"  {nom}")

        # revenir sur la vue principale pour les crops
        page.click("#tabh-main")
        time.sleep(8)
        # Le seul élément collant est .topbar (cf. assets/style.css) : masquer
        # CET élément suffit, alors que tout mettre en position:static casse la
        # mise en page interne de Plotly (crops noirs, constaté le 2026-07-30).
        page.add_style_tag(content=".topbar{visibility:hidden !important}")
        time.sleep(1)
        for el_id, nom in CROPS:
            try:
                el = page.locator(f"#{el_id}")
                if el.count() == 0:
                    print(f"  ! {el_id} absent")
                    continue
                el.first.screenshot(path=str(OUT / nom))
                print(f"  {nom}")
            except Exception as e:  # noqa: BLE001
                print(f"  ! {el_id} : {e}")
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
