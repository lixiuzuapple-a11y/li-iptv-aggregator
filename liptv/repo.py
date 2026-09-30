"""数据访问层：对 8 张业务表的最小 CRUD / 归集操作。

CLI 与测试都调用这里，保证行为单一来源。
"""

from __future__ import annotations

import sqlite3

from .util import sha256_hex, utcnow_iso


class BindingConflictError(RuntimeError):
    """source_channel 已有归属，被请求改绑到另一 canonical（默认拒绝）。"""


def _now(now: str | None) -> str:
    return now or utcnow_iso()


# ------------------------------------------------------------------ source

def add_source(
    conn: sqlite3.Connection,
    name: str,
    kind: str,
    url: str | None = None,
    *,
    now: str | None = None,
) -> int:
    """新增或复用同名 source，返回 source.id。"""
    stamp = _now(now)
    row = conn.execute("SELECT id FROM source WHERE name = ?", (name,)).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE source SET kind = ?, url = COALESCE(?, url), updated_at = ? WHERE id = ?",
            (kind, url, stamp, row["id"]),
        )
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO source (name, kind, url, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (name, kind, url, stamp, stamp),
    )
    return int(cur.lastrowid)


def list_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM source ORDER BY id").fetchall()


def mark_source_fetched(
    conn: sqlite3.Connection, source_id: int, status: str, *, now: str | None = None
) -> None:
    stamp = _now(now)
    conn.execute(
        "UPDATE source SET last_fetch_at = ?, last_fetch_status = ?, updated_at = ? WHERE id = ?",
        (stamp, status, stamp, source_id),
    )


# ---------------------------------------------------------- source_channel

def source_channel_identity_hash(
    raw_name: str, raw_group: str | None, raw_stream_url: str
) -> str:
    """原始条目的可解释复合身份哈希。

    身份 = 来源内「名称 + 分组 + URL」。刻意**不**只用 URL：
    同一来源中名称不同、URL 相同的两条原始条目必须各自保留独立身份。
    `raw_group` 缺失时归一为空串，避免 SQLite 把 NULL 当作互不相同导致重复导入产生新行。
    """
    payload = "\x00".join([raw_name or "", raw_group or "", raw_stream_url or ""])
    return sha256_hex(payload)


def upsert_source_channel(
    conn: sqlite3.Connection,
    source_id: int,
    entry,
    *,
    now: str | None = None,
) -> tuple[int, bool]:
    """按 (source_id, identity_hash) 归并原始条目。

    返回 (source_channel_id, created)。
    """
    stamp = _now(now)
    identity = source_channel_identity_hash(entry.name, entry.group_title, entry.url)
    row = conn.execute(
        "SELECT id FROM source_channel WHERE source_id = ? AND identity_hash = ?",
        (source_id, identity),
    ).fetchone()

    if row is not None:
        conn.execute(
            "UPDATE source_channel SET identity_hash = ?, external_id = ?, raw_name = ?, "
            "raw_group = ?, raw_logo = ?, raw_epg_id = ?, last_seen_at = ?, active = 1 WHERE id = ?",
            (
                identity,
                entry.tvg_id,
                entry.name,
                entry.group_title,
                entry.tvg_logo,
                entry.tvg_id,
                stamp,
                row["id"],
            ),
        )
        return int(row["id"]), False

    cur = conn.execute(
        "INSERT INTO source_channel (source_id, identity_hash, external_id, raw_name, raw_group, "
        "raw_logo, raw_epg_id, raw_stream_url, first_seen_at, last_seen_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (
            source_id,
            identity,
            entry.tvg_id,
            entry.name,
            entry.group_title,
            entry.tvg_logo,
            entry.tvg_id,
            entry.url,
            stamp,
            stamp,
        ),
    )
    return int(cur.lastrowid), True


def list_source_channels(conn: sqlite3.Connection, source_id: int | None = None) -> list[sqlite3.Row]:
    if source_id is None:
        return conn.execute("SELECT * FROM source_channel ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM source_channel WHERE source_id = ? ORDER BY id", (source_id,)
    ).fetchall()


# ------------------------------------------------------- canonical_channel

def add_canonical_channel(
    conn: sqlite3.Connection,
    name: str,
    *,
    category: str | None = None,
    tvg_id: str | None = None,
    logo: str | None = None,
    priority: int = 100,
    now: str | None = None,
) -> int:
    stamp = _now(now)
    cur = conn.execute(
        "INSERT INTO canonical_channel (name, category, preferred_tvg_id, preferred_logo, "
        "enabled, priority, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
        (name, category, tvg_id, logo, priority, stamp, stamp),
    )
    return int(cur.lastrowid)


def list_canonical_channels(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM canonical_channel ORDER BY id").fetchall()


# ------------------------------------------------------------ channel_binding

def get_binding(conn: sqlite3.Connection, source_channel_id: int) -> sqlite3.Row | None:
    """返回某 source_channel 当前的绑定（V1 至多一条），未绑定时返回 None。"""
    return conn.execute(
        "SELECT * FROM channel_binding WHERE source_channel_id = ?", (source_channel_id,)
    ).fetchone()


def bind_source_channel(
    conn: sqlite3.Connection,
    source_channel_id: int,
    canonical_channel_id: int,
    *,
    method: str = "manual",
    confidence: float = 1.0,
    rebind: bool = False,
    now: str | None = None,
) -> tuple[int, bool]:
    """建立 source_channel → canonical_channel 绑定（幂等）。

    V1 约束：一条 source_channel 只属于一个 canonical_channel。
      * 未绑定 → 新建，返回 (id, True)；
      * 已绑定同一 canonical → 幂等更新 method/confidence，返回 (id, False)；
      * 已绑定另一 canonical → 默认抛 BindingConflictError（禁止静默多归属）；
        仅当显式 rebind=True 时才删除旧绑定并新建（可审计的迁移）。
    """
    stamp = _now(now)
    existing = get_binding(conn, source_channel_id)

    if existing is None:
        cur = conn.execute(
            "INSERT INTO channel_binding (source_channel_id, canonical_channel_id, method, "
            "confidence, created_at) VALUES (?, ?, ?, ?, ?)",
            (source_channel_id, canonical_channel_id, method, confidence, stamp),
        )
        return int(cur.lastrowid), True

    current_canonical = int(existing["canonical_channel_id"])
    if current_canonical == int(canonical_channel_id):
        conn.execute(
            "UPDATE channel_binding SET method = ?, confidence = ? WHERE id = ?",
            (method, confidence, existing["id"]),
        )
        return int(existing["id"]), False

    if not rebind:
        raise BindingConflictError(
            f"source_channel {source_channel_id} 已归属 canonical_channel {current_canonical}，"
            f"拒绝静默改绑到 canonical_channel {canonical_channel_id}；"
            f"如需迁移请显式指定 rebind=True"
        )

    conn.execute("DELETE FROM channel_binding WHERE id = ?", (existing["id"],))
    cur = conn.execute(
        "INSERT INTO channel_binding (source_channel_id, canonical_channel_id, method, "
        "confidence, created_at) VALUES (?, ?, ?, ?, ?)",
        (source_channel_id, canonical_channel_id, method, confidence, stamp),
    )
    return int(cur.lastrowid), True


def list_bindings(conn: sqlite3.Connection, canonical_channel_id: int | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT b.*, sc.raw_name, sc.raw_stream_url, sc.source_id, "
        "cc.name AS canonical_name FROM channel_binding b "
        "JOIN source_channel sc ON sc.id = b.source_channel_id "
        "JOIN canonical_channel cc ON cc.id = b.canonical_channel_id"
    )
    if canonical_channel_id is None:
        return conn.execute(sql + " ORDER BY b.id").fetchall()
    return conn.execute(
        sql + " WHERE b.canonical_channel_id = ? ORDER BY b.id", (canonical_channel_id,)
    ).fetchall()


# ----------------------------------------------------------------- stream

def sync_streams(
    conn: sqlite3.Connection,
    *,
    canonical_channel_id: int | None = None,
    now: str | None = None,
) -> dict[str, int]:
    """从已绑定的 source_channel 归集出 stream 与 stream_source。

    * URL 去重限定在 canonical 作用域内：查找键为 (canonical_channel_id, url_hash)。
      同一 canonical 下多来源提供相同 URL → 只有一条 stream（多来源挂 stream_source）；
      不同 canonical 的相同 URL → 各自独立 stream，绝不跨频道错链；
    * 同一条 stream 可挂多个来源（stream_source）；
    * 已存在的 stream 只更新 last_seen_at / status；
    * 收尾做一次一致性清理：删除「来源已不再属于该 stream 的 canonical」的旧
      stream_source（例如 rebind 之后残留的跨频道错链），并把因此失去全部来源的
      stream 标记为 stale（选择器会跳过 stale，来源回归时自动恢复 observed）。
    """
    stamp = _now(now)
    sql = (
        "SELECT b.canonical_channel_id AS cid, sc.id AS scid, sc.raw_stream_url AS url "
        "FROM channel_binding b JOIN source_channel sc ON sc.id = b.source_channel_id "
        "WHERE sc.active = 1"
    )
    params: tuple = ()
    if canonical_channel_id is not None:
        sql += " AND b.canonical_channel_id = ?"
        params = (canonical_channel_id,)
    rows = conn.execute(sql + " ORDER BY b.canonical_channel_id, sc.id", params).fetchall()

    stats = {
        "streams_created": 0,
        "streams_updated": 0,
        "links_created": 0,
        "links_updated": 0,
        "links_removed": 0,
        "streams_marked_stale": 0,
    }

    for row in rows:
        cid, scid, url = int(row["cid"]), int(row["scid"]), row["url"]
        url_hash = sha256_hex(url)

        existing = conn.execute(
            "SELECT id, status FROM stream WHERE canonical_channel_id = ? AND url_hash = ?",
            (cid, url_hash),
        ).fetchone()
        if existing is None:
            cur = conn.execute(
                "INSERT INTO stream (canonical_channel_id, url, url_hash, first_seen_at, "
                "last_seen_at, enabled, status) VALUES (?, ?, ?, ?, ?, 1, 'observed')",
                (cid, url, url_hash, stamp, stamp),
            )
            stream_id = int(cur.lastrowid)
            stats["streams_created"] += 1
        else:
            stream_id = int(existing["id"])
            new_status = "observed" if existing["status"] == "stale" else existing["status"]
            conn.execute(
                "UPDATE stream SET last_seen_at = ?, status = ? WHERE id = ?",
                (stamp, new_status, stream_id),
            )
            stats["streams_updated"] += 1

        link = conn.execute(
            "SELECT id FROM stream_source WHERE stream_id = ? AND source_channel_id = ?",
            (stream_id, scid),
        ).fetchone()
        if link is None:
            conn.execute(
                "INSERT INTO stream_source (stream_id, source_channel_id, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?)",
                (stream_id, scid, stamp, stamp),
            )
            stats["links_created"] += 1
        else:
            conn.execute(
                "UPDATE stream_source SET last_seen_at = ? WHERE id = ?", (stamp, link["id"])
            )
            stats["links_updated"] += 1

    # 收尾：清除跨频道错链（来源已不属于该 stream 的 canonical）
    invalid = conn.execute(
        "SELECT COUNT(*) AS c FROM stream_source ss "
        "JOIN stream s ON s.id = ss.stream_id "
        "LEFT JOIN channel_binding b ON b.source_channel_id = ss.source_channel_id "
        "AND b.canonical_channel_id = s.canonical_channel_id "
        "WHERE b.id IS NULL"
    ).fetchone()["c"]
    if invalid:
        conn.execute(
            "DELETE FROM stream_source WHERE id IN ("
            "  SELECT ss.id FROM stream_source ss "
            "  JOIN stream s ON s.id = ss.stream_id "
            "  LEFT JOIN channel_binding b ON b.source_channel_id = ss.source_channel_id "
            "  AND b.canonical_channel_id = s.canonical_channel_id "
            "  WHERE b.id IS NULL)"
        )
    stats["links_removed"] = int(invalid)

    # 失去全部来源的 stream 标记为 stale（选择器跳过；来源回归时恢复 observed）
    stale_cur = conn.execute(
        "UPDATE stream SET status = 'stale' WHERE status <> 'stale' "
        "AND NOT EXISTS (SELECT 1 FROM stream_source ss WHERE ss.stream_id = stream.id)"
    )
    stats["streams_marked_stale"] = int(stale_cur.rowcount or 0)

    return stats


def list_streams(conn: sqlite3.Connection, canonical_channel_id: int | None = None) -> list[sqlite3.Row]:
    if canonical_channel_id is None:
        return conn.execute("SELECT * FROM stream ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM stream WHERE canonical_channel_id = ? ORDER BY id", (canonical_channel_id,)
    ).fetchall()


def list_stream_sources(conn: sqlite3.Connection, stream_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ss.*, sc.raw_name, sc.source_id FROM stream_source ss "
        "JOIN source_channel sc ON sc.id = ss.source_channel_id "
        "WHERE ss.stream_id = ? ORDER BY ss.id",
        (stream_id,),
    ).fetchall()


# ------------------------------------------------------------ probe_result

def ensure_probe(
    conn: sqlite3.Connection,
    name: str,
    location: str | None = None,
    *,
    now: str | None = None,
) -> int:
    row = conn.execute("SELECT id FROM probe WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO probe (name, location, enabled, last_seen_at) VALUES (?, ?, 1, ?)",
        (name, location, _now(now)),
    )
    return int(cur.lastrowid)


def list_probes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM probe ORDER BY id").fetchall()


def add_probe_result(
    conn: sqlite3.Connection,
    *,
    stream_id: int,
    probe_id: int,
    success: bool,
    checked_at: str | None = None,
    error_type: str | None = None,
    http_status: int | None = None,
    connect_ms: int | None = None,
    startup_ms: int | None = None,
    resolution_width: int | None = None,
    resolution_height: int | None = None,
    bitrate_kbps: int | None = None,
    protocol: str | None = None,
    ipv_family: str | None = None,
) -> int:
    stamp = checked_at or utcnow_iso()
    cur = conn.execute(
        "INSERT INTO probe_result (stream_id, probe_id, checked_at, success, error_type, "
        "http_status, connect_ms, startup_ms, resolution_width, resolution_height, "
        "bitrate_kbps, protocol, ipv_family) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            stream_id,
            probe_id,
            stamp,
            1 if success else 0,
            error_type,
            http_status,
            connect_ms,
            startup_ms,
            resolution_width,
            resolution_height,
            bitrate_kbps,
            protocol,
            ipv_family,
        ),
    )
    conn.execute("UPDATE probe SET last_seen_at = ? WHERE id = ?", (stamp, probe_id))
    return int(cur.lastrowid)


def list_probe_results(conn: sqlite3.Connection, stream_id: int | None = None) -> list[sqlite3.Row]:
    if stream_id is None:
        return conn.execute(
            "SELECT * FROM probe_result ORDER BY checked_at DESC, id DESC"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM probe_result WHERE stream_id = ? ORDER BY checked_at DESC, id DESC",
        (stream_id,),
    ).fetchall()


# ----------------------------------------------------------------- summary

def counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "source",
        "source_channel",
        "canonical_channel",
        "channel_binding",
        "stream",
        "stream_source",
        "probe",
        "probe_result",
    )
    return {
        table: int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
        for table in tables
    }
