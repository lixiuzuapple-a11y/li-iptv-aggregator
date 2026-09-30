"""TASK-002 Review 02 定向返工的永久回归（QA-002C）。

阻断点：``.gitignore`` **只对尚未被跟踪的文件生效**。一个动态快照文件如果历史上被
``git add -f`` 强制加进索引，之后往同名文件写入就会直接变成待提交的已跟踪变更。

本文件的用例全部使用**真实的临时 Git 仓库**（``git init`` 出来的），
不再使用只建 ``.git`` 目录的假工作树 —— 因为要验证的正是「Git 索引」本身。
临时仓库建在 pytest 的临时目录下，**绝不触碰本项目仓库的任何文件**。

不依赖任何公网赛事源。
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from liptv import ingest
from liptv.cli import main as cli_main

SIGNED_SNAPSHOT = (
    "#EXTM3U\n"
    "#EXTINF:-1,签名线路\n"
    "http://stream.invalid.example/x.m3u8?txSecret=DEADBEEF1234567890&txTime=CAFEBABE\n"
)


# --------------------------------------------------------------- git 小工具

def _git_exe() -> str:
    exe = ingest._resolve_git_executable()
    if exe is None:
        pytest.skip("本机找不到 git 可执行文件，无法建立真实临时仓库")
    return exe


def _git(*args: str, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [_git_exe(), *args],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} 失败：{proc.stderr.strip()}"
    return proc


def _init_repo(path: pathlib.Path, ignore: str = "out/\n") -> pathlib.Path:
    """建一个真实的最小 Git 仓库（不产生提交，足够验证索引判定）。"""
    path.mkdir(parents=True, exist_ok=True)
    _git("-c", "init.defaultBranch=main", "init", "-q", cwd=path)
    # 统一用 LF 写文件：Windows 的默认换行转换会干扰「原字节不变」的比对
    (path / ".gitignore").write_text(ignore, encoding="utf-8", newline="\n")
    return path


def _stage_forced(root: pathlib.Path, rel: str, content: str) -> pathlib.Path:
    """把 root/<rel> 写成 content，并用 ``git add -f`` 强制加入索引。"""
    target = root.joinpath(*rel.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")
    _git("add", "-f", "--", rel, cwd=root)
    return target


# ============================================================== QA-002C
# 已跟踪文件不能靠 .gitignore 保护

def test_qa002c_real_repo_ignored_but_tracked_file_is_rejected(tmp_path):
    """大G 反例（真实仓库版）：out/tmp/signed.m3u 被 add -f 后，写入必须拒绝且旧字节不变。"""
    root = _init_repo(tmp_path / "real_repo")
    allowed = root / "out" / "tmp"
    allowed.mkdir(parents=True)
    target = _stage_forced(root, "out/tmp/signed.m3u", "OLD-CONTENT\n")

    # 前提自检：.gitignore 认它，但索引里确实有它 —— 这正是漏洞成立的条件
    assert ingest.is_ignored_by_gitignore(target, root) is True
    assert ingest.is_tracked_by_git(target, root) is True

    before = target.read_bytes()
    status_before = _git("status", "--porcelain", cwd=root).stdout
    with pytest.raises(ValueError) as info:
        ingest.write_dynamic_snapshot(SIGNED_SNAPSHOT, target, allowed_dir=allowed)

    message = str(info.value)
    assert "跟踪" in message and "拒绝写入" in message
    assert target.read_bytes() == before            # 旧文件原字节未变
    assert SIGNED_SNAPSHOT not in target.read_text(encoding="utf-8")

    # 没有多出任何待提交改动 ⇒ 索引与工作区都没被这次拒绝写入影响
    assert _git("status", "--porcelain", cwd=root).stdout == status_before


def test_qa002c_real_repo_untracked_ignored_file_still_writable(tmp_path):
    """正向：未跟踪、且确实被忽略的文件依旧可以写（默认 out/tmp 流程不能回归）。"""
    root = _init_repo(tmp_path / "real_repo")
    allowed = root / "out" / "tmp"
    target = allowed / "fresh.m3u"

    snapshot = ingest.write_dynamic_snapshot(SIGNED_SNAPSHOT, target, allowed_dir=allowed)

    assert target.exists()
    assert target.read_text(encoding="utf-8") == SIGNED_SNAPSHOT
    assert snapshot["git_worktree"] == str(root.resolve())
    assert snapshot["git_ignored"] is True
    assert snapshot["git_tracked"] is False


def test_qa002c_guard_follows_the_index_not_the_file_name(tmp_path):
    """同一个路径：移出索引后重新变为可写 —— 判定跟着索引走，不是按文件名一刀切。"""
    root = _init_repo(tmp_path / "real_repo")
    allowed = root / "out" / "tmp"
    allowed.mkdir(parents=True)
    target = _stage_forced(root, "out/tmp/snapshot.m3u", "OLD\n")

    with pytest.raises(ValueError):
        ingest.write_dynamic_snapshot(SIGNED_SNAPSHOT, target, allowed_dir=allowed)

    _git("rm", "--cached", "-q", "--", "out/tmp/snapshot.m3u", cwd=root)
    assert ingest.is_tracked_by_git(target, root) is False

    snapshot = ingest.write_dynamic_snapshot(SIGNED_SNAPSHOT, target, allowed_dir=allowed)
    assert snapshot["git_tracked"] is False
    assert target.read_text(encoding="utf-8") == SIGNED_SNAPSHOT


def test_qa002c_rejects_tracked_path_with_spaces(tmp_path):
    """OneDrive 风格的带空格路径（仓库根、子目录、文件名都含空格）同样必须拦住。"""
    root = _init_repo(tmp_path / "OneDrive - Swire Properties Limited" / "my repo",
                      ignore="out dir/\n")
    allowed = root / "out dir" / "tmp"
    allowed.mkdir(parents=True)
    target = _stage_forced(root, "out dir/tmp/signed snapshot.m3u", "OLD\n")

    assert ingest.is_tracked_by_git(target, root) is True
    with pytest.raises(ValueError) as info:
        ingest.write_dynamic_snapshot(SIGNED_SNAPSHOT, target, allowed_dir=allowed)
    assert "跟踪" in str(info.value)
    assert target.read_bytes() == b"OLD\n"

    # 同一带空格目录下，换个没被跟踪的文件名仍然可写
    ok = ingest.write_dynamic_snapshot(SIGNED_SNAPSHOT, allowed / "other.m3u",
                                       allowed_dir=allowed)
    assert ok["git_tracked"] is False


def test_qa002c_nested_worktree_is_detected(tmp_path):
    """nested worktree（``.git`` 是文件、指向别处的 gitdir）也要能判定为已跟踪。"""
    main_root = _init_repo(tmp_path / "main repo")
    (main_root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git("add", "--", "seed.txt", cwd=main_root)
    _git("-c", "user.email=t@example.com", "-c", "user.name=tester",
         "commit", "-q", "-m", "base", cwd=main_root)

    linked = tmp_path / "linked worktree"
    _git("worktree", "add", "-q", "--detach", str(linked), cwd=main_root)

    assert (linked / ".git").is_file()      # 附属工作树的 .git 是文件，不是目录
    (linked / ".gitignore").write_text("out/\n", encoding="utf-8")
    allowed = linked / "out" / "tmp"
    allowed.mkdir(parents=True)
    target = _stage_forced(linked, "out/tmp/signed.m3u", "OLD\n")

    assert ingest.is_tracked_by_git(target, linked) is True
    with pytest.raises(ValueError) as info:
        ingest.write_dynamic_snapshot(SIGNED_SNAPSHOT, target, allowed_dir=allowed)
    assert "跟踪" in str(info.value)
    assert target.read_bytes() == b"OLD\n"


# ------------------------------------------- git 不可用时的索引回退

def test_qa002c_index_fallback_detects_tracked_without_git_binary(tmp_path, monkeypatch):
    """拿不到 git 可执行文件时，直接解析 .git/index 仍要认出已跟踪文件。"""
    root = _init_repo(tmp_path / "real_repo")
    allowed = root / "out" / "tmp"
    allowed.mkdir(parents=True)
    target = _stage_forced(root, "out/tmp/signed.m3u", "OLD\n")

    monkeypatch.setattr(ingest, "_resolve_git_executable", lambda: None)
    assert ingest.is_tracked_by_git(target, root) is True

    with pytest.raises(ValueError) as info:
        ingest.write_dynamic_snapshot(SIGNED_SNAPSHOT, target, allowed_dir=allowed)
    assert "跟踪" in str(info.value)
    assert target.read_bytes() == b"OLD\n"


def test_qa002c_index_fallback_handles_v4_index(tmp_path, monkeypatch):
    """index v4（路径前缀压缩）也要能正确解析 —— 版本 4 不是「读不懂就放行」。"""
    root = _init_repo(tmp_path / "real_repo")
    allowed = root / "out" / "tmp"
    allowed.mkdir(parents=True)
    _stage_forced(root, "out/tmp/aaa.m3u", "A\n")
    target = _stage_forced(root, "out/tmp/signed.m3u", "OLD\n")
    _git("update-index", "--index-version", "4", cwd=root)

    monkeypatch.setattr(ingest, "_resolve_git_executable", lambda: None)
    assert ingest.is_tracked_by_git(target, root) is True
    assert ingest.is_tracked_by_git(allowed / "not-there.m3u", root) is False


def test_qa002c_unresolvable_index_state_is_refused(tmp_path, monkeypatch):
    """既没有 git、又读不到 git 目录 ⇒ 无法证明未跟踪 ⇒ 保守拒绝（不静默放行）。"""
    root = _init_repo(tmp_path / "real_repo")
    allowed = root / "out" / "tmp"

    monkeypatch.setattr(ingest, "_resolve_git_executable", lambda: None)
    monkeypatch.setattr(ingest, "_resolve_git_dir", lambda _root: None)

    target = allowed / "signed.m3u"
    with pytest.raises(ValueError) as info:
        ingest.write_dynamic_snapshot(SIGNED_SNAPSHOT, target, allowed_dir=allowed)
    assert "无法确认" in str(info.value)
    assert not target.exists()          # 拒绝时不创建文件


# ------------------------------------------------------------- CLI 端到端

def test_qa002c_cli_rejects_tracked_snapshot_path(capsys, mock_server, tmp_path):
    """端到端：--out 指向一个已被跟踪（虽然被忽略）的文件 → CLI 必须拒绝并退出。"""
    _server, base = mock_server
    root = _init_repo(tmp_path / "real_repo")
    allowed = root / "out" / "tmp"
    allowed.mkdir(parents=True)
    target = _stage_forced(root, "out/tmp/snap.m3u", "OLD\n")

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[fetch]\n" f'dynamic_tmp_dir = "{allowed.as_posix()}"\n',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as info:
        cli_main([
            "dynamic-fetch", "--url", f"{base}/dynamic.m3u",
            "--out", str(target), "--config", str(cfg),
        ])
    assert "跟踪" in str(info.value)
    capsys.readouterr()
    assert target.read_bytes() == b"OLD\n"


def test_qa002c_cli_writes_to_untracked_ignored_path(capsys, mock_server, tmp_path):
    """端到端正向：同一真实仓库里未被跟踪的 out/tmp 目标仍然写得进去。"""
    _server, base = mock_server
    root = _init_repo(tmp_path / "real_repo")
    allowed = root / "out" / "tmp"

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[fetch]\n" f'dynamic_tmp_dir = "{allowed.as_posix()}"\n',
        encoding="utf-8",
    )

    code = cli_main([
        "dynamic-fetch", "--url", f"{base}/dynamic.m3u",
        "--out", str(allowed / "snap.m3u"), "--config", str(cfg), "--json",
    ])
    capsys.readouterr()

    assert code == 0
    assert (allowed / "snap.m3u").exists()
    # 快照落在被忽略目录里 ⇒ 不会以任何形式出现在 git status
    assert "snap.m3u" not in _git("status", "--porcelain", cwd=root).stdout
