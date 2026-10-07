"""TASK-012 §7 辅助：probe 时间分布 + runtime 状态（只读）。

用来判断「生产 probe 的真实 cadence」与「距离上次 probe 多久」，
从而决定 TASK-012 多轮 probe 怎么排（§7 要求真实时间间隔）。
"""

from __future__ import annotations

import os
import sqlite3

DB = "/var/lib/li-iptv-aggregator/liptv.sqlite3"
RT = "/var/lib/li-iptv-aggregator/runtime-status.json"

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = conn.execute(
    "SELECT substr(checked_at, 1, 13) AS h, count(*) AS n, sum(success) AS ok"
    " FROM probe_result GROUP BY h ORDER BY h"
).fetchall()

print("HOURS_WITH_PROBE", len(rows))
print("FIRST", rows[0] if rows else None)
print("LAST", rows[-1] if rows else None)
print("-- last 30 hours --")
for h, n, ok in rows[-30:]:
    print("  ", h, "n", n, "ok", ok)

# 按天聚合，看真正的长期 cadence
print("-- per day --")
days = conn.execute(
    "SELECT substr(checked_at, 1, 10) AS d, count(*) AS n,"
    " count(DISTINCT substr(checked_at,1,13)) AS hours"
    " FROM probe_result GROUP BY d ORDER BY d"
).fetchall()
for d, n, h in days:
    print("  ", d, "rows", n, "active_hours", h)

print("-- runtime status --")
if os.path.exists(RT):
    with open(RT, encoding="utf-8") as fh:
        print(fh.read()[:1200])
else:
    print("MISSING", RT)
