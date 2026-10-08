"""TASK-012 最终验收：§43 里还没直接实测的几条（只读）。"""
import json
import os
import re
import sqlite3
import sys
import urllib.request

LIB = "/var/lib/li-iptv-aggregator"


def get(path, base="http://127.0.0.1:8080"):
    try:
        r = urllib.request.urlopen(base + path, timeout=8)
        return r.status, r.headers.get("Content-Type", ""), r.read()
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__, b""


def main():
    print("=== 三端点（验收 52/53/54）===")
    for path in ("/healthz", "/live.m3u", "/epg.xml"):
        st, ct, body = get(path)
        extra = ""
        if path == "/live.m3u":
            extra = " EXTINF=%d" % body.count(b"#EXTINF")
        if path == "/healthz":
            try:
                d = json.loads(body)
                extra = " status=%s stale=%s" % (
                    d.get("status"), (d.get("freshness") or {}).get("is_stale"))
            except Exception:
                pass
        print(" %-11s %s %s bytes=%d%s" % (path, st, ct[:30], len(body), extra))

    print("=== localhost binding（验收 55）===")
    out = os.popen("netstat -ltnp 2>/dev/null | grep 8080").read().strip()
    print(out or "(netstat 无输出)")

    print("=== EPG mapping 保持（验收 56）===")
    st, ct, body = get("/epg.xml")
    text = body.decode("utf-8", "replace")
    chans = re.findall(r'<channel id="([^"]+)"', text)
    progs = text.count("<programme ")
    print(" epg channels=%d programmes=%d" % (len(chans), progs))
    for want in ("CCTV1", "CCTV5", "CCTV6"):
        print("  has %-8s %s" % (want, want in chans))

    print("=== live.m3u tvg-id 与 EPG 交集（口径分清）===")
    st, ct, m3u = get("/live.m3u")
    ids = re.findall(r'tvg-id="([^"]*)"', m3u.decode("utf-8", "replace"))
    nonempty = [i for i in ids if i]
    hit = [i for i in nonempty if i in chans]
    print(" fixed entries=%d  with tvg-id=%d  EPG-hit=%d"
          % (m3u.count(b"#EXTINF"), len(nonempty), len(hit)))
    miss = sorted({i for i in nonempty if i not in chans})
    print(" miss=%s" % (miss[:8],))

    print("=== CCTV-5+ 专项（验收 25）===")
    conn = sqlite3.connect(f"file:{LIB}/liptv.sqlite3?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id FROM canonical_channel WHERE name LIKE '%CCTV-5%'").fetchone()
    if row:
        cid = row["id"]
        agg = conn.execute(
            "SELECT count(*) n, sum(success) ok FROM probe_result pr"
            " JOIN stream s ON s.id = pr.stream_id"
            " WHERE s.canonical_channel_id = ?", (cid,)).fetchone()
        hours = conn.execute(
            "SELECT count(DISTINCT substr(pr.checked_at,1,13)) FROM probe_result pr"
            " JOIN stream s ON s.id = pr.stream_id"
            " WHERE s.canonical_channel_id = ?", (cid,)).fetchone()[0]
        print(" cid=%d probes=%d ok=%d distinct_hours=%d"
              % (cid, agg["n"], agg["ok"] or 0, hours))
    else:
        print(" (未找到 CCTV-5+ 频道)")

    print("=== 120.76.248.139 跨 round 观察（验收 23）===")
    hosts = conn.execute(
        "SELECT s.url u, sum(pr.success) ok, count(*) n"
        " FROM stream s JOIN probe_result pr ON pr.stream_id = s.id"
        " WHERE s.url LIKE '%176.106.125.216%'"
        " GROUP BY s.url").fetchall()
    for h in hosts:
        print(" ", h["u"][:60], "ok", h["ok"], "/", h["n"])

    print("=== cross-source / single-stream（验收 21/22）===")
    multi = conn.execute(
        "SELECT canonical_channel_id cid, count(*) n FROM stream"
        " WHERE enabled=1 GROUP BY cid HAVING n>1").fetchall()
    single = conn.execute(
        "SELECT c.name FROM canonical_channel c JOIN stream s"
        " ON s.canonical_channel_id=c.id WHERE s.enabled=1"
        " GROUP BY c.id HAVING count(*)=1").fetchall()
    print(" multi_stream=%d  single_stream=%d %s"
          % (len(multi), len(single), [s["name"] for s in single][:5]))
    conn.close()


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
