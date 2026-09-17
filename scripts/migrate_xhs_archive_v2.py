#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / ".workbench" / "copy_workbench.sqlite3"
MIGRATION_SQL = PROJECT_ROOT / "migrations" / "xhs_archive_v2.sql"

COLUMNS: tuple[tuple[str, str], ...] = (
    ("body_raw", "TEXT"),
    ("body_normalized", "TEXT"),
    ("body_source", "TEXT NOT NULL DEFAULT ''"),
    ("body_evidence_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("archive_status", "TEXT NOT NULL DEFAULT 'NOT_ARCHIVED'"),
    ("verification_status", "TEXT NOT NULL DEFAULT 'UNVERIFIED'"),
    ("content_sha256", "TEXT"),
    ("latest_archive_id", "TEXT"),
    ("expected_asset_count", "INTEGER"),
    ("saved_asset_count", "INTEGER NOT NULL DEFAULT 0"),
)


def _statements(script: str):
    buffer: list[str] = []
    for line in script.splitlines():
        buffer.append(line)
        candidate = "\n".join(buffer).strip()
        if candidate and sqlite3.complete_statement(candidate):
            yield candidate
            buffer.clear()
    if "\n".join(buffer).strip():
        raise RuntimeError("迁移 SQL 最后一条语句不完整")


def _backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    with sqlite3.connect(target) as check:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"备份完整性检查失败：{result}")


def migrate(database: Path) -> dict[str, object]:
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在：{database}")
    script = MIGRATION_SQL.read_text(encoding="utf-8")
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='xhs_post_extractions'"
        ).fetchone():
            raise RuntimeError("数据库缺少 xhs_post_extractions，拒绝迁移错误文件")
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = {
                row[1] for row in connection.execute("PRAGMA table_info(xhs_post_extractions)")
            }
            added: list[str] = []
            for name, definition in COLUMNS:
                if name not in existing:
                    connection.execute(
                        f"ALTER TABLE xhs_post_extractions ADD COLUMN {name} {definition}"
                    )
                    added.append(name)
            for statement in _statements(script):
                connection.execute(statement)
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','8')"
            )
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise RuntimeError(f"外键检查失败：{foreign_key_errors[:3]}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return {
            "database": str(database),
            "added_columns": added,
            "schema_version": connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0],
            "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="安全迁移小红书归档 V2 数据结构")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--backup", type=Path, help="迁移前创建 SQLite 一致性备份")
    args = parser.parse_args()
    database = args.database.expanduser().resolve()
    if args.backup:
        backup = args.backup.expanduser().resolve()
        _backup(database, backup)
        digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        print(f"backup={backup}")
        print(f"backup_sha256={digest}")
    result = migrate(database)
    for key, value in result.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
