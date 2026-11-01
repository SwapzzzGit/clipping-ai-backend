"""Push clips rendered on this machine to the deployed Clipper Studio, ready to review and publish.

    python backend/push_clips.py <job_id | output folder> [--remote https://clipping-ai-backend.onrender.com]

The folder must hold manifest.json plus the clip files (as written by the pipeline). Settings come
from the environment or <repo>/.env:
    CLIPPER_REMOTE_URL       the deployed backend (upload straight to it, not through Vercel)
    CLIPPER_REMOTE_PASSWORD  the deployed APP_PASSWORD
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parent
REPO = BACKEND.parent
sys.path.insert(0, str(REPO))

from clipper.util import read_json  # noqa: E402
from publisher.config import load_dotenv  # noqa: E402  (reads <repo>/.env)


def find_folder(arg: str) -> Path:
    p = Path(arg)
    if (p / "manifest.json").is_file():
        return p
    data = Path(os.environ.get("CLIPPER_DATA", BACKEND / "data"))
    for cand in (data / "output" / arg, REPO / "output" / arg):
        if (cand / "manifest.json").is_file():
            return cand
    sys.exit(f"No manifest.json for {arg!r}")


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="local backend job id, CLI video id, or a folder with manifest.json")
    ap.add_argument("--remote", default=os.environ.get("CLIPPER_REMOTE_URL", "https://clipping-ai-backend.onrender.com"))
    a = ap.parse_args()
    password = os.environ.get("CLIPPER_REMOTE_PASSWORD", "")
    if not password:
        sys.exit("Set CLIPPER_REMOTE_PASSWORD (the deployed APP_PASSWORD) in .env or the environment.")
    folder = find_folder(a.source)
    manifest = read_json(folder / "manifest.json")
    remote = a.remote.rstrip("/")
    with httpx.Client(headers={"Authorization": f"Bearer {password}"},
                      timeout=httpx.Timeout(120.0, read=600.0, write=600.0)) as http:
        r = http.post(f"{remote}/api/import/jobs", json={"manifest": manifest})
        if r.status_code >= 400:
            sys.exit(f"Import refused: HTTP {r.status_code} {r.text[:300]}")
        job_id, files = r.json()["job_id"], r.json()["files"]
        print(f"remote job {job_id}: uploading {len(files)} files")
        for name in files:
            data = (folder / name).read_bytes()
            r = http.put(f"{remote}/api/import/jobs/{job_id}/files/{name}", content=data,
                         headers={"Content-Type": "application/octet-stream"})
            if r.status_code >= 400:
                sys.exit(f"Upload of {name} failed: HTTP {r.status_code} {r.text[:300]}")
            print(f"  {name}  {len(data) / 1e6:.1f} MB")
        r = http.post(f"{remote}/api/import/jobs/{job_id}/finish")
        if r.status_code >= 400:
            sys.exit(f"Finish failed: HTTP {r.status_code} {r.text[:300]}")
        print(f"done: {len(r.json()['clips'])} clips in remote job {job_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
