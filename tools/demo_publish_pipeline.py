"""本地无网「统一 M3U 组合与安全发布」端到端演示（TASK-003）。

一条命令跑完全部场景，**不访问任何公网地址**：

    python tools/demo_publish_pipeline.py

流程（全部打本机 mock HTTP 服务）：

  0. 初始化数据库 + 按配置注册来源；
  1. 固定源：抓取 → 建 canonical/绑定 → 归集 stream → 写入「模拟测活」结果；
  2. 只发固定频道（`publish` 默认完全不联网）；
  3. 固定 + 本轮动态赛事统一发布（离线 mock 动态样本）；
  4. 展示动态纳入/过滤规则的可解释计数（重复/回放/宣传/TG 推广）；
  5. 动态源失败 → fail-closed 降级为只发布固定频道（不复用旧签名线路）；
  6. `--require-dynamic` → 整次拒绝，当前与上一版文件**字节不变**；
  7. `--dry-run` → 不改任何文件；
  8. 信息边界：stdout JSON 与发布摘要都不含完整签名 URL / playpath；
  9. QA-003A 反例：**真实上游结构**（联赛名分组 + `# ===== 直播/回放 =====` 注释分区）
     默认被正确纳入；
 10. QA-003B 反例：**多动态源有一个失败** → 状态、计数与**实际文件内容**三者一致
     （真正只发固定，不留任何动态线路）。

产物全部落在 out/demo-task003/（已被 .gitignore 忽略），不含任何真实公网地址。
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
from liptv import m3u as m3u_mod  # noqa: E402
from liptv import publish as publish_mod  # noqa: E402

# 固定参考时刻：让选线窗口与摘要里的时间戳可复现（不依赖真实时钟）
NOW = "2026-09-30T12:00:00+00:00"

WORK_DIR = REPO_ROOT / "out" / "demo-task003"

# mock 动态样本里刻意带上的短时签名材料；任何输出里都不许出现
SECRETS = ("txSecret", "txTime", "AAA111", "BBB222", "CCC333", "GGG777",
           "DDD444", "EEE555", "FFF666", "H1H1H1", "H8H8H8", "HAHAHA")

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
    out_dir = WORK_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = WORK_DIR / "config.toml"
    db = WORK_DIR / "liptv.sqlite3"
    live = out_dir / "live.m3u"
    previous = out_dir / "live.previous.m3u"
    summary = out_dir / "publish-summary.json"

    with RunningMock() as (server, base):
        cfg.write_text(
            "[database]\n"
            f'path = "{db.as_posix()}"\n'
            "\n[output]\n"
            f'm3u_path = "{live.as_posix()}"\n'
            "keep_previous = true\n"
            "\n[fetch]\n"
            "timeout_seconds = 5.0\n"
            "max_bytes = 2000000\n"
            "max_redirects = 3\n"
            f'dynamic_tmp_dir = "{(WORK_DIR / "tmp").as_posix()}"\n'
            "\n[publish]\n"
            f'summary_path = "{summary.as_posix()}"\n'
            "\n[[sources]]\n"
            'name = "demo-fixed"\n'
            'kind = "fixed_m3u"\n'
            f'url = "{base}/seq.m3u"\n'
            "enabled = true\n"
            "\n[[sources]]\n"
            'name = "demo-dynamic"\n'
            'kind = "dynamic_event_m3u"\n'
            f'url = "{base}/dynamic-publish.m3u"\n'
            "enabled = false\n"
            "\n[[sources]]\n"
            'name = "demo-dynamic-bad"\n'
            'kind = "dynamic_event_m3u"\n'
            f'url = "{base}/error.m3u"\n'
            "enabled = false\n"
            "\n[[sources]]\n"
            'name = "demo-dynamic-real"\n'
            'kind = "dynamic_event_m3u"\n'
            f'url = "{base}/dynamic-real-structure.m3u"\n'
            "enabled = false\n",
            encoding="utf-8",
        )

        print(f"mock server : {base}")
        print(f"工作目录    : {WORK_DIR}")

        # ---------------------------------------------------------- 0
        section("0. 初始化与来源注册")
        code, payload = run("init-db", "--config", cfg, "--json")
        check("init-db", code == 0 and payload["schema_version"] == 1, f"exit={code}")
        code, payload = run("source-register", "--from-config", "--config", cfg, "--json")
        check(
            "source-register（默认禁用动态源）",
            code == 0 and payload["registered"] == 4 and payload["enabled_count"] == 1,
            f"registered={payload['registered']} enabled={payload['enabled_count']} "
            f"disabled={payload['disabled_count']}",
        )

        # ---------------------------------------------------------- 1
        section("1. 固定源：抓取 → 归一化 → 写入模拟测活结果")
        server.state.set_ok()
        code, payload = run("fetch", "--all", "--config", cfg, "--json")
        check(
            "fetch --all（只抓 enabled 的 fixed 源）",
            code == 0 and payload["total_created"] == 3,
            f"exit={code} requested={payload['requested']} created={payload['total_created']}",
        )

        code, rows = run("source-channel-list", "--config", cfg, "--json")
        assert code == 0, rows
        canonical_ids: list[int] = []
        for row in rows:
            code, payload = run(
                "canonical-add", "--name", row["raw_name"],
                "--category", row["raw_group"] or "其他",
                "--now", NOW, "--config", cfg, "--json",
            )
            assert code == 0, payload
            cid = payload["canonical_channel_id"]
            canonical_ids.append(cid)
            code, payload = run(
                "binding-add", "--source-channel-id", row["id"], "--canonical-id", cid,
                "--now", NOW, "--config", cfg, "--json",
            )
            assert code == 0, payload
        code, payload = run("stream-sync", "--now", NOW, "--config", cfg, "--json")
        assert code == 0, payload

        code, streams = run("stream-list", "--config", cfg, "--json")
        assert code == 0, streams
        for stream in streams:
            code, payload = run(
                "probe-result-add", "--stream-id", stream["id"], "--probe", "demo-probe",
                "--checked-at", NOW, "--startup-ms", 1200,
                "--resolution", "1920x1080", "--bitrate", 4000,
                "--protocol", "hls", "--config", cfg, "--json",
            )
            assert code == 0, payload
        check(
            "固定侧准备完成",
            len(canonical_ids) == 3 and len(streams) == 3,
            f"canonical={len(canonical_ids)} stream={len(streams)} probe_result={len(streams)}",
        )

        # ---------------------------------------------------------- 2
        section("2. 只发固定频道（publish 默认完全不联网）")
        code, payload = run("publish", "--config", cfg, "--now", NOW, "--json")
        check(
            "publish（无动态）",
            code == 0 and payload["status"] == publish_mod.STATUS_OK
            and payload["fixed_count"] == 3 and payload["dynamic_count"] == 0
            and payload["include_dynamic"] is False,
            f"exit={code} status={payload['status']} "
            f"fixed={payload['fixed_count']} dynamic={payload['dynamic_count']}",
        )
        first_text = live.read_text(encoding="utf-8")
        parsed = m3u_mod.parse_text(first_text)
        check(
            "输出结构合法（头 + 3 对 EXTINF/URL + 分类顺序）",
            parsed.has_header and parsed.entry_count == 3
            and [e.group_title for e in parsed.entries] == ["体育", "新闻", "纪录片"],
            f"header={parsed.has_header} entries={parsed.entry_count} "
            f"groups={[e.group_title for e in parsed.entries]}",
        )
        check(
            "默认路径不写 previous（首次发布）",
            payload["previous"] is None,
            f"previous={payload['previous']}",
        )

        # ---------------------------------------------------------- 3
        section("3. 固定 + 动态赛事统一发布（离线 mock 动态源）")
        code, payload = run("publish", "--dynamic-source", "demo-dynamic",
                            "--config", cfg, "--now", NOW, "--json")
        parsed = m3u_mod.parse_text(live.read_text(encoding="utf-8"))
        names = [e.name for e in parsed.entries]
        groups = [e.group_title for e in parsed.entries]
        dynamic_group = publish_mod.DEFAULT_DYNAMIC_GROUP_TITLE
        check(
            "两类条目齐备、固定在前、动态独立分组",
            code == 0 and payload["status"] == publish_mod.STATUS_OK
            and payload["fixed_count"] == 3 and payload["dynamic_count"] == 3
            and payload["channel_count"] == 6
            and groups[:3] == ["体育", "新闻", "纪录片"]
            and groups[3:] == [dynamic_group] * 3,
            f"exit={code} fixed={payload['fixed_count']} dynamic={payload['dynamic_count']} "
            f"groups={groups}",
        )
        check(
            "[解说] / [原声] 各自保留（不合并）",
            "[解说] 曼城 vs 阿森纳" in names and "[原声] 曼城 vs 阿森纳" in names,
            f"names={names}",
        )
        check(
            "每 canonical 只输出一条线路",
            len([e for e in parsed.entries if e.group_title != dynamic_group]) == 3,
            f"fixed_entries={len([e for e in parsed.entries if e.group_title != dynamic_group])}",
        )
        check(
            "上一版被留到 live.previous.m3u",
            previous.exists() and previous.read_text(encoding="utf-8") == first_text,
            f"previous_exists={previous.exists()}",
        )

        # ---------------------------------------------------------- 4
        section("4. 纳入/过滤规则的可解释计数")
        report = payload["dynamic_sources"][0]
        labels = report["excluded_by_reason"]
        dup = labels.get(publish_mod.REASON_LABELS[publish_mod.REASON_DUPLICATE], 0)
        replay = labels.get(publish_mod.REASON_LABELS[publish_mod.REASON_REPLAY_DISABLED], 0)
        promo = labels.get(publish_mod.REASON_LABELS[publish_mod.REASON_EXCLUDED_GROUP], 0)
        keyword = labels.get(publish_mod.REASON_LABELS[publish_mod.REASON_EXCLUDED_KEYWORD], 0)
        check(
            "同源字节重复去重 1 条",
            report["fetched_entries"] == 7 and report["included"] == 3 and dup == 1,
            f"fetched={report['fetched_entries']} included={report['included']} duplicate={dup}",
        )
        check(
            "回放默认关闭 + 宣传/TG 推广被排除",
            replay == 1 and promo == 1 and keyword == 1,
            f"replay_disabled={replay} excluded_group={promo} excluded_keyword={keyword}",
        )

        # ---------------------------------------------------------- 5
        section("5. 动态源失败 → fail-closed 降级（不复用旧签名线路）")
        code, payload = run("publish", "--dynamic-source", "demo-dynamic-bad",
                            "--config", cfg, "--now", NOW, "--json")
        degraded_text = live.read_text(encoding="utf-8")
        check(
            "降级为只发布固定频道（exit 0）",
            code == 0 and payload["status"] == publish_mod.STATUS_DEGRADED_FIXED_ONLY
            and payload["published"] is True and payload["dynamic_count"] == 0,
            f"exit={code} status={payload['status']} published={payload['published']}",
        )
        check(
            "新文件里没有动态分组、也没有旧签名材料",
            dynamic_group not in degraded_text
            and not any(s in degraded_text for s in SECRETS),
            f"动态分组出现={'是' if dynamic_group in degraded_text else '否'}",
        )

        # ---------------------------------------------------------- 6
        section("6. --require-dynamic → 整次拒绝，文件字节不变")
        live_before = live.read_bytes()
        prev_before = previous.read_bytes()
        code, payload = run("publish", "--dynamic-source", "demo-dynamic-bad",
                            "--require-dynamic", "--config", cfg, "--now", NOW, "--json")
        check(
            "整次拒绝（exit 1）",
            code == 1 and payload["status"] == publish_mod.STATUS_REJECTED_DYNAMIC_REQUIRED
            and payload["published"] is False,
            f"exit={code} status={payload['status']} reason={payload.get('reason')}",
        )
        check(
            "当前与上一版文件一个字节都没改",
            live.read_bytes() == live_before and previous.read_bytes() == prev_before,
            f"live未变={live.read_bytes() == live_before} "
            f"previous未变={previous.read_bytes() == prev_before}",
        )

        # ---------------------------------------------------------- 7
        section("7. --dry-run 不写任何文件")
        live_before = live.read_bytes()
        summary_before = summary.read_bytes()
        code, payload = run("publish", "--dynamic", "--dry-run",
                            "--config", cfg, "--now", NOW, "--json")
        check(
            "dry-run 只组合与校验",
            code == 0 and payload["status"] == publish_mod.STATUS_DRY_RUN
            and payload["published"] is False and bool(payload["expected_checksum"])
            and payload["summary"] is None,
            f"exit={code} status={payload['status']} "
            f"expected_checksum={str(payload['expected_checksum'])[:12]}…",
        )
        check(
            "live.m3u 与摘要文件均未被改写",
            live.read_bytes() == live_before and summary.read_bytes() == summary_before,
            f"live未变={live.read_bytes() == live_before} "
            f"摘要未变={summary.read_bytes() == summary_before}",
        )

        # ---------------------------------------------------------- 8
        section("8. 信息边界：签名 URL 只存在 live.m3u")
        code, payload = run("publish", "--dynamic-source", "demo-dynamic",
                            "--config", cfg, "--now", NOW, "--json")
        stdout_blob = json.dumps(payload, ensure_ascii=False)
        summary_text = summary.read_text(encoding="utf-8")
        live_text = live.read_text(encoding="utf-8")
        check(
            "stdout JSON 与发布摘要都不含签名参数 / playpath",
            not any(s in stdout_blob for s in SECRETS)
            and not any(s in summary_text for s in SECRETS)
            and "txSecret" not in stdout_blob and "txSecret" not in summary_text
            and "/live/mci-ars/" not in summary_text and "pc.m3u8" not in summary_text,
            "脱敏口径：只留 scheme://host，path 与 query 一律抹掉",
        )
        check(
            "播放器要读的 live.m3u 里线路原样保留（否则没法播）",
            "txSecret=AAA111" in live_text,
            "live.m3u 内保留完整线路",
        )
        check(
            "摘要含监测字段（计数/过滤/来源/checksum/退出码）",
            all(k in json.loads(summary_text) for k in (
                "published_at", "status", "fixed_count", "dynamic_count", "channel_count",
                "dynamic_sources", "dynamic_excluded_by_reason", "warnings",
                "checksum", "bytes", "exit_code", "note")),
            f"summary={summary}",
        )

        # ---------------------------------------------------------- 9
        section("9. QA-003A 反例：真实上游结构（联赛名分组 + 直播/回放注释分区）")
        code, payload = run("publish", "--dynamic-source", "demo-dynamic-real",
                            "--config", cfg, "--now", NOW, "--json")
        report = payload["dynamic_sources"][0]
        labels = report["excluded_by_reason"]
        real_text = live.read_text(encoding="utf-8")
        real_names = [e.name for e in m3u_mod.parse_text(real_text).entries]
        check(
            "默认策略纳入各联赛分组（旧版本这里会是 0/N）",
            code == 0 and report["fetched_entries"] == 11 and report["included"] == 5
            and payload["dynamic_count"] == 5 and payload["status"] == publish_mod.STATUS_OK,
            f"fetched={report['fetched_entries']} included={report['included']} "
            f"dynamic={payload['dynamic_count']}",
        )
        check(
            "宣传/TG 推广/回放（分组名与分区两种写法）都被排除、理由可解释",
            labels.get(publish_mod.REASON_LABELS[publish_mod.REASON_EXCLUDED_GROUP]) == 1
            and labels.get(publish_mod.REASON_LABELS[publish_mod.REASON_EXCLUDED_KEYWORD]) == 1
            and labels.get(publish_mod.REASON_LABELS[publish_mod.REASON_REPLAY_DISABLED]) == 2
            and labels.get(publish_mod.REASON_LABELS[publish_mod.REASON_REPLAY_SECTION]) == 1
            and publish_mod.REASON_LABELS[publish_mod.REASON_NOT_IN_INCLUDE_LIST] not in labels,
            f"labels={labels}",
        )
        check(
            "分区被识别；[解说]/[原声] 保留；回放与推广不落盘",
            report["sections_seen"].get("正在直播", 0) >= 1
            and report["sections_seen"].get("赛事回放", 0) >= 1
            and "[解说] 纽约自由人 vs 拉斯维加斯王牌" in real_names
            and "[原声] 纽约自由人 vs 拉斯维加斯王牌" in real_names
            and "官方 App" not in real_text and "TG频道@stymei" not in real_text
            and "旧比赛之一" not in real_text and "上周的自由人" not in real_text,
            f"sections={report['sections_seen']} names={len(real_names)}",
        )

        # ---------------------------------------------------------- 10
        section("10. QA-003B 反例：多动态源部分失败 → 状态与文件内容一致")
        code, payload = run("publish", "--dynamic-source", "demo-dynamic",
                            "--dynamic-source", "demo-dynamic-bad",
                            "--config", cfg, "--now", NOW, "--json")
        partial_text = live.read_text(encoding="utf-8")
        partial_parsed = m3u_mod.parse_text(partial_text)
        check(
            "状态=仅固定，且 dynamic_count=0 / fail-closed 被如实记录",
            code == 0 and payload["status"] == publish_mod.STATUS_DEGRADED_FIXED_ONLY
            and payload["dynamic_count"] == 0 and payload["dynamic_fail_closed"] is True
            and payload["dynamic_discarded"] == 3,
            f"exit={code} status={payload['status']} discarded={payload['dynamic_discarded']}",
        )
        check(
            "实际文件里没有任何动态线路（旧版本这里会残留成功来源的动态条目）",
            dynamic_group not in partial_text
            and partial_parsed.entry_count == 3
            and all(e.group_title != dynamic_group for e in partial_parsed.entries)
            and "曼城 vs 阿森纳" not in partial_text
            and not any(s in partial_text for s in SECRETS),
            f"entries={partial_parsed.entry_count} 动态分组出现="
            f"{'是' if dynamic_group in partial_text else '否'}",
        )
        partial_summary = json.loads(summary.read_text(encoding="utf-8"))
        check(
            "摘要与状态一致（dynamic_count=0 / fail_closed=true）",
            partial_summary["status"] == publish_mod.STATUS_DEGRADED_FIXED_ONLY
            and partial_summary["dynamic_count"] == 0
            and partial_summary["dynamic_fail_closed"] is True
            and partial_summary["dynamic_discarded"] == 3,
            f"summary_status={partial_summary['status']}",
        )

    failed = [r for r in _results if not r[0]]
    print(f"\n===== 汇总：{len(_results) - len(failed)}/{len(_results)} 通过 =====")
    for _ok, label, detail in failed:
        print(f"  FAIL {label}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
