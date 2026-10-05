"""TASK-010 第二轮侦察 —— 只验证**真实存在**的入口。

第一轮教训：Guovin/iptv-api 只有 `gd` 一个分支（我猜的 hd/ah/sd 全404），
fanmingming 只有 ipv6 版（200），ipv4/global 全 404。
⇒ 不再猜路径，只用「已实测 200」或经网页核实存在的入口。

目标：找到**独立于 iptv-org-cn 与 guovin-gd-ipv4** 的第三、第四个 fixed 源，
因为跨源 multi-stream 硬指标需要≥5 个 canonical 有 2+ 独立源 stream。
"""
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from urllib.parse import parse_qsl, urlparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

BANNED_QUERY_KEYS = {
    "token", "auth", "key", "secret", "msisdn", "migutoken",
    "sign", "hdnts", "expire", "mdspid", "authid", "livekey",
    "jsbt", "jsbk", "sessionid", "uid", "uuid", "password", "passwd",
}

# 全部为「已知社区公开 IPTV 聚合项目」的常见入口。
# 判定一律以实测 HTTP 为准，不以知名度为准（TASK-009 §0 原则）。
CANDIDATES = [
    # 已实测 200 —— 复验 + 详细画像（TASK-009 判为备选/淘汰，本轮重新评估）
    {"name": "iptv-org-hk", "url": "https://iptv-org.github.io/iptv/countries/hk.m3u",
     "country": "hk", "cat": "regional", "project": "iptv-org",
     "intent": "§D 香港公开频道；复验 200 与可播性"},

    {"name": "iptv-org-tw", "url": "https://iptv-org.github.io/iptv/countries/tw.m3u",
     "country": "tw", "cat": "regional", "project": "iptv-org",
     "intent": "§D 台湾公开频道；复验 200 与可播性"},

    {"name": "iptv-org-jp", "url": "https://iptv-org.github.io/iptv/countries/jp.m3u",
     "country": "jp", "cat": "regional", "project": "iptv-org",
     "intent": "§D 日本公开频道；TASK-009 记录仅 7 条，复验是否恢复"},

    {"name": "iptv-org-kr", "url": "https://iptv-org.github.io/iptv/countries/kr.m3u",
     "country": "kr", "cat": "regional", "project": "iptv-org",
     "intent": "§D 韩国公开频道；复验81 条"},

    {"name": "fanmingming-ipv6", "url": "https://live.fanmingming.com/tv/m3u/ipv6.m3u",
     "country": "cn", "cat": "domestic", "project": "fanmingming/live",
     "intent": "TASK-009 判 IPv6-only 生产 0/12 可达；本轮复验（可能网络条件已变）"},

    # 任务书 §C点名的国际新闻 —— 逐个用官方/公开入口实测
    {"name": "aljazeera-en", "url": "https://live-hls-web-aje.getaj.net/AJE/01.m3u8",
     "country": "qa", "cat": "news", "project": "Al Jazeera (official)",
     "intent": "§C 优先新闻源；官方公开 HLS 直链候选"},

    {"name": "dw-en", "url": "https://dwamdstream107.akamaized.net/hls/live/2015525/dwstream107/index.m3u8",
     "country": "de", "cat": "news", "project": "DW (official CDN)",
     "intent": "§C 优先新闻源；官方公开 HLS 候选"},

    {"name": "france24-en", "url": "https://live.france24.com/hls/live/2037218/F24_EN_HI_HLS/master_5000.m3u8",
     "country": "fr", "cat": "news", "project": "France 24 (official)",
     "intent": "§C 优先新闻源；官方公开 HLS 候选"},

    {"name": "nhk-world-jp", "url": "https://master.nhkworld.jp/nhkworld-tv/playlist/live.m3u8",
     "country": "jp", "cat": "news", "project": "NHK World (official)",
     "intent": "§C 优先新闻源；官方公开 HLS 候选"},

    {"name": "cgtn-doc", "url": "https://livedoc.cgtn.com/500d/prog_index.m3u8",
     "country": "cn", "cat": "news", "project": "CGTN Documentary (official)",
     "intent": "§C 点名 CGTN Documentary；官方公开 HLS 候选"},

    {"name": "aljazeera-ar", "url": "https://live-hls-web-aje.getaj.net/AJE/02.m3u8",
     "country": "qa", "cat": "news", "project": "Al Jazeera (official)",
     "intent": "§C Al Jazeera 语种变体，用于验证『同源多 stream』是否可作multi-stream"},

    # 央视/卫视官方公开直播入口（TASK-010 §2.A 明确要求 CCTV 系列）
    {"name": "cctv1-direct", "url": "https://live.cctv.com/live/cctv1",
     "country": "cn", "cat": "cctv", "project": "CCTV official",
     "intent": "§2.A CCTV-1；探测官方页面能否解析出 HLS 直链"},

    {"name": "cctv5-direct", "url": "https://live.cctv.com/live/cctv5",
     "country": "cn", "cat": "cctv", "project": "CCTV official",
     "intent": "§2.A CCTV-5 体育；官方页面解析探测"},
]

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        body = r.read()
        return r.status, body, time.perf_counter() - t0


def is_m3u(body):
    head = body[:400].decode("utf-8", errors="replace").lstrip()
    return head.startswith("#EXTM3U") or head.startswith("#EXTINF")


def parse_m3u(body):
    text = body.decode("utf-8", errors="replace")
    entries = []
    name = group = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs = dict(ATTR_RE.findall(line))
            name = attrs.get("tvg-name", "")
            group = attrs.get("group-title", "")
            disp = line.split(",", 1)
            if len(disp) > 1 and disp[1].strip():
                name = disp[1].strip()
        elif not line.startswith("#") and "://" in line:
            entries.append((name or "", group or "", line.strip()))
    return entries


def classify(url):
    try:
        keys = [k.lower() for k, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)]
    except Exception:
        return [], []
    return sorted({k for k in keys} & BANNED_QUERY_KEYS), sorted(set(keys))


def host_of(u):
    try:
        return urlparse(u).netloc.lower()
    except Exception:
        return ""


def probe_single_stream(u, timeout=15):
    """抽样媒体可达性：跟随重定向，GET 前512B，看是否媒体而非 HTML。

    只取首包特征，不落盘（任务书 §14）。
    """
    req = urllib.request.Request(u, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            body = r.read(512)
            return {
                "status": r.status,
                "content_type": ctype,
                "final_host": host_of(r.geturl()),
                "looks_media": (
                    "mpegurl" in ctype
                    or "video/" in ctype
                    or body[:5] == b"\x47\x23\x45\x58"  # not real check
                    or body[:1] == b"\x47"
                ),
                "looks_html": b"<html" in body[:200].lower() or "text/html" in ctype,
                "final_query": classify(r.geturl())[0],
            }
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": "HTTPError"}
    except Exception as e:
        return {"status": None, "error": f"{type(e).__name__}"}


def main():
    out = []
    for c in CANDIDATES:
        rec = {
            "name": c["name"], "entry_url": c["url"], "country": c["country"],
            "category": c["cat"], "maintainer_project": c["project"],
            "intent": c["intent"],
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        try:
            status, body, dt = http_get(c["url"])
            rec["http_status"] = status
            rec["latency_s"] = round(dt, 2)
            rec["bytes"] = len(body)
        except urllib.error.HTTPError as e:
            rec["http_status"] = e.code
            rec["error"] = f"HTTPError {e.code}"
            out.append(rec)
            print(f"[{c['name']}] HTTP {e.code}", file=sys.stderr)
            continue
        except Exception as e:
            rec["http_status"] = None
            rec["error"] = f"{type(e).__name__}: {e}"
            out.append(rec)
            print(f"[{c['name']}] FAIL {type(e).__name__}", file=sys.stderr)
            continue

        if is_m3u(body):
            entries = parse_m3u(body)
            rec["kind"] = "fixed_m3u"
            rec["parseable"] = True
            rec["entry_count"] = len(entries)
            bc, ac, hosts, urlc = Counter(), Counter(), Counter(), Counter()
            clean = 0
            for _n, _g, u in entries:
                b, allk = classify(u)
                bc.update(b)
                ac.update(allk)
                urlc[u] += 1
                if not b:
                    clean += 1
                    hosts[host_of(u)] += 1
            catchall = [u for u, k in urlc.items() if k >= 5]
            rec.update({
                "clean_entry_count": clean,
                "banned_query_keys": dict(bc.most_common(10)),
                "all_query_keys": dict(ac.most_common(10)),
                "distinct_hosts": len(hosts),
                "catchall_entry_count": sum(urlc[u] for u in catchall),
                "groups_top": dict(Counter(g for _n, g, _u in entries).most_common(6)),
            })
        else:
            # 单流 HLS 或 HTML 页面
            rec["kind"] = "single_stream_or_page"
            rec["parseable"] = is_m3u(body)
            rec["entry_count"] = 1
            ctype = ""
            if body[:200].lower().find(b"<html") >= 0:
                ctype = "html_page"
            elif b"#EXTM3U" in body[:200]:
                ctype = "hls_playlist"
            rec["body_kind"] = ctype
            b, allk = classify(c["url"])
            rec["banned_query_keys"] = {k: 1 for k in b}
            rec["clean_entry_count"] = 1
            rec["distinct_hosts"] = 1
            rec["catchall_entry_count"] = 0
            if ctype == "hls_playlist":
                p = probe_single_stream(c["url"])
                rec["sample_probe"] = p
        out.append(rec)
        print(f"[{c['name']}] {rec['http_status']} kind={rec.get('kind')} "
              f"entries={rec.get('entry_count')} clean={rec.get('clean_entry_count')} "
              f"hosts={rec.get('distinct_hosts')} catch={rec.get('catchall_entry_count')}",
              file=sys.stderr)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print("", file=sys.stderr)


if __name__ == "__main__":
    main()