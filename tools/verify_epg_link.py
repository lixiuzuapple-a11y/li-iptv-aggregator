"""TASK-011 §19 —— M3U ↔ XMLTV 关联核验（**只读**）。

任务书 §19 要求抽样核对::

    M3U tvg-id == XMLTV channel id

为什么要单独一个脚本：这是本轮**唯一**能证明「节目单真的能用」的检查。
只检查「M3U 里有没有 tvg-id」完全不够 —— TASK-011 实测发现初版
tvg-id 与 XMLTV channel id 的**交集是 0**，也就是说节目单 100% 失效，
而那种缺陷在存在性检查下是隐形的。

因此本脚本做三件事：

1. 算交集，并**列出未匹配项**（而不是只报一个数字）；
2. 按 §19 点名清单抽样打印（CCTV-1/5/13、北京/东方/湖南/广东卫视…），
   每个都显示 M3U 侧 id 与 XMLTV 侧 id，让「对上了」可以被肉眼复核；
3. 反向检查：XMLTV 里的 programme 是不是都挂在**已声明**的 channel 上。

只读：不写DB、不改文件、不联网。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

#: §19 点名的抽样清单（找不到就如实报missing，不静默跳过）
SAMPLE = [
    "CCTV-1 综合", "CCTV-5 体育", "CCTV-5+ 体育赛事", "CCTV-13 新闻",
    "北京卫视", "东方卫视", "湖南卫视", "广东卫视",
    "France 24", "NHK World",
]

ATTR = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def parse_m3u(path: pathlib.Path) -> list[dict]:
    """从 M3U 抽 (display_name, tvg_id, group, logo)。"""
    out: list[dict] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith("#EXTINF"):
            continue
        attrs = dict(ATTR.findall(line))
        disp = line.split(",", 1)
        name = disp[1].strip() if len(disp) > 1 else ""
        out.append({
            "name": name or attrs.get("tvg-name", ""),
            "tvg_id": attrs.get("tvg-id", ""),
            "logo": attrs.get("tvg-logo", ""),
            "group": attrs.get("group-title", ""),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--m3u", default="/var/lib/li-iptv-aggregator/live.m3u")
    ap.add_argument("--epg", default="/etc/li-iptv-aggregator/epg.xml")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    entries = parse_m3u(pathlib.Path(args.m3u))
    tree = ET.parse(args.epg)
    root = tree.getroot()

    channels = {c.get("id"): c for c in root.findall("channel")}
    programmes = root.findall("programme")
    prog_channels = {p.get("channel") for p in programmes}

    m3u_ids = {e["tvg_id"] for e in entries if e["tvg_id"]}
    with_id = [e for e in entries if e["tvg_id"]]
    matched = sorted(m3u_ids & set(channels))
    unmatched_m3u = sorted(m3u_ids - set(channels))
    orphan_prog = sorted(prog_channels - set(channels))

    report = {
        "m3u": args.m3u,
        "epg": args.epg,
        "m3u_entries": len(entries),
        "m3u_with_tvg_id": len(with_id),
        "m3u_with_logo": sum(1 for e in entries if e["logo"]),
        "xmltv_channels": len(channels),
        "xmltv_programmes": len(programmes),
        "intersection": len(matched),
        "unmatched_m3u_ids": unmatched_m3u,
        "orphan_programme_channels": orphan_prog,
        "dynamic_without_tvg_id": sorted(
            e["name"] for e in entries if not e["tvg_id"]),
    }

    print(f"M3U entries        : {len(entries)}"
          f"（tvg-id {len(with_id)}、logo {report['m3u_with_logo']}）")
    print(f"XMLTV channels     : {len(channels)}")
    print(f"XMLTV programmes   : {len(programmes)}")
    print(f"INTERSECTION       : {len(matched)} / {len(m3u_ids)}")
    if unmatched_m3u:
        print(f"⚠ M3U 有 id 但 XMLTV 没有: {unmatched_m3u}")
    else:
        print("✓ M3U 的每个 tvg-id 都能在 XMLTV 里找到 channel")
    if orphan_prog:
        print(f"⚠ programme 引用了未声明 channel: {orphan_prog[:8]}")
    else:
        print("✓ 没有 programme 引用未声明的 channel")

    dyn = report["dynamic_without_tvg_id"]
    print(f"无 tvg-id 的条目（动态赛事，符合预期）: {len(dyn)}")

    print("\n--- §19 抽样核对 ---")
    by_name = {e["name"]: e for e in entries}
    for want in SAMPLE:
        hit = None
        for nm, e in by_name.items():
            if nm == want or nm.startswith(want):
                hit = e
                break
        if hit is None:
            print(f"  {want:16s} —— M3U 里不存在（如实报告，不静默跳过）")
            continue
        tid = hit["tvg_id"]
        ok = tid in channels
        disp = channels[tid].findtext("display-name") if ok else None
        print(f"  {want:16s} tvg-id={tid!r:14s} "
              f"XMLTV={'HIT' if ok else 'MISS'} display-name={disp!r}")

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        pathlib.Path(args.out).write_text(text + "\n", encoding="utf-8", newline="\n")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())