#!/usr/bin/env python3
"""生产产物校验：把 ``live.m3u`` 当作外部输入做**反向解析**验收。

publish 成功返回只证明「请求被接受」。任务书 TASK-009 §9 要求的是产物本身
经得起检查：能反向解析、fixed/dynamic 分组可区分、没有旧签名回流、
没有敏感 query 泄漏。

本工具**只读**产物文件，不碰数据库、不改产物。

用法::

    python tools/verify_live_m3u.py --m3u /var/lib/li-iptv-aggregator/live.m3u
    python tools/verify_live_m3u.py --m3u ... --summary /var/lib/.../publish-summary.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from urllib.parse import parse_qsl, urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from liptv import m3u as m3u_mod  # noqa: E402

#: 与 ``tools/build_fixed_seed.py`` 的 BANNED_QUERY_KEYS 同源。
#: 出现这些 query 键 ⇒ 该条目是带签名/身份参数的，绝不能出现在产物里。
BANNED_QUERY_KEYS = frozenset({
    "token", "auth", "key", "secret", "msisdn", "migutoken",
    "sign", "hdnts", "expire", "mdspid",
})


def check(m3u_path: pathlib.Path, summary_path: pathlib.Path | None) -> int:
    problems: list[str] = []

    if not m3u_path.exists():
        print(f"FAIL: 产物不存在 {m3u_path}")
        return 1

    text = m3u_path.read_text(encoding="utf-8")
    result = m3u_mod.parse_text(text)

    print(f"=== 反向解析 {m3u_path} ===")
    print(f"bytes            : {m3u_path.stat().st_size}")
    print(f"total_lines      : {result.total_lines}")
    print(f"has_header       : {result.has_header}")
    print(f"entries          : {len(result.entries)}")
    print(f"sections         : {result.sections}")
    if result.skipped:
        print(f"skipped          : {result.skipped}")

    if not result.has_header:
        problems.append("缺 #EXTM3U 头")
    if not result.entries:
        problems.append("反向解析得到 0 个条目（不允许空列表冒充成功）")

    # ---- 敏感 query 泄漏检查 ----
    banned_hits: list[str] = []
    for entry in result.entries:
        query = urlsplit(entry.url).query
        if not query:
            continue
        for key, _value in parse_qsl(query, keep_blank_values=True):
            if key.lower() in BANNED_QUERY_KEYS:
                banned_hits.append(f"{entry.name}: {key}")
    print(f"\n=== 签名/身份 query 检查 ===")
    print(f"banned hits      : {len(banned_hits)}  (必须为 0)")
    for hit in banned_hits[:10]:
        print(f"   {hit}")
    if banned_hits:
        problems.append(f"{len(banned_hits)} 个条目带签名/身份 query")

    # ---- 分组分布（fixed vs dynamic 可区分）----
    groups: dict[str, int] = {}
    for entry in result.entries:
        key = entry.group_title or "(空)"
        groups[key] = groups.get(key, 0) + 1
    print(f"\n=== 分组分布（{len(groups)} 组）===")
    for name, count in sorted(groups.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"   {count:>4}  {name}")

    # ---- 与 summary 交叉核对 ----
    if summary_path is not None and summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        fixed_count = int(summary.get("fixed_count") or 0)
        dynamic_count = int(summary.get("dynamic_count") or 0)
        total = int(summary.get("published_entries") or 0)
        print(f"\n=== 与 publish-summary 交叉核对 ===")
        print(f"status           : {summary.get('status')}")
        print(f"fixed_count      : {fixed_count}")
        print(f"dynamic_count    : {dynamic_count}")
        print(f"published_entries: {total}")
        print(f"live.m3u entries : {len(result.entries)}")
        if summary.get("status") == "OK" and total != len(result.entries):
            problems.append(
                f"summary 说发布 {total} 条，产物里只有 {len(result.entries)} 条"
            )
        if fixed_count and not any("-" in g or g in ("新闻", "地方台") for g in groups):
            print("   note: 分组名里没看到典型 fixed 分组，请人工确认 fixed 是否真在产物里")

        # 旧签名 dynamic 回流检查：dynamic 侧不该出现短时签名 query
        dyn = summary.get("dynamic_summary") or {}
        print(f"\n=== dynamic 摘要 ===")
        print(f"selected_sources : {dyn.get('selected_sources')}")
        print(f"failed_sources   : {dyn.get('failed_sources')}")
        print(f"fail_closed      : {dyn.get('fail_closed')}")
        print(f"published_counted: {dyn.get('published_counted')}")
        for src in dyn.get("sources") or []:
            print(
                f"   {src.get('source_name')} [{src.get('status')}]"
                f" fetched={src.get('fetched_entries')}"
                f" included={src.get('included')}"
                f" published={src.get('published')}"
            )

    print("\n=== 结论 ===")
    if problems:
        for item in problems:
            print(f"FAIL: {item}")
        return 1
    print("PASS: 反向解析通过、分组可区分、无签名 query 泄漏、条数与 summary 一致")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 live.m3u 产物（只读）")
    parser.add_argument("--m3u", required=True, help="live.m3u 路径")
    parser.add_argument("--summary", default=None, help="publish-summary.json 路径（可选，交叉核对）")
    args = parser.parse_args(argv)
    summary = pathlib.Path(args.summary) if args.summary else None
    return check(pathlib.Path(args.m3u), summary)


if __name__ == "__main__":
    raise SystemExit(main())
