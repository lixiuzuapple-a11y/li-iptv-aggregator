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
    enabled: int | None = None,
    now: str | None = None,
) -> int:
    """新增或复用同名 source，返回 source.id。

    `enabled=None` 表示不动既有来源的启用状态（新建时为 1）；
    显式传 0/1 才会改写 —— 用于来源注册命令。
    """
    stamp = _now(now)
    row = conn.execute("SELECT id FROM source WHERE name = ?", (name,)).fetchone()
    if row is not None:
        if enabled is None:
            conn.execute(
                "UPDATE source SET kind = ?, url = COALESCE(?, url), updated_at = ? WHERE id = ?",
                (kind, url, stamp, row["id"]),
            )
        else:
            conn.execute(
                "UPDATE source SET kind = ?, url = COALESCE(?, url), enabled = ?, updated_at = ? "
                "WHERE id = ?",
                (kind, url, int(enabled), stamp, row["id"]),
            )
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO source (name, kind, url, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, kind, url, 1 if enabled is None else int(enabled), stamp, stamp),
    )
    return int(cur.lastrowid)


def get_source(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM source WHERE id = ?", (source_id,)).fetchone()


def get_source_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM source WHERE name = ?", (name,)).fetchone()


def resolve_source(conn: sqlite3.Connection, token: str | int) -> sqlite3.Row | None:
    """把 CLI 传入的 `--source` 解析成来源行：先按 id（纯数字），再按名称。"""
    text = str(token).strip()
    if text.isdigit():
        row = get_source(conn, int(text))
        if row is not None:
            return row
    return get_source_by_name(conn, text)


def list_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM source ORDER BY id").fetchall()


def list_sources_by_kind(
    conn: sqlite3.Connection, kind: str, *, enabled_only: bool = False
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM source WHERE kind = ?"
    if enabled_only:
        sql += " AND enabled = 1"
    return conn.execute(sql + " ORDER BY id", (kind,)).fetchall()


def set_source_enabled(
    conn: sqlite3.Connection, source_id: int, enabled: bool, *, now: str | None = None
) -> None:
    conn.execute(
        "UPDATE source SET enabled = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, _now(now), source_id),
    )


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


# ------------------------------------------------- 来源快照（TASK-002 fetch）

def deactivate_missing_source_channels(
    conn: sqlite3.Connection,
    source_id: int,
    keep_ids: list[int],
) -> int:
    """把本来源中「本次快照未出现」的条目置 active=0。

    约束（TASK-002）：
      * 只碰**这一个来源**的行，不影响其他来源的条目/绑定/stream/probe 历史；
      * 只置 active，**绝不硬删**，也不改 last_seen_at（保留最后一次见到的时间）；
      * 重复出现时由 upsert_source_channel 恢复 active=1，first_seen_at 与绑定仍在。
    """
    if keep_ids:
        marks = ",".join("?" for _ in keep_ids)
        cur = conn.execute(
            f"UPDATE source_channel SET active = 0 "
            f"WHERE source_id = ? AND active = 1 AND id NOT IN ({marks})",
            (source_id, *keep_ids),
        )
    else:
        cur = conn.execute(
            "UPDATE source_channel SET active = 0 WHERE source_id = ? AND active = 1",
            (source_id,),
        )
    return int(cur.rowcount or 0)


def apply_source_snapshot(
    conn: sqlite3.Connection,
    source_id: int,
    entries,
    *,
    now: str | None = None,
) -> dict[str, int]:
    """把一次抓取到的 M3U 快照应用到**单一来源**。

    返回 created / updated / reactivated / deactivated 计数。
    本函数不做事务控制 —— 事务边界由调用方（ingest.ingest_fixed_source）持有，
    保证「库存变更 + fetch 状态」同生共死。
    """
    stamp = _now(now)
    created = updated = reactivated = 0
    seen_ids: list[int] = []

    for entry in entries:
        identity = source_channel_identity_hash(entry.name, entry.group_title, entry.url)
        previous = conn.execute(
            "SELECT id, active FROM source_channel WHERE source_id = ? AND identity_hash = ?",
            (source_id, identity),
        ).fetchone()

        channel_id, is_new = upsert_source_channel(conn, source_id, entry, now=stamp)
        seen_ids.append(channel_id)
        if is_new:
            created += 1
        else:
            updated += 1
            if previous is not None and int(previous["active"]) == 0:
                reactivated += 1

    deactivated = deactivate_missing_source_channels(conn, source_id, seen_ids)
    return {
        "created": created,
        "updated": updated,
        "reactivated": reactivated,
        "deactivated": deactivated,
    }


def source_channel_stats(conn: sqlite3.Connection) -> dict[int, dict[str, int]]:
    """每个来源的条目统计：active / inactive / total。"""
    rows = conn.execute(
        "SELECT source_id, "
        "SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active_count, "
        "SUM(CASE WHEN active = 0 THEN 1 ELSE 0 END) AS inactive_count, "
        "COUNT(*) AS total_count "
        "FROM source_channel GROUP BY source_id"
    ).fetchall()
    return {
        int(r["source_id"]): {
            "active": int(r["active_count"] or 0),
            "inactive": int(r["inactive_count"] or 0),
            "total": int(r["total_count"] or 0),
        }
        for r in rows
    }


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


# ------------------------------------------------------ 可测活固定库存（TASK-005）
#
# 这是「哪些 stream 允许被真实 ffprobe 探测」的**唯一**查询口径，CLI 与 scheduler
# 都调用它，不另造第二套身份/库存判断。
#
# 与 ingest.KIND_DYNAMIC 同值：**不能用模块级 import**（ingest 反过来 import 本模块，
# 会造成循环导入），因此这里独立声明，并由 tests/test_probe.py 断言两者恒等。

#: 绝不进入 probe 的来源类别：动态赛事临时 URL 只做短时预览，不落库也不测活。
PROBE_EXCLUDED_SOURCE_KIND = "dynamic_event_m3u"

_PROBE_CANDIDATE_SQL = (
    "SELECT s.*, cc.name AS canonical_name FROM stream s "
    "JOIN canonical_channel cc ON cc.id = s.canonical_channel_id "
    "WHERE s.enabled = 1 "
    "  AND s.status <> 'stale' "
    "  AND EXISTS ("
    "    SELECT 1 FROM stream_source ss "
    "    JOIN source_channel sc ON sc.id = ss.source_channel_id "
    "    JOIN source src ON src.id = sc.source_id "
    "    WHERE ss.stream_id = s.id "
    "      AND sc.active = 1 "
    "      AND src.kind <> ?"
    "  )"
)


def list_probe_candidates(
    conn: sqlite3.Connection,
    *,
    stream_id: int | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """返回允许被真实测活探测的固定库存 stream。

    口径（TASK-005 §1）：
      * 只测 ``stream.enabled = 1`` 的长期固定库存；
      * ``status = 'stale'``（已失去全部来源）与 disable 的 stream 默认不测；
      * 必须存在**至少一条** ``active = 1`` 的 source_channel provenance
        —— 「orphan stream」（没有任何 active 来源）因此被排除；
      * provenance 的来源类别**不能**是 ``dynamic_event_m3u``：
        动态赛事临时 URL 绝不进入 probe_result；
      * 只读，不创建 canonical / binding / stream。
    """
    sql = _PROBE_CANDIDATE_SQL
    params: list = [PROBE_EXCLUDED_SOURCE_KIND]
    if stream_id is not None:
        sql += " AND s.id = ?"
        params.append(int(stream_id))
    sql += " ORDER BY s.id"
    if limit is not None and int(limit) > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, tuple(params)).fetchall()


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
