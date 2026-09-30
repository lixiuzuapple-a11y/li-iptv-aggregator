"""QA-001 永久回归测试：数据身份与跨频道关联（TASK-001 Review-01 返工）。

覆盖三类必须永久防回归的场景：
  1. 两个不同来源、两个不同 canonical、相同 URL → 各自独立 stream，来源不错链；
  2. 同一来源内两条不同原始频道共享 URL → 身份不被合并，重复导入仍幂等；
  3. 同一 source_channel 被请求绑定第二个 canonical → 默认拒绝；仅显式 rebind 才迁移；
另附：同一 canonical 多来源相同 URL 仍只产生一条 stream（确保修复没有反向破坏）。
"""

from __future__ import annotations

import json

import pytest

from liptv import m3u, repo
from liptv import select as select_mod
from liptv.cli import main as cli_main
from liptv.repo import BindingConflictError
from tests.conftest import NOW

SHARED_URL = "http://shared.example/live.m3u8"


def _entry(name: str, url: str, group: str | None = None, tvg_id: str | None = None) -> m3u.M3UEntry:
    return m3u.M3UEntry(name=name, url=url, group_title=group, tvg_id=tvg_id)


def _import_entries(conn, source_name, entries, now=NOW):
    source_id = repo.add_source(conn, source_name, "local-m3u", now=now)
    for entry in entries:
        repo.upsert_source_channel(conn, source_id, entry, now=now)
    return source_id


def _only_source_channel(conn, source_id) -> int:
    rows = repo.list_source_channels(conn, source_id)
    assert len(rows) == 1
    return int(rows[0]["id"])


# --------------------------------------------------------------------- 场景 1

def test_same_url_different_canonical_keeps_independent_streams(conn):
    """复现大G QA-001 问题一：同 URL 跨 canonical 不得错配身份或污染来源。"""
    sid_a = _import_entries(conn, "src-a", [_entry("频道甲", SHARED_URL, "新闻")])
    sid_b = _import_entries(conn, "src-b", [_entry("频道乙", SHARED_URL, "体育")])
    sc_a = _only_source_channel(conn, sid_a)
    sc_b = _only_source_channel(conn, sid_b)

    ca = repo.add_canonical_channel(conn, "频道甲", category="新闻", now=NOW)
    cb = repo.add_canonical_channel(conn, "频道乙", category="体育", now=NOW)
    repo.bind_source_channel(conn, sc_a, ca, now=NOW)
    repo.bind_source_channel(conn, sc_b, cb, now=NOW)

    stats = repo.sync_streams(conn, now=NOW)
    assert stats["streams_created"] == 2  # 每个 canonical 一条独立 stream（修复前是 1）

    streams_a = repo.list_streams(conn, ca)
    streams_b = repo.list_streams(conn, cb)
    assert [s["url"] for s in streams_a] == [SHARED_URL]
    assert [s["url"] for s in streams_b] == [SHARED_URL]
    assert int(streams_a[0]["id"]) != int(streams_b[0]["id"])

    # 每个来源只出现在自己 canonical 的 stream 上 —— 无跨频道错链
    links_a = repo.list_stream_sources(conn, int(streams_a[0]["id"]))
    links_b = repo.list_stream_sources(conn, int(streams_b[0]["id"]))
    assert [int(l["source_channel_id"]) for l in links_a] == [sc_a]
    assert [int(l["source_channel_id"]) for l in links_b] == [sc_b]
    assert sc_b not in {int(l["source_channel_id"]) for l in links_a}
    assert sc_a not in {int(l["source_channel_id"]) for l in links_b}


# --------------------------------------------------------------------- 场景 2

def test_same_source_two_raw_channels_share_url_keep_identity(conn):
    """复现 QA-001 问题二：同来源内名称不同、URL 相同的条目不得被过早合并。"""
    entries = [_entry("甲频道", SHARED_URL, "新闻"), _entry("乙频道", SHARED_URL, "体育")]
    sid = _import_entries(conn, "src-x", entries)

    rows = repo.list_source_channels(conn, sid)
    assert len(rows) == 2
    assert {r["raw_name"] for r in rows} == {"甲频道", "乙频道"}
    assert {r["raw_stream_url"] for r in rows} == {SHARED_URL}
    assert len({r["identity_hash"] for r in rows}) == 2  # 复合身份不同，未被 URL 合并

    # 重复导入必须幂等：不新增行、不改变 id
    ids_before = sorted(int(r["id"]) for r in rows)
    _import_entries(conn, "src-x", entries)
    rows_again = repo.list_source_channels(conn, sid)
    assert len(rows_again) == 2
    assert sorted(int(r["id"]) for r in rows_again) == ids_before


def test_same_source_identical_entry_reimport_merges(conn):
    """同一来源、完全相同的条目 → 归并到同一行（幂等），并刷新 last_seen_at。"""
    sid = _import_entries(conn, "src-y", [_entry("甲", SHARED_URL, "新闻")], now="2026-09-01T00:00:00+00:00")
    first_id = _only_source_channel(conn, sid)
    _import_entries(conn, "src-y", [_entry("甲", SHARED_URL, "新闻")], now=NOW)

    rows = repo.list_source_channels(conn, sid)
    assert len(rows) == 1
    assert int(rows[0]["id"]) == first_id
    assert rows[0]["last_seen_at"] == NOW
    assert rows[0]["first_seen_at"] == "2026-09-01T00:00:00+00:00"


# --------------------------------------------------------------------- 场景 3

def test_source_channel_rejects_second_canonical_binding(conn):
    """复现 QA-001 问题三：一条 source_channel 只能属于一个 canonical（默认拒绝改绑）。"""
    sid = _import_entries(conn, "src-a", [_entry("甲", SHARED_URL)])
    sc = _only_source_channel(conn, sid)
    ca = repo.add_canonical_channel(conn, "甲", now=NOW)
    cb = repo.add_canonical_channel(conn, "乙", now=NOW)

    repo.bind_source_channel(conn, sc, ca, now=NOW)
    with pytest.raises(BindingConflictError):
        repo.bind_source_channel(conn, sc, cb, now=NOW)

    bindings = conn.execute(
        "SELECT id, canonical_channel_id FROM channel_binding WHERE source_channel_id = ?", (sc,)
    ).fetchall()
    assert len(bindings) == 1
    assert int(bindings[0]["canonical_channel_id"]) == ca


def test_source_channel_explicit_rebind_is_auditable(conn):
    """显式 rebind 才允许迁移，且迁移后仍只有一条绑定（可审计的 old→new）。"""
    sid = _import_entries(conn, "src-a", [_entry("甲", SHARED_URL)])
    sc = _only_source_channel(conn, sid)
    ca = repo.add_canonical_channel(conn, "甲", now=NOW)
    cb = repo.add_canonical_channel(conn, "乙", now=NOW)

    repo.bind_source_channel(conn, sc, ca, now=NOW)
    binding_id, created = repo.bind_source_channel(conn, sc, cb, rebind=True, now=NOW)
    assert created is True
    assert int(repo.get_binding(conn, sc)["canonical_channel_id"]) == cb

    rows = conn.execute(
        "SELECT id FROM channel_binding WHERE source_channel_id = ?", (sc,)
    ).fetchall()
    assert len(rows) == 1
    assert int(rows[0]["id"]) == binding_id


def test_rebind_then_sync_removes_cross_channel_link(conn):
    """rebind 后重新归集：旧 canonical 不再挂该来源，跨频道错链被清除且标 stale。"""
    sid = _import_entries(conn, "src-a", [_entry("甲", SHARED_URL)])
    sc = _only_source_channel(conn, sid)
    ca = repo.add_canonical_channel(conn, "甲", now=NOW)
    cb = repo.add_canonical_channel(conn, "乙", now=NOW)

    repo.bind_source_channel(conn, sc, ca, now=NOW)
    repo.sync_streams(conn, now=NOW)
    old_stream = int(repo.list_streams(conn, ca)[0]["id"])
    assert [int(l["source_channel_id"]) for l in repo.list_stream_sources(conn, old_stream)] == [sc]

    repo.bind_source_channel(conn, sc, cb, rebind=True, now=NOW)
    stats = repo.sync_streams(conn, now=NOW)
    assert stats["links_removed"] == 1
    assert stats["streams_marked_stale"] == 1

    # 新 canonical 独立持有该来源与 stream（新 id）
    streams_b = repo.list_streams(conn, cb)
    assert len(streams_b) == 1
    assert int(streams_b[0]["id"]) != old_stream
    assert [int(l["source_channel_id"]) for l in repo.list_stream_sources(conn, int(streams_b[0]["id"]))] == [sc]

    # 旧 canonical 的 stream 已无跨频道来源，且被标记 stale
    assert repo.list_stream_sources(conn, old_stream) == []
    assert conn.execute("SELECT status FROM stream WHERE id = ?", (old_stream,)).fetchone()["status"] == "stale"


def test_stale_stream_is_excluded_from_selection(conn):
    """已被清空 provenance 的 stream 不得再被选中（即使有历史成功探针）。"""
    sid = _import_entries(conn, "src-a", [_entry("甲", SHARED_URL)])
    sc = _only_source_channel(conn, sid)
    ca = repo.add_canonical_channel(conn, "甲", now=NOW)
    cb = repo.add_canonical_channel(conn, "乙", now=NOW)

    repo.bind_source_channel(conn, sc, ca, now=NOW)
    repo.sync_streams(conn, now=NOW)
    old_stream = int(repo.list_streams(conn, ca)[0]["id"])
    probe_id = repo.ensure_probe(conn, "windows-local")
    repo.add_probe_result(
        conn, stream_id=old_stream, probe_id=probe_id, success=True, checked_at=NOW, startup_ms=200
    )
    assert select_mod.select_best_stream(conn, ca, now=NOW) is not None

    repo.bind_source_channel(conn, sc, cb, rebind=True, now=NOW)
    repo.sync_streams(conn, now=NOW)
    assert select_mod.select_best_stream(conn, ca, now=NOW) is None


# ------------------------------------------- 反向保护：同 canonical 多来源同 URL

def test_same_canonical_multiple_sources_same_url_single_stream(conn):
    """修复不得反向破坏：同一 canonical 下多来源提供相同 URL 仍只有一条 stream。"""
    sid_a = _import_entries(conn, "src-a", [_entry("五星体育", SHARED_URL, "体育")])
    sid_b = _import_entries(conn, "src-b", [_entry("五星体育高清", SHARED_URL, "体育")])
    sc_a = _only_source_channel(conn, sid_a)
    sc_b = _only_source_channel(conn, sid_b)

    canonical = repo.add_canonical_channel(conn, "五星体育", category="体育", now=NOW)
    repo.bind_source_channel(conn, sc_a, canonical, now=NOW)
    repo.bind_source_channel(conn, sc_b, canonical, now=NOW)

    stats = repo.sync_streams(conn, now=NOW)
    assert stats["streams_created"] == 1

    streams = repo.list_streams(conn, canonical)
    assert len(streams) == 1
    links = repo.list_stream_sources(conn, int(streams[0]["id"]))
    assert {int(l["source_channel_id"]) for l in links} == {sc_a, sc_b}


# ------------------------------------------------------------- CLI 端到端（审计）

def test_cli_binding_conflict_then_explicit_rebind(capsys, db_path, tmp_path):
    db = str(db_path)
    cli_main(["init-db", "--db", db, "--json"])
    capsys.readouterr()

    m3u_path = tmp_path / "src.m3u"
    m3u_path.write_text("#EXTM3U\n#EXTINF:-1,甲频道\n" + SHARED_URL + "\n", encoding="utf-8")
    cli_main(["import-m3u", str(m3u_path), "--source", "src-a", "--db", db, "--now", NOW, "--json"])
    cli_main(["canonical-add", "--name", "甲", "--db", db, "--now", NOW, "--json"])
    cli_main(["canonical-add", "--name", "乙", "--db", db, "--now", NOW, "--json"])
    cli_main(["binding-add", "--source-channel-id", "1", "--canonical-id", "1",
              "--db", db, "--now", NOW, "--json"])
    capsys.readouterr()

    # 静默改绑 → 必须拒绝（非 0 退出）
    with pytest.raises(SystemExit):
        cli_main(["binding-add", "--source-channel-id", "1", "--canonical-id", "2",
                  "--db", db, "--now", NOW, "--json"])
    capsys.readouterr()

    # 显式 rebind → 成功且输出 old→new
    code = cli_main(["binding-add", "--source-channel-id", "1", "--canonical-id", "2", "--rebind",
                     "--db", db, "--now", NOW, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["rebound_from"] == 1
    assert payload["canonical_channel_id"] == 2
