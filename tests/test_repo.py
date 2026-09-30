"""数据访问层测试：导入 / 绑定 / 归集 / 测活记录。"""

from __future__ import annotations

from liptv import m3u, repo
from tests.conftest import NOW


def _import(conn, path, source_name, now=NOW):
    parsed = m3u.parse_file(path)
    source_id = repo.add_source(conn, source_name, "local-m3u", now=now)
    for entry in parsed.entries:
        repo.upsert_source_channel(conn, source_id, entry, now=now)
    repo.mark_source_fetched(conn, source_id, "ok", now=now)
    return source_id, parsed


def _channel_id(conn, source_id, raw_name):
    rows = repo.list_source_channels(conn, source_id)
    return next(int(r["id"]) for r in rows if r["raw_name"] == raw_name)


def test_import_creates_source_and_raw_channels(conn, source_a):
    source_id, parsed = _import(conn, source_a, "src-a")
    assert parsed.entry_count == 3
    rows = repo.list_source_channels(conn, source_id)
    assert len(rows) == 3
    assert {r["raw_name"] for r in rows} == {"CCTV-1 综合", "五星体育", "CCTV 电视剧, 高清"}
    assert repo.list_sources(conn)[0]["last_fetch_status"] == "ok"


def test_reimport_is_idempotent(conn, source_a):
    _import(conn, source_a, "src-a")
    _import(conn, source_a, "src-a")
    assert len(repo.list_source_channels(conn)) == 3


def test_reimport_updates_last_seen(conn, source_a):
    source_id, _ = _import(conn, source_a, "src-a", now="2026-09-01T00:00:00+00:00")
    _import(conn, source_a, "src-a", now=NOW)
    rows = repo.list_source_channels(conn, source_id)
    assert all(r["last_seen_at"] == NOW for r in rows)
    assert all(r["first_seen_at"] == "2026-09-01T00:00:00+00:00" for r in rows)


def test_two_source_channels_bind_to_one_canonical(conn, source_a, source_b):
    """验收 3：两个 source_channel → 一个 canonical_channel。"""
    sid_a, _ = _import(conn, source_a, "src-a")
    sid_b, _ = _import(conn, source_b, "src-b")
    canonical_id = repo.add_canonical_channel(conn, "CCTV-1 综合", category="新闻", now=NOW)

    sc_a = _channel_id(conn, sid_a, "CCTV-1 综合")
    sc_b = _channel_id(conn, sid_b, "CCTV1 高清")

    _, created_a = repo.bind_source_channel(conn, sc_a, canonical_id, now=NOW)
    _, created_b = repo.bind_source_channel(conn, sc_b, canonical_id, now=NOW)
    assert created_a and created_b

    bindings = repo.list_bindings(conn, canonical_id)
    assert len(bindings) == 2
    assert {b["source_channel_id"] for b in bindings} == {sc_a, sc_b}


def test_binding_is_idempotent(conn, source_a):
    sid_a, _ = _import(conn, source_a, "src-a")
    canonical_id = repo.add_canonical_channel(conn, "CCTV-1 综合", now=NOW)
    sc_a = _channel_id(conn, sid_a, "CCTV-1 综合")

    repo.bind_source_channel(conn, sc_a, canonical_id, now=NOW)
    binding_id, created = repo.bind_source_channel(conn, sc_a, canonical_id, now=NOW)
    assert created is False
    assert len(repo.list_bindings(conn, canonical_id)) == 1
    assert binding_id > 0


def test_one_canonical_gets_multiple_streams(conn, source_a, source_b):
    """验收 4：一个 canonical_channel 保存多条 stream。"""
    sid_a, _ = _import(conn, source_a, "src-a")
    sid_b, _ = _import(conn, source_b, "src-b")
    canonical_id = repo.add_canonical_channel(conn, "CCTV-1 综合", category="新闻", now=NOW)
    repo.bind_source_channel(conn, _channel_id(conn, sid_a, "CCTV-1 综合"), canonical_id, now=NOW)
    repo.bind_source_channel(conn, _channel_id(conn, sid_b, "CCTV1 高清"), canonical_id, now=NOW)

    stats = repo.sync_streams(conn, now=NOW)
    assert stats["streams_created"] == 2

    streams = repo.list_streams(conn, canonical_id)
    assert len(streams) == 2
    assert {s["url"] for s in streams} == {
        "http://src-a.example/cctv1/index.m3u8",
        "http://src-b.example/cctv1/hd.m3u8",
    }
    assert all(s["canonical_channel_id"] == canonical_id for s in streams)


def test_one_stream_keeps_multiple_provenances(conn, source_a, source_b):
    """停止条件之一：一 stream 多来源（同一 URL 被两个来源提供）。"""
    sid_a, _ = _import(conn, source_a, "src-a")
    sid_b, _ = _import(conn, source_b, "src-b")
    canonical_id = repo.add_canonical_channel(conn, "五星体育", category="体育", now=NOW)
    repo.bind_source_channel(conn, _channel_id(conn, sid_a, "五星体育"), canonical_id, now=NOW)
    repo.bind_source_channel(conn, _channel_id(conn, sid_b, "五星体育高清"), canonical_id, now=NOW)

    stats = repo.sync_streams(conn, now=NOW)
    assert stats["streams_created"] == 1

    streams = repo.list_streams(conn, canonical_id)
    assert len(streams) == 1
    links = repo.list_stream_sources(conn, int(streams[0]["id"]))
    assert len(links) == 2
    assert {link["raw_name"] for link in links} == {"五星体育", "五星体育高清"}


def test_stream_sync_is_idempotent(conn, source_a):
    sid_a, _ = _import(conn, source_a, "src-a")
    canonical_id = repo.add_canonical_channel(conn, "CCTV-1 综合", now=NOW)
    repo.bind_source_channel(conn, _channel_id(conn, sid_a, "CCTV-1 综合"), canonical_id, now=NOW)

    first = repo.sync_streams(conn, now=NOW)
    second = repo.sync_streams(conn, now=NOW)
    assert first["streams_created"] == 1
    assert second["streams_created"] == 0
    assert second["streams_updated"] == 1
    assert len(repo.list_streams(conn, canonical_id)) == 1


def test_probe_result_insert_and_list(conn, source_a):
    sid_a, _ = _import(conn, source_a, "src-a")
    canonical_id = repo.add_canonical_channel(conn, "CCTV-1 综合", now=NOW)
    repo.bind_source_channel(conn, _channel_id(conn, sid_a, "CCTV-1 综合"), canonical_id, now=NOW)
    repo.sync_streams(conn, now=NOW)
    stream_id = int(repo.list_streams(conn, canonical_id)[0]["id"])

    probe_id = repo.ensure_probe(conn, "windows-local")
    repo.add_probe_result(
        conn, stream_id=stream_id, probe_id=probe_id, success=True, checked_at=NOW,
        http_status=200, startup_ms=800, resolution_width=1920, resolution_height=1080,
        bitrate_kbps=6000, protocol="hls", ipv_family="ipv4",
    )
    repo.add_probe_result(
        conn, stream_id=stream_id, probe_id=probe_id, success=False,
        checked_at="2026-09-30T12:05:00+00:00", error_type="timeout",
    )
    rows = repo.list_probe_results(conn, stream_id)
    assert len(rows) == 2
    assert rows[0]["success"] == 0  # 按时间倒序
    assert rows[1]["resolution_width"] == 1920


def test_counts_reports_all_v1_tables(conn, source_a):
    sid_a, _ = _import(conn, source_a, "src-a")
    canonical_id = repo.add_canonical_channel(conn, "CCTV-1 综合", now=NOW)
    repo.bind_source_channel(conn, _channel_id(conn, sid_a, "CCTV-1 综合"), canonical_id, now=NOW)
    repo.sync_streams(conn, now=NOW)

    result = repo.counts(conn)
    assert result["source"] == 1
    assert result["source_channel"] == 3
    assert result["canonical_channel"] == 1
    assert result["channel_binding"] == 1
    assert result["stream"] == 1
    assert result["stream_source"] == 1
    assert result["probe"] == 2
    assert result["probe_result"] == 0
