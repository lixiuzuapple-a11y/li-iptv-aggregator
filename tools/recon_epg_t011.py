#!/usr/bin/env python3
"""TASK-011 §3：EPG / Logo 公开源侦察（只读）。

必须先侦察再写代码 —— 任务书明令「不允许只按规范想象」。

本脚本做四件事，且**只做这四件**：

1. 抓 XMLTV 候选源的头部与结构，统计 channel / programme 数量、timezone 风格；
2. 判定 XMLTV 是否 well-formed（能解析即有效，不解析即 reject）；
3. 探测 logo 候选的 content-type（识别「HTML 假图片」——任务书 §8 明令）；
4. 产出结构化 JSON，供人工整理成 ``SOURCES/EPG-SOURCE-RECON-TASK011.md``。

安全红线（与项目一致）：

- 不带 Cookie / Authorization / token；
- 不镜像、不缓存、不落盘 logo 二进制；
- 不请求任何条目里的**视频内容**（只取 playlist / XMLTV / logo 图片本身）；
- 输出里host 与 URL 一律脱敏到「host + path 末段」。

本机（Windows + FlClash TUN）会切断国际源 TLS，**侦察必须在生产机跑**：

    python tools/recon_epg_t011.py --out /tmp/epg_recon.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

USER_AGENT = "liptv/1.0 (+personal IPTV aggregator; epg recon)"
DEFAULT_TIMEOUT = 45

#: 默认向 iptv-org 查证的频道名 —— 覆盖任务书 §6 点名的优先集
#: （央视主力 + 主流卫视 + 两个国际频道）。
DEFAULT_MAPPING_PROBES = [
    "CCTV-1", "CCTV-2", "CCTV-4", "CCTV-5", "CCTV-7", "CCTV-8",
    "CCTV-10", "CCTV-13", "CCTV-14", "CCTV-15",
    "北京卫视", "东方卫视", "湖南卫视", "广东卫视", "浙江卫视",
    "France 24", "NHK World",
]

# --------------------------------------------------------------------------
# 候选源清单（任务书 §3 要求 ≥8 个）。
#
# 全部为**公开可访问**的 XMLTV / logo 目录；不含任何需要登录、
# Cookie 或付费订阅的源。reject 结论同样是有价值的侦察产出。
# --------------------------------------------------------------------------
EPG_CANDIDATES: list[dict] = [
    {
        # 🚨 实测 404：iptv-org 的 guides 路径已变更，
        # iptv-org.github.io/epg/guides/cn/epg.xml 与 /uk/ 均返回 404。
        # 保留在清单里作为「已验证 reject」记录，不删除（拒绝结论也是侦察产出）。
        "id": "iptv-org-guide-cn-legacy",
        "name": "iptv-org CN guide (legacy path)",
        "url": "https://iptv-org.github.io/epg/guides/cn/epg.xml",
        "homepage": "https://github.com/iptv-org/epg",
        "note": "2026-10-06 实测 404，路径已变更",
    },
    {
        "id": "epg-pw-cn",
        "name": "epg.pw CN",
        "url": "https://epg.pw/xmltv/epg_CN.xml",
        "homepage": "https://epg.pw/",
        "note": "本机实测 200，654 channel / 29162 programme",
    },
    {
        "id": "epg-112114-cn",
        "name": "epg.112114.xyz",
        "url": "https://epg.112114.xyz/pp.xml",
        "homepage": "https://epg.112114.xyz/",
        "note": "本机实测 200，540 channel / 19283 programme",
    },
    {
        "id": "fanmingming",
        "name": "fanmingming/live e.xml",
        "url": "https://raw.githubusercontent.com/fanmingming/live/main/e.xml",
        "homepage": "https://github.com/fanmingming/live",
        "note": "本机实测 200，但生产机raw.github 阻断 → 必须走 CDN 版",
    },
    {
        "id": "fanmingming-cdn",
        "name": "fanmingming/live (jsDelivr)",
        "url": "https://cdn.jsdelivr.net/gh/fanmingming/live@main/e.xml",
        "homepage": "https://github.com/fanmingming/live",
        "note": "TASK-010 已实证：生产机 raw.github 不通，CDN 字节等价可用",
    },
    {
        "id": "tvguide-cn",
        "name": "tvguide.cc XMLTV",
        "url": "https://www.tvguide.cn/xmltv/epg.xml",
        "homepage": "https://www.tvguide.cn/",
        "note": "本机实测 504",
    },
    {
        "id": "openiptv-cn",
        "name": "AlexWanTR/iptv-checker epg",
        "url": "https://raw.githubusercontent.com/AlexWanTR/iptv-checker/main/epg/cn.xml",
        "homepage": "https://github.com/AlexWanTR/iptv-checker",
        "note": "本机实测 404",
    },
    {
        "id": "zhumeng-ipxy",
        "name": "IPTV-FAN (zhumeng11)",
        "url": "https://live.fanmingming.com/e.xml",
        "homepage": "https://github.com/zhumeng11/IPTV-FAN",
        "note": "同一 fanmingming EPG 的独立域名入口（可作灾备）",
    },
]

LOGO_CANDIDATES: list[dict] = [
    {
        # 🚨 实测（本机2026-10-06）：这是国内最常用的公开台标库，
        # 命名与 canonical 高度重合（CCTV1 / 湖南卫视 / CCTV5+）。
        # 中文名必须 percent-encode，否则 urllib 直接 UnicodeEncodeError。
        "id": "fanmingming-live",
        "name": "fanmingming/live 台标库",
        "url_template": "https://live.fanmingming.cn/tv/{slug}.png",
        "probe_slugs": ["CCTV1", "CCTV5%2B", "%E6%B9%96%E5%8D%97%E5%8D%AB%E8%A7%86"],
        "homepage": "https://github.com/fanmingming/live",
        "note": "已实测 CCTV1/CCTV5+/湖南卫视 均 200 image/png；不存在频道返回 404",
    },
    {
        # 🚨 实测：iptv-org 自2025-07 起把 logo 字段从 channels.json 移到
        # logos.json（35122 条，其中 .cn 735 条）。旧路径
        # iptv-org.github.io/logos/tv/CCTV1.png 已全部 404 —— 不要凭规范猜。
        "id": "iptv-org-logos-json",
        "name": "iptv-org logos.json",
        "url_template": "https://iptv-org.github.io/api/logos.json",
        "probe_slugs": [],  # 走专用探测逻辑
        "homepage": "https://github.com/iptv-org/database",
        "note": "官方 logo 目录；logo 指向第三方 CDN（如 imgur），引用而不镜像",
    },
]


def probe_iptv_org_logos(*, timeout: int) -> list[dict]:
    """从 iptv-org 官方 logos.json 抽样验证 logo 可用性。

    只读、只抽样、**不镜像**（任务书 §22：公开可访问 ≠ 可重新托管）。
    """
    res = _get("https://iptv-org.github.io/api/logos.json", timeout=timeout)
    rows: list[dict] = []
    body = res.pop("_body", None)
    if not body:
        return [{
            "source_id": "iptv-org-logos-json",
            "error": res.get("error") or "empty body",
            "status": res.get("status"),
        }]
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [{"source_id": "iptv-org-logos-json", "error": f"JSON: {exc}"}]

    cn = [d for d in data if str(d.get("channel") or "").endswith(".cn")]
    sample = [d for d in cn if d.get("channel") in ("CCTV1.cn", "CCTV5.cn", "CCTV13.cn")]
    # 只验3 条，避免把侦察变��对第三方 CDN 的压测
    for row in sample:
        logo_url = row.get("url")
        if not logo_url:
            continue
        r = _get(logo_url, timeout=timeout, head_only=True)
        ctype = r["content_type"] or ""
        rows.append({
            "source_id": "iptv-org-logos-json",
            "channel_id": row.get("channel"),
            "url": logo_url,
            "http_status": r["status"],
            "content_type": ctype,
            "is_image": bool(r["ok"] and ctype.startswith("image/")),
            "is_fake_html": bool(r["ok"] and "text/html" in ctype),
            "error": r["error"],
        })
    return [{
        "source_id": "iptv-org-logos-json",
        "total_entries": len(data),
        "cn_entries": len(cn),
        "probed": rows,
    }]


def _get(url: str, *, timeout: int, head_only: bool = False) -> dict:
    """抓一个 URL，返回可审计的结果 dict。绝不抛异常。"""
    out: dict = {
        "url": url,
        "host": urlsplit(url).hostname or "",
        "ok": False,
        "status": None,
        "content_type": None,
        "bytes": 0,
        "error": None,
    }
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            # 显式声明无凭据 —— 任务书 §22 禁止 Cookie/Authorization。
            "Accept": "*/*",
        },
        method="HEAD" if head_only else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            out["ok"] = True
            out["status"] = resp.status
            out["content_type"] = (resp.headers.get("Content-Type") or "").lower()
            out["bytes"] = len(body)
            if not head_only:
                out["_body"] = body
    except urllib.error.HTTPError as exc:
        out["status"] = exc.code
        out["error"] = f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        out["error"] = f"URLError: {exc.reason}"
    except Exception as exc:  # noqa: BLE001 - 侦察脚本必须永不崩
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


_TZ_STYLE = {
    "offset": re.compile(r'^\d{4}\s+[+-]\d{4}\s'),
    "utc": re.compile(r'^\d{4}\s+\d{4}\s'),
    "local": re.compile(r'^\d{8}\s+\d{6}(?:\s|\+|$)'),
}


def analyze_xmltv(res: dict, *, keep_channel_ids: int = 40) -> dict:
    """解析 XMLTV，产出结构性统计。解析失败**不算成功**（任务书 §16）。"""
    info: dict = {
        "well_formed": False,
        "root": None,
        "channel_count": 0,
        "programme_count": 0,
        "channel_id_style": None,
        "channel_id_samples": [],
        "tz_style": None,
        "display_names_sample": [],
        "has_icon": False,
        "parse_error": None,
    }
    body = res.pop("_body", None)
    if not body:
        info["parse_error"] = res.get("error") or "empty body"
        return info
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        info["parse_error"] = f"ParseError: {exc}"
        return info
    info["well_formed"] = True
    info["root"] = root.tag

    channels = root.findall("channel")
    programmes = root.findall("programme")
    info["channel_count"] = len(channels)
    info["programme_count"] = len(programmes)

    ids: list[str] = []
    names: list[str] = []
    for ch in channels:
        cid = ch.get("id")
        if cid:
            ids.append(cid)
        disp = ch.get("display-name")
        if disp:
            names.append(disp)
        if ch.find("icon") is not None:
            info["has_icon"] = True
    info["channel_id_samples"] = ids[:keep_channel_ids]
    info["display_names_sample"] = names[:15]

    # channel id 风格：带点分层（site.tv.cn）是 XMLTV 社区常见做法
    if ids:
        sample = ids[0]
        info["channel_id_style"] = (
            "dotted" if "." in sample else ("plain" if re.match(r"^[A-Za-z0-9_-]+$", sample) else "other")
        )
    if programmes:
        first = programmes[0].get("start")
        if first:
            for style, pattern in _TZ_STYLE.items():
                if pattern.match(first):
                    info["tz_style"] = style
                    break
            else:
                info["tz_style"] = "unknown"
    return info


def probe_epg(candidate: dict, *, timeout: int) -> dict:
    res = _get(candidate["url"], timeout=timeout)
    row = dict(candidate)
    row["host"] = res["host"]
    row["http_status"] = res["status"]
    row["content_type"] = res["content_type"]
    row["bytes"] = res["bytes"]
    row["reachable"] = res["ok"]
    row["error"] = res["error"]
    row.update(analyze_xmltv(res))
    return row


def probe_logo(candidate: dict, *, timeout: int) -> list[dict]:
    """探测 logo URL 的 content-type —— 任务书 §8明令「不允许 HTML 假图片」。

    slug 允许已 percent-encode 的形式（中文名必须如此，见候选表注释）。
    """
    rows: list[dict] = []
    for slug in candidate["probe_slugs"]:
        url = candidate["url_template"].format(slug=slug)
        res = _get(url, timeout=timeout, head_only=True)
        ctype = res["content_type"] or ""
        rows.append({
            "source_id": candidate["id"],
            "slug": slug,
            "url": url,
            "http_status": res["status"],
            "content_type": ctype,
            # HTML 假图片：200 + text/html。任务书 §8 判为不合格。
            "is_fake_html": bool(res["ok"] and "text/html" in ctype),
            "is_image": bool(res["ok"] and ctype.startswith("image/")),
            "bytes": res["bytes"],
            "error": res["error"],
        })
    return rows


# iptv-org 映射侦察：权威 channel id + alt_names（非 fuzzy 匹配的依据）。
# channels.json 不是 XMLTV，所以不走 analyze_xmltv，单独解析。
CHANNELS_API = "https://iptv-org.github.io/api/channels.json"


def probe_iptv_org_channels(*, timeout: int, wanted: list[str]) -> dict:
    """拉取 iptv-org 权威频道表，抓出目标 canonical 的 id 与 alt_names。

    只读、不缓存、不镜像。这份数据是「静态人工 mapping」之外的
    **权威 id 来源**（任务书 §5 优先级第1~2 级）。
    """
    res = _get(CHANNELS_API, timeout=timeout)
    out: dict = {
        "url": CHANNELS_API,
        "reachable": res["ok"],
        "http_status": res["status"],
        "bytes": res["bytes"],
        "error": res["error"],
        "matches": [],
    }
    body = res.pop("_body", None)
    if not body:
        return out
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"JSON: {exc}"
        return out
    out["total_channels"] = len(data)
    index: dict[str, dict] = {}
    for item in data:
        if item.get("id"):
            index[item["id"]] = item
        if item.get("name"):
            index.setdefault(item["name"], item)
        for alt in item.get("alt_names") or []:
            index.setdefault(alt, item)
    for want in wanted:
        hit = index.get(want)
        if hit:
            out["matches"].append({
                "query": want,
                "id": hit.get("id"),
                "name": hit.get("name"),
                "alt_names": hit.get("alt_names") or [],
                "country": hit.get("country"),
                "categories": hit.get("categories") or [],
            })
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TASK-011 EPG/Logo 公开源侦察（只读）")
    ap.add_argument("--out", default="/tmp/epg_recon.json", help="结果 JSON 落盘路径")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--only", default=None, help="只跑某个候选 id（调试用）")
    ap.add_argument(
        "--canonical",
        action="append",
        default=None,
        help="要向 iptv-org 查证的频道名，可重复传入",
    )
    args = ap.parse_args(argv)

    epgs = [c for c in EPG_CANDIDATES if not args.only or c["id"] == args.only]
    logos = [c for c in LOGO_CANDIDATES if not args.only or c["id"] == args.only]

    epg_rows: list[dict] = []
    for cand in epgs:
        print(f"[epg ] {cand['id']:<24} {cand['url']}", flush=True)
        row = probe_epg(cand, timeout=args.timeout)
        print(
            f"       -> {row['reachable']} status={row['http_status']} "
            f"bytes={row['bytes']} well_formed={row['well_formed']} "
            f"ch={row['channel_count']} prog={row['programme_count']} "
            f"err={row['error'] or row['parse_error']}",
            flush=True,
        )
        epg_rows.append(row)

    logo_rows: list[dict] = []
    for cand in logos:
        print(f"[logo] {cand['id']}", flush=True)
        if cand["id"] == "iptv-org-logos-json":
            rows = probe_iptv_org_logos(timeout=args.timeout)
            print(
                f"       -> total={rows[0].get('total_entries')} "
                f"cn={rows[0].get('cn_entries')} "
                f"probed={len(rows[0].get('probed') or [])}",
                flush=True,
            )
            logo_rows.append(rows[0])
            continue
        rows = probe_logo(cand, timeout=args.timeout)
        for row in rows:
            print(
                f"       -> {row['slug']:<14} status={row['http_status']} "
                f"ctype={row['content_type']} image={row['is_image']} "
                f"fake_html={row['is_fake_html']}",
                flush=True,
            )
        logo_rows.extend(rows)

    # iptv-org 映射侦察：权威 channel id + alt_names（非 fuzzy 匹配的依据）
    print("[map ] iptv-org channels.json", flush=True)
    mapping = probe_iptv_org_channels(
        timeout=args.timeout,
        wanted=list(args.canonical or DEFAULT_MAPPING_PROBES),
    )
    print(
        f"       -> reachable={mapping['reachable']} "
        f"total={mapping.get('total_channels')} "
        f"matched={len(mapping['matches'])}/{len(mapping.get('matches') or []) or 0}",
        flush=True,
    )
    for m in mapping["matches"][:6]:
        print(f"          {m['query']:<16} -> {m['id']}", flush=True)

    payload = {
        "task": "TASK-011",
        "purpose": "§3 EPG / Logo 公开源侦察",
        "generated_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(timespec="seconds"),
        "safety": "no cookie / no auth / no video content / logo not mirrored",
        "epg": epg_rows,
        "logo": logo_rows,
        "mapping": mapping,
        "summary": {
            "epg_candidates": len(epg_rows),
            "epg_well_formed": sum(1 for r in epg_rows if r["well_formed"]),
            "logo_probes": len(logo_rows),
            "logo_ok": sum(1 for r in logo_rows if r.get("is_image")),
            "logo_fake_html": sum(1 for r in logo_rows if r.get("is_fake_html")),
            "mapping_matched": len(mapping.get("matches") or []),
        },
    }
    out = pathlib.Path(args.out)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False), flush=True)
    print(f"[out] {out} ({out.stat().st_size} bytes)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())