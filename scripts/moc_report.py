"""Validation de la pression MOC estimée — cf. gex/moc_report.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gex.moc_report import main  # noqa: E402

if __name__ == "__main__":
    main()
