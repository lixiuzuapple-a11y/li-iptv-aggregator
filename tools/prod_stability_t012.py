"""TASK-012 生产只读分析：用新模块算真实稳定性与 reliability 摘要。

⚠️ 严格只读：DB 以 mode=ro 打开，全程不写任何表、不发布、不重启。
"""
import json
import os
import sys

sys.path.insert(0, "/opt/li-iptv-aggregator/releases/96251065bb6e-20261006T154316Z")

from liptv import errors as E           # noqa: E402
from liptv import reliability as R      # noqa: E402
from liptv import stability as S        # noqa: E402
import sqlite3                          # noqa: E402

DB = "/var/lib/li-iptv-aggregator/liptv.sqlite3"
RT = "/var/lib/li-iptv-aggregator/runtime-status.json"

conn = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

print("=== 1. fixed 稳定性派生（真实生产数据）===")
chs = S.derive_all(conn)
agg = S.aggregate(chs)
print("canonical_total", agg["canonical_total"])
print("stable", agg["stable"], "degraded", agg["degraded"],
      "failed", agg["failed"], "unknown", agg["unknown"])
print("stable_rate", "%.1f%%" % (agg["stable_rate"] * 100))
print("multi_stream", agg["multi_stream"],
      "with_backup", agg["multi_stream_with_backup"])
print("stream_total", agg["stream_total"], "eligible", agg["stream_eligible"])

print()
print("=== 2. 非 STABLE 频道明细 ===")
for ch in sorted(chs.values(), key=lambda c: (c.state != S.FAILED,
                                               c.success_rate, c.name)):
    if ch.state == S.STABLE:
        continue
    print("  %-22s %-8s rate=%5.1f%% streams=%d/%d sel=%s cat=%s" % (
        ch.name[:22], ch.state, ch.success_rate * 100,
        ch.stream_eligible, ch.stream_total, ch.selected_stream_id,
        ch.top_error_category))
    if ch.reason:
        print("      reason:", ch.reason)

print()
print("=== 3. probe 错误分布（统一口径）===")
rows = conn.execute(
    "select error_type from probe_result where success=0").fetchall()
dist = E.distribution(r["error_type"] for r in rows)
total_bad = sum(dist.values())
print("FAILED_ROWS", total_bad)
for k, v in dist.items():
    print("  %-26s %6d  %5.1f%%" % (k, v, v * 100.0 / max(1, total_bad)))

print()
print("=== 4. 从未成功 / 已恢复的 stream ===")
never = conn.execute(
    "select count(*) from stream s where s.enabled=1 and s.status<>'stale'"
    " and not exists (select 1 from probe_result p"
    " where p.stream_id=s.id and p.success=1)").fetchone()[0]
recovered = conn.execute(
    "select count(*) from (select stream_id from probe_result"
    " group by stream_id having"
    " sum(case when success=0 then 1 else 0 end)>0 and sum(success)>0)"
).fetchone()[0]
print("NEVER_SUCCESS_STREAMS", never)
print("RECOVERED_STREAMS", recovered)

print()
print("=== 5. reliability summary（真实写盘）===")
rt = json.load(open(RT, encoding="utf-8"))
pub_sum = None
for cand in ("/var/lib/li-iptv-aggregator/publish-summary.json",
             "/var/lib/li-iptv-aggregator/out/publish-summary.json"):
    if os.path.isfile(cand):
        pub_sum = json.load(open(cand, encoding="utf-8"))
        print("publish_summary_from", cand)
        break
summary = R.build_summary(
    conn, window_days=7, selection_kwargs={"window_days": 7},
    publish_summary=pub_sum, runtime_status=rt,
)
print(R.render_human(summary))

out = "/home/ubuntu/t012/reliability-summary.json"
res = R.write_summary(summary, out)
print()
print("WRITE", res["written"], res["bytes"], "bytes ->", res["path"])
print("no_sensitive_url", "playtoken" not in json.dumps(summary).lower())

print()
print("=== 6. CCTV-5+ 专项（§16）===")
for r in conn.execute(
        "select cc.name nm, count(distinct s.id) nstreams,"
        " sum(p.success) oks, count(p.id) probes"
        " from canonical_channel cc"
        " join stream s on s.canonical_channel_id=cc.id"
        " left join probe_result p on p.stream_id=s.id"
        " where cc.name like '%5+%' or cc.name like '%5 %'"
        " group by cc.id"):
    print("  ", r["nm"], "streams", r["nstreams"], "probes", r["probes"],
          "success", r["oks"] or 0)
    for r2 in conn.execute(
            "select s.id, s.url, sum(p.success) oks, count(p.id) probes"
            " from stream s"
            " left join probe_result p on p.stream_id=s.id"
            " where s.canonical_channel_id=(select id from canonical_channel"
            " where name=?) group by s.id", (r["nm"],)):
        print("     stream", r2["id"], "probes", r2["probes"],
              "success", r2["oks"] or 0)
