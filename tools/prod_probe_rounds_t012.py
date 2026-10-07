"""TASK-012 只读生产探针：probe 轮次分布 + scheduler 轮次记录。"""
import sqlite3
import json
import os

DB = "/var/lib/li-iptv-aggregator/liptv.sqlite3"
RT = "/var/lib/li-iptv-aggregator/runtime-status.json"

c = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row

n = c.execute("select count(*) from probe_result").fetchone()[0]
mx = c.execute("select max(checked_at) from probe_result").fetchone()[0]
mn = c.execute("select min(checked_at) from probe_result").fetchone()[0]
print("PROBE_ROWS", n, "FIRST", mn, "LAST", mx)

days = c.execute(
    "select substr(checked_at,1,10) d, count(*) n,"
    " count(distinct substr(checked_at,1,13)) h"
    " from probe_result group by d order by d").fetchall()
for r in days:
    print("  DAY", r["d"], "rows", r["n"], "hours", r["h"])

# 每个 canonical 的最新 probe 时间（看真实 cadence）
rows = c.execute(
    "select cc.name, count(pr.id) n, max(pr.checked_at) last_at"
    " from canonical_channel cc"
    " join stream s on s.canonical_channel_id = cc.id"
    " join probe_result pr on pr.stream_id = s.id"
    " group by cc.id order by cc.id").fetchall()
print("CANONICAL_WITH_PROBE", len(rows))
multi = c.execute(
    "select canonical_channel_id, count(*) n from stream"
    " where enabled=1 and status<>'stale' group by canonical_channel_id"
    " having n>1 order by n desc").fetchall()
print("MULTI_STREAM_CANONICAL", len(multi))
for r in multi[:12]:
    print("   cid", r["canonical_channel_id"], "streams", r["n"])

if not os.path.exists(RT):
    print("RUNTIME_STATUS MISSING")
    raise SystemExit(0)

d = json.load(open(RT, encoding="utf-8"))
print("ROUNDS_KEPT", len(d.get("rounds") or []))
print("LAST_SUCCESS_PUBLISH", d.get("last_success_publish_at"))
cr = d.get("current_round") or {}
print("CURRENT", cr.get("round_id"), cr.get("outcome"), cr.get("finished_at"),
      cr.get("publish_status"))
for r in (d.get("rounds") or [])[-8:]:
    print("  ROUND", r.get("round_id"), r.get("outcome"),
          r.get("started_at"), "->", r.get("finished_at"),
          r.get("publish_status"))
