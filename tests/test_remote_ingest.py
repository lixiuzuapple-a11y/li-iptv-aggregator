"""fixed_m3u 远程获取、校验与来源生命周期测试（TASK-002 验收门槛 1/2/3）。

全部用本机 mock 服务，不依赖公网。
"""

from __future__ import annotations

import json

import pytest

from liptv import db as db_mod
from liptv import fetch as fetch_mod
from liptv import ingest
from liptv import repo
from liptv.cli import main as cli_main

LIMITS = fetch_mod.FetchLimits(
    timeout_seconds=5.0, max_bytes=2_000_000, max_redirects=3
)
NOW = "2026-09-30T12:00:00+00:00"
LATER = "2026-09-30T15:00:00+00:00"


# ------------------------------------------------------------------ 工具函数

def _add_fixed(conn, name: str, url: str) -> int:
    sid = repo.add_source(conn, name, ingest.KIND_FIXED, url, now=NOW)
    conn.commit()
    return sid


def _fetch(conn, sid: int, *, now: str = NOW) -> dict:
    return ingest.ingest_fixed_source(conn, repo.get_source(conn, sid), limits=LIMITS, now=now)


def _channels(conn, sid: int) -> dict[str, dict]:
    return {r["raw_name"]: dict(r) for r in repo.list_source_channels(conn, sid)}


# ------------------------------------------------------- fixed 生命周期

def test_fixed_first_fetch_creates_channels(conn, mock_server):
    server, base = mock_server
    server.state.set_ok()
    sid = _add_fixed(conn, "mock-fixed", f"{base}/seq.m3u")

    result = _fetch(conn, sid)

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["entries"] == 3
    assert result["created"] == 3
    assert result["updated"] == 0
    assert result["deactivated"] == 0
    assert result["http_status"] == 200

    rows = repo.list_source_channels(conn, sid)
    assert len(rows) == 3
    assert all(int(r["active"]) == 1 for r in rows)
    assert all(r["first_seen_at"] == NOW for r in rows)

    source = repo.get_source(conn, sid)
    assert source["last_fetch_status"] == "ok"
    assert source["last_fetch_at"] == NOW


def test_fixed_repeat_fetch_is_idempotent(conn, mock_server):
    server, base = mock_server
    server.state.set_ok()
    sid = _add_fixed(conn, "mock-fixed", f"{base}/seq.m3u")
    _fetch(conn, sid)
    first_ids = {r["raw_name"]: (int(r["id"]), r["first_seen_at"])
                 for r in repo.list_source_channels(conn, sid)}

    second = _fetch(conn, sid, now=LATER)

    assert second["created"] == 0
    assert second["updated"] == 3
    assert second["deactivated"] == 0
    rows = repo.list_source_channels(conn, sid)
    assert len(rows) == 3  # 没有产生重复行
    for row in rows:
        cid, first_seen = first_ids[row["raw_name"]]
        assert int(row["id"]) == cid            # 同一行
        assert row["first_seen_at"] == first_seen  # 首次发现时间保留
        assert row["last_seen_at"] == LATER     # 最近见到时间更新
        assert int(row["active"]) == 1


def test_fixed_disappeared_becomes_inactive_not_deleted(conn, mock_server):
    server, base = mock_server
    server.state.set_ok()
    sid = _add_fixed(conn, "mock-fixed", f"{base}/seq.m3u")
    _fetch(conn, sid)
    before = _channels(conn, sid)
    doc_id = int(before["演示纪录台"]["id"])

    server.state.set_changed()
    result = _fetch(conn, sid, now=LATER)

    assert result["ok"] is True
    assert result["created"] == 1        # 演示音乐台
    assert result["updated"] == 2        # 新闻 / 体育
    assert result["deactivated"] == 1    # 演示纪录台

    after = _channels(conn, sid)
    assert set(after) == {"演示新闻台", "演示体育台", "演示纪录台", "演示音乐台"}
    # 没有被硬删，只是 active=0，且 first_seen_at 保留
    assert int(after["演示纪录台"]["id"]) == doc_id
    assert int(after["演示纪录台"]["active"]) == 0
    assert after["演示纪录台"]["first_seen_at"] == NOW
    assert int(after["演示音乐台"]["active"]) == 1


def test_fixed_reappeared_restores_identity_and_active(conn, mock_server):
    server, base = mock_server
    server.state.set_ok()
    sid = _add_fixed(conn, "mock-fixed", f"{base}/seq.m3u")
    _fetch(conn, sid)
    doc_id = int(_channels(conn, sid)["演示纪录台"]["id"])

    server.state.set_changed()
    _fetch(conn, sid, now=LATER)
    assert int(_channels(conn, sid)["演示纪录台"]["active"]) == 0

    server.state.set_ok()
    result = _fetch(conn, sid, now="2026-09-30T18:00:00+00:00")

    assert result["reactivated"] == 1
    restored = _channels(conn, sid)["演示纪录台"]
    assert int(restored["id"]) == doc_id          # 身份复用，不新建行
    assert int(restored["active"]) == 1
    assert restored["first_seen_at"] == NOW       # 首次发现仍是最初那次
    assert len(repo.list_source_channels(conn, sid)) == 4


def test_fixed_inactive_channel_keeps_binding_and_history(conn, mock_server):
    """消失只置 active=0；绑定与已归集的 stream 历史都保留。"""
    server, base = mock_server
    server.state.set_ok()
    sid = _add_fixed(conn, "mock-fixed", f"{base}/seq.m3u")
    _fetch(conn, sid)

    doc = _channels(conn, sid)["演示纪录台"]
    cid = repo.add_canonical_channel(conn, "演示纪录台", category="纪录片", now=NOW)
    repo.bind_source_channel(conn, int(doc["id"]), cid, now=NOW)
    repo.sync_streams(conn, now=NOW)
    streams_before = [dict(s) for s in repo.list_streams(conn, cid)]
    assert len(streams_before) == 1

    server.state.set_changed()
    _fetch(conn, sid, now=LATER)
    repo.sync_streams(conn, now=LATER)

    binding = repo.get_binding(conn, int(doc["id"]))
    assert binding is not None and int(binding["canonical_channel_id"]) == cid
    # stream 行仍在（只标 stale，不删），provenance 仍在
    assert len(repo.list_streams(conn, cid)) == 1
    assert int(_channels(conn, sid)["演示纪录台"]["active"]) == 0

    # 恢复后 stream 自动回到 observed，可再次参与选择
    server.state.set_ok()
    _fetch(conn, sid, now="2026-09-30T18:00:00+00:00")
    repo.sync_streams(conn, now="2026-09-30T18:00:00+00:00")
    statuses = {s["status"] for s in repo.list_streams(conn, cid)}
    assert statuses == {"observed"}


# ------------------------------------------------------- 失败不污染库存

@pytest.mark.parametrize(
    "path,expected_category",
    [
        ("/error.m3u", fetch_mod.ERROR_HTTP_STATUS),
        ("/empty.m3u", fetch_mod.ERROR_EMPTY_LIST),
        ("/broken.m3u", fetch_mod.ERROR_EMPTY_LIST),
        ("/notm3u.m3u", fetch_mod.ERROR_INVALID_M3U),
        ("/badutf8.m3u", fetch_mod.ERROR_DECODE),
        ("/slow.m3u?seconds=5", fetch_mod.ERROR_TIMEOUT),
    ],
)
def test_fixed_failure_keeps_inventory_intact(conn, mock_server, path, expected_category):
    server, base = mock_server
    server.state.set_ok()
    sid = _add_fixed(conn, "mock-fixed", f"{base}/seq.m3u")
    _fetch(conn, sid)
    snapshot = {r["id"]: dict(r) for r in repo.list_source_channels(conn, sid)}

    # 换成会失败的地址（同时把 timeout 调小，避免真等 5 秒）
    repo.add_source(conn, "mock-fixed", ingest.KIND_FIXED, f"{base}{path}", now=LATER)
    conn.commit()
    result = ingest.ingest_fixed_source(
        conn,
        repo.get_source(conn, sid),
        limits=fetch_mod.FetchLimits(timeout_seconds=0.5, max_bytes=2_000_000, max_redirects=3),
        now=LATER,
    )

    assert result["ok"] is False
    assert result["status"] == expected_category
    assert result["created"] == 0 and result["updated"] == 0
    assert result["deactivated"] == 0

    after = {r["id"]: dict(r) for r in repo.list_source_channels(conn, sid)}
    assert after == snapshot                                  # 库存逐字段未变
    assert repo.get_source(conn, sid)["last_fetch_status"] == expected_category
    assert repo.get_source(conn, sid)["last_fetch_at"] == LATER


def test_fixed_failure_does_not_deactivate_on_http_error_after_success(conn, mock_server):
    """先成功、后 HTTP 500：绝不允许把已有条目整体判为「消失」。"""
    server, base = mock_server
    server.state.set_ok()
    sid = _add_fixed(conn, "mock-fixed", f"{base}/seq.m3u")
    _fetch(conn, sid)

    repo.add_source(conn, "mock-fixed", ingest.KIND_FIXED, f"{base}/error.m3u", now=LATER)
    conn.commit()
    result = _fetch(conn, sid, now=LATER)

    assert result["ok"] is False
    assert all(int(r["active"]) == 1 for r in repo.list_source_channels(conn, sid))


# ----------------------------------------------------------- 来源之间隔离

def test_fixed_sources_are_isolated(conn, mock_server):
    server, base = mock_server
    server.state.set_ok()
    sid_a = _add_fixed(conn, "src-a", f"{base}/seq.m3u")
    sid_b = _add_fixed(conn, "src-b", f"{base}/ok.m3u")
    _fetch(conn, sid_a)
    _fetch(conn, sid_b)
    assert len(repo.list_source_channels(conn, sid_a)) == 3
    assert len(repo.list_source_channels(conn, sid_b)) == 3

    # A 失败 → B 完全不受影响
    repo.add_source(conn, "src-a", ingest.KIND_FIXED, f"{base}/error.m3u", now=LATER)
    conn.commit()
    _fetch(conn, sid_a, now=LATER)
    assert all(int(r["active"]) == 1 for r in repo.list_source_channels(conn, sid_b))
    assert len(repo.list_source_channels(conn, sid_b)) == 3

    # A 有 1 条消失 → 只有 A 的那一行 inactive，B 的条目全部不受影响
    repo.add_source(conn, "src-a", ingest.KIND_FIXED, f"{base}/changed.m3u", now=LATER)
    conn.commit()
    _fetch(conn, sid_a, now=LATER)
    a_rows = _channels(conn, sid_a)
    b_rows = _channels(conn, sid_b)
    assert int(a_rows["演示纪录台"]["active"]) == 0
    assert all(int(r["active"]) == 1 for r in b_rows.values())
    assert {int(r["source_id"]) for r in repo.list_source_channels(conn, sid_a)} == {sid_a}
    assert {int(r["source_id"]) for r in repo.list_source_channels(conn, sid_b)} == {sid_b}


def test_fixed_same_channel_names_across_sources_stay_separate(conn, mock_server):
    """两个来源提供同名同 URL 的条目时，各自保留独立身份（TASK-001 约束不回归）。"""
    server, base = mock_server
    server.state.set_ok()
    sid_a = _add_fixed(conn, "dup-a", f"{base}/ok.m3u")
    sid_b = _add_fixed(conn, "dup-b", f"{base}/ok.m3u")
    _fetch(conn, sid_a)
    _fetch(conn, sid_b)

    a_ids = {int(r["id"]) for r in repo.list_source_channels(conn, sid_a)}
    b_ids = {int(r["id"]) for r in repo.list_source_channels(conn, sid_b)}
    assert len(a_ids) == 3 and len(b_ids) == 3
    assert a_ids.isdisjoint(b_ids)


# ------------------------------------------------------------------ CLI

def _prepare(capsys, cfg):
    code = cli_main(["init-db", "--config", str(cfg), "--json"])
    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out)["schema_version"] == 1
    code = cli_main(["source-register", "--from-config", "--config", str(cfg), "--json"])
    assert code == 0
    return json.loads(capsys.readouterr().out)


def test_cli_source_register_from_config(capsys, remote_config):
    cfg, _db, _base, _server = remote_config
    payload = _prepare(capsys, cfg)
    assert payload["registered"] == 3
    assert payload["enabled_count"] == 1
    assert payload["disabled_count"] == 2
    assert all(item["created"] for item in payload["sources"])

    # 再注册一次：不重复建源，只是更新
    code = cli_main(["source-register", "--from-config", "--config", str(cfg), "--json"])
    assert code == 0
    payload2 = json.loads(capsys.readouterr().out)
    assert payload2["registered"] == 3
    assert not any(item["created"] for item in payload2["sources"])


def test_cli_fetch_all_skips_disabled_and_dynamic(capsys, remote_config):
    cfg, _db, _base, server = remote_config
    _prepare(capsys, cfg)
    server.state.set_ok()

    code = cli_main(["fetch", "--all", "--config", str(cfg), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["requested"] == 1                       # 只抓启用中的 fixed 源
    assert payload["ok_count"] == 1
    assert [r["source_name"] for r in payload["results"]] == ["mock-fixed"]
    assert [s["name"] for s in payload["skipped_disabled"]] == ["mock-disabled"]
    assert [s["name"] for s in payload["skipped_dynamic"]] == ["mock-dynamic"]
    assert payload["total_created"] == 3


def test_cli_fetch_source_rejects_dynamic_source(capsys, remote_config):
    cfg, _db, _base, _server = remote_config
    _prepare(capsys, cfg)
    with pytest.raises(SystemExit) as info:
        cli_main(["fetch", "--source", "mock-dynamic", "--config", str(cfg)])
    assert "dynamic-fetch" in str(info.value)


def test_cli_fetch_requires_exactly_one_target(capsys, remote_config):
    cfg, _db, _base, _server = remote_config
    _prepare(capsys, cfg)
    with pytest.raises(SystemExit):
        cli_main(["fetch", "--config", str(cfg)])
    with pytest.raises(SystemExit):
        cli_main(["fetch", "--all", "--source", "mock-fixed", "--config", str(cfg)])


def test_cli_fetch_failure_exit_code_and_category(capsys, remote_config):
    cfg, db, base, _server = remote_config
    _prepare(capsys, cfg)
    # 把唯一启用的 fixed 源换成会返回 500 的地址
    conn = db_mod.connect(str(db))
    repo.add_source(conn, "mock-fixed", ingest.KIND_FIXED, f"{base}/error.m3u")
    conn.commit()
    conn.close()

    code = cli_main(["fetch", "--all", "--config", str(cfg), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["failed_count"] == 1
    assert payload["results"][0]["status"] == fetch_mod.ERROR_HTTP_STATUS
    assert payload["results"][0]["http_status"] == 500


def test_cli_fetch_nothing_to_do_is_ok(capsys, remote_config):
    cfg, _db, _base, _server = remote_config
    cli_main(["init-db", "--config", str(cfg), "--json"])
    capsys.readouterr()
    code = cli_main(["fetch", "--all", "--config", str(cfg), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["requested"] == 0


def test_cli_source_status_reports_lifecycle(capsys, remote_config):
    cfg, _db, _base, server = remote_config
    _prepare(capsys, cfg)
    server.state.set_ok()
    cli_main(["fetch", "--all", "--config", str(cfg), "--json"])
    capsys.readouterr()

    server.state.set_changed()
    cli_main(["fetch", "--source", "mock-fixed", "--config", str(cfg), "--json"])
    capsys.readouterr()

    code = cli_main(["source-status", "--config", str(cfg), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    by_name = {s["name"]: s for s in payload["sources"]}

    fixed = by_name["mock-fixed"]
    assert fixed["kind"] == ingest.KIND_FIXED
    assert fixed["enabled"] == 1
    assert fixed["last_fetch_status"] == "ok"
    assert fixed["channels_active"] == 3
    assert fixed["channels_inactive"] == 1
    assert fixed["channels_total"] == 4

    assert by_name["mock-disabled"]["enabled"] == 0
    assert by_name["mock-disabled"]["last_fetch_status"] is None
    assert by_name["mock-dynamic"]["kind"] == ingest.KIND_DYNAMIC


def test_cli_source_add_disable_flag(capsys, remote_config):
    cfg, _db, _base, _server = remote_config
    cli_main(["init-db", "--config", str(cfg), "--json"])
    capsys.readouterr()
    code = cli_main([
        "source-add", "--name", "manual-off", "--kind", "fixed_m3u",
        "--url", "https://example.invalid/x.m3u", "--disable",
        "--config", str(cfg), "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["enabled"] == 0
