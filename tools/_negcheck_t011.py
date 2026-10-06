"""TASK-011 负向验证：证明新断言真能抓到对应缺陷。

**方法论红线**：只写「全绿」的测试没意义。必须证明每个关键断言在
「实现改回旧行为 / 去掉保护」时确实会失败，否则它只是一行永远为真的
装饰。

本脚本做 5 组验证，每组都**真刀真枪**地改坏实现再跑对应测试：

  1. metadata 冲突不再 fail-closed（后者覆盖前者）⇒ 对应测试必须 failed
  2. EPG 质量门禁放宽（stale feed 也算 usable）⇒ stale 测试必须 failed
  3. LKG 保护失效（失败时也写盘）⇒ LKG 测试必须 failed
  4. XMLTV display-name 改回 attribute 读取（引入 TASK-011 真bug）⇒
     display-name 测试必须 failed
  5. logo 假图片不再识别（text/html 也算可用）⇒ fake_html 测试必须 failed

跑法：``python tools/_negcheck_t011.py``（退出码 0 = 全部验证通过）
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
PYTEST = [sys.executable, "-m", "pytest", "-o", "addopts=", "-p", "no:cacheprovider", "-q"]


def _run(tests: list[str], basetemp: str) -> tuple[int, str]:
    proc = subprocess.run(
        PYTEST + [f"--basetemp={basetemp}", *tests],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "")[-3000:]


def _mutate(rel_path: str, pattern: str, replacement: str) -> pathlib.Path:
    """把仓库文件复制到临时目录并做定向替换，返回改坏后的副本路径。

    用**副本**而不是原地修改，避免「验证失败后忘了恢复」把仓库带坏 ——
    TASK-010 的负向验证就踩过这个坑（替换失败却以为成功了）。
    """
    src = REPO / rel_path
    backup = pathlib.Path(tempfile.mkdtemp()) / src.name
    shutil.copy2(src, backup)
    text = src.read_text(encoding="utf-8")
    new_text, count = re.subn(pattern, replacement, text, count=1)
    if count != 1:
        raise RuntimeError(
            f"{rel_path}: 模式未命中（{pattern!r}），负向验证无法进行。"
            f"这说明实现结构已变，必须同步更新本脚本 —— 而不是跳过验证。"
        )
    backup.write_text(new_text, encoding="utf-8", newline="\n")
    return backup


def check(name: str, *, rel_path: str, pattern: str, replacement: str,
          tests: list[str], expect: str) -> bool:
    """把实现改坏 → 跑指定测试 → 断言它真的 failed。"""
    print(f"\n[{name}]")
    print(f"    注入缺陷: {rel_path}")
    print(f"    期望    : {tests[0]} 等必须 failed")
    try:
        bad = _mutate(rel_path, pattern, replacement)
    except RuntimeError as exc:
        print(f"    ❌ {exc}")
        return False

    src = REPO / rel_path
    original = src.read_text(encoding="utf-8")
    try:
        src.write_text(bad.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        rc, out = _run(tests, tempfile.mkdtemp())
    finally:
        src.write_text(original, encoding="utf-8", newline="\n")

    failed = rc != 0
    tail = [ln for ln in out.splitlines() if ln.strip()][-1:] or ["(无输出)"]
    if failed:
        print(f"    ✅ 抓到 ✓  {tail[0][:110]}")
    else:
        print(f"    ❌ 没抓到!!  {tail[0][:110]}")
        print(f"    ⇒ 期望 {expect}，但这些测试在缺陷存在时仍然通过 ⇒ 断言无效")
    return failed


def main() -> int:
    results: list[bool] = []
    tmp = tempfile.mkdtemp()

    # 1) metadata tvg-id 冲突从 fail-closed 退化成「后者覆盖前者」
    results.append(check(
        "1. metadata tvg-id 冲突不再 fail-closed",
        rel_path="liptv/channel_metadata.py",
        pattern=r"                if owner is not None and owner != meta\.canonical:\n                    raise MetadataError\(",
        replacement="                if False:  # 负向验证：故意不报错\n                    raise MetadataError(",
        tests=["tests/test_task011.py::test_03_duplicate_tvg_id_rejected"],
        expect="duplicate tvg-id fail closed",
    ))

    # 2) EPG 质量门禁放宽：stale feed 也算usable
    results.append(check(
        "2. EPG stale 门禁失效",
        rel_path="liptv/epg.py",
        pattern=r"        if self\.channels_with_future_programme \* 2 < self\.channel_count:\n            return False",
        replacement="        if False:  # 负向验证：故意放过 stale feed\n            return False",
        tests=["tests/test_task011.py::test_stale_feed_rejected"],
        expect="过期 feed 被拒",
    ))

    # 3) LKG 保护失效：所有源都挂时也把空结果写盘
    results.append(check(
        "3. LKG 保护失效（源全挂也写盘）",
        rel_path="liptv/epg_refresh.py",
        pattern=r"        return RefreshResult\(\n            ok=False,\n            feeds=infos,\n            error=\"所有 EPG 源都不可用或解析失败\",",
        replacement="        try:  # 负向验证：故意覆盖 LKG\n            epg_mod.write_epg_atomic(b'<?xml version=\"1.0\"?><tv/>', target)\n        except Exception:\n            pass\n        return RefreshResult(\n            ok=False,\n            feeds=infos,\n            error=\"所有 EPG 源都不可用或解析失败\",",
        tests=["tests/test_task011.py::test_16_lkg_preserved_on_source_failure"],
        expect="源失败时 LKG 保留",
    ))

    # 4) 重新引入 display-name 用 attribute 读取的真实 bug
    results.append(check(
        "4. XMLTV display-name 改回 attribute 读取（本轮真 bug）",
        rel_path="liptv/epg.py",
        pattern=r'                \(node\.findtext\("display-name"\) or ""\)\.strip\(\)\n                or \(node\.get\("display-name"\) or ""\)\.strip\(\)\n                or cid',
        replacement='                (node.get("display-name") or "").strip() or cid  # 负向验证',
        tests=["tests/test_task011.py::test_10b_display_name_is_child_element_not_attribute"],
        expect="display-name 从子元素正确读取",
    ))

    # 5) logo 假图片不再识别
    results.append(check(
        "5. logo 假图片不再识别",
        rel_path="tools/helper_logo_probe.py",
        pattern=r'        if "text/html" in ctype:\n            return False, "fake_html"',
        replacement='        if False:  # 负向验证：HTML 也当图片\n            return False, "fake_html"',
        tests=["tests/test_task011.py::test_19_fake_html_logo_detected"],
        expect="HTML 假图片被识别",
    ))

    # 6) 🚨 TASK-011 生产验证抓到的真实缺陷：tvg-id 与 epg_channel_id 不一致
    results.append(check(
        "6. tvg-id 与 epg_channel_id 不一致不再被拒（生产真实缺陷）",
        rel_path="liptv/channel_metadata.py",
        pattern=r"            if meta\.epg_channel_id and meta\.tvg_id != meta\.epg_channel_id:\n                raise MetadataError\(",
        replacement="            if False:  # 负向验证：故意放过不一致\n                raise MetadataError(",
        tests=["tests/test_task011.py::test_23c_loader_rejects_mismatched_tvg_id"],
        expect="tvg-id 必须等于 epg_channel_id",
    ))

    passed = sum(1 for r in results if r)
    total = len(results)
    print("\n" + "=" * 62)
    if passed == total:
        print(f"✅ 负向验证 {passed}/{total} 全部通过：新断言确实在守护对应缺陷")
        return 0
    print(f"❌ 负向验证发现 {total - passed} 个问题：这些断言在缺陷存在时仍会通过")
    return 1


if __name__ == "__main__":
    sys.exit(main())