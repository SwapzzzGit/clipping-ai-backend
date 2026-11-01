"""Publishing layer for clipper: connect social accounts by OAuth and post rendered clips to many
accounts and platforms at once, with a per-platform variant of each clip.

    from publisher.service import Publisher
    pub = Publisher()          # file-backed stores under <CLIPPER_DATA>/publish
    pub.start()                # background queue
    app.include_router(publisher.api.build_router(pub), prefix="/api/publish")

See docs/publishing.md for developer-app registration and env vars.
"""

import sys
from pathlib import Path

# Same layout trick as backend/app: the clipper library lives at the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
