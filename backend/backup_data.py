"""Back up the data folder.

    python backend/backup_data.py              # jobs, approvals, accounts, branding, settings
    python backend/backup_data.py --all       # also uploads, work files and rendered clips
    python backend/backup_data.py --dest D:/backups

The archive holds secret.key next to the encrypted account tokens, so keep it private.
Stop the backend first so nothing is written mid-copy. To restore, unzip it into the data folder.
"""

import argparse
from pathlib import Path

from app.backup import write_backup
from app.store import DATA_DIR


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", type=Path, default=DATA_DIR.parent / "backups")
    ap.add_argument("--all", action="store_true", help="include uploads, work files and rendered clips")
    args = ap.parse_args()
    archive = write_backup(DATA_DIR, args.dest, include_all=args.all)
    print(f"wrote {archive} ({archive.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
