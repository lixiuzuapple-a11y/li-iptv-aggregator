"""TASK-011 §12/§13 生产复验 —— 国际频道 + 坏流治理。

设计约束（沿用 TASK-009/010 教训）：

* **只读探测**：本脚本**不写生产 DB、不改 config、不重启服务**。只输出 JSON 报告。
* **不代理视频**：只取 master playlist 与首段前若干 KB，不拉完整流。
* **不镜像 logo**：logo 只做 HEAD/Range 校验，不落盘。
* **一次失败不永久封禁**：本脚本不做任何排除/下线动作，只产出证据。
* **无凭据**：不带 Cookie / Authorization / token，不逆向签名。

两轮采样（TASK-011 §13「再增加真实 probe history」）::

    python3 tools/recon_t011_prod.py --round 1
    python3 tools/recon_t011_prod.py --round 2

两次都成功的候选才允许进入 fixed seed（TASK-011 §12「稳定可播」）。
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

#: master playlist 探测超时（秒）。国际台官方 HLS 冷启动普遍偏慢。
TIMEOUT = 25

#: 单个流最多并发探测数。2G 共享主机，不能打满。
CONCURRENCY = 4

# ============================================================ TASK-011 §12 国际频道
# 「上海 HLS 可达」是 TASK-010 reviewer 的事实陈述，本轮必须**独立复验**，
# 且门槛不降低：不因为「国际分类好看」就收不可播的源。
INTERNATIONAL = [
    {
        "key": "france24-en",
        "canonical": "France 24",
        "group": "国际",
        "country": "fr",
        "url": "https://live.france24.com/hls/live/2037218/F24_EN_HI_HLS/master_5000.m3u8",
        "note": "TASK-010 reviewer 称上海 HLS 可达；TASK-010 本机实测 200 但零条目（假 200）",
    },
    {
        "key": "nhk-world-jp",
        "canonical": "NHK World",
        "group": "国际",
        "country": "jp",
        "url": "https://master.nhkworld.jp/nhkworld-tv/playlist/live.m3u8",
        "note": "TASK-010 本机 200 且 8 条目，待生产复验",
    },
    # 备选：TASK-011 §12 允许「可用其它真实候选替代」
    {
        "key": "dw-en",
        "canonical": "DW English",
        "group": "国际",
        "country": "de",
        "url": "https://dwamdstream107.akamaized.net/hls/live/2015525/dwstream107/index.m3u8",
        "note": "TASK-010 §12 记 DW 旧入口 404，此为当前 CDN 入口，复验",
    },
    {
        "key": "aljazeera-en",
        "canonical": "Al Jazeera English",
        "group": "国际",
        "country": "qa",
        "url": "https://live-hls-web-aje.getaj.net/AJE/01.m3u8",
        "note": "TASK-010 本机 URLError（疑 FlClash TUN 截 TLS），生产复验",
    },
    {
        "key": "cgtn-doc",
        "canonical": "CGTN Documentary",
        "group": "国际",
        "country": "cn",
        "url": "https://livedoc.cgtn.com/500d/prog_index.m3u8",
        "note": "TASK-010 本机 URLError，生产复验",
    },
]

# ============================================================ TASK-011 §13 坏流复验
# segment-404 4 条 + 持续超时 host + CCTV-5+。
# 具体 URL 全部从生产 DB 动态取（见 load_bad_flows），避免在本文件里写死
# 短时效签名 URL（写死 = 下轮必然过期 = 假失败）。


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def http_get(url: str, timeout: int = TIMEOUT, read_bytes: int = 65536) -> dict:
    """GET 一个 URL，只读前 ``read_bytes`` 字节。返回结构化证据。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    })
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
            body = r.read(read_bytes)
            return {
                "status": r.status,
                "content_type": (r.headers.get("Content-Type") or "").lower(),
                "content_length": r.headers.get("Content-Length"),
                "final_url": r.geturl(),
                "final_host": host_of(r.geturl()),
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "head": body[:2048].decode("utf-8", errors="replace"),
                "read_bytes": len(body),
                "error": None,
            }
    except urllib.error.HTTPError as e:
        return {
            "status": e.code,
            "content_type": (e.headers.get("Content-Type") or "").lower() if e.headers else "",
            "final_url": url,
            "final_host": host_of(url),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "head": "",
            "error": "HTTPError",
        }
    except Exception as e:
        return {
            "status": None,
            "content_type": "",
            "final_url": url,
            "final_host": host_of(url),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "head": "",
            "error": type(e).__name__,
        }


def parse_m3u(text: str) -> list[dict]:
    """极简 M3U 解析：只要条目数+ 是否含分片引用。

    🚨 TASK-011 生产实测教训（2026-10-06）：
    HLS media playlist 的分片引用**大量是相对路径**（无 ``://``）::

        #EXTINF:10.00000,
        20260625T062934/master_5000/00445/master_5000_00641.ts

    初版只认含 ``://`` 的行 ⇒ France 24 有 8 个分片却被判成 ``empty``。
    这种假阴性会直接导致「真实可播的源被误判为坏流」，比漏判更危险。
    正确判据：**非注释、非空行**就是条目（不要求绝对 URL）。
    """
    entries: list[dict] = []
    name = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            disp = line.split(",", 1)
            name = disp[1].strip() if len(disp) > 1 else ""
        elif not line.startswith("#"):
            entries.append({"name": name or "", "url": line})
            name = None
    return entries


def probe_hls(url: str) -> dict:
    """探测一个 HLS master/媒体 playlist。

    判定分档（**不猜测**，全部来自实测字节）：

    * ``unreachable``  —— DNS/TCP/TLS 层失败
    * ``http_error``    —— 4xx/5xx
    * ``fake_200``      —— 200 但 body 是 HTML 错误页
    * ``empty``         —— 200 但零条目
    * ``ok``            —— 200 且有条目
    """
    res = http_get(url)
    ctype = res["content_type"]
    head = res["head"]
    verdict = "unreachable"
    detail = ""

    if res["status"] is None:
        detail = res["error"] or "unknown"
    elif res["status"] >= 400:
        verdict = "http_error"
        detail = f"HTTP {res['status']}"
    elif "text/html" in ctype or "<html" in head[:200].lower():
        verdict = "fake_200"
        detail = f"200 但 content-type={ctype or '空'} 且 body 像 HTML"
    else:
        entries = parse_m3u(head)
        if not entries:
            verdict = "empty"
            detail = f"200 但前 {res['read_bytes']}B 内零条目"
        else:
            verdict = "ok"
            variant = next((e["url"] for e in entries if ".m3u8" in e["url"]), None)
            detail = f"{len(entries)} 条目"
            if variant:
                sub = http_get(variant, read_bytes=16384)
                sub_entries = parse_m3u(sub["head"])
                detail += f"；变体 {sub['status']} {len(sub_entries)} 分片"
                res["variant"] = {
                    "url": variant,
                    "status": sub["status"],
                    "segments": len(sub_entries),
                    "error": sub["error"],
                }

    res["verdict"] = verdict
    res["detail"] = detail
    return res


def probe_one(item: dict) -> dict:
    out = dict(item)
    out.update(probe_hls(item["url"]))
    out.pop("head", None)
    out["probe_host"] = host_of(item["url"])
    return out


def load_bad_flows(db_path: str) -> list[dict]:
    """从生产 DB 读出待复验的坏流 URL。

    只SELECT，不写。找不到就返回空列表（脚本仍应能跑完）。
    """
    import sqlite3

    targets: list[dict] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # §13 明确点名的：持续超时 host
        rows = conn.execute(
            "SELECT s.id AS stream_id, s.url, c.name AS canonical "
            "FROM stream s JOIN canonical_channel c ON c.id = s.canonical_channel_id "
            "WHERE s.url LIKE '%120.76.248.139%'"
        ).fetchall()
        for r in rows:
            targets.append({
                "key": "timeout-host-120.76.248.139",
                "canonical": r["canonical"],
                "url": r["url"],
                "note": "TASK-011 §13 持续超时 host 复验",
            })

        # §13 明确点名的：CCTV-5+（注意：绝不能与 CCTV-5 混为一谈，
        # TASK-011 §5 硬要求二者 tvg-id 独立）
        for r in conn.execute(
            "SELECT s.id AS stream_id, s.url, c.name AS canonical "
            "FROM stream s JOIN canonical_channel c ON c.id = s.canonical_channel_id "
            "WHERE c.name LIKE 'CCTV-5%' AND c.name <> 'CCTV-5'"
        ).fetchall():
            targets.append({
                "key": "cctv-5plus",
                "canonical": r["canonical"],
                "url": r["url"],
                "note": "TASK-011 §13 CCTV-5+ 复验",
            })

# §13「家庭 context 发现 segment 404 的 4 条线路」：按 probe_result 里的
        # 失败历史反查，不写死 URL（签名 URL 会过期，写死=下轮必然假失败）。
        seg = conn.execute(
            "SELECT DISTINCT s.id AS stream_id, s.url, c.name AS canonical "
            "FROM probe_result pr "
            "JOIN stream s ON s.id = pr.stream_id "
            "JOIN canonical_channel c ON c.id = s.canonical_channel_id "
            "WHERE pr.success = 0 "
            "ORDER BY s.id LIMIT 30"
        ).fetchall()
        for r in seg:
            targets.append({
                "key": "segment-404",
                "canonical": r["canonical"],
                "url": r["url"],
                "note": "TASK-011 §13 segment-404 复验",
            })
    finally:
        conn.close()

    # 去重（同一 URL 只探一次）
    seen: set[str] = set()
    uniq: list[dict] = []
    for t in targets:
        if t["url"] not in seen:
            seen.add(t["url"])
            uniq.append(t)
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser(description="TASK-011 生产只读复验")
    ap.add_argument("--round", type=int, default=1, help="采样轮次，仅记录用")
    ap.add_argument("--db", default="/var/lib/li-iptv-aggregator/liptv.sqlite3",
                    help="生产 DB 路径（只读打开）")
    ap.add_argument("--skip-badflow", action="store_true",
                    help="跳过 §13 坏流复验（DB 不可用时）")
    ap.add_argument("--out", default="", help="报告输出路径（默认 stdout）")
    args = ap.parse_args()

    targets = list(INTERNATIONAL)
    badflow_error = None
    if not args.skip_badflow:
        try:
            targets.extend(load_bad_flows(args.db))
        except Exception as e:
            badflow_error = f"{type(e).__name__}: {e}"

    results: list[dict] = []
    with futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        for res in ex.map(probe_one, targets):
            results.append(res)

    ok = [r for r in results if r["verdict"] == "ok"]
    report = {
        "round": args.round,
        "host_env": "ev-lab-shanghai",
        "probed_at_epoch": int(time.time()),
        "total": len(results),
        "ok": len(ok),
        "badflow_db_error": badflow_error,
        "results": sorted(results, key=lambda r: (r["verdict"] != "ok", r["key"])),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())