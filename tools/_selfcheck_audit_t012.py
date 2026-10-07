"""本地验证 audit_probe_history_t012.py 的 SQL：造一份贴近生产的假 DB 跑一遍。

只在本机跑，不碰生产。用 schema/schema_v1.sql 建表。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = os.path.join(ROOT, "schema", "schema_v1.sql")
TOOL = os.path.join(ROOT, "tools", "audit_probe_history_t012.py")

CONN = sqlite3.connect(":memory:")
CONN.row_factory = sqlite3.Row
CONN.executescript(open(SCHEMA, encoding="utf-8").read())

CONN.execute("INSERT INTO source (id,name,kind,url,enabled,created_at,updated_at)"
             " VALUES (1,'fixed-a','fixed_m3u',NULL,1,'t','t')")
CONN.execute("INSERT INTO source (id,name,kind,url,enabled,created_at,updated_at)"
             " VALUES (2,'fixed-b','fixed_m3u',NULL,1,'t','t')")
CONN.execute("INSERT OR REPLACE INTO probe (id,name,location,enabled)"
             " VALUES (1,'shanghai-cloud','Shanghai',1)")

# canonical：3 个，c1 单线路、c2 双线路跨源、c3 双线路同源
CONN.execute("INSERT INTO canonical_channel (id,name,category,enabled,priority,"
             "created_at,updated_at) VALUES (1,'CCTV-1','央视',1,10,'t','t')")
CONN.execute("INSERT INTO canonical_channel (id,name,category,enabled,priority,"
             "created_at,updated_at) VALUES (2,'CCTV-5','央视',1,10,'t','t')")
CONN.execute("INSERT INTO canonical_channel (id,name,category,enabled,priority,"
             "created_at,updated_at) VALUES (3,'DeadTV','央视',1,10,'t','t')")

STREAMS = [
    # id, ccid, url, source_id
    (1, 1, "http://a.example.com/live/s1.m3u8", 1),
    (2, 2, "http://b.example.com/live/s2.m3u8", 1),
    (3, 2, "http://c.example.com/live/s3.m3u8", 2),
    (4, 3, "http://d.example.com/live/s4.m3u8", 1),
    (5, 3, "http://d.example.com/live/s5.m3u8", 1),
]
for sid, ccid, url, src in STREAMS:
    CONN.execute("INSERT INTO stream (id,canonical_channel_id,url,url_hash,"
                 "first_seen_at,last_seen_at,enabled,status)"
                 " VALUES (?,?,?,?, 't','t',1,'observed')", (sid, ccid, url, f"h{sid}"))
    CONN.execute("INSERT INTO source_channel (id,source_id,identity_hash,raw_name,"
                 "raw_stream_url,first_seen_at,last_seen_at,active)"
                 " VALUES (?,?,?,?,?,'t','t',1)", (sid, src, f"i{sid}", f"n{sid}", url))
    CONN.execute("INSERT INTO stream_source (id,stream_id,source_channel_id,"
                 "first_seen_at,last_seen_at) VALUES (?,?,?,'t','t')", (sid, sid, sid))

# probe_result：stream1 健康；stream2 三连败（应被 selector 淘汰）；
# stream3 从失败恢复（recovery 场景）；stream4/5 从未成功
# 元组顺序：(stream_id, probe_id, checked_at, success, error_type)
PR = [
    (1, 1, "2026-10-06T00:00:00+00:00", 0, "TIMEOUT"),
    (1, 1, "2026-10-06T01:00:00+00:00", 1, None),
    (1, 1, "2026-10-06T02:00:00+00:00", 1, None),
    (2, 1, "2026-10-06T00:00:00+00:00", 1, None),
    (2, 1, "2026-10-06T01:00:00+00:00", 0, "HTTP_5XX"),
    (2, 1, "2026-10-06T02:00:00+00:00", 0, "HTTP_5XX"),
    (3, 1, "2026-10-06T00:00:00+00:00", 0, "TIMEOUT"),
    (3, 1, "2026-10-06T01:00:00+00:00", 0, "TIMEOUT"),
    (3, 1, "2026-10-06T02:00:00+00:00", 1, None),
    (4, 1, "2026-10-06T00:00:00+00:00", 0, "SEGMENT_UNREACHABLE"),
    (5, 1, "2026-10-06T00:00:00+00:00", 0, "SEGMENT_UNREACHABLE"),
]
for sid, pid, at, succ, err in PR:
    CONN.execute("INSERT INTO probe_result (stream_id,probe_id,checked_at,success,"
                 "error_type,startup_ms,resolution_width,resolution_height)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (sid, pid, at, succ, err, 300 if succ else None,
                  1920 if succ else None, 1080 if succ else None))
CONN.commit()

tmp = os.path.join(tempfile.gettempdir(), "t012_audit_check.sqlite")
CONN.backup(sqlite3.connect(tmp))
CONN.close()

env = dict(os.environ, LIPTV_DB=tmp)
proc = subprocess.run([sys.executable, TOOL], env=env,
                      capture_output=True, text=True, encoding="utf-8")
print("RC", proc.returncode)
if proc.returncode != 0:
    print(proc.stderr[-2500:])
    raise SystemExit(1)
print(proc.stdout[:4000])
