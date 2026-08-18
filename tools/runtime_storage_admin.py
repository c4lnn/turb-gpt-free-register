"""Administrative commands for SQLite runtime storage."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db
from core.sqlite_store import SQLiteRuntimeStore


def _restore(source: Path, destination: Path) -> dict:
    SQLiteRuntimeStore(source).integrity_check()
    destination.parent.mkdir(parents=True, exist_ok=True)
    previous = destination.with_name(
        f"{destination.stem}.before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}{destination.suffix}"
    )
    temp = destination.with_suffix(destination.suffix + ".restore.tmp")
    shutil.copy2(source, temp)
    SQLiteRuntimeStore(temp).integrity_check()
    if destination.exists():
        shutil.copy2(destination, previous)
    temp.replace(destination)
    SQLiteRuntimeStore(destination).integrity_check()
    return {"ok": True, "restored_from": str(source), "database": str(destination), "previous": str(previous)}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    backup = sub.add_parser("backup")
    backup.add_argument("--directory", type=Path)
    backup.add_argument("--keep", type=int, default=7)
    sub.add_parser("export")
    restore = sub.add_parser("restore")
    restore.add_argument("source", type=Path)
    restore.add_argument("--database", type=Path, default=Path("runtime.db"))
    args = parser.parse_args()

    if args.command == "check":
        db.validate_runtime_storage()
        result = {"ok": True, "database": db.storage_paths()["runtime_db"]}
    elif args.command == "backup":
        result = {"ok": True, "backup": str(db.create_runtime_backup(args.directory, keep=args.keep))}
    elif args.command == "export":
        result = {"ok": True, **db.export_runtime_snapshots()}
    else:
        result = _restore(args.source, args.database)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
