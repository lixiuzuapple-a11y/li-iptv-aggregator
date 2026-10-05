"""区分 URLError 的归属：DNS 失败 / 代理拦截 / TLS / 源真不可用。

任务书要求区分 aggregator reachability（上海能否访问），
本机测不到不等于源不可用——必须在生产主机复验。
本脚本只负责把本机侧的错误原因拆清楚。
"""
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

TARGETS = [
    "https://live-hls-web-aje.getaj.net/AJE/01.m3u8",
    "https://dwamdstream107.akamaized.net/hls/live/2015525/dwstream107/index.m3u8",
    "https://live.france24.com/hls/live/2037218/F24_EN_HI_HLS/master_5000.m3u8",
    "https://master.nhkworld.jp/nhkworld-tv/playlist/live.m3u8",
    "https://livedoc.cgtn.com/500d/prog_index.m3u8",
    "https://live.cctv.com/live/cctv1",
    "https://iptv-org.github.io/iptv/countries/cn.m3u",
]


def dns_probe(host):
    out = {}
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        out["dns"] = "OK"
        out["ips"] = ips[:6]
        out["ip_count"] = len(ips)
    except Exception as e:
        out["dns"] = f"{type(e).__name__}"
        out["dns_detail"] = str(e)[:120]
    return out


def http_probe(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            head = r.read(256)
            return {
                "result": "HTTP",
                "status": r.status,
                "content_type": (r.headers.get("Content-Type") or "")[:40],
                "elapsed_s": round(time.perf_counter() - t0, 2),
                "final_host": urlparse(r.geturl()).netloc,
                "head_kind": ("hls" if b"#EXTM3U" in head
                              else "html" if b"<html" in head.lower()
                              else "binary"),
            }
    except urllib.error.HTTPError as e:
        return {"result": "HTTPError", "status": e.code,
                "elapsed_s": round(time.perf_counter() - t0, 2)}
    except urllib.error.URLError as e:
        return {"result": "URLError", "reason": type(e.reason).__name__,
                "detail": str(e.reason)[:160],
                "elapsed_s": round(time.perf_counter() - t0, 2)}
    except Exception as e:
        return {"result": type(e).__name__, "detail": str(e)[:160],
                "elapsed_s": round(time.perf_counter() - t0, 2)}


def main():
    print("=== 本机代理环境 ===", file=sys.stderr)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        v = __import__("os").environ.get(k)
        print(f"  {k} = {v}", file=sys.stderr)
    print("", file=sys.stderr)

    results = []
    for u in TARGETS:
        host = urlparse(u).netloc
        rec = {"url": u, "host": host}
        rec.update(dns_probe(host))
        rec.update(http_probe(u))
        results.append(rec)
        print(f"[{host}] dns={rec.get('dns')} http={rec.get('result')} "
              f"status={rec.get('status')} {rec.get('reason') or ''} "
              f"{rec.get('detail') or ''}", file=sys.stderr)

    json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
    print("", file=sys.stderr)


if __name__ == "__main__":
    main()