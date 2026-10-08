"""TASK-012：在生产上验证 growth 口径修正（只读）。

只依赖 ``liptv.retention``，不碰 reliability / cadence，避免为验一个数字
就往生产塞三个文件。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.environ.get(
    "LIPTV_SRC", "/opt/li-iptv-aggregator/current"))

from liptv import retention as R  # noqa: E402

DB = "/var/lib/li-iptv-aggregator/liptv.sqlite3"


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    doc = R.estimate(conn)
    print("=== GROWTH (只读，生产真实数据) ===")
    print("available", doc.get("available"))
    print("verdict", doc.get("verdict"))
    print("rate_source", doc.get("rate_source"))
    print("bytes_per_row", doc.get("bytes_per_row"))
    print("basis", doc.get("bytes_per_row_basis"))
    print("threshold", doc.get("threshold_bytes"))
    print("retention_executable", doc.get("retention_executable"))
    daily = doc.get("daily") or {}
    for k in ("days_observed", "active_days", "basis_days", "latest_day",
              "latest_day_rows", "mean_rows_per_day", "max_rows_per_day"):
        print(" daily." + k, daily.get(k))
    print("--- projections ---")
    for key, item in (doc.get("projections") or {}).items():
        print(" ", key, item["rows"], "rows",
              round(item["bytes"] / 1048576, 1), "MiB")
    print("--- 按 mean 口径对照 ---")
    alt = R.estimate(conn, rate_source="mean")
    print(" mean verdict", alt.get("verdict"),
          "| 365d", alt["projections"]["365d"]["rows"], "rows",
          round(alt["projections"]["365d"]["bytes"] / 1048576, 1), "MiB")
    print("--- 人读输出 ---")
    print(R.render_human(doc))
    conn.close()


if __name__ == "__main__":
    main()
