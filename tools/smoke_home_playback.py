"""TASK-010 §12 家庭/VPN playback smoke（本机 context）。

**这是产品验证，不是云端 selector 输入。**

严格区分两个 context（§4）：
- ``shanghai-cloud``：聚合器能否抓到 source M3U（云端 probe 负责）
- ``home-windows-vpn``：真实观看网络能否播底层 stream（本脚本负责）

本脚本**绝不**把两个结果合成单一 PASS/FAIL。

安全红线：
- 只发HEAD/GET-range 短请求，**不下载媒体内容**（最多读1 字节确认有body）
- 不设Cookie / Authorization
- host 脱敏（只保留主域）
- 不读取/导出任何 VPN 凭据
"""
from __future__ import annotations

import json
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

#本机 ffprobe 不可用（PATH 里没有），任务书 §12 允许「HLS playlist 是否可达」
# 与「media segment 是否可达」两级验证。第二级用 HTTP Range 代替 ffprobe。
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) liptv-smoke/1.0"
TIMEOUT = 10


def mask_host(url: str) -> str:
    """脱敏：只保留主域（把子域压成 *.主域）。"""
    host = (urlsplit(url).hostname or "").lower()
    parts = host.split(".")
    return "*." + ".".join(parts[-2:]) if len(parts) > 2 else host


def head_or_range(url: str, *, want_body: bool = False) -> dict:
    """只取状态码/类型/首字节，绝不下载媒体内容。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    out = {"url_masked": mask_host(url), "ok": False}
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read(1) if want_body else b""
            out.update(
                ok=True,
                http_status=r.status,
                content_type=(r.headers.get("Content-Type") or "").split(";")[0],
                first_byte=bool(body),
                elapsed_ms=int((time.time() - t0) * 1000),
            )
    except urllib.error.HTTPError as e:
        out.update(ok=False, http_status=e.code, error=f"HTTP {e.code}")
    except urllib.error.URLError as e:
        out.update(ok=False, error=f"{type(e).__name__}: {e.reason}")
    except Exception as e:  # noqa: BLE001
        out.update(ok=False, error=f"{type(e).__name__}: {e}")
    return out


def probe_hls(url: str) -> dict:
    """两级验证：playlist 可达 ⇒ 抽第一个 segment 试 Range。"""
    res = {"playlist": head_or_range(url), "segment": None}
    if not res["playlist"]["ok"]:
        res["verdict"] = "PLAYLIST_UNREACHABLE"
        return res
    # 取 playlist 文本找第一个非注释 URI
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            text = r.read(200000).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        res["verdict"] = "PLAYLIST_BODY_UNREADABLE"
        res["error"] = f"{type(e).__name__}: {e}"
        return res

    lines = [ln.strip() for ln in text.splitlines()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        res["verdict"] = "NOT_HLS_OR_HTML_FAKE"
        res["html_hint"] = bool(re.search(r"<\s*html|<!doctype", text[:2000], re.I))
        return res

    seg = next((ln for ln in lines if ln and not ln.startswith("#")), None)
    if not seg:
        res["verdict"] = "PLAYLIST_NO_SEGMENT"
        return res
    # 🚨 第一版这里写的是 ``urllib.parse.urljoin(url, seg) if hasattr(urllib, "parse") else seg``，
    # 而 urllib.parse 当时**没有 import** —— hasattr 返回 False，于是静默把
    # **相对路径**原样当绝对 URL 去请求，必然 404。5 条 PLAYLIST_OK_SEGMENT_FAIL
    # 全部是这个bug 制造的假象，不是真的播不了。
    # 「条件表达式 + 没导入的模块」是个陷阱：它不报错，只是悄悄走错分支。
    seg_url = urllib.parse.urljoin(url, seg)
    res["segment"] = head_or_range(seg_url, want_body=True)
    if not res["segment"]["ok"]:
        res["verdict"] = "PLAYLIST_OK_SEGMENT_FAIL"
    elif res["segment"].get("first_byte"):
        res["verdict"] = "PLAYABLE"
    else:
        res["verdict"] = "SEGMENT_EMPTY"
    return res


def run(name: str, urls: list[str], *, kind: str) -> list[dict]:
    rows = []
    for u in urls:
        r = probe_hls(u)
        rows.append({"source": name, "kind": kind, **r})
        print(f"  [{r['verdict']:<24}] {name:<16} {r['playlist'].get('url_masked')}"
              f"  playlist={r['playlist'].get('http_status') or r['playlist'].get('error')}")
    return rows


if __name__ == "__main__":
    print("=" * 72)
    print("TASK-010 §12 家庭/VPN playback smoke")
    print(f"context = home-windows-vpn (TUN: FlClash)")
    print("=" * 72)
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
    rows: list[dict] = []
    for grp in payload:
        rows += run(grp["name"], grp["urls"], kind=grp.get("kind", "fixed"))

    print("\n" + "=" * 72)
    print("汇总（**不可与 shanghai-cloud 结果合并**）")
    tally: dict[str, int] = {}
    for r in rows:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    for k, v in sorted(tally.items()):
        print(f"  {k:<26} {v}")
    json.dump(rows, open(sys.argv[2], "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n结果写入 {sys.argv[2]}")
    print("=" * 72)
