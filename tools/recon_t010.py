"""TASK-010 增量fixed source 侦察 —— 复用 TASK-009 资产，只做增量。

输出结构化 JSON 到 stdout，供后续生成 FIXED-SOURCE-RECON-TASK010.md。
严格约束（沿用 TASK-009 结论）：
  * 只用公开无认证入口；
  * 不设置 Cookie / Authorization；
  * 识别并统计 token / 签名 / 身份参数画像；
  * 不下载媒体内容（只看首包特征）。
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

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# 已知的签名 / 身份参数（TASK-009 §3 冻结的排除集，扩充了 hdnts/mdspid）
BANNED_QUERY_KEYS = {
    "token", "auth", "key", "secret", "msisdn", "migutoken",
    "sign", "hdnts", "expire", "mdspid", "authid", "livekey",
    "jsbt", "jsbk", "mdsp", "sessionid", "uid", "uuid",
}

# 任务书 §3.2 要求新增/复验 ≥8 个价值候选
CANDIDATES = [
    # --- iptv-org 家族：已被TASK-009 判定「同上游、重叠条目无独立 stream」，
    #     本轮只复验「是否还能抓」「是否仍是同上游」，不做新采用源。
    {"name": "iptv-org-news", "kind": "fixed_m3u",
     "url": "https://iptv-org.github.io/iptv/categories/news.m3u",
     "cat": "news", "country": "global", "project": "iptv-org",
     "intent": "复验TASK-009 淘汰理由是否仍成立（token 多+ 同上游重叠）"},

    {"name": "iptv-org-hk", "kind": "fixed_m3u",
     "url": "https://iptv-org.github.io/iptv/countries/hk.m3u",
     "cat": "regional", "country": "hk", "project": "iptv-org",
     "intent": "TASK-009 备选（18 条，量小）；本轮复验并评估能否支撑 §D 港澳台"},

    {"name": "iptv-org-tw", "kind": "fixed_m3u",
     "url": "https://iptv-org.github.io/iptv/countries/tw.m3u",
     "cat": "regional", "country": "tw", "project": "iptv-org",
     "intent": "TASK-009 备选（26 条，量小）；复验 + 评估"},

    # --- Guovin 其它明确安全输出（TASK-009 §3.2 点名范围）
    {"name": "guovin-hd-ipv4", "kind": "fixed_m3u",
     "url": "https://raw.githubusercontent.com/Guovin/iptv-api/hd/output/ipv4/result.m3u",
     "cat": "domestic", "country": "cn", "project": "Guovin/iptv-api",
     "intent": "任务书点名的『Guovin 其它明确安全输出』；验证是否提供独立 host"},

    {"name": "guovin-hd-ipv6", "kind": "fixed_m3u",
     "url": "https://raw.githubusercontent.com/Guovin/iptv-api/hd/output/ipv6/result.m3u",
     "cat": "domestic", "country": "cn", "project": "Guovin/iptv-api",
     "intent": "Guovin HD 系列 IPv6；IPv6-only 源在生产主机通常不可达，需实测"},

    {"name": "guovin-ah-ipv4", "kind": "fixed_m3u",
     "url": "https://raw.githubusercontent.com/Guovin/iptv-api/ah/output/ipv4/result.m3u",
     "cat": "domestic", "country": "cn", "project": "Guovin/iptv-api",
     "intent": "Guovin 省级分集；验证跨省分集能否提供独立 host"},

    {"name": "guovin-sd-ipv4", "kind": "fixed_m3u",
     "url": "https://raw.githubusercontent.com/Guovin/iptv-api/sd/output/ipv4/result.m3u",
     "cat": "domestic", "country": "cn", "project": "Guovin/iptv-api",
     "intent": "Guovin 省级分集（山东），验证多省源可覆盖省级卫视"},

    # --- fanmingming 当前可用新入口（TASK-009 判定 ipv6 版不可用，global 版 404）
    {"name": "fanmingming-ipv4", "kind": "fixed_m3u",
     "url": "https://live.fanmingming.com/tv/m3u/ipv4.m3u",
     "cat": "domestic", "country": "cn", "project": "fanmingming/live",
     "intent": "任务书点名的『fanmingming 当前可用的新入口（若恢复）』；ipv4 版是关键差异"},

    # --- 其它无需鉴权的公开固定频道来源
    {"name": "Zhejiang-IPTV-List", "kind": "fixed_m3u",
     "url": "https://raw.githubusercontent.com/YanG-1989/tvlist/main/tvlist.m3u",
     "cat": "domestic", "country": "cn", "project": "YanG-1989/tvlist",
     "intent": "复验 TASK-009 记录的 404 是否已恢复"},

    {"name": "fanmingming-live", "kind": "fixed_m3u",
     "url": "https://live.fanmingming.com/tv/m3u6/ipv4.m3u",
     "cat": "domestic", "country": "cn", "project": "fanmingming/live",
     "intent": "fanmingming 备选入口变体"},
]

ATTR_RE = re.compile(r'\s+([A-Za-z0-9_-]+)="([^"]*)"')
EXTS = (".m3u", ".m3u8")


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        body = r.read()
        dt = time.perf_counter() - t0
        return r.status, dict(r.headers), body, dt


def parse_m3u(body):
    """返回 [(name, group, url, attrs)]。"""
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return []
    entries = []
    name = group = None
    attrs = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m = ATTR_RE.search(line)
            attrs = {m.group(1): m.group(2)} if m else {}
            name = attrs.get("tvg-name") or ""
            group = attrs.get("group-title") or ""
            # display name = 最后一个逗号之后
            disp = line.split(",", 1)
            disp = disp[1].strip() if len(disp) > 1 else ""
            if disp:
                name = disp
        elif not line.startswith("#") and ("://" in line):
            entries.append((name or "", group or "", line.strip(), attrs))
    return entries


def classify_query(url):
    """返回 (banned_keys_present, all_key_names)。"""
    try:
        q = urlparse(url).query
        keys = [k for k, _ in parse_qsl(q, keep_blank_values=True)]
    except Exception:
        return [], []
    banned = sorted({k.lower() for k in keys} & BANNED_QUERY_KEYS)
    return banned, sorted({k.lower() for k in keys})


def host_of(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def main():
    out = []
    for c in CANDIDATES:
        rec = {
            "name": c["name"], "kind": c["kind"], "entry_url": c["url"],
            "category": c["cat"], "country": c["country"],
            "maintainer_project": c["project"], "intent": c["intent"],
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        try:
            status, headers, body, dt = http_get(c["url"])
            rec["http_status"] = status
            rec["latency_s"] = round(dt, 2)
            rec["bytes"] = len(body)
        except urllib.error.HTTPError as e:
            rec["http_status"] = e.code
            rec["error"] = f"HTTPError {e.code}"
            out.append(rec)
            print(f"[{c['name']}] HTTP {e.code} -> skip", file=sys.stderr)
            continue
        except Exception as e:
            rec["http_status"] = None
            rec["error"] = f"{type(e).__name__}: {e}"
            out.append(rec)
            print(f"[{c['name']}] FAIL {e}", file=sys.stderr)
            continue

        entries = parse_m3u(body)
        rec["parseable"] = bool(entries)
        rec["entry_count"] = len(entries)
        if entries:
            banned_counter = Counter()
            key_counter = Counter()
            clean = 0
            hosts = Counter()
            for _n, _g, u, _a in entries:
                b, allk = classify_query(u)
                if b:
                    banned_counter.update(b)
                key_counter.update(allk)
                if not b:
                    clean += 1
                    hosts[host_of(u)] += 1
            rec["clean_entry_count"] = clean
            rec["banned_query_keys"] = dict(banned_counter.most_common(12))
            rec["all_query_keys"] = dict(key_counter.most_common(12))
            rec["distinct_hosts"] = len(hosts)
            # 万能流检测：同一 URL 被 >=5 个 entry 引用
            urlc = Counter(u for _n, _g, u, _a in entries)
            catchall = [u for u, k in urlc.items() if k >= 5]
            rec["catchall_url_count"] = len(catchall)
            rec["catchall_entry_count"] = sum(urlc[u] for u in catchall)
            rec["groups_top"] = dict(Counter(g for _n, g, _u, _a in entries).most_common(8))
        out.append(rec)
        print(f"[{c['name']}] {rec['http_status']} entries={rec.get('entry_count')} "
              f"clean={rec.get('clean_entry_count')} hosts={rec.get('distinct_hosts')} "
              f"catchall={rec.get('catchall_entry_count')}", file=sys.stderr)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print("", file=sys.stderr)


if __name__ == "__main__":
    main()