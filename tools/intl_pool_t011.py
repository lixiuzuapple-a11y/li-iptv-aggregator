"""TASK-011 §12 国际频道**候选池扩展** —— 为「至少 2 个可用国际频道」找替补。

背景（2026-10-06 生产实测）::

    France 24   master 200 →媒体 playlist 60 分片 → 分片 200 + MPEG-TS 0x47  => PLAYABLE
    NHK World   master 200 → 8 variant →全部 404 / 403（地域限制）      => NOT playable
    DW(旧入口)  404
    Al Jazeera  URLError
    CGTN Doc    URLError

NHK 挂了，而任务书 §12 允许「可用其它真实候选替代」，但**禁止降低门槛**。
所以本脚本的作用是：**扩大候选池 + 用同一把尺子（分片级）逐个量**。

候选来源（全部为公开、免凭据的官方/社区入口）：

* iptv-org 各国条目里已验证的国际新闻台（走CDN，避开直连地域限制）
* 各国 broadcasters 公开 HLS

**纪律**：

* 不带Cookie / Authorization / token，不逆向签名，不破 DRM；
* 不代理视频：每条只取首包 ≤ 1KB；
* 一次失败不永久封禁：本脚本只**读**，不做任何排除/下线；
* 判定必须到**分片级**并校验 MPEG-TS 同步字节 0x47 ——
  playlist 200 不等于可播（TASK-011 §13 segment-404 就是反例）。
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ============================================================ 候选池
# 全部经过「公开入口 + 免凭据」筛选。同一canonical 的多个 URL 是允许的
# （multi-stream），不是重复条目。
CANDIDATES = [
    # ---------- 已确认可播，作为对照基准 ----------
    {"key": "france24-en", "canonical": "France 24", "group": "国际",
     "url": "https://live.france24.com/hls/live/2037218/F24_EN_HI_HLS/master_5000.m3u8",
     "origin": "official"},

    # ---------- France24 语种变体：同源multi-stream，先验证 ----------
    {"key": "france24-fr", "canonical": "France 24", "group": "国际",
     "url": "https://live.france24.com/hls/live/2037218/F24_FR_HI_HLS/master_5000.m3u8",
     "origin": "official"},

    # ---------- NHK World：原入口已失效，换 iptv-org 实测仍在册的官方域名 ----
    # 2026-10-06 生产实测：master.nhkworld.jp 的 master 200 但 8 个 variant
    # 全部 404/403（地域限制）⇒ 不可播。iptv-org streams.json 里 NHK World
    # 的**当前**官方入口是 media-*.hls.nhkworld.jp，必须重新量。
    {"key": "nhk-osa", "canonical": "NHK World", "group": "国际",
     "url": "https://media-osa.hls.nhkworld.jp/hls/w/live/master.m3u8",
     "origin": "official"},
    {"key": "nhk-tyo", "canonical": "NHK World", "group": "国际",
     "url": "https://media-tyo.hls.nhkworld.jp/hls/w/live/master.m3u8",
     "origin": "official"},
    {"key": "nhk-smarttv", "canonical": "NHK World", "group": "国际",
     "url": "https://masterpl.hls.nhkworld.jp/hls/w/live/smarttv.m3u8",
     "origin": "official"},

    # ---------- France24 法语/西语/阿语官方入口 + -b 备用域 ----------
    {"key": "france24-fr2", "canonical": "France 24", "group": "国际",
     "url": "https://live.france24.com/hls/live/2037179/F24_FR_HI_HLS/master_5000.m3u8",
     "origin": "official"},
    {"key": "france24-es2", "canonical": "France 24", "group": "国际",
     "url": "https://live.france24.com/hls/live/2037220/F24_ES_HI_HLS/master_5000.m3u8",
     "origin": "official"},
    {"key": "france24-ar2", "canonical": "France 24", "group": "国际",
     "url": "https://live.france24.com/hls/live/2037222/F24_AR_HI_HLS/master_5000.m3u8",
     "origin": "official"},
    {"key": "france24-b-en", "canonical": "France 24", "group": "国际",
     "url": "https://live.france24.com/hls/live/2037218-b/F24_EN_HI_HLS/master_5000.m3u8",
     "origin": "official"},

    # ---------- DW 当前官方 CDN ----------
    {"key": "dw-107", "canonical": "DW English", "group": "国际",
     "url": "https://dwamdstream107.akamaized.net/hls/live/2015525/dwstream107/index.m3u8",
     "origin": "official"},

    # ---------- 其它真实国际公共台（iptv-org 在册、公开免凭据）----------
    {"key": "amagi-fr24", "canonical": "France 24", "group": "国际",
     "url": "https://amg00106-amg00106c1-samsung-nz-4151.playouts.now.amagi.tv/playlist.m3u",
     "origin": "third-party"},
    {"key": "klowdtv-fr24", "canonical": "France 24", "group": "国际",
     "url": "https://a-cdn.klowdtv.com/live2/france24_720p/playlist.m3u8",
     "origin": "third-party"},
    {"key": "antik-fr24", "canonical": "France 24", "group": "国际",
     "url": "https://dash3.antik.sk/live/test_france24_eng/playlist.m3u8",
     "origin": "third-party"},
    {"key": "aljazeera-en", "canonical": "Al Jazeera English", "group": "国际",
     "url": "https://live-hls-web-aje.getaj.net/AJE/01.m3u8", "origin": "official"},
    {"key": "cgtn-doc", "canonical": "CGTN Documentary", "group": "国际",
     "url": "https://livedoc.cgtn.com/500d/prog_index.m3u8", "origin": "official"},
]

#: 保留一个**故意的坏候选**做自检：它必须被判not_playable，
#: 否则说明探测器会把什么都判成可播（那比漏判更危险）。
_SELFCHECK_BAD = {
    "key": "selfcheck-bad",
    "canonical": "(selfcheck) 故意不可达",
    "group": "国际",
    "url": "https://invalid.example.invalid/nonexistent/master.m3u8",
    "origin": "selfcheck",
}


def _ctx() -> ssl.SSLContext:
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def grab(url: str, timeout: int = 15, limit: int = 4096) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity",
    })
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
            body = r.read(limit)
            return {"status": r.status, "ctype": (r.headers.get("Content-Type") or ""),
                    "body": body, "ms": int((time.perf_counter() - t0) * 1000),
                    "final": r.geturl(), "err": None}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "ctype": "", "body": b"",
                "ms": int((time.perf_counter() - t0) * 1000), "final": url, "err": "HTTPError"}
    except Exception as e:
        return {"status": None, "ctype": "", "body": b"",
                "ms": int((time.perf_counter() - t0) * 1000), "final": url,
                "err": type(e).__name__}


def refs_of(body: bytes) -> list[str]:
    return [x.strip() for x in body.decode("utf-8", errors="replace").splitlines()
            if x.strip() and not x.strip().startswith("#")]


def is_playlist(body: bytes) -> bool:
    return body.lstrip()[:7].startswith(b"#EXTM3U")


def rel(base: str, u: str) -> str:
    if u.startswith("http"):
        return u
    p = urlparse(base)
    root = f"{p.scheme}://{p.netloc}"
    return root + u if u.startswith("/") else base.rsplit("/", 1)[0] + "/" + u


def deep(item: dict) -> dict:
    """两级下探到分片，校验 MPEG-TS 0x47。"""
    out = {k: v for k, v in item.items() if k != "url"}
    out["url_host"] = (urlparse(item["url"]).hostname or "")
    lv1 = grab(item["url"], timeout=20)
    out["l1"] = {"status": lv1["status"], "ctype": lv1["ctype"][:40],
                 "ms": lv1["ms"], "err": lv1["err"]}
    if lv1["status"] is None or lv1["status"] >= 400:
        out.update({"verdict": "unreachable" if lv1["status"] is None else "http_error",
                    "detail": lv1["err"] or f"HTTP {lv1['status']}",
                    "playback_reachable": False})
        return out
    if not is_playlist(lv1["body"][:64]):
        out.update({"verdict": "not_playlist",
                    "detail": f"200 但不是 playlist（ctype={lv1['ctype'][:30]}）",
                    "playback_reachable": False})
        return out

    r1 = refs_of(lv1["body"])
    playable = False
    seg_detail = []
    for ref in r1[:3]:
        u2 = rel(lv1["final"], ref)
        lv2 = grab(u2, timeout=15)
        if lv2["status"] == 200 and lv2["body"][:1] == b"\x47":
            playable = True
            seg_detail.append({"url": u2[:170], "status": 200,
                               "ctype": lv2["ctype"][:30], "bytes": len(lv2["body"]),
                               "mpegts_sync": True})
            continue
        if not is_playlist(lv2["body"][:64]):
            seg_detail.append({"url": u2[:170], "status": lv2["status"],
                               "ctype": lv2["ctype"][:30], "bytes": len(lv2["body"]),
                               "mpegts_sync": False, "err": lv2["err"]})
            continue
        for sg in refs_of(lv2["body"])[:2]:
            u3 = rel(lv2["final"], sg)
            g = grab(u3, timeout=15, limit=1024)
            ok = g["status"] == 200 and g["body"][:1] == b"\x47"
            seg_detail.append({"url": u3[:170], "status": g["status"],
                               "ctype": g["ctype"][:30], "bytes": len(g["body"]),
                               "mpegts_sync": ok, "err": g["err"]})
            if ok:
                playable = True

    out["l1_refs"] = len(r1)
    out["segments"] = seg_detail
    out["playback_reachable"] = playable
    out["verdict"] = "playable" if playable else "not_playable"
    out["detail"] = (f"l1={lv1['status']} refs={len(r1)}; "
                     f"{sum(1 for s in seg_detail if s['mpegts_sync'])}/{len(seg_detail)} 分片 TS 同步")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/t011_intl_pool.json")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    pool = list(CANDIDATES) + [_SELFCHECK_BAD]
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        results = list(ex.map(deep, pool))

    report = {
        "generated_at_epoch": int(time.time()),
        "host_env": "ev-lab-shanghai",
        "total": len(results),
        "playable": sum(1 for r in results if r["verdict"] == "playable"),
        "results": sorted(results, key=lambda r: (r["verdict"] != "playable", r["key"])),
    }
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    for r in report["results"]:
        print(f"{r['verdict']:14s} {r['key']:18s} {r['canonical'][:14]:16s} {r['detail'][:66]}")
    sc = next((r for r in results if r["key"] == _SELFCHECK_BAD["key"]), None)
    if sc is None:
        print("!!自检缺失")
    elif sc["verdict"] == "playable":
        print("!! 自检失败：故意不可达的候选被判playable，探测器不可信")
    else:
        print(f"selfcheck OK（故意不可达=> {sc['verdict']}）")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())