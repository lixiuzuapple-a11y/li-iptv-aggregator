"""dynamic_event_m3u 临时获取与脱敏测试（TASK-002 验收门槛 2/3）。"""

from __future__ import annotations

import json

import pytest

from liptv import db as db_mod
from liptv import fetch as fetch_mod
from liptv import ingest
from liptv import repo
from liptv.cli import main as cli_main
from tests.conftest import REPO_ROOT

LIMITS = fetch_mod.FetchLimits(
    timeout_seconds=5.0, max_bytes=2_000_000, max_redirects=3
)
NOW = "2026-09-30T12:00:00+00:00"

# mock 动态列表里刻意埋的签名参数，任何输出都不允许出现
SECRETS = ("txSecret", "DEADBEEF1234567890", "CAFEBABE9876543210", "0F0F0F0F0F0F0F0F")


def _dynamic_source(base: str) -> dict:
    return {
        "id": None,
        "name": "mock-dynamic",
        "kind": ingest.KIND_DYNAMIC,
        "url": f"{base}/dynamic.m3u",
    }


# --------------------------------------------------------------- 脱敏工具

def test_redact_url_drops_query_and_truncates_path():
    redacted = ingest.redact_url(
        "http://h.example/live/very/long/path/that/keeps/going/on/index.m3u8"
        "?txSecret=SECRETVALUE&txTime=6A1B2C3D"
    )
    assert "SECRETVALUE" not in redacted
    assert "txSecret" not in redacted
    assert redacted.endswith("?<redacted:2-param(s)>")
    assert redacted.startswith("http://h.example/live/")
    assert "…" in redacted


def test_redact_url_handles_junk():
    assert ingest.redact_url("") == "<unparsable-url>"
    assert ingest.redact_url("not a url") == "<unparsable-url>"


def test_url_extension_and_tags():
    assert ingest.url_extension("http://h/x.m3u8?a=1") == ".m3u8"
    assert ingest.url_extension("http://h/x.flv") == ".flv"
    assert ingest.url_extension("http://h/x") == "(none)"
    assert ingest.extract_tags("[解说] 曼城 vs 阿森纳") == ["解说"]
    assert ingest.extract_tags("[原声][高清] A vs B") == ["原声", "高清"]
    assert ingest.extract_tags("曼城 vs 阿森纳") == []


# --------------------------------------------------------- 临时解析结果

def test_dynamic_preview_parses_entries(mock_server):
    _server, base = mock_server
    result = ingest.preview_dynamic_source(_dynamic_source(base), limits=LIMITS, now=NOW)

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["persisted"] is False
    assert result["entry_count"] == 4
    assert result["groups"] == {"正在直播": 2, "即将开始": 1, "宣传": 1}
    assert result["fetched_at"] == NOW

    first = result["entries"][0]
    assert first["name"] == "[解说] 曼城 vs 阿森纳"
    assert first["group_title"] == "正在直播"
    assert first["tags"] == ["解说"]
    assert first["url_ext"] == ".m3u8"
    assert first["url_scheme"] == "http"


def test_dynamic_preview_never_exposes_signed_urls(mock_server):
    _server, base = mock_server
    result = ingest.preview_dynamic_source(_dynamic_source(base), limits=LIMITS, now=NOW)
    blob = json.dumps(result, ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in blob
    assert "?<redacted:" in blob


def test_dynamic_preview_does_not_touch_database(conn, mock_server):
    _server, base = mock_server
    before = repo.counts(conn)

    ingest.preview_dynamic_source(_dynamic_source(base), limits=LIMITS, now=NOW)

    assert repo.counts(conn) == before
    assert before["canonical_channel"] == 0
    assert before["stream"] == 0


def test_dynamic_preview_failure_is_classified(mock_server):
    _server, base = mock_server
    source = _dynamic_source(base)
    source["url"] = f"{base}/error.m3u"
    result = ingest.preview_dynamic_source(source, limits=LIMITS, now=NOW)
    assert result["ok"] is False
    assert result["status"] == fetch_mod.ERROR_HTTP_STATUS
    assert result["entry_count"] == 0
    assert result["entries"] == []


def test_dynamic_preview_missing_url_is_reported():
    result = ingest.preview_dynamic_source(
        {"id": None, "name": "x", "kind": ingest.KIND_DYNAMIC, "url": None}, now=NOW
    )
    assert result["ok"] is False
    assert result["status"] == fetch_mod.ERROR_NETWORK


# ------------------------------------------------------------------ CLI

def test_cli_dynamic_fetch_json_is_redacted(capsys, remote_config):
    cfg, _db, _base, _server = remote_config
    cli_main(["init-db", "--config", str(cfg), "--json"])
    cli_main(["source-register", "--from-config", "--config", str(cfg), "--json"])
    capsys.readouterr()

    code = cli_main(["dynamic-fetch", "--source", "mock-dynamic", "--config", str(cfg), "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["entry_count"] == 4
    assert payload["snapshot"] is None
    for secret in SECRETS:
        assert secret not in out


def test_cli_dynamic_fetch_table_output_is_redacted(capsys, remote_config):
    cfg, _db, _base, _server = remote_config
    cli_main(["init-db", "--config", str(cfg), "--json"])
    cli_main(["source-register", "--from-config", "--config", str(cfg), "--json"])
    capsys.readouterr()

    code = cli_main(["dynamic-fetch", "--source", "mock-dynamic", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0
    assert "短时快照" in out
    for secret in SECRETS:
        assert secret not in out


def test_cli_dynamic_fetch_out_writes_snapshot_inside_tmp(capsys, remote_config):
    cfg, _db, _base, _server = remote_config
    cli_main(["init-db", "--config", str(cfg), "--json"])
    cli_main(["source-register", "--from-config", "--config", str(cfg), "--json"])
    capsys.readouterr()

    target = cfg.parent / "out" / "tmp" / "snap.m3u"
    code = cli_main([
        "dynamic-fetch", "--source", "mock-dynamic", "--out", str(target),
        "--config", str(cfg), "--json",
    ])
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert code == 0
    assert target.exists()
    assert payload["snapshot"]["path"] == str(target.resolve())
    assert payload["snapshot"]["bytes"] > 0
    # 落盘的是原始 M3U（含签名），但**输出里不得出现**
    assert "txSecret" in target.read_text(encoding="utf-8")
    for secret in SECRETS:
        assert secret not in out


def test_cli_dynamic_fetch_rejects_out_outside_tmp(capsys, remote_config):
    cfg, _db, _base, _server = remote_config
    cli_main(["init-db", "--config", str(cfg), "--json"])
    cli_main(["source-register", "--from-config", "--config", str(cfg), "--json"])
    capsys.readouterr()

    outside = cfg.parent / "leak.m3u"   # 不在 out/tmp 之内
    with pytest.raises(SystemExit) as info:
        cli_main([
            "dynamic-fetch", "--source", "mock-dynamic", "--out", str(outside),
            "--config", str(cfg),
        ])
    assert "拒绝写入" in str(info.value)
    assert not outside.exists()


def test_cli_dynamic_fetch_by_url_without_database(capsys, mock_server):
    _server, base = mock_server
    code = cli_main([
        "dynamic-fetch", "--url", f"{base}/dynamic.m3u", "--name", "ad-hoc", "--json",
    ])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 0
    assert payload["source_id"] is None
    assert payload["entry_count"] == 4
    for secret in SECRETS:
        assert secret not in out


def test_cli_dynamic_fetch_rejects_fixed_source(capsys, remote_config):
    cfg, _db, _base, _server = remote_config
    cli_main(["init-db", "--config", str(cfg), "--json"])
    cli_main(["source-register", "--from-config", "--config", str(cfg), "--json"])
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli_main(["dynamic-fetch", "--source", "mock-fixed", "--config", str(cfg)])


def test_cli_dynamic_source_never_enters_live_m3u(capsys, remote_config):
    """动态赛事源不会因为「出现过」而永久留在固定频道输出里。"""
    cfg, db, _base, _server = remote_config
    cli_main(["init-db", "--config", str(cfg), "--json"])
    cli_main(["source-register", "--from-config", "--config", str(cfg), "--json"])
    capsys.readouterr()

    code = cli_main(["dynamic-fetch", "--source", "mock-dynamic", "--config", str(cfg), "--json"])
    capsys.readouterr()
    assert code == 0

    conn = db_mod.connect(str(db))
    counts = repo.counts(conn)
    conn.close()
    assert counts["canonical_channel"] == 0
    assert counts["stream"] == 0
    assert counts["source_channel"] == 0

    code = cli_main(["select", "--all", "--config", str(cfg), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["selected"] == []
    assert payload["skipped"] == []

    out_file = cfg.parent / "out" / "live.m3u"
    code = cli_main(["generate-m3u", "--out", str(out_file), "--config", str(cfg), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["channel_count"] == 0
    assert out_file.read_text(encoding="utf-8") == "#EXTM3U\n"


# --------------------------------------------------- 快照目录的 Git 安全

def test_dynamic_tmp_dir_is_covered_by_gitignore():
    candidate = REPO_ROOT / "out" / "tmp" / "anything.m3u"
    assert ingest.is_ignored_by_gitignore(candidate, REPO_ROOT)
    assert not ingest.is_ignored_by_gitignore(REPO_ROOT / "REPORTS" / "x.m3u", REPO_ROOT)


def test_write_dynamic_snapshot_rejects_path_outside_allowed_dir(tmp_path):
    allowed = tmp_path / "out" / "tmp"
    allowed.mkdir(parents=True)
    with pytest.raises(ValueError):
        ingest.write_dynamic_snapshot("#EXTM3U\n", tmp_path / "escape.m3u", allowed_dir=allowed)
    ok = ingest.write_dynamic_snapshot(
        "#EXTM3U\n", allowed / "ok.m3u", allowed_dir=allowed
    )
    assert (allowed / "ok.m3u").exists()
    assert ok["checksum"]
