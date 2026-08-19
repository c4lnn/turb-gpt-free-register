"""Migrate legacy JSON snapshots into a validated SQLite runtime database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.sqlite_store import StorageError, migrate_legacy_snapshots


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--accounts", type=Path)
    parser.add_argument("--jobs", type=Path)
    parser.add_argument("--outlook-emails", type=Path)
    parser.add_argument("--icloud-emails", type=Path)
    parser.add_argument("--generic-api-emails", type=Path)
    parser.add_argument("--domain-emails", type=Path)
    parser.add_argument("--mailcom-emails", type=Path)
    parser.add_argument("--mailcom-aliases", type=Path)
    parser.add_argument("--accounts-txt", type=Path)
    parser.add_argument("--tokens-txt", type=Path)
    parser.add_argument("--outlook-txt", type=Path)
    parser.add_argument("--generic-api-txt", type=Path)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    snapshots = {
        kind: path for kind, path in {
            "accounts": args.accounts,
            "jobs": args.jobs,
            "outlook_emails": args.outlook_emails,
            "icloud_emails": args.icloud_emails,
            "generic_api_emails": args.generic_api_emails,
            "domain_emails": args.domain_emails,
            "mailcom_emails": args.mailcom_emails,
            "mailcom_aliases": args.mailcom_aliases,
        }.items() if path is not None
    }
    if not snapshots:
        parser.error("至少提供一个 JSON 快照")
    text_snapshots = {
        name: path for name, path in {
            "accounts_txt": args.accounts_txt,
            "tokens_txt": args.tokens_txt,
            "outlook_txt": args.outlook_txt,
            "generic_api_txt": args.generic_api_txt,
        }.items() if path is not None
    }
    try:
        report = migrate_legacy_snapshots(
            args.database, snapshots, replace=args.replace, text_snapshots=text_snapshots
        )
    except StorageError as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
