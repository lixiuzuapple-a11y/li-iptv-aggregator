"""TASK-012 生产只读体检 B：probe 轮次分布 + multi-stream canonical + scheduler 轮次。"""
import json
import sqlite3

DB = "/var/lib/li-iptv-aggregator/liptv.sqlite3"
RT = "/var/lib/li-iptv-aggregator/runtime-status.json"

c = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row

print("ROWS", c.execute("select count(*) from probe_result").fetchone()[0])
print("LAST", c.execute("select max(checked_at) from probe_result").fetchone()[0])

for r in c.execute(
        "select substr(checked_at,1,10) d, count(*) n,"
        " count(distinct substr(checked_at,1,13)) h"
        " from probe_result group by d order by d"):
    print("DAY", r["d"], "rows", r["n"], "hours", r["h"])

rows = c.execute(
    "select cc.name nm, count(pr.id) n, sum(pr.success) ok"
    " from canonical_channel cc"
    " join stream s on s.canonical_channel_id=cc.id"
    " join probe_result pr on pr.stream_id=s.id"
    " group by cc.id order by n desc").fetchall()
print("CHANNELS", len(rows))
bad = [r for r in rows if r["ok"] * 2 < r["n"]]
print("MAJORITY_FAIL", len(bad))
for r in bad[:14]:
    print("  ", r["nm"], "n", r["n"], "ok", r["ok"])

multi = c.execute(
    "select s.canonical_channel_id cid, cc.name nm, count(*) n"
    " from stream s join canonical_channel cc on cc.id=s.canonical_channel_id"
    " where s.enabled=1 and s.status<>'stale'"
    " group by s.canonical_channel_id having n>1"
    " order by n desc, cc.name").fetchall()
print("MULTI", len(multi))
for r in multi:
    print("  ", r["nm"], r["n"])

d = json.load(open(RT, encoding="utf-8"))
rs = d.get("rounds") or []
print("ROUNDS", len(rs))
for r in rs[-6:]:
    print("  R", r.get("round_id"), r.get("outcome"),
          r.get("started_at"), r.get("finished_at"), r.get("publish_status"))
