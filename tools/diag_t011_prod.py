"""TASK-011 §12/§13 深挖诊断 —— 在 recon_t011_prod.py 判定之上再钻一层。

R1 得到的三个事实需要定位根因，而不是停在标签上：

1. ``120.76.248.139`` 的 15 条 URL 全部 ``200 text/html`` ⇒ 高度可疑的**假流**
   （服务器回网页而非 playlist）。要确认它到底返回什么 HTML。
2. 27 条 ``200 application/vnd.apple.mpegurl`` 但**零条目** ⇒ 真 M3U 却空。
   要看 body 前几行：是空壳、还是只有 ``#EXTM3U`` 头。
3. NHK World master ``200`` 8 条目但第一个变体 ``404`` ⇒ 要逐个变体探测，
   确认是真的全挂还是只是挑错了档位。

**本脚本仍然只读**：不写DB、不改config、不重启服务、不代理视频。
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


def _ctx() -> ssl.SSLContext:
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def grab(url: str, timeout: int = 20, limit: int = 4000) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity",
    })
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
            body = r.read(limit)
            return {
                "status": r.status,
                "ctype": (r.headers.get("Content-Type") or ""),
                "clen": r.headers.get("Content-Length"),
                "server": r.headers.get("Server"),
                "final": r.geturl(),
                "ms": int((time.perf_counter() - t0) * 1000),
                "body": body.decode("utf-8", errors="replace"),
                "err": None,
            }
    except urllib.error.HTTPError as e:
        return {"status": e.code, "ctype": "", "clen": None, "server": None,
                "final": url, "ms": int((time.perf_counter() - t0) * 1000),
                "body": "", "err": "HTTPError"}
    except Exception as e:
        return {"status": None, "ctype": "", "clen": None, "server": None,
                "final": url, "ms": int((time.perf_counter() - t0) * 1000),
                "body": "", "err": type(e).__name__}


def variants(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "://" in line:
            out.append(line)
    return out


def rel(base: str, u: str) -> str:
    """把相对/裸路径变体补成绝对 URL。"""
    if u.startswith("http"):
        return u
    p = urlparse(base)
    root = f"{p.scheme}://{p.netloc}"
    if u.startswith("/"):
        return root + u
    return base.rsplit("/", 1)[0] + "/" + u


def diagnose_batch(hosts: list[str], label: str) -> dict:
    out = {"label": label, "probes": []}
    for u in hosts:
        g = grab(u)
        body = g["body"]
        vs = [rel(g["final"], v) for v in variants(body)]
        rec = {
            "url": u,
            "status": g["status"],
            "ctype": g["ctype"],
            "server": g["server"],
            "clen": g["clen"],
            "ms": g["ms"],
            "err": g["err"],
            "head": body[:600],
            "variant_count": len(vs),
        }
        # 逐个探测变体（最多 6 个），确认是「全挂」还是「挑错档位」
        vprobe = []
        for v in vs[:6]:
            sg = grab(v, timeout=15, limit=1500)
            segs = [x for x in variants(sg["body"]) if x]
            vprobe.append({
                "variant": v,
                "status": sg["status"],
                "ctype": sg["ctype"],
                "segments": len(segs),
                "first_seg": segs[0][:160] if segs else None,
                "err": sg["err"],
            })
        rec["variants"] = vprobe
        rec["any_segment"] = any(v["segments"] > 0 for v in vprobe)
        out["probes"].append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--r1", default="/tmp/t011_prod_r1.json")
    ap.add_argument("--mode", choices=["nhk", "fake200", "empty", "all"], default="all")
    ap.add_argument("--out", default="/tmp/t011_diag.json")
    args = ap.parse_args()

    with open(args.r1, encoding="utf-8") as fh:
        r1 = json.load(fh)

    buckets: dict[str, list[str]] = {"nhk": [], "fake200": [], "empty": []}
    for r in r1["results"]:
        if r["key"] == "nhk-world-jp":
            buckets["nhk"].append(r["url"])
        elif r["verdict"] == "fake_200":
            buckets["fake200"].append(r["url"])
        elif r["verdict"] == "empty":
            buckets["empty"].append(r["url"])

    modes = ["nhk", "fake200", "empty"] if args.mode == "all" else [args.mode]
    report = {"generated_at_epoch": int(time.time()), "buckets": {}}
    for m in modes:
        urls = buckets[m]
        if not urls:
            continue
        # 每类最多 6 条，够定位根因又不轰源
        sample = urls[:6]
        report["buckets"][m] = diagnose_batch(sample, f"{m} (n={len(urls)}, sampled {len(sample)})")

    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"wrote {args.out}")
    for m, b in report["buckets"].items():
        anyseg = sum(1 for p in b["probes"] if p["any_segment"])
        print(f"{m}: {len(b['probes'])} probed, {anyseg} with segments")
    return 0


if __name__ == "__main__":
    sys.exit(main())