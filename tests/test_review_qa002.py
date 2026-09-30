"""TASK-002 Review 01 定向返工的永久回归（QA-002A / QA-002B）。

两个阻断点各自配可长期保留的回归用例，**全部离线**：
  * QA-002A 用本机 mock HTTP 服务的响应文本；
  * QA-002B 用临时搭建的最小「假 Git 工作树」（只有 .git 目录 + .gitignore），
    绝不触碰真实仓库文件。

不依赖任何公网赛事源。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from liptv import db as db_mod
from liptv import fetch as fetch_mod
from liptv import ingest
from liptv import repo
from liptv.cli import main as cli_main
from tools.mock_source_server import TRUNCATED_M3U

LIMITS = fetch_mod.FetchLimits(timeout_seconds=5.0, max_bytes=2_000_000, max_redirects=3)
NOW = "2026-09-30T12:00:00+00:00"
LATER = "2026-09-30T15:00:00+00:00"

# 结构完整、但确实缩减为 1 条（真正的删台，必须照常生效）
REDUCED_M3U = """#EXTM3U
#EXTINF:-1 tvg-id="mock-news.cn" tvg-name="Mock News" group-title="新闻",演示新闻台
http://stream.invalid.example/news/index.m3u8
"""

# 有 M3U 结构但零有效条目（应保持 EMPTY_LIST 归类，不因 QA-002A 修复而回归）
STRUCTURE_ONLY_M3U = '#EXTM3U\n#EXTINF:-1,只有声明没有地址\n'


def _add_fixed(conn, name: str, url: str) -> int:
    sid = repo.add_source(conn, name, ingest.KIND_FIXED, url, now=NOW)
    conn.commit()
    return sid


def _fetch(conn, sid: int, *, now: str = NOW) -> dict:
    return ingest.ingest_fixed_source(conn, repo.get_source(conn, sid), limits=LIMITS, now=now)


def _channels(conn, sid: int) -> dict[str, dict]:
    return {r["raw_name"]: dict(r) for r in repo.list_source_channels(conn, sid)}


def _prepare_cli(capsys, cfg: pathlib.Path) -> None:
    assert cli_main(["init-db", "--config", str(cfg), "--json"]) == 0
    capsys.readouterr()
    assert cli_main(["source-register", "--from-config", "--config", str(cfg), "--json"]) == 0
    capsys.readouterr()


# ============================================================== QA-002A
# 截断的「部分成功前缀」不得被当作完整快照

def test_qa002a_truncated_prefix_rejected_and_inventory_intact(conn, mock_server):
    """复刻大G 反例：先正常 3 条，再收到「Alpha 完整 + Beta 截断」→ 必须整体拒绝。"""
    server, base = mock_server
    server.state.set_ok()
    sid = _add_fixed(conn, "mock-fixed", f"{base}/seq.m3u")
    assert _fetch(conn, sid)["created"] == 3
    before = {r["id"]: dict(r) for r in repo.list_source_channels(conn, sid)}

    server.state.content = TRUNCATED_M3U
    result = _fetch(conn, sid, now=LATER)

    assert result["ok"] is False
    assert result["status"] == fetch_mod.ERROR_INVALID_M3U
    assert result["error_category"] == fetch_mod.ERROR_INVALID_M3U
    assert result["created"] == 0 and result["updated"] == 0
    assert result["deactivated"] == 0 and result["reactivated"] == 0

    after = {r["id"]: dict(r) for r in repo.list_source_channels(conn, sid)}
    assert after == before                                    # 库存逐字段未变
    assert all(int(r["active"]) == 1 for r in after.values())  # 没有频道被静默下线
    assert repo.get_source(conn, sid)["last_fetch_status"] == fetch_mod.ERROR_INVALID_M3U
    assert repo.get_source(conn, sid)["last_fetch_at"] == LATER


def test_qa002a_truncated_via_mock_endpoint(conn, mock_server):
    """同一反例走固定 mock 端点（/truncated.m3u），保证可手工复现。"""
    server, base = mock_server
    server.state.set_ok()
    sid = _add_fixed(conn, "mock-fixed", f"{base}/seq.m3u")
    _fetch(conn, sid)

    repo.add_source(conn, "mock-fixed", ingest.KIND_FIXED, f"{base}/truncated.m3u", now=LATER)
    conn.commit()
    result = _fetch(conn, sid, now=LATER)

    assert result["ok"] is False
    assert result["status"] == fetch_mod.ERROR_INVALID_M3U
    assert all(int(r["active"]) == 1 for r in repo.list_source_channels(conn, sid))


def test_qa002a_complete_reduced_snapshot_still_applies(conn, mock_server):
    """真正结构完整的缩减（删台）不能被误判为截断：消失条目仍须照常置 inactive。"""
    server, base = mock_server
    server.state.set_ok()
    sid = _add_fixed(conn, "mock-fixed", f"{base}/seq.m3u")
    _fetch(conn, sid)

    server.state.content = REDUCED_M3U
    result = _fetch(conn, sid, now=LATER)

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["deactivated"] == 2

    rows = _channels(conn, sid)
    assert int(rows["演示新闻台"]["active"]) == 1
    assert int(rows["演示体育台"]["active"]) == 0
    assert int(rows["演示纪录台"]["active"]) == 0
    assert len(repo.list_source_channels(conn, sid)) == 3     # 只置 inactive，不硬删


def test_qa002a_validator_classification_unchanged_for_other_cases():
    """校验分档：截断 → INVALID_M3U；零条目 → EMPTY_LIST；完整 → 通过。"""
    with pytest.raises(fetch_mod.FetchError) as truncated:
        ingest.validate_m3u_text(TRUNCATED_M3U)
    assert truncated.value.category == fetch_mod.ERROR_INVALID_M3U

    with pytest.raises(fetch_mod.FetchError) as structure_only:
        ingest.validate_m3u_text(STRUCTURE_ONLY_M3U)
    assert structure_only.value.category == fetch_mod.ERROR_EMPTY_LIST

    assert ingest.validate_m3u_text(REDUCED_M3U).entry_count == 1


def test_qa002a_dynamic_preview_shares_the_same_guard(mock_server):
    """动态源复用同一校验：截断文本不得被当成有效预览。"""
    server, base = mock_server
    server.state.set_ok()
    server.state.content = TRUNCATED_M3U
    result = ingest.preview_dynamic_source(
        {"id": None, "name": "dyn", "kind": ingest.KIND_DYNAMIC, "url": f"{base}/seq.m3u"},
        limits=LIMITS,
        now=NOW,
    )
    assert result["ok"] is False
    assert result["status"] == fetch_mod.ERROR_INVALID_M3U
    assert result["entry_count"] == 0
    assert result["entries"] == []


def test_qa002a_cli_fetch_truncated_fails_without_touching_inventory(capsys, remote_config):
    cfg, db, _base, server = remote_config
    server.state.set_ok()
    _prepare_cli(capsys, cfg)
    assert cli_main(["fetch", "--all", "--config", str(cfg), "--json"]) == 0
    capsys.readouterr()

    conn = db_mod.connect(str(db))
    before = {r["id"]: dict(r) for r in repo.list_source_channels(conn, 1)}
    conn.close()

    server.state.content = TRUNCATED_M3U
    code = cli_main(["fetch", "--all", "--config", str(cfg), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["failed_count"] == 1
    assert payload["results"][0]["status"] == fetch_mod.ERROR_INVALID_M3U

    conn = db_mod.connect(str(db))
    after = {r["id"]: dict(r) for r in repo.list_source_channels(conn, 1)}
    conn.close()
    assert after == before


# ============================================================== QA-002B
# 动态快照落盘必须真正受 .gitignore 保护

def _fake_worktree(tmp_path: pathlib.Path, ignore_text: str) -> pathlib.Path:
    """造一个最小假 Git 工作树（.git 目录 + .gitignore），不触碰真实仓库。"""
    root = tmp_path / "fake_repo"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text(ignore_text, encoding="utf-8")
    return root


def test_qa002b_rejects_unignored_dir_inside_worktree(tmp_path):
    """dynamic_tmp_dir 被配成仓库内未被忽略的目录（如 SOURCES/）→ 拒绝且不创建文件。"""
    root = _fake_worktree(tmp_path, "out/\n")
    src_dir = root / "SOURCES"
    src_dir.mkdir()
    target = src_dir / "leak.m3u"

    with pytest.raises(ValueError) as info:
        ingest.write_dynamic_snapshot(
            "#EXTM3U\n#EXTINF:-1,签名线路\nhttp://h/x.m3u8?txSecret=SECRET\n",
            target,
            allowed_dir=src_dir,
        )

    assert "忽略" in str(info.value)
    assert not target.exists()
    assert list(src_dir.iterdir()) == []          # 什么都没落盘


def test_qa002b_allows_ignored_dir_inside_worktree(tmp_path):
    """被 .gitignore 覆盖的目录仍然允许写入（默认 out/tmp 的行为不能回归）。"""
    root = _fake_worktree(tmp_path, "out/\n")
    allowed = root / "out" / "tmp"

    snapshot = ingest.write_dynamic_snapshot(
        "#EXTM3U\n", allowed / "ok.m3u", allowed_dir=allowed
    )

    assert (allowed / "ok.m3u").exists()
    assert snapshot["git_worktree"] == str(root.resolve())
    assert snapshot["git_ignored"] is True


def test_qa002b_path_outside_any_worktree_is_allowed(tmp_path):
    """不在任何 Git 工作树内的路径不存在被跟踪风险 → 允许（明确策略，非默认放行）。"""
    allowed = tmp_path / "loose" / "tmp"

    snapshot = ingest.write_dynamic_snapshot(
        "#EXTM3U\n", allowed / "ok.m3u", allowed_dir=allowed
    )

    assert (allowed / "ok.m3u").exists()
    assert snapshot["git_worktree"] is None
    assert snapshot["git_ignored"] is None


def test_qa002b_allowed_dir_check_still_enforced(tmp_path):
    """原有的 allowed_dir 边界检查不能被削弱。"""
    root = _fake_worktree(tmp_path, "out/\n")
    allowed = root / "out" / "tmp"
    with pytest.raises(ValueError):
        ingest.write_dynamic_snapshot("#EXTM3U\n", root / "escape.m3u", allowed_dir=allowed)
    assert not (root / "escape.m3u").exists()


def test_qa002b_gitignore_matcher_basics():
    """忽略判定本身：目录模式 / 通配 / 命名目录 / 工作树外。"""
    root = pathlib.Path(__file__).resolve().parents[1]
    assert ingest.is_ignored_by_gitignore(root / "out" / "tmp" / "x.m3u", root)
    assert ingest.is_ignored_by_gitignore(root / "data" / "liptv.sqlite3", root)
    assert not ingest.is_ignored_by_gitignore(root / "SOURCES" / "x.m3u", root)
    assert not ingest.is_ignored_by_gitignore(root / "TASKS" / "TASK-002.md", root)
    # 目标不在给定 root 内 → False（调用方据此判断是否拒绝）
    assert not ingest.is_ignored_by_gitignore(root.parent / "elsewhere.m3u", root)


def test_qa002b_cli_rejects_unignored_tmp_dir_config(capsys, mock_server, tmp_path):
    """端到端：配置把 dynamic_tmp_dir 指到仓库内未被忽略的目录 → CLI 必须拒绝。"""
    _server, base = mock_server
    root = _fake_worktree(tmp_path, "out/\n")
    src_dir = root / "SOURCES"
    src_dir.mkdir()

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[fetch]\n" f'dynamic_tmp_dir = "{src_dir.as_posix()}"\n',
        encoding="utf-8",
    )

    target = src_dir / "snap.m3u"
    with pytest.raises(SystemExit) as info:
        cli_main([
            "dynamic-fetch", "--url", f"{base}/dynamic.m3u",
            "--out", str(target), "--config", str(cfg),
        ])

    assert "拒绝" in str(info.value)
    assert not target.exists()
    assert list(src_dir.iterdir()) == []


def test_qa002b_default_dynamic_tmp_dir_is_gitignored():
    """默认配置的 out/tmp 在真实仓库里确实被忽略（默认路径必须安全）。"""
    root = pathlib.Path(__file__).resolve().parents[1]
    from liptv import config as config_mod

    default_dir = config_mod.DEFAULT_CONFIG["fetch"]["dynamic_tmp_dir"]
    assert default_dir == "out/tmp"
    assert ingest.is_ignored_by_gitignore(root / default_dir / "snapshot.m3u", root)
