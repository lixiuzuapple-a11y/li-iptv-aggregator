"""TASK-012 §6 生产 probe history 只读审计（第1 步：基线盘点）。

只读：DB 以 mode=ro 打开，不写任何东西，不改生产。
输出给REPORTS/TASK-012-PROBE-AUDIT.md 用的原始统计。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

DB = os.environ.get("LIPTV_DB", "/var/lib/li-iptv-aggregator/liptv.sqlite3")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def q1(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return row[0] if row else None


def main() -> int:
    conn = connect()
    out: dict = {}

    # ---------- 1. 库存 ----------
    out["stream_total"] = q1(conn, "SELECT count(*) FROM stream")
    out["stream_enabled"] = q1(conn, "SELECT count(*) FROM stream WHERE enabled = 1")
    out["stream_stale"] = q1(
        conn, "SELECT count(*) FROM stream WHERE status = 'stale'")
    out["canonical_total"] = q1(conn, "SELECT count(*) FROM canonical_channel")
    out["canonical_enabled"] = q1(
        conn, "SELECT count(*) FROM canonical_channel WHERE enabled = 1")

    # ---------- 2. probe history 总量 ----------
    out["probe_rows"] = q1(conn, "SELECT count(*) FROM probe_result")
    out["probe_success"] = q1(
        conn, "SELECT count(*) FROM probe_result WHERE success = 1")
    out["probe_failure"] = out["probe_rows"] - out["probe_success"]
    rng = conn.execute(
        "SELECT min(checked_at) AS a, max(checked_at) AS b FROM probe_result"
    ).fetchone()
    out["probe_first_at"] = rng["a"]
    out["probe_last_at"] = rng["b"]
    out["probe_distinct_streams"] = q1(
        conn, "SELECT count(DISTINCT stream_id) FROM probe_result")

    # ---------- 3. 每 stream 样本分桶 ----------
    rows = q(conn, """
        SELECT s.id AS sid,
               (SELECT count(*) FROM probe_result p
                 WHERE p.stream_id = s.id) AS n
          FROM stream s
    """)
    buckets = {"0": 0, "1": 0, "2": 0, "ge2": 0, "ge4": 0, "ge6": 0}
    per_stream_n = []
    for r in rows:
        n = int(r["n"])
        per_stream_n.append(n)
        if n == 0:
            buckets["0"] += 1
        elif n == 1:
            buckets["1"] += 1
        else:
            buckets["2"] += 1
        if n >= 2:
            buckets["ge2"] += 1
        if n >= 4:
            buckets["ge4"] += 1
        if n >= 6:
            buckets["ge6"] += 1
    out["sample_buckets"] = buckets
    out["max_samples_on_one_stream"] = max(per_stream_n) if per_stream_n else 0

    # ---------- 4. 错误类型分布 ----------
    out["error_distribution"] = [
        dict(r) for r in q(conn, """
            SELECT COALESCE(error_type, '(success)') AS error_type, count(*) AS n
              FROM probe_result
             GROUP BY COALESCE(error_type, '(success)')
             ORDER BY n DESC
        """)
    ]

    # ---------- 5. canonical 多线路结构 ----------
    out["canonical_stream_shape"] = [
        dict(r) for r in q(conn, """
            SELECT CASE WHEN n = 1 THEN 'single' ELSE 'multi' END AS shape,
                   count(*) AS canonical
              FROM (SELECT canonical_channel_id, count(*) AS n
                      FROM stream WHERE enabled = 1 AND status <> 'stale'
                     GROUP BY canonical_channel_id)
             GROUP BY shape
        """)
    ]
    out["cross_source_multi"] = q1(conn, """
        SELECT count(*) FROM (
            SELECT ss.stream_id
              FROM stream_source ss
              JOIN source_channel sc ON sc.id = ss.source_channel_id
             GROUP BY ss.stream_id
            HAVING count(DISTINCT sc.source_id) > 1
        )
    """)

    # ---------- 6. multi-stream 明细（failover 候选池）----------
    out["multi_stream_detail"] = [
        dict(r) for r in q(conn, """
            SELECT s.canonical_channel_id AS ccid, c.name AS name,
                   count(DISTINCT s.id) AS streams,
                   count(DISTINCT sc.source_id) AS sources,
                   sum(CASE WHEN p.n > 0 THEN 1 ELSE 0 END) AS streams_probed
              FROM stream s
              JOIN canonical_channel c ON c.id = s.canonical_channel_id
              LEFT JOIN stream_source ss ON ss.stream_id = s.id
              LEFT JOIN source_channel sc ON sc.id = ss.source_channel_id
              LEFT JOIN (SELECT stream_id, count(*) AS n
                           FROM probe_result GROUP BY stream_id) p
                     ON p.stream_id = s.id
             WHERE s.enabled = 1 AND s.status <> 'stale'
             GROUP BY s.canonical_channel_id
            HAVING count(DISTINCT s.id) > 1
             ORDER BY count(DISTINCT s.id) DESC, c.name
        """)
    ]

    # ---------- 7. host 集中度 ----------
    out["host_concentration"] = [
        dict(r) for r in q(conn, """
            SELECT h.host AS host, count(*) AS streams
              FROM (
                  SELECT s.id AS sid,
                         substr(s.url, instr(s.url, '://') + 3,
                                CASE
                                  WHEN instr(substr(s.url, instr(s.url, '://') + 3), '/') > 0
                                  THEN instr(substr(s.url, instr(s.url, '://') + 3), '/') - 1
                                  ELSE length(s.url)
                                END) AS host
                    FROM stream s WHERE s.enabled = 1
              ) h
             GROUP BY h.host ORDER BY streams DESC LIMIT 15
        """)
    ]

    # ---------- 8. never-success / recovered ----------
    out["never_success_streams"] = [
        dict(r) for r in q(conn, """
            SELECT s.id AS sid, s.canonical_channel_id AS ccid, c.name AS name
              FROM stream s
              JOIN canonical_channel c ON c.id = s.canonical_channel_id
             WHERE s.enabled = 1
               AND NOT EXISTS (SELECT 1 FROM probe_result p
                                WHERE p.stream_id = s.id AND p.success = 1)
        """)
    ]
    out["all_failing_canonical"] = [
        dict(r) for r in q(conn, """
            SELECT c.id AS ccid, c.name AS name, count(s.id) AS streams
              FROM canonical_channel c JOIN stream s
                ON s.canonical_channel_id = c.id
             WHERE c.enabled = 1 AND s.enabled = 1 AND s.status <> 'stale'
             GROUP BY c.id
            HAVING sum(CASE WHEN EXISTS (
                        SELECT 1 FROM probe_result p
                         WHERE p.stream_id = s.id AND p.success = 1)
                    THEN 1 ELSE 0 END) = 0
        """)
    ]

    # ---------- 9. 当前 selector 视角 ----------
    sys.path.insert(0, "/opt/li-iptv-aggregator/releases"
                           "/96251065bb6e-20261006T154316Z")
    try:
        from liptv import select as select_mod  # type: ignore

        pub, skipped = [], []
        for r in q(conn, """
                SELECT id, name FROM canonical_channel
                 WHERE enabled = 1 ORDER BY id"""):
            best = select_mod.select_best_stream(conn, int(r["id"]))
            (pub if best else skipped).append(
                {"ccid": int(r["id"]), "name": r["name"],
                 "stream_id": best.stream_id if best else None,
                 "rate": round(best.success_rate, 3) if best else None,
                 "cons_fail": best.consecutive_failures if best else None,
                 "probes": best.probe_count if best else 0})
        out["selector_published"] = pub
        out["selector_skipped"] = skipped
    except Exception as exc:  # noqa: BLE001
        out["selector_error"] = f"{type(exc).__name__}: {exc}"

    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
