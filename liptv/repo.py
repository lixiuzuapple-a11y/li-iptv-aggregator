"""数据访问层：对 8 张业务表的最小 CRUD / 归集操作。

CLI 与测试都调用这里，保证行为单一来源。
"""

from __future__ import annotations

import sqlite3

from .util import sha256_hex, utcnow_iso


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

def upsert_source_channel(
    conn: sqlite3.Connection,
    source_id: int,
    entry,
    *,
    now: str | None = None,
) -> tuple[int, bool]:
    """按 (source_id, raw_stream_url) 归并原始条目。

    返回 (source_channel_id, created)。
    """
    stamp = _now(now)
    row = conn.execute(
        "SELECT id FROM source_channel WHERE source_id = ? AND raw_stream_url = ?",
        (source_id, entry.url),
    ).fetchone()

    if row is not None:
        conn.execute(
            "UPDATE source_channel SET external_id = ?, raw_name = ?, raw_group = ?, "
            "raw_logo = ?, raw_epg_id = ?, last_seen_at = ?, active = 1 WHERE id = ?",
            (
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
        "INSERT INTO source_channel (source_id, external_id, raw_name, raw_group, raw_logo, "
        "raw_epg_id, raw_stream_url, first_seen_at, last_seen_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (
            source_id,
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

def bind_source_channel(
    conn: sqlite3.Connection,
    source_channel_id: int,
    canonical_channel_id: int,
    *,
    method: str = "manual",
    confidence: float = 1.0,
    now: str | None = None,
) -> tuple[int, bool]:
    """建立 source_channel → canonical_channel 绑定（幂等）。"""
    stamp = _now(now)
    row = conn.execute(
        "SELECT id FROM channel_binding WHERE source_channel_id = ? AND canonical_channel_id = ?",
        (source_channel_id, canonical_channel_id),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE channel_binding SET method = ?, confidence = ? WHERE id = ?",
            (method, confidence, row["id"]),
        )
        return int(row["id"]), False

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

    * URL 去重（url_hash 唯一）；
    * 同一条 stream 可挂多个来源（stream_source）；
    * 已存在的 stream 只更新 last_seen_at / status。
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
    }

    for row in rows:
        cid, scid, url = int(row["cid"]), int(row["scid"]), row["url"]
        url_hash = sha256_hex(url)

        existing = conn.execute("SELECT id, status FROM stream WHERE url_hash = ?", (url_hash,)).fetchone()
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
