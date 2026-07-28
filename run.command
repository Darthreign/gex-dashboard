#!/bin/bash
# Double-clic dans le Finder (macOS) : lance le dashboard. Le suffixe
# .command est ce qui fait que macOS l'exécute au lieu de l'ouvrir dans un
# éditeur de texte.
cd "$(dirname "$0")"
.venv/bin/python run.py
read -p "Le programme s'est arrêté. Appuie sur Entrée pour fermer..."
