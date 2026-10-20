# Legacy collector tests (moved from news-daily scripts/collect) import modules flat.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "newsd" / "collector"))
