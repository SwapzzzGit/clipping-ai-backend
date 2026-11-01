"""Clipper Studio backend: FastAPI + a job worker around the `clipper` package."""

import sys
from pathlib import Path

# The clipper library lives at the repo root (one level above backend/).
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
