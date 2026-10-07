"""TASK-012：在生产 release 上跑真实 reliability-status（只读）。"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.environ.get(
    "LIPTV_SRC", "/opt/li-iptv-aggregator/current"))
LIB = "/var/lib/li-iptv-aggregator"

from liptv import reliability as R  # noqa: E402


def load(name):
    p = os.path.join(LIB, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    conn = sqlite3.connect(f"file:{LIB}/liptv.sqlite3?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    summary = R.build_summary(
        conn, window_days=7,
        publish_summary=load("publish-summary.json"),
        runtime_status=load("runtime-status.json"),
        epg_status=load("epg-status.json"),
    )
    print("=== HUMAN ===")
    print(R.render_human(summary))
    print("\n=== KEY NUMBERS ===")
    fixed = summary["fixed"]
    print("canonical_total", fixed["canonical_total"])
    print("published", fixed["published"])
    for k in ("stable", "degraded", "failed", "unknown", "stable_rate"):
        print(" ", k, fixed.get(k))
    print("stream_total", fixed["stream_total"])
    print("stream_healthy", fixed["stream_healthy"])
    print("stream_failed", fixed["stream_failed"])
    print("multi_stream", fixed["multi_stream"])
    print("single_stream", fixed.get("single_stream"))
    print("failover_count", fixed.get("failover_count"))
    print("recovered_count", fixed.get("recovered_count"))
    print("\nunstable_top10", json.dumps(
        (fixed.get("unstable") or [])[:10], ensure_ascii=False))
    print("\n=== CADENCE ===")
    print(json.dumps(summary["cadence"], ensure_ascii=False, indent=2))
    print("\n=== GROWTH ===")
    print(json.dumps(summary["growth"], ensure_ascii=False, indent=2))
    print("\n=== DYNAMIC ===")
    print(json.dumps(summary["dynamic"], ensure_ascii=False, indent=2))
    print("\n=== PROBE ===")
    print(json.dumps(summary["probe"], ensure_ascii=False, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
