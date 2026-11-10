# -*- coding: utf-8 -*-
"""一次性清理提链遗留数据：账户记录里的 extract_link_* 字段与本地 CDK 池。

提链功能已从代码中移除，本脚本负责把历史数据里残留的字段与 CDK 池清掉。
默认只统计不写盘，确认后加 --apply 执行；CDK 池文件会先改名为 .bak 再删除，
不会直接抹掉。

用法：
    python tools/cleanup_extract_link_data.py
    python tools/cleanup_extract_link_data.py --apply
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.infra import db

_FIELD_PREFIX = "extract_link_"
_POOL_FILES = ("提链CDK池.json",)


def _pool_paths() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    return [root / name for name in _POOL_FILES if (root / name).exists()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="执行清理；缺省只统计")
    args = parser.parse_args()

    accounts = db._load_accounts()
    pending: list[tuple[object, object, list[str]]] = []
    for row in accounts:
        keys = sorted(key for key in row if key.startswith(_FIELD_PREFIX))
        if keys:
            pending.append((row.get("id"), row.get("email"), keys))

    print(f"账户记录 {len(accounts)} 条，含提链字段 {len(pending)} 条")
    for account_id, email, keys in pending[:10]:
        print(f"  id={account_id} email={email} 待删字段 {len(keys)} 个: {', '.join(keys[:4])}...")
    if len(pending) > 10:
        print(f"  ... 其余 {len(pending) - 10} 条省略")

    pools = _pool_paths()
    for path in pools:
        print(f"CDK 池文件: {path.name}（{path.stat().st_size / 1024:.0f} KB）")

    if not args.apply:
        print("\n未加 --apply，未修改任何数据。")
        return 0

    if pending:
        for row in accounts:
            for key in [key for key in row if key.startswith(_FIELD_PREFIX)]:
                row.pop(key, None)
        db._save_accounts(accounts)
        print(f"已清理 {len(pending)} 条账户记录的提链字段。")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(__file__).resolve().parents[1] / "data" / "removed"
    for path in pools:
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{path.name}.{stamp}.bak"
        path.rename(backup)
        print(f"已移除 CDK 池: {path.name} → {backup.relative_to(backup_dir.parents[1])}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
