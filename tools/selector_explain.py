#!/usr/bin/env python3
"""selector 解释器：把「为什么选这条线路」摊开成可核对的事实。

任务书 TASK-009 §7 要求「selector 的选择必须可由 probe history 解释」。
本工具**只读**：不写库、不改状态，只把三件事打印出来：

  1. ``probe_result`` 的原始历史（每条 stream 的 PASS/FAIL 与错误类型）；
  2. 每个 canonical 下每条 stream 的评分事实与 ``eligible`` 判定理由；
  3. ``select_best_stream`` / ``select_playlist`` 的最终选择。

URL 一律脱敏成 ``scheme://host/...``（与 publish 侧口径一致）——
解释线路选择不需要看完整地址，看主机就够定位了。

用法::

    python tools/selector_explain.py --db /path/to/liptv.sqlite3
    python tools/selector_explain.py --db ... --canonical 1 --full-url
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from liptv import db as db_mod  # noqa: E402
from liptv import select as select_mod  # noqa: E402


def short_url(url: str) -> str:
    """脱敏到 scheme://host —— 与 publish/publish 侧 redact_url_light 同口径。"""
    try:
        tail = url.split("://", 1)[1]
        host = tail.split("/", 1)[0]
        return f"{url.split('://', 1)[0]}://{host}/..."
    except (IndexError, AttributeError):
        return "scheme://host/..."


def print_history(conn) -> None:
    total = conn.execute("SELECT COUNT(*) FROM probe_result").fetchone()[0]
    ok = conn.execute("SELECT COUNT(*) FROM probe_result WHERE success = 1").fetchone()[0]
    print("=== probe_result 原始历史 ===")
    print(f"total={total}  success={ok}  failure={total - ok}")
    rows = conn.execute(
        "SELECT stream_id, success, error_type, COUNT(*) c FROM probe_result"
        " GROUP BY stream_id, success, error_type ORDER BY stream_id"
    ).fetchall()
    for r in rows:
        mark = "PASS" if int(r["success"]) else "FAIL"
        print(f"  stream {int(r['stream_id']):>4}  {mark}  x{int(r['c']):<3} {r['error_type'] or ''}")


def explain_canonical(conn, row, *, show_url, **score_kwargs) -> None:
    cid = int(row["id"])
    scores = select_mod.score_streams(conn, cid, **score_kwargs)
    best = select_mod.select_best_stream(conn, cid, **score_kwargs)
    print(f"\n[{cid}] {row['name']}  ({row['category']})  streams={len(scores)}")
    for s in scores:
        flag = "  <== SELECTED" if best is not None and s.stream_id == best.stream_id else ""
        url = s.url if show_url else short_url(s.url)
        print(
            f"   id={s.stream_id:<4} status={s.status:<10} probes={s.probe_count:<3}"
            f" ok={s.success_count:<3} rate={s.success_rate:.2f}"
            f" consec_fail={s.consecutive_failures}"
            f" startup={s.median_startup_ms}ms"
            f" {s.resolution_width or '-'}x{s.resolution_height or '-'}"
            f" eligible={s.eligible} reason={s.reason or '-'}{flag}"
        )
        print(f"        {url}")
    if best is None:
        print("   => 无线路达到门槛，该 canonical 不发布")
    else:
        print(f"   => SELECTED stream_id={best.stream_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解释 selector 的线路选择依据（只读）")
    parser.add_argument("--db", required=True, help="sqlite 数据库路径")
    parser.add_argument("--canonical", type=int, default=None, help="只看某个 canonical_channel id")
    parser.add_argument("--full-url", action="store_true", help="打印完整 stream URL（默认脱敏）")
    parser.add_argument("--no-history", action="store_true", help="跳过 probe_result 原始历史段")
    parser.add_argument("--window-days", type=int, default=select_mod.DEFAULT_WINDOW_DAYS)
    parser.add_argument("--max-consecutive-failures", type=int,
                        default=select_mod.DEFAULT_MAX_CONSECUTIVE_FAILURES)
    parser.add_argument("--min-successes", type=int, default=select_mod.DEFAULT_MIN_SUCCESSES)
    args = parser.parse_args(argv)

    score_kwargs = {
        "window_days": args.window_days,
        "max_consecutive_failures": args.max_consecutive_failures,
        "min_successes": args.min_successes,
    }

    conn = db_mod.connect(args.db)
    try:
        if not args.no_history:
            print_history(conn)

        if args.canonical is not None:
            row = conn.execute(
                "SELECT id, name, category FROM canonical_channel WHERE id = ?",
                (args.canonical,),
            ).fetchone()
            if row is None:
                print(f"canonical_channel {args.canonical} 不存在")
                return 1
            explain_canonical(conn, row, show_url=args.full_url, **score_kwargs)
            return 0

        print("\n=== 逐 canonical 的评分与选择 ===")
        rows = conn.execute(
            "SELECT id, name, category FROM canonical_channel ORDER BY id"
        ).fetchall()
        for row in rows:
            explain_canonical(conn, row, show_url=args.full_url, **score_kwargs)

        print("\n=== select_playlist 汇总（按现有门槛）===")
        result = select_mod.select_playlist(conn, group_order=[], **score_kwargs)
        print(f"entries={len(result['entries'])}  skipped={len(result['skipped'])}")
        for e in result["entries"]:
            url = e["url"] if args.full_url else short_url(e["url"])
            print(f"   {e['name']}  <- stream {e['stream_id']}  {url}")
        for s in result["skipped"]:
            print(f"   SKIPPED {s['name']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
