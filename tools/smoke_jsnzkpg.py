"""JSNZKPG 动态赛事源 —— 一次性手工 smoke（TASK-002 §交付 7）。

可选、手工、非自动化测试：
    python tools/smoke_jsnzkpg.py

行为约定（与 TASK-002 一致）：
  * 只 GET 入口 M3U 一次，验证响应头与可解析条目数；
  * **不抓底层赛事视频**，不对条目里的播放地址发任何请求；
  * 打印的播放地址一律脱敏（去 query、截断 path），不留完整 URL 到终端/报告/Git；
  * 公网不可达时标记 NETWORK_UNAVAILABLE，**退出码仍为 0** —— 离线不算失败，
    自动化测试从不依赖这个脚本。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liptv import fetch as fetch_mod  # noqa: E402
from liptv import ingest  # noqa: E402

DEFAULT_URL = "https://jsnzkpg.de5.net/all.m3u"

# 明确归类为「本机/公网不可达」，不算 smoke 失败
_UNREACHABLE = (
    fetch_mod.ERROR_NETWORK,
    fetch_mod.ERROR_TIMEOUT,
    fetch_mod.ERROR_TOO_MANY_REDIRECTS,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JSNZKPG 动态赛事源手工 smoke")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--sample", type=int, default=5, help="打印多少条脱敏字段样本")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    limits = fetch_mod.FetchLimits(timeout_seconds=15.0, max_bytes=5_000_000, max_redirects=3)
    report: dict = {"url": args.url, "url_redacted": ingest.redact_url(args.url)}

    try:
        fetched = fetch_mod.fetch_text(args.url, limits=limits)
        parsed = ingest.validate_m3u_text(fetched.text)
    except fetch_mod.FetchError as exc:
        report.update(
            {
                "status": "NETWORK_UNAVAILABLE" if exc.category in _UNREACHABLE else "SMOKE_ERROR",
                "error_category": exc.category,
                "error": str(exc),
                "note": "本脚本为可选手工 smoke；不可达不影响 TASK-002 验收。",
            }
        )
        _print(report, args.json)
        return 0

    groups: dict[str, int] = {}
    for entry in parsed.entries:
        key = entry.group_title or "(no-group)"
        groups[key] = groups.get(key, 0) + 1

    report.update(
        {
            "status": "OK",
            "http_status": fetched.status,
            "content_type": fetched.content_type,
            "charset": fetched.charset,
            "bytes": fetched.byte_count,
            "entry_count": parsed.entry_count,
            "has_header": parsed.has_header,
            "skipped": parsed.skipped,
            "groups": groups,
            # 只放脱敏样本，绝不输出完整带签名 URL
            "sample": ingest.summarize_entries(parsed.entries)[: args.sample],
        }
    )
    _print(report, args.json)
    return 0


def _print(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    print(f"url           : {report['url_redacted']}")
    print(f"status        : {report['status']}")
    if report["status"] != "OK":
        print(f"error         : [{report.get('error_category')}] {report.get('error')}")
        print("note          : 本脚本为可选手工 smoke；不可达不影响 TASK-002 验收。")
        return
    print(f"http_status   : {report['http_status']}")
    print(f"content_type  : {report['content_type']} (charset={report['charset']})")
    print(f"bytes         : {report['bytes']}")
    print(f"entry_count   : {report['entry_count']}")
    print(f"has_header    : {report['has_header']}")
    print(f"skipped       : {report['skipped']}")
    print("groups        : " + ", ".join(f"{k}={v}" for k, v in sorted(report["groups"].items())))
    print(f"\n字段样本（前 {len(report['sample'])} 条，播放地址已脱敏）：")
    for item in report["sample"]:
        print(
            f"  {item['index']:>3}. name={item['name']!r} "
            f"group={item['group_title']!r} tags={item['tags']} "
            f"ext={item['url_ext']} url={item['url_redacted']}"
        )
    print("\n⚠ 本次未播放验证任何线路，也未核验赛事版权/重分发授权。")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
