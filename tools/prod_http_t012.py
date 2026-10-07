"""TASK-012 生产只读体检 A：HTTP 三端点（healthz / live.m3u / epg.xml）。"""
import json
import urllib.request

B = "http://127.0.0.1:8080"


def g(p):
    try:
        r = urllib.request.urlopen(B + p, timeout=6)
        return r.status, r.headers.get("Content-Type", ""), r.read()
    except Exception as e:  # noqa: BLE001
        return 0, type(e).__name__, b""


c, t, b = g("/healthz")
print("HEALTHZ", c, t)
if c == 200:
    d = json.loads(b)
    print("STATUS", d["status"])
    print("STALE", d["freshness"]["is_stale"])
    print("LASTPUB", d["freshness"]["last_success_publish_at"])
    print("UPTIME", d["service"]["uptime_seconds"])
    print("PBYTES", d["playlist"]["bytes"])
    r = d.get("last_run") or {}
    print("LASTRUN", r.get("round_id"), r.get("outcome"), r.get("publish_status"))

c, t, b = g("/live.m3u")
print("LIVEM3U", c, t, len(b), b.count(b"#EXTINF"))

c, t, b = g("/epg.xml")
print("EPGXML", c, t, len(b))
