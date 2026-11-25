"""Rapport d'edge de la lecture /scalp v2 — cf. gex/edge_report.py.

Usage :
    python scripts/edge_report.py              # NQ et ES
    python scripts/edge_report.py NQ --days 120
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gex.edge_report import main  # noqa: E402

if __name__ == "__main__":
    main()
