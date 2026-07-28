#!/bin/bash
# Linux : ./run.sh depuis un terminal, ou "Exécuter dans un terminal" via le
# clic droit selon le gestionnaire de fichiers. Sur macOS, préférer
# run.command (double-clic direct dans le Finder).
cd "$(dirname "$0")"
.venv/bin/python run.py
read -p "Le programme s'est arrêté. Appuie sur Entrée pour fermer..."
