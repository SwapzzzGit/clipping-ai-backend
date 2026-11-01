import os
import sys
import tempfile
from pathlib import Path

# Point the app at a throwaway data dir before app.store is imported.
os.environ["CLIPPER_DATA"] = tempfile.mkdtemp(prefix="clipper-test-")
# Publishing tests only ever use the offline mock platform.
os.environ["PUBLISHER_MOCK"] = "1"
os.environ["PUBLISHER_MOCK_DELAY"] = "0.02"
os.environ.pop("PUBLISHER_DATA", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
