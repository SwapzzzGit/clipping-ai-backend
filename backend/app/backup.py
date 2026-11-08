"""Zip the data folder (jobs, approvals, publishing accounts, branding, settings) for safekeeping."""

import datetime as dt
import zipfile
from pathlib import Path

BULKY = ("work", "output", "uploads")   # rebuildable or re-uploadable; left out unless asked for


def write_backup(data_dir: Path, dest: Path, include_all: bool = False) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / f"clipper-data-{dt.datetime.now():%Y%m%d-%H%M%S}.zip"
    skip = () if include_all else BULKY
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(data_dir.rglob("*")):
            rel = path.relative_to(data_dir)
            if path.is_file() and path.resolve() != archive.resolve() and rel.parts[0] not in skip:
                z.write(path, rel.as_posix())
    return archive
