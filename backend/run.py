"""Start the Clipper Studio backend: python backend/run.py [--port 8000] [--reload]"""

import argparse
import sys
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    a = p.parse_args()
    backend = Path(__file__).resolve().parent
    sys.path.insert(0, str(backend))
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    uvicorn.run("app.main:app", host=a.host, port=a.port, reload=a.reload, app_dir=str(backend))
