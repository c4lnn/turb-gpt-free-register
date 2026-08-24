"""SQLite runtime storage primitives and legacy snapshot migration."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
KINDS = (
    "accounts",
    "jobs",
    "outlook_emails",
    "icloud_emails",
    "generic_api_emails",
    "domain_emails",
    "mailcom_emails",
    "mailcom_aliases",
    "email_pool_lifecycle",
)
logger = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """Raised when runtime storage is unreadable or fails validation."""


class SQLiteRuntimeStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            return conn
        except Exception:
            conn.close()
            raise

    @contextmanager
    def connection(self):
        """提供确定性关闭的 SQLite 连接，避免 Windows 上残留文件句柄。"""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_records (
                    kind TEXT NOT NULL,
                    record_id INTEGER NOT NULL,
                    email TEXT,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (kind, record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_records_email
                    ON runtime_records(kind, email);
                CREATE TABLE IF NOT EXISTS mailcom_alias_domains (
                    domain TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """
            )
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row[0]) != SCHEMA_VERSION:
                raise StorageError(f"不支持的 SQLite schema 版本: {row[0]}")

    def load_mailcom_alias_domains(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT domain, enabled, created_at, updated_at "
                "FROM mailcom_alias_domains ORDER BY domain"
            ).fetchall()
        return [
            {
                "domain": str(row[0]),
                "enabled": bool(row[1]),
                "created_at": row[2],
                "updated_at": row[3],
            }
            for row in rows
        ]

    def upsert_mailcom_alias_domain(self, domain: str, enabled: bool, now: str) -> dict[str, Any]:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO mailcom_alias_domains(domain, enabled, created_at, updated_at) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(domain) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
                (domain, int(bool(enabled)), now, now),
            )
            row = conn.execute(
                "SELECT domain, enabled, created_at, updated_at FROM mailcom_alias_domains WHERE domain=?",
                (domain,),
            ).fetchone()
        return {
            "domain": str(row[0]),
            "enabled": bool(row[1]),
            "created_at": row[2],
            "updated_at": row[3],
        }

    def integrity_check(self) -> None:
        if not self.path.exists():
            raise StorageError(f"SQLite 数据库不存在: {self.path}")
        try:
            with self.connection() as conn:
                version = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                if version is None or int(version[0]) != SCHEMA_VERSION:
                    raise StorageError("SQLite schema 缺失或版本不兼容")
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise StorageError(f"SQLite 完整性检查失败: {result}")
        except sqlite3.DatabaseError as exc:
            raise StorageError(f"SQLite 数据库不可读: {self.path}") from exc

    def load(self, kind: str) -> list[dict[str, Any]]:
        self._check_kind(kind)
        self.integrity_check()
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM runtime_records WHERE kind=? ORDER BY record_id",
                (kind,),
            ).fetchall()
        try:
            return [json.loads(row[0]) for row in rows]
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageError(f"SQLite {kind} 记录包含无效 JSON") from exc

    def replace_all(self, kind: str, records: Iterable[dict[str, Any]]) -> None:
        self.replace_many({kind: records})

    def replace_many(self, collections: dict[str, Iterable[dict[str, Any]]]) -> None:
        prepared_by_kind = {}
        now = datetime.now().isoformat(timespec="seconds")
        for kind, records in collections.items():
            self._check_kind(kind)
            prepared = []
            for record in records:
                if not isinstance(record, dict):
                    raise StorageError(f"{kind} 包含非对象记录")
                try:
                    record_id = int(record.get("id"))
                except (TypeError, ValueError) as exc:
                    raise StorageError(f"{kind} 记录缺少有效 id") from exc
                prepared.append(
                    (kind, record_id, str(record.get("email") or ""),
                     json.dumps(record, ensure_ascii=False), now)
                )
            prepared_by_kind[kind] = prepared
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for kind, prepared in prepared_by_kind.items():
                conn.execute("DELETE FROM runtime_records WHERE kind=?", (kind,))
                conn.executemany(
                    "INSERT INTO runtime_records(kind, record_id, email, payload, updated_at) VALUES(?,?,?,?,?)",
                    prepared,
                )
            conn.execute("COMMIT")

    def backup(self, destination: str | Path) -> Path:
        self.integrity_check()
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix(destination.suffix + ".tmp")
        if temp.exists():
            temp.unlink()
        source = self.connect()
        target = sqlite3.connect(temp)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        SQLiteRuntimeStore(temp).integrity_check()
        temp.replace(destination)
        logger.info("SQLite 备份完成: %s", destination)
        return destination

    @staticmethod
    def _check_kind(kind: str) -> None:
        if kind not in KINDS:
            raise StorageError(f"未知运行时数据类型: {kind}")


def read_legacy_snapshot(path: str | Path) -> tuple[list[dict[str, Any]], str]:
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError(f"旧快照不是有效 JSON: {path}") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise StorageError(f"旧快照必须是对象数组: {path}")
    return value, digest


def inspect_legacy_text(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StorageError(f"旧文本快照不是 UTF-8: {path}") from exc
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "nonempty_lines": sum(1 for line in text.splitlines() if line.strip()),
    }


def migrate_legacy_snapshots(
    destination: str | Path,
    snapshots: dict[str, str | Path],
    *,
    replace: bool = False,
    text_snapshots: dict[str, str | Path] | None = None,
) -> dict[str, Any]:
    """Import validated JSON snapshots into a temporary SQLite database."""
    destination = Path(destination)
    if destination.exists() and not replace:
        raise StorageError(f"目标数据库已存在，需显式 replace=True: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="runtime-migration-", dir=destination.parent))
    temp_db = temp_dir / "runtime.db"
    report: dict[str, Any] = {"database": str(destination), "snapshots": {}, "text_snapshots": {}}
    try:
        store = SQLiteRuntimeStore(temp_db)
        store.initialize()
        for kind, path in snapshots.items():
            records, digest = read_legacy_snapshot(path)
            store.replace_all(kind, records)
            report["snapshots"][kind] = {
                "path": str(path), "sha256": digest, "count": len(records)
            }
        for name, path in (text_snapshots or {}).items():
            report["text_snapshots"][name] = inspect_legacy_text(path)
        store.integrity_check()
        if destination.exists():
            destination.unlink()
        shutil.move(str(temp_db), str(destination))
        report["ok"] = True
        shutil.rmtree(temp_dir, ignore_errors=True)
        return report
    except Exception as exc:
        report["ok"] = False
        report["error"] = str(exc)
        (temp_dir / "migration-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.error("SQLite 迁移失败，诊断已保留在 %s: %s", temp_dir, type(exc).__name__)
        raise
