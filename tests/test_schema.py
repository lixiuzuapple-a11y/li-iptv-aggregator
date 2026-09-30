"""schema 与数据模型约束测试。"""

from __future__ import annotations

import sqlite3

import pytest

from liptv import db as db_mod
from liptv import repo
from liptv.util import sha256_hex

EXPECTED_TABLES = [
    "canonical_channel",
    "channel_binding",
    "probe",
    "probe_result",
    "schema_version",
    "source",
    "source_channel",
    "stream",
    "stream_source",
]


def test_init_db_is_idempotent_and_versioned(conn):
    assert db_mod.read_schema_version(conn) == 1
    db_mod.init_db(conn)  # 再跑一次不应报错
    assert db_mod.read_schema_version(conn) == 1
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    assert len(rows) == 1


def test_all_expected_tables_exist(conn):
    assert db_mod.table_names(conn) == EXPECTED_TABLES


def test_v1_seed_probes(conn):
    names = [row["name"] for row in repo.list_probes(conn)]
    assert names == ["shanghai-cloud", "windows-local"]


@pytest.mark.parametrize(
    "table",
    ["source", "source_channel", "canonical_channel", "channel_binding",
     "stream", "stream_source", "probe", "probe_result"],
)
def test_primary_key_is_internal_id_only(conn, table):
    """验收 9 / 12：主键只能是内部整型 id，不能是频道名 / URL / tvg-id。"""
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    pk_columns = [row["name"] for row in info if row["pk"]]
    assert pk_columns == ["id"]
    id_row = next(row for row in info if row["name"] == "id")
    assert id_row["type"].upper() == "INTEGER"


def test_no_unique_constraint_on_names_or_urls(conn):
    """频道名 / URL 只做去重辅助，不作主键。"""
    canonical = conn.execute("PRAGMA index_list(canonical_channel)").fetchall()
    assert all(not row["unique"] for row in canonical)

    source_channel_info = conn.execute("PRAGMA table_info(source_channel)").fetchall()
    assert [r["name"] for r in source_channel_info if r["pk"]] == ["id"]


def test_stream_url_hash_is_scoped_to_canonical(conn):
    """Review-01：URL 去重只在 canonical 作用域内 —— 跨 canonical 相同 URL 各为独立 stream。"""
    cid1 = repo.add_canonical_channel(conn, "甲频道")
    cid2 = repo.add_canonical_channel(conn, "乙频道")
    url = "http://x.example/a.m3u8"
    url_hash = sha256_hex(url)
    insert = (
        "INSERT INTO stream (canonical_channel_id, url, url_hash, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, 't', 't')"
    )

    conn.execute(insert, (cid1, url, url_hash))
    # 同一 canonical 内重复 URL → 拒绝
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (cid1, url, url_hash))
    # 不同 canonical 相同 URL → 允许（各自独立身份）
    conn.execute(insert, (cid2, url, url_hash))
    assert conn.execute("SELECT COUNT(*) AS c FROM stream").fetchone()["c"] == 2


def test_review01_identity_indexes_present(conn):
    """新增/替换的唯一约束必须就位（身份完整性兜底）。"""
    sc_indexes = {row["name"] for row in conn.execute("PRAGMA index_list(source_channel)")}
    assert "ux_source_channel_identity" in sc_indexes
    assert "ux_source_channel_source_url" not in sc_indexes

    binding_indexes = {row["name"]: row for row in conn.execute("PRAGMA index_list(channel_binding)")}
    assert binding_indexes["ux_channel_binding_source"]["unique"] == 1
    assert "ux_channel_binding_pair" not in binding_indexes

    stream_indexes = {row["name"]: row for row in conn.execute("PRAGMA index_list(stream)")}
    assert stream_indexes["ux_stream_canonical_url"]["unique"] == 1


def test_probe_result_keeps_history_across_probes_and_time(conn):
    """停止条件之一：stream × probe × time 必须能留多条记录。"""
    cid = repo.add_canonical_channel(conn, "历史频道")
    sid = repo.add_source(conn, "src", "local-m3u")
    entry = type("E", (), {"name": "n", "url": "http://h.example/s.m3u8",
                           "tvg_id": None, "tvg_logo": None, "group_title": None})()
    sc_id, _ = repo.upsert_source_channel(conn, sid, entry)
    repo.bind_source_channel(conn, sc_id, cid)
    repo.sync_streams(conn)
    stream_id = repo.list_streams(conn, cid)[0]["id"]

    probes = {row["name"]: int(row["id"]) for row in repo.list_probes(conn)}
    for probe_name in probes:
        for minute in (0, 10):
            repo.add_probe_result(
                conn, stream_id=stream_id, probe_id=probes[probe_name], success=True,
                checked_at=f"2026-09-30T12:{minute:02d}:00+00:00", startup_ms=500,
            )
    rows = repo.list_probe_results(conn, stream_id)
    assert len(rows) == 4


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO channel_binding (source_channel_id, canonical_channel_id, method, "
            "confidence, created_at) VALUES (9999, 9999, 'manual', 1.0, 't')"
        )
