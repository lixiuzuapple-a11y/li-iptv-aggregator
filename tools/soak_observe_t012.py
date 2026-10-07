"""TASK-012 §22/§23/§24：生产 soak 观测（**只读**）。

采集内容
--------
* §22：服务 uptime / 重启次数 / scheduler 轮次历史 / 锁 / 临时文件 /
  publish-summary / runtime-status / epg-status / SQLite 体积 /
  journal 错误 / 内存 / CPU / 磁盘 / open fd / ffprobe 子进程与僵尸。
* §23：DB 增长、probe_result 速率与 30/180/365 天外推。
* §24：状态文件是否覆盖式、临时目录是否有界。

「不能只看服务还活着」是任务书原话，所以这里刻意把
**重启次数、临时文件残留、fd 数、僵尸进程、journal 错误**都算进输出。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sqlite3
import sys

LIB = "/var/lib/li-iptv-aggregator"
DB = os.path.join(LIB, "liptv.sqlite3")
RT = os.path.join(LIB, "runtime-status.json")
EPG_STATUS = os.path.join(LIB, "epg-status.json")
PS = os.path.join(LIB, "publish-summary.json")
REL_CURRENT = "/opt/li-iptv-aggregator/current"


def sh(*args):
    """跑一条只读命令；TAT 环境没有 curl/ss，用这些替代。"""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=25)
        return out.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"ERR {type(exc).__name__}"


def section(title):
    print(f"\n=== {title} ===")


def main():
    now = dt.datetime.now(dt.timezone.utc)
    print("SOAK_AT", now.isoformat())

    # ---------------------------------------------------------- §22 服务
    section("服务与进程")
    print("IS_ACTIVE", sh("systemctl", "is-active", "li-iptv.service"))
    props = sh("systemctl", "show", "li-iptv.service",
               "-p", "ActiveEnterTimestamp", "-p", "NRestarts",
               "-p", "MainPID", "-p", "MemoryCurrent", "-p", "CPUUsageNSec",
               "-p", "TasksCurrent")
    for line in props.splitlines():
        print("  PROP", line)
    print("UPTIME", sh("systemctl", "show", "li-iptv.service",
                       "-p", "ActiveEnterTimestampMonotonic", "-p", "ExecMainStartTimestamp"))
    print("FD_COUNT", sh("sh", "-c",
                         "ls /proc/$(systemctl show li-iptv.service -p MainPID"
                         " --value)/fd 2>/dev/null | wc -l"))
    print("ZOMBIE", sh("sh", "-c",
                       "ps -eo stat= 2>/dev/null | grep -c Z || echo 0"))
    print("FFPROBE_CHILD", sh("sh", "-c",
                              "pgrep -c ffprobe 2>/dev/null || echo 0"))
    print("SERVICE_PROCS", sh("sh", "-c",
                              "ps -eo pid,rss,comm 2>/dev/null | grep -E 'liptv|python' "
                              "| head -6"))

    # ---------------------------------------------------------- §22 磁盘
    section("磁盘与库体积")
    print("DISK", sh("df", "-h", LIB))
    for name in ("liptv.sqlite3", "liptv.sqlite3-wal", "liptv.sqlite3-shm",
                 "live.m3u", "live.previous.m3u", "epg.xml"):
        path = os.path.join(LIB, name)
        if os.path.exists(path):
            print(f"  FILE {name} {os.path.getsize(path)}")
    print("JOURNAL_MODE", end=" ")
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    print(conn.execute("PRAGMA journal_mode").fetchone()[0],
          "| page_size", conn.execute("PRAGMA page_size").fetchone()[0],
          "| page_count", conn.execute("PRAGMA page_count").fetchone()[0])

    # ---------------------------------------------------------- §22 journal
    section("systemd journal（近 200 行里的报错）")
    j = sh("journalctl", "-u", "li-iptv.service", "-n", "200", "--no-pager",
           "-p", "warning")
    lines = [x for x in j.splitlines() if x.strip()]
    print("WARNING_LINES", len(lines))
    for line in lines[:12]:
        print("  J", line[:160])

    # ---------------------------------------------------------- §22 临时文件
    section("临时文件与状态文件（§24 有界性）")
    for pattern in ("*.tmp", "*.tmp.*", "live.*.m3u.tmp"):
        found = sh("sh", "-c", f"ls -1 {LIB}/{pattern} 2>/dev/null | wc -l")
        print(f"  TMP {pattern} = {found}")
    tmpdir = sh("sh", "-c", f"ls -1 {LIB}/tmp 2>/dev/null | wc -l")
    print("  TMPDIR entries =", tmpdir)
    for name in ("runtime-status.json", "epg-status.json", "publish-summary.json",
                 "reliability-summary.json"):
        path = os.path.join(LIB, name)
        if os.path.exists(path):
            print(f"  STATE {name} {os.path.getsize(path)}")

    # ---------------------------------------------------------- 轮次历史
    section("scheduler 轮次")
    rt = None
    if os.path.exists(RT):
        rt = json.load(open(RT, encoding="utf-8"))
    if rt:
        rounds = rt.get("rounds") or []
        print("ROUNDS_KEPT", len(rounds))
        print("LAST_PUBLISH", rt.get("last_success_publish_at"))
        for r in rounds[-8:]:
            print("  R", r.get("round_id"), r.get("outcome"),
                  r.get("started_at"), "->", r.get("finished_at"),
                  "| publish", r.get("publish_status"))
    else:
        print("MISSING", RT)

    if os.path.exists(EPG_STATUS):
        es = json.load(open(EPG_STATUS, encoding="utf-8"))
        print("\nEPG_STATUS keys", sorted(es.keys())[:12])
        print("EPG last_success_epoch", es.get("last_success_epoch"),
              "| error", es.get("error"))

    # ---------------------------------------------------------- §23 增长
    section("数据增长（§23）")
    total = conn.execute("SELECT count(*) FROM probe_result").fetchone()[0]
    first, last = conn.execute(
        "SELECT min(checked_at), max(checked_at) FROM probe_result").fetchone()
    print("PROBE_ROWS", total, "| first", first, "| last", last)
    days = conn.execute(
        "SELECT substr(checked_at,1,10) d, count(*) n FROM probe_result"
        " GROUP BY d ORDER BY d").fetchall()
    for d, n in days[-6:]:
        print("  DAY", d, n)

    # ---------------------------------------------------------- §22 发布摘要
    section("发布摘要（近几轮）")
    if os.path.exists(PS):
        ps = json.load(open(PS, encoding="utf-8"))
        print("status", ps.get("status"), "| fixed", ps.get("fixed_count"),
              "| dynamic", ps.get("dynamic_count"),
              "| rewritten", ps.get("rewritten"),
              "| published_at", ps.get("published_at"))
        ds = ps.get("dynamic_sources") or []
        for item in ds:
            print("  SRC", item.get("source_name"), "ok", item.get("ok"),
                  "status", item.get("status"),
                  "cat", item.get("error_category"))
    conn.close()


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ""))
    main()
