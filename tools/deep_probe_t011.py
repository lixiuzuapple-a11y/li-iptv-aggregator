"""TASK-011 §12/§13 —— 国际频道 + CCTV-5+ 的**分片级**可播性判定。

为什么必须单独一个脚本：

* ``recon_t011_prod.py`` 只判定「playlist 层面能不能拿到」，
  那回答的是 *Aggregator reachability*。
* 任务书 §12 要的是「稳定可播」，即 *Playback reachability*。
  中间隔着**分片**：playlist 200 但分片 404 是TASK-011 §13 点名的
  segment-404 现象（2026-10-06 生产实测：27 条 playlist 正常、
  分片全部不可达）。
* 两者**不得混为一个 PASS/FAIL**（冻结认识）。

本脚本因此下探一层：master → media playlist → **真实 .ts 分片首包**，
并检查 MPEG-TS 同步字节 ``0x47``。

**只读**：不写 DB、不改 config、不代理整路视频（每条只取首包 ≤1KB）。
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

TARGETS = [
    {
        "key": "france24-en",
        "canonical": "France 24",
        "url": "https://live.france24.com/hls/live/2037218/F24_EN_HI_HLS/master_5000.m3u8",
    },
    {
        "key": "nhk-world-jp",
        "canonical": "NHK World",
        "url": "https://master.nhkworld.jp/nhkworld-tv/playlist/live.m3u8",
    },
]


def _ctx() -> ssl.SSLContext:
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def grab(url: str, timeout: int = 15, limit: int = 1200) -> dict:
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


def lines(body: bytes) -> list[str]:
    """抽出 playlist 里的非注释行。

    🚨 只能在**已知是文本 playlist** 的响应上调用：
    MPEG-TS 是二进制，若误传进来会解出大量乱码"URL"，
    下一步就会得到一堆 InvalidURL —— 那是探测器的 bug，不是流的结论。
    """
    return [x.strip() for x in body.decode("utf-8", errors="replace").splitlines()
            if x.strip() and not x.strip().startswith("#")]


def looks_like_playlist(head: bytes) -> bool:
    """按 magic 判断响应体是不是文本 playlist（而不是二进制媒体）。"""
    return head.lstrip()[:7].startswith(b"#EXTM3U")


def rel(base: str, u: str) -> str:
    if u.startswith("http"):
        return u
    p = urlparse(base)
    root = f"{p.scheme}://{p.netloc}"
    return root + u if u.startswith("/") else base.rsplit("/", 1)[0] + "/" + u


def deep_probe(item: dict) -> dict:
    """master → variant → segment 三级下探。"""
    out = dict(item)
    level1 = grab(item["url"], limit=4000)
    refs = lines(level1["body"])
    out["master"] = {
        "status": level1["status"], "ctype": level1["ctype"],
        "ms": level1["ms"], "refs": len(refs), "err": level1["err"],
    }
    out["levels"] = []
    playable = False

    for ref in refs[:4]:
        full = rel(item["url"], ref)
        lv2 = grab(full, limit=4096)
        body_text = lv2["body"].decode("utf-8", errors="replace")
        # 🚨 生产实测（2026-10-06）：HLS 层级**不是固定两级**。
        # france24 的 master 直接就是**媒体 playlist**（60 个 .ts 分片），
        # NHK 的 master 是 **variant 列表**（另一个 .m3u8）。
        # 判据必须看内容而不是「层级序号」——
        #   行以 .m3u8 结尾 → 下一层 playlist
        #   行以 .ts / .aac / .mp4 结尾 → 已是媒体分片，直接探
        if not looks_like_playlist(lv2["body"][:64]):
            # level2 直接就是二进制媒体（罕见但合法）：它本身就是内容
            is_ts = lv2["body"][:1] == b"G"
            entry = {
                "level2_url": full[:180], "status": lv2["status"],
                "ctype": lv2["ctype"], "ms": lv2["ms"], "segments": 0,
                "kind": "media", "err": lv2["err"],
                "segment_probes": [{
                    "segment": full[:180], "status": lv2["status"],
                    "ctype": lv2["ctype"][:40], "bytes": len(lv2["body"]),
                    "mpegts_sync": is_ts, "err": lv2["err"],
                }],
            }
            if lv2["status"] == 200 and is_ts:
                playable = True
            out["levels"].append(entry)
            continue
        segs = lines(lv2["body"])
        is_playlist = any(s.split("?")[0].lower().endswith(".m3u8") for s in segs)
        entry = {
            "level2_url": full[:180],
            "status": lv2["status"],
            "ctype": lv2["ctype"],
            "ms": lv2["ms"],
            "segments": len(segs),
            "kind": "playlist" if is_playlist else "media",
            "err": lv2["err"],
            "segment_probes": [],
        }

        if is_playlist:
            # 第三层：再进一次 playlist 拿分片
            nxt = next(s for s in segs if s.split("?")[0].lower().endswith(".m3u8"))
            lv3 = grab(rel(full, nxt), limit=4096)
            cand = lines(lv3["body"])
        else:
            cand = segs

        for sg in cand[:2]:
            sfull = rel(lv2["final"], sg)
            g = grab(sfull, timeout=15, limit=1024)
            is_ts = g["body"][:1] == b"\x47"
            entry["segment_probes"].append({
                "segment": sfull[:180],
                "status": g["status"],
                "ctype": g["ctype"][:40],
                "bytes": len(g["body"]),
                "mpegts_sync": is_ts,
                "err": g["err"],
            })
            if g["status"] == 200 and is_ts:
                playable = True
        out["levels"].append(entry)

    out["playback_reachable"] = playable
    out["verdict"] = "playable" if playable else "not_playable"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--r2", default="/tmp/t011_prod_r2.json",
                    help="第二轮复验 JSON（用于取 DB 里的 CCTV-5+ URL）")
    ap.add_argument("--out", default="/tmp/t011_deep.json")
    ap.add_argument("--include-db", action="store_true",
                    help="同时深探 R2 里 CCTV-5+ 的 URL")
    args = ap.parse_args()

    targets = list(TARGETS)
    if args.include_db:
        try:
            with open(args.r2, encoding="utf-8") as fh:
                r2 = json.load(fh)
            for r in r2["results"]:
                if r["key"] == "cctv-5plus":
                    targets.append({"key": "cctv-5plus",
                                    "canonical": r.get("canonical", ""),
                                    "url": r["url"]})
        except Exception:
            pass

    report = {"generated_at_epoch": int(time.time()), "probes": []}
    for t in targets:
        report["probes"].append(deep_probe(t))

    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    for p in report["probes"]:
        m = p["master"]
        print(f"{p['key']:14s} master={m['status']} refs={m['refs']} -> {p['verdict']}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())