"""TASK-002 Review 返工证据脚本（完全离线）。

复刻 Reviewer 的反例，并打印修复后的行为：

  QA-002A  截断的「部分成功前缀」绝不能被当作完整快照 —— 不得静默下线已有频道；
           同时验证「结构完整的真正缩减」仍须照常置 inactive（正常删台不能被打死）。
  QA-002B  动态快照落盘必须真正落在受 .gitignore 保护的路径；
           仓库内未被忽略的目录（如 SOURCES/）必须被拒绝且不落盘。
  QA-002C  已被 Git 索引跟踪的文件不能靠 .gitignore 保护：
           真实临时仓库里先 `git add -f out/tmp/signed.m3u`，再写同名文件必须被拒绝
           且旧字节不变；未跟踪的同类文件仍须可写。

用法（只跑本机 mock 服务，不访问任何公网地址）：

    python tools/qa002_repro.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liptv import db as db_mod  # noqa: E402
from liptv import fetch as fetch_mod  # noqa: E402
from liptv import ingest  # noqa: E402
from liptv import repo  # noqa: E402
from tools.mock_source_server import TRUNCATED_M3U, RunningMock  # noqa: E402

LIMITS = fetch_mod.FetchLimits(timeout_seconds=5.0, max_bytes=2_000_000, max_redirects=3)
NOW = "2026-09-30T12:00:00+00:00"
LATER = "2026-09-30T15:00:00+00:00"

# 结构完整、但确实缩减为 1 条（真正的删台）
REDUCED_M3U = """#EXTM3U
#EXTINF:-1 tvg-id="mock-news.cn" tvg-name="Mock News" group-title="新闻",演示新闻台
http://stream.invalid.example/news/index.m3u8
"""

# 带短时签名参数的假快照（签名值本身是假的，不来自任何真实来源）
SIGNED_PAYLOAD = "#EXTM3U\n#EXTINF:-1,签名线路\nhttp://h/x.m3u8?txSecret=SECRET\n"

_results: list[tuple[bool, str, str]] = []


def check(label: str, ok: bool, detail: str) -> None:
    _results.append((ok, label, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")


def repro_a(conn, base: str, server) -> None:
    print("\n=== QA-002A：截断的部分成功前缀 ===")
    server.state.set_ok()
    sid = repo.add_source(conn, "repro-a", ingest.KIND_FIXED, f"{base}/seq.m3u", now=NOW)
    conn.commit()

    first = ingest.ingest_fixed_source(conn, repo.get_source(conn, sid), limits=LIMITS, now=NOW)
    before = {r["id"]: dict(r) for r in repo.list_source_channels(conn, sid)}
    active = sum(int(r["active"]) for r in before.values())
    print(f"  ① 首次正常：created={first['created']} active={active}")

    # ② 入口 HTTP 200，但正文是「完整条目 + 末尾 #EXTINF 缺 URL」的截断文本
    server.state.content = TRUNCATED_M3U
    partial = ingest.ingest_fixed_source(conn, repo.get_source(conn, sid), limits=LIMITS, now=LATER)
    after = {r["id"]: dict(r) for r in repo.list_source_channels(conn, sid)}
    print(
        f"  ② 截断响应：ok={partial['ok']} status={partial['status']} "
        f"deactivated={partial['deactivated']}"
    )
    check(
        "截断被拒绝（INVALID_M3U）",
        partial["ok"] is False and partial["status"] == fetch_mod.ERROR_INVALID_M3U,
        partial["status"],
    )
    check("库存逐字段未变", after == before, f"rows={len(after)}")
    check(
        "无频道被静默下线",
        all(int(r["active"]) == 1 for r in after.values()),
        f"active={sum(int(r['active']) for r in after.values())}/{len(after)}",
    )

    # ③ 对照：结构完整的真正缩减必须照常生效
    server.state.content = REDUCED_M3U
    reduced = ingest.ingest_fixed_source(
        conn, repo.get_source(conn, sid), limits=LIMITS, now="2026-09-30T18:00:00+00:00"
    )
    rows = {r["raw_name"]: int(r["active"]) for r in repo.list_source_channels(conn, sid)}
    print(f"  ③ 完整缩减：status={reduced['status']} deactivated={reduced['deactivated']}")
    check(
        "完整缩减仍正常置 inactive",
        reduced["ok"] is True
        and reduced["deactivated"] == 2
        and rows.get("演示新闻台") == 1
        and rows.get("演示体育台") == 0
        and rows.get("演示纪录台") == 0,
        f"active={rows}",
    )


def repro_b(tmp: pathlib.Path) -> None:
    print("\n=== QA-002B：快照落盘路径的 Git 忽略强制 ===")
    root = tmp / "fake_repo"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text("out/\n", encoding="utf-8")
    src_dir = root / "SOURCES"
    src_dir.mkdir()
    payload = SIGNED_PAYLOAD

    # ① 把 dynamic_tmp_dir 配成仓库内**未被忽略**的目录 → 必须拒绝且不落盘
    leak = src_dir / "leak.m3u"
    rejected = False
    message = ""
    try:
        ingest.write_dynamic_snapshot(payload, leak, allowed_dir=src_dir)
    except ValueError as exc:
        rejected = True
        message = str(exc)
    print(f"  ① 未忽略目录：rejected={rejected}")
    if message:
        print(f"     理由：{message}")
    check(
        "仓库内未忽略目录被拒绝且不落盘",
        rejected and not leak.exists() and not list(src_dir.iterdir()),
        f"exists={leak.exists()}",
    )

    # ② 被 .gitignore 覆盖的目录（out/tmp）→ 允许
    allowed = root / "out" / "tmp"
    snap = ingest.write_dynamic_snapshot(payload, allowed / "ok.m3u", allowed_dir=allowed)
    print(f"  ② 已忽略目录：git_worktree={snap['git_worktree']} git_ignored={snap['git_ignored']}")
    check(
        "已忽略目录允许写入",
        (allowed / "ok.m3u").exists() and snap["git_ignored"] is True,
        f"file={(allowed / 'ok.m3u').exists()}",
    )


def repro_c(tmp: pathlib.Path) -> None:
    print("\n=== QA-002C：已跟踪文件不能靠 .gitignore 保护 ===")
    git_exe = ingest._resolve_git_executable()
    if git_exe is None:
        check("建立真实临时 Git 仓库", False, "本机找不到 git 可执行文件")
        return

    root = tmp / "real_repo"
    root.mkdir(parents=True)
    (root / ".gitignore").write_text("out/\n", encoding="utf-8", newline="\n")

    def git(*args: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [git_exe, *args], cwd=str(root), stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
        return proc

    git("-c", "init.defaultBranch=main", "init", "-q")
    allowed = root / "out" / "tmp"
    allowed.mkdir(parents=True)
    target = allowed / "signed.m3u"
    target.write_text("OLD\n", encoding="utf-8", newline="\n")

    noted = ingest.is_ignored_by_gitignore(target, root)
    print(f"  BEFORE_TRACKED {ingest.is_tracked_by_git(target, root)}  (ignored={noted})")

    # 大G 反例的原始步骤：即使 .gitignore 覆盖了 out/，强制加进索引后仍会被 Git 跟踪
    git("add", "-f", "--", "out/tmp/signed.m3u")
    tracked = ingest.is_tracked_by_git(target, root)
    print(f"  AFTER_GIT_ADD_F {tracked}")
    check("被强制加入索引后判定为已跟踪", tracked is True, f"tracked={tracked}")

    before = target.read_bytes()
    rejected, message = False, ""
    try:
        ingest.write_dynamic_snapshot(SIGNED_PAYLOAD, target, allowed_dir=allowed)
    except ValueError as exc:
        rejected, message = True, str(exc)
    print(f"  WRITE_ALLOWED {not rejected}")
    if message:
        print(f"     理由：{message}")
    check("已跟踪的（虽被忽略）快照文件写入被拒绝", rejected, f"rejected={rejected}")
    check("旧文件原字节不变", target.read_bytes() == before, f"bytes={len(before)}")

    # 对照：同一目录里未被跟踪的文件仍须照常可写
    fresh = allowed / "fresh.m3u"
    snap = ingest.write_dynamic_snapshot(SIGNED_PAYLOAD, fresh, allowed_dir=allowed)
    check(
        "未跟踪且被忽略的文件仍可写",
        fresh.exists() and snap["git_tracked"] is False and snap["git_ignored"] is True,
        f"git_tracked={snap['git_tracked']}",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = pathlib.Path(tmpdir)
        conn = db_mod.connect(str(tmp / "repro.sqlite3"))
        db_mod.init_db(conn)
        with RunningMock() as (server, base):
            print(f"mock source server: {base}")
            repro_a(conn, base, server)
        conn.close()
        repro_b(tmp)
        repro_c(tmp)

    failed = [r for r in _results if not r[0]]
    print(f"\n===== 汇总：{len(_results) - len(failed)}/{len(_results)} 通过 =====")
    for _ok, label, detail in failed:
        print(f"  FAIL {label}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
