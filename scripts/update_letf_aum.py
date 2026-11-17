"""Met à jour les encours des ETF à levier (page /moc) — cf. gex/letf_aum.py.

Usage :
    python scripts/update_letf_aum.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gex.letf_aum import main  # noqa: E402

if __name__ == "__main__":
    main()
