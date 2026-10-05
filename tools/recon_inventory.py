"""盘点两个已采用fixed 源的全部可用频道，看能扩到多少 canonical。

TASK-010 §2A/B 要求优先 CCTV-1~17 + 省级卫视。
TASK-009 只用了 10 个 canonical，本轮要从已有源扩到 >=30。

严格沿用 TASK-009 的确定性归一（不fuzzy）：
  1. 剥离末尾分辨率括号(720p)/(1080p)/(1080i)/(576i)/(480p) 等；
  2. 归一后要求两侧逐字节相等；
  3. 任一侧不唯一 ⇒ ambiguous，跳过不绑。

输出：可用 canonical 候选清单 + 分类建议 + 跨源交集分析。
只输出频道名/分组/host 计数，**不输出完整 stream URL**。
"""
import json
import re
import ssl
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from urllib.parse import urlparse

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

SOURCES = {
    "iptv-org-cn": "https://iptv-org.github.io/iptv/countries/cn.m3u",
    "guovin-gd-ipv4": "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv4/result.m3u",
}

BANNED_QUERY_KEYS = {
    "token", "auth", "key", "secret", "msisdn", "migutoken",
    "sign", "hdnts", "expire", "mdspid", "authid", "livekey",
    "jsbt", "jsbk", "sessionid", "uid", "uuid",
}

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
# 只剥末尾的分辨率括号 —— 白名单式，绝不动其它字符
RES_SUFFIX_RE = re.compile(r"\s*[\(\[](720p|1080p|1080i|576i|480p|2160p|4k|fhd|hd|sd)\s*[\)\]]\s*$", re.I)


def http_get(url, timeout=45):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def normalize_name(n):
    """白名单式归一：只剥末尾分辨率括号，其余逐字节保留。"""
    prev = None
    cur = n.strip()
    while prev != cur:
        prev = cur
        cur = RES_SUFFIX_RE.sub("", cur).strip()
    return cur


def has_banned(u):
    from urllib.parse import parse_qsl
    try:
        keys = {k.lower() for k, _ in parse_qsl(urlparse(u).query, keep_blank_values=True)}
    except Exception:
        return True
    return bool(keys & BANNED_QUERY_KEYS)


def parse(body):
    text = body.decode("utf-8", errors="replace")
    out = []
    group = None
    name = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs = dict(ATTR_RE.findall(line))
            group = attrs.get("group-title", "")
            disp = line.split(",", 1)
            name = disp[1].strip() if len(disp) > 1 else attrs.get("tvg-name", "")
        elif not line.startswith("#") and "://" in line:
            out.append((name or "", group or "", line.strip()))
            name = group = None
    return out


def host_of(u):
    try:
        return urlparse(u).netloc.lower()
    except Exception:
        return ""


def main():
    data = {}
    for sname, url in SOURCES.items():
        try:
            body = http_get(url)
        except Exception as e:
            print(f"[{sname}] FETCH FAIL {type(e).__name__}", file=sys.stderr)
            data[sname] = []
            continue
        entries = parse(body)
        # 万能流：同一 URL 被 >=5 条引用 ⇒ 整条排除（TASK-009 §4）
        urlc = Counter(u for _n, _g, u in entries)
        catchall = {u for u, k in urlc.items() if k >= 5}
        clean = [(n, g, u) for n, g, u in entries
                 if u not in catchall and not has_banned(u)]
        print(f"[{sname}] raw={len(entries)} catchall_entries="
              f"{sum(urlc[u] for u in catchall)} clean={len(clean)}", file=sys.stderr)
        data[sname] = clean

    # 归一后建立索引：norm -> [(name, group, url)]
    idx = defaultdict(list)
    for sname, items in data.items():
        for n, g, u in items:
            nm = normalize_name(n)
            if nm:
                idx[nm].append((sname, n, g, u))

    # TASK-009 §「歧义检查」原义：同一 source 内该归一名对应**多个不同原始名** ⇒ 不绑。
    # ⚠️ 同一 source 内**同名多条**（不同 host）**不是**歧义，那正是「同一 canonical
    # 多 stream」。我第一版把「条目数 > 1」误当歧义，导致 12 个跨源 CCTV 全被错杀。
    per_source_variants = defaultdict(set)
    per_source_count = Counter()
    for sname, items in data.items():
        for n, _g, _u in items:
            nm = normalize_name(n)
            per_source_variants[(sname, nm)].add(n)
            per_source_count[(sname, nm)] += 1

    rows = []
    for nm, lst in idx.items():
        srcs = {s for s, _n, _g, _u in lst}
        # 真歧义 = 同源内多个**不同**原始名（与 build_fixed_seed.py:193-205 完全一致）
        ambiguous = any(len(per_source_variants[(s, nm)]) > 1 for s in srcs)
        hosts = {host_of(u) for _s, _n, _g, u in lst}
        rows.append({
            "normalized": nm,
            "display_names": sorted({n for _s, n, _g, _u in lst}),
            "groups": sorted({g for _s, _n, g, _u in lst}),
            "sources": sorted(srcs),
            "source_count": len(srcs),
            "stream_count": len(lst),
            "per_source_streams": {
                s: per_source_count[(s, nm)] for s in sorted(srcs)
            },
            "distinct_hosts": len(hosts),
            "ambiguous": ambiguous,
        })

    rows.sort(key=lambda r: (-r["source_count"], -r["stream_count"], r["normalized"]))
    json.dump({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "total_normalized": len(rows),
        "non_ambiguous": sum(1 for r in rows if not r["ambiguous"]),
        "cross_source": sum(1 for r in rows if r["source_count"] >= 2 and not r["ambiguous"]),
        "rows": rows,
    }, sys.stdout, ensure_ascii=False, indent=2)
    print("", file=sys.stderr)


if __name__ == "__main__":
    main()