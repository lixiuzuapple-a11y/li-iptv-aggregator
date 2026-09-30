"""本地无网端到端演示（TASK-002 §交付 6）。

一条命令跑完全部场景，**不访问任何公网地址**：

    python tools/demo_local_pipeline.py

覆盖：成功首次抓取、重复幂等、条目变更（消失/新增）、恢复、HTTP 错误、
空 M3U、损坏 M3U、非 M3U（HTML 错误页）、动态赛事源带签名 URL 的脱敏预览。

所有产物都落在 out/demo-task002/（已被 .gitignore 忽略）。
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import shutil
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.mock_source_server import RunningMock  # noqa: E402

from liptv.cli import main as cli_main  # noqa: E402

WORK_DIR = REPO_ROOT / "out" / "demo-task002"

_results: list[tuple[bool, str, str]] = []


def run(*argv) -> tuple[int, object]:
    """调用 CLI 并解析输出（--json 时返回 dict）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main([str(a) for a in argv])
    text = buf.getvalue()
    try:
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, text


def check(label: str, ok: bool, detail: str) -> None:
    _results.append((ok, label, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    (WORK_DIR / "tmp").mkdir(parents=True, exist_ok=True)
    cfg = WORK_DIR / "config.toml"
    db = WORK_DIR / "liptv.sqlite3"

    with RunningMock() as (server, base):
        cfg.write_text(
            "[database]\n"
            f'path = "{db.as_posix()}"\n'
            "[fetch]\n"
            "timeout_seconds = 5.0\n"
            "max_bytes = 2000000\n"
            "max_redirects = 3\n"
            f'dynamic_tmp_dir = "{(WORK_DIR / "tmp").as_posix()}"\n'
            "[[sources]]\n"
            'name = "demo-fixed"\n'
            'kind = "fixed_m3u"\n'
            f'url = "{base}/seq.m3u"\n'
            "enabled = true\n"
            "[[sources]]\n"
            'name = "demo-disabled"\n'
            'kind = "fixed_m3u"\n'
            f'url = "{base}/ok.m3u"\n'
            "enabled = false\n"
            "[[sources]]\n"
            'name = "demo-dynamic"\n'
            'kind = "dynamic_event_m3u"\n'
            f'url = "{base}/dynamic.m3u"\n'
            "enabled = false\n",
            encoding="utf-8",
        )

        print(f"mock server: {base}")
        print(f"工作目录 : {WORK_DIR}")
        section("0. 初始化与来源注册")
        code, payload = run("init-db", "--config", cfg, "--json")
        check("init-db", code == 0 and payload["schema_version"] == 1, f"exit={code}")
        code, payload = run("source-register", "--from-config", "--config", cfg, "--json")
        check(
            "source-register",
            code == 0 and payload["registered"] == 3 and payload["disabled_count"] == 2,
            f"registered={payload['registered']} enabled={payload['enabled_count']} "
            f"disabled={payload['disabled_count']}",
        )

        section("1. 首次抓取（成功）")
        server.state.set_ok()
        code, payload = run("fetch", "--all", "--config", cfg, "--json")
        check(
            "fetch --all 首抓",
            code == 0 and payload["total_created"] == 3,
            f"exit={code} requested={payload['requested']} created={payload['total_created']}",
        )
        check(
            "禁用源 / 动态源被跳过",
            [s["name"] for s in payload["skipped_disabled"]] == ["demo-disabled"]
            and [s["name"] for s in payload["skipped_dynamic"]] == ["demo-dynamic"],
            f"skipped_disabled={[s['name'] for s in payload['skipped_disabled']]} "
            f"skipped_dynamic={[s['name'] for s in payload['skipped_dynamic']]}",
        )

        section("2. 重复抓取（幂等）")
        code, payload = run("fetch", "--source", "demo-fixed", "--config", cfg, "--json")
        row = payload["results"][0]
        check(
            "幂等",
            code == 0 and row["created"] == 0 and row["updated"] == 3,
            f"created={row['created']} updated={row['updated']} deactivated={row['deactivated']}",
        )

        section("3. 条目变更（1 条消失 / 1 条新增）")
        server.state.set_changed()
        code, payload = run("fetch", "--source", "demo-fixed", "--config", cfg, "--json")
        row = payload["results"][0]
        check(
            "消失置 inactive + 新增",
            code == 0 and row["deactivated"] == 1 and row["created"] == 1,
            f"created={row['created']} updated={row['updated']} deactivated={row['deactivated']}",
        )
        code, payload = run("source-status", "--config", cfg, "--json")
        fixed = next(s for s in payload["sources"] if s["name"] == "demo-fixed")
        check(
            "库存统计",
            fixed["channels_active"] == 3 and fixed["channels_inactive"] == 1,
            f"active={fixed['channels_active']} inactive={fixed['channels_inactive']} "
            f"total={fixed['channels_total']}",
        )

        section("4. 条目恢复")
        server.state.set_ok()
        code, payload = run("fetch", "--source", "demo-fixed", "--config", cfg, "--json")
        row = payload["results"][0]
        check(
            "恢复 active（复用原身份）",
            code == 0 and row["reactivated"] == 1,
            f"reactivated={row['reactivated']}",
        )

        section("5. 各类失败：库存必须零改动")
        for label, path, expected in (
            ("HTTP 500", "/error.m3u", "HTTP_STATUS"),
            ("空响应体", "/empty.m3u", "EMPTY_LIST"),
            ("有结构无条目", "/broken.m3u", "EMPTY_LIST"),
            ("HTML 错误页", "/notm3u.m3u", "INVALID_M3U"),
        ):
            before = run("source-status", "--config", cfg, "--json")[1]
            before_fixed = next(s for s in before["sources"] if s["name"] == "demo-fixed")
            run("source-add", "--name", "demo-fixed", "--kind", "fixed_m3u",
                "--url", f"{base}{path}", "--config", cfg, "--json")
            code, payload = run("fetch", "--source", "demo-fixed", "--config", cfg, "--json")
            after = run("source-status", "--config", cfg, "--json")[1]
            after_fixed = next(s for s in after["sources"] if s["name"] == "demo-fixed")
            unchanged = (
                before_fixed["channels_active"] == after_fixed["channels_active"]
                and before_fixed["channels_inactive"] == after_fixed["channels_inactive"]
            )
            check(
                f"{label} → {expected}",
                code == 1
                and payload["results"][0]["status"] == expected
                and unchanged,
                f"exit={code} status={payload['results'][0]['status']} "
                f"库存未变={unchanged}",
            )
        # 恢复成可用地址，便于后续步骤
        run("source-add", "--name", "demo-fixed", "--kind", "fixed_m3u",
            "--url", f"{base}/seq.m3u", "--config", cfg, "--json")

        section("6. 动态赛事源（带签名 URL）")
        code, payload = run("dynamic-fetch", "--source", "demo-dynamic",
                            "--config", cfg, "--json")
        blob = json.dumps(payload, ensure_ascii=False)
        leaked = [s for s in ("txSecret", "DEADBEEF1234567890", "CAFEBABE9876543210")
                  if s in blob]
        check(
            "临时解析 + 脱敏",
            code == 0 and payload["entry_count"] == 4 and not leaked,
            f"exit={code} entries={payload['entry_count']} "
            f"groups={payload['groups']} 泄漏={leaked or '无'}",
        )
        check(
            "动态源不落库",
            payload["persisted"] is False,
            f"persisted={payload['persisted']}",
        )

        section("7. 动态源不进入固定频道输出")
        code, payload = run("select", "--all", "--config", cfg, "--json")
        check(
            "select --all 无动态条目",
            code == 0 and payload["selected"] == [],
            f"selected={len(payload['selected'])} skipped={len(payload['skipped'])}",
        )

    failed = [r for r in _results if not r[0]]
    print(f"\n===== 汇总：{len(_results) - len(failed)}/{len(_results)} 通过 =====")
    for _ok, label, detail in failed:
        print(f"  FAIL {label}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
