"""TASK-012 生产只读体检：/healthz + /live.m3u + /epg.xml + probe 轮次。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8080"


def get(path):
    try:
        r = urllib.request.urlopen(BASE + path, timeout=6)
        return r.status, r.headers.get("Content-Type", ""), r.read()
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__ + ": " + str(exc)[:80], b""


code, ctype, body = get("/healthz")
print("HEALTHZ", code, ctype)
if code == 200:
    d = json.loads(body)
    print("  status", d["status"])
    print("  stale", d["freshness"]["is_stale"])
    print("  last_publish", d["freshness"]["last_success_publish_at"])
    print("  uptime_s", d["service"]["uptime_seconds"])
    print("  playlist_bytes", d["playlist"]["bytes"])
    lr = d.get("last_run") or {}
    print("  last_round", lr.get("round_id"), lr.get("outcome"),
          lr.get("publish_status"))

code, ctype, body = get("/live.m3u")
extinf = body.count(b"#EXTINF")
print("LIVE_M3U", code, ctype, "bytes", len(body), "extinf", extinf)

code, ctype, body = get("/epg.xml")
print("EPG_XML", code, ctype, "bytes", len(body))

import sqlite3
DB = "/var/lib/li-iptv-aggregator/liptv.sqlite3"
c = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row
n = c.execute("select count(*) from probe_result").fetchone()[0]
last = c.execute("select max(checked_at) from probe_result").fetchone()[0]
print("PROBE_ROWS", n, "LAST", last)

days = c.execute(
    "select substr(checked_at,1,10) d, count(*) n,"
    " count(distinct substr(checked_at,1,13)) h"
    " from probe_result group by d order by d").fetchall()
for r in days:
    print("  DAY", r["d"], "rows", r["n"], "hours", r["h"])

multi = c.execute(
    "select s.canonical_channel_id cid, cc.name, count(*) n"
    " from stream s join canonical_channel cc on cc.id=s.canonical_channel_id"
    " where s.enabled=1 and s.status<>'stale'"
    " group by s.canonical_channel_id having n>1 order by n desc").fetchall()
print("MULTI_STREAM", len(multi))
for r in multi[:12]:
    print("   ", r["name"], "streams", r["n"])

RT = "/var/lib/li-iptv-aggregator/runtime-status.json"
d = json.load(open(RT, encoding="utf-8"))
rounds = d.get("rounds") or []
print("ROUNDS_KEPT", len(rounds))
for r in rounds[-6:]:
    print("  ROUND", r.get("round_id"), r.get("outcome"),
          r.get("started_at"), "->", r.get("finished_at"),
          r.get("publish_status"))
