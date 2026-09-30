"""SQLite 连接与 schema 初始化。

V1 刻意不引入 ORM、不引入 migration framework：
只有一份 schema_v1.sql 和一张 schema_version 表。
"""

from __future__ import annotations

import pathlib
import sqlite3

from .util import utcnow_iso

SCHEMA_VERSION = 1

_PKG_DIR = pathlib.Path(__file__).resolve().parent
SCHEMA_V1_PATH = _PKG_DIR.parent / "schema" / "schema_v1.sql"


def connect(db_path: str | pathlib.Path, *, create_parents: bool = True) -> sqlite3.Connection:
    """打开（或创建）数据库连接，开启外键约束。"""
    path = pathlib.Path(db_path)
    if create_parents and path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def read_schema_version(conn: sqlite3.Connection) -> int | None:
    """返回已应用的 schema 版本号；未初始化时返回 None。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return None
    cur = conn.execute("SELECT MAX(version) AS v FROM schema_version")
    result = cur.fetchone()
    return None if result is None or result["v"] is None else int(result["v"])


def init_db(conn: sqlite3.Connection, *, schema_path: pathlib.Path | None = None) -> int:
    """应用 schema_v1.sql 并登记版本号。可重复执行（幂等）。

    返回应用后的 schema 版本号。
    """
    path = pathlib.Path(schema_path) if schema_path else SCHEMA_V1_PATH
    sql = path.read_text(encoding="utf-8")
    conn.executescript(sql)
    current = read_schema_version(conn)
    if current is None or current < SCHEMA_VERSION:
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utcnow_iso()),
        )
    conn.commit()
    return SCHEMA_VERSION


def table_names(conn: sqlite3.Connection) -> list[str]:
    """列出业务表名（不含 sqlite 内部表）。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]
