"""TASK-012 §23：数据增长估算（**只估算，不删除**）。

任务书原文
----------
「检查 ``probe_result`` 是否会无限增长，估算当前速率下 30 天 / 180 天 / 1 年规模。
如果很小：明确结论，不做 retention。如果明显膨胀：只设计 retention 方案，
默认不执行不可逆大清理。」

本模块做的事
------------
1. 从**真实** ``probe_result`` 的时间分布算出日增行数（用实际跨度，不用配置猜）；
2. 按该速率外推 30 / 180 / 365 天，并换算成 SQLite 文件体积；
3. 给出 ``WORTH_RETENTION`` / ``FINE_AS_IS`` / ``UNKNOWN`` 三档结论。

🚨 冻结红线
----------
* 本模块**不含任何 DELETE / VACUUM**。它只读、只算。
* 行数估算按「最近完整天」而非全量平均 —— 全量平均会被早期的低频轮次拉低，
  得出偏乐观的结论。
* 速率样本不足（< 1 个完整天）时返回 ``UNKNOWN``，**不外推**。
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

from .util import iso_to_dt

RETENTION_UNKNOWN = "UNKNOWN"
RETENTION_FINE = "FINE_AS_IS"
RETENTION_WORTH = "WORTH_RETENTION"

ALL_VERDICTS = (RETENTION_FINE, RETENTION_WORTH, RETENTION_UNKNOWN)

#: 判定阈值：**一年**外推体积超过这个数就值得设计 retention（仍不自动执行）。
YEAR_BYTES_THRESHOLD = 512 * 1024 * 1024  # 512 MiB

#: 估算外推的天数。
HORIZONS = (30, 180, 365)


def daily_rows(conn: sqlite3.Connection, *, now: _dt.datetime | None = None) -> dict:
    """真实日增行数。用「最近一个有数据的自然日」做基准。"""
    reference = now or _dt.datetime.now(_dt.timezone.utc)
    rows = conn.execute(
        "SELECT substr(checked_at, 1, 10) AS d, count(*) AS n"
        " FROM probe_result WHERE checked_at IS NOT NULL"
        " GROUP BY d ORDER BY d DESC"
    ).fetchall()
    if not rows:
        return {"days_observed": 0, "latest_day": None, "latest_day_rows": None,
                "mean_rows_per_day": None, "active_days": 0}

    counts = {r["d"]: r["n"] for r in rows}
    # 只取「已完整过去」的自然日：今天还在累积中，算进去会低估速率。
    today = reference.date().isoformat()
    complete = [(d, n) for d, n in counts.items() if d < today]
    basis = complete or [(d, n) for d, n in counts.items()]

    latest_day, latest_rows = basis[0]
    return {
        "days_observed": len(counts),
        "active_days": len(basis),
        "latest_day": latest_day,
        "latest_day_rows": latest_rows,
        "mean_rows_per_day": round(sum(n for _, n in basis) / len(basis), 1),
    }


def estimate(conn: sqlite3.Connection, *,
             now: _dt.datetime | None = None,
             bytes_per_row: float | None = None,
             threshold_bytes: int = YEAR_BYTES_THRESHOLD) -> dict:
    """外推 30/180/365 天的行数与体积，并给出结论档位。

    ``bytes_per_row`` 不传则**实测**：读 ``page_count``/``page_size`` 算文件
    平均每行字节数。用真实值而不是猜的常数，是 §36「数字口径」的要求。
    """
    stats = daily_rows(conn, now=now)
    rate = stats.get("mean_rows_per_day")

    if not rate or stats.get("active_days", 0) < 1:
        return {
            "available": False,
            "verdict": RETENTION_UNKNOWN,
            "reason": "probe_result 无完整自然日样本，不外推（§37）",
            "daily": stats,
            "projections": {},
        }

    per_row = bytes_per_row if bytes_per_row is not None else _measure_bytes_per_row(conn)
    per_row_basis = "caller" if bytes_per_row is not None else _bytes_per_row_basis(conn)
    projections = {}
    for days in HORIZONS:
        rows_1y = rate * days
        projections[f"{days}d"] = {
            "days": days,
            "rows": int(round(rows_1y)),
            "bytes": int(round(rows_1y * per_row)),
        }

    year_bytes = projections["365d"]["bytes"]
    verdict = RETENTION_WORTH if year_bytes > threshold_bytes else RETENTION_FINE
    return {
        "available": True,
        "verdict": verdict,
        "reason": (
            f"按实测 {rate:.0f} 行/天、{per_row:.0f} B/行外推，"
            f"1 年约 {year_bytes / 1024 / 1024:.1f} MiB；"
            + ("超过阈值，建议设计 retention（本模块不执行任何删除）"
               if verdict == RETENTION_WORTH
               else "低于阈值，结论是不需要 retention")
        ),
        "bytes_per_row": per_row,
        "bytes_per_row_basis": per_row_basis,
        "threshold_bytes": threshold_bytes,
        "daily": stats,
        "projections": projections,
        # 明确写清：本模块不删数据（§23 的默认是「只设计，不执行」）。
        "retention_executable": False,
    }


def _bytes_per_row_basis(conn: sqlite3.Connection) -> str:
    """如实说明每行字节是**怎么**测出来的（§36 口径必须可解释）。"""
    try:
        conn.execute("SELECT count(DISTINCT pageno) FROM dbstat "
                     "WHERE name = 'probe_result'").fetchone()
    except sqlite3.Error:
        return "whole_db/rows（dbstat 不可用，含固定开销，偏保守）"
    return "dbstat:probe_result 页 / 行数"


def _measure_bytes_per_row(conn: sqlite3.Connection) -> float:
    """实测平均每行字节。

    优先用 SQLite 的 ``dbstat`` 虚表只取 ``probe_result`` 自己的页 ——
    整库大小除以行数会被**固定开销**放大（小库尤其离谱：实测一个只有几千行的
    测试库会算出 10KB/行，外推一年 37 MiB，纯属幻觉）。
    ``dbstat`` 不可用时退回「整库 / 行数」，但把口径如实标出来。
    """
    total = conn.execute("SELECT count(*) FROM probe_result").fetchone()[0]
    if not total:
        return 64.0
    try:
        rows = conn.execute(
            "SELECT count(DISTINCT pageno) FROM dbstat WHERE name = 'probe_result'"
        ).fetchone()
        pages = rows[0] if rows and rows[0] else 0
        if pages:
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            return max(1.0, (pages * page_size) / total)
    except sqlite3.Error:
        pass
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    except sqlite3.Error:
        return 64.0
    return max(1.0, (page_count * page_size) / total)


def render_human(estimate_doc: dict) -> str:
    """≤10 行人读输出。"""
    if not estimate_doc.get("available"):
        return "[增长] " + str(estimate_doc.get("reason") or "数据不足，不外推")
    daily = estimate_doc.get("daily") or {}
    lines = [
        f"[增长] 实测 {daily.get('mean_rows_per_day')} 行/天"
        f"（基准日 {daily.get('latest_day')}，{daily.get('latest_day_rows')} 行；"
        f"已观测 {daily.get('days_observed')} 天）",
        f"  每行约 {estimate_doc.get('bytes_per_row', 0):.0f} B"
    ]
    for key in ("30d", "180d", "365d"):
        item = (estimate_doc.get("projections") or {}).get(key)
        if not item:
            continue
        lines.append(
            f"  {key}：{item['rows']:,} 行 / {item['bytes'] / 1024 / 1024:.1f} MiB")
    verdict = estimate_doc.get("verdict")
    lines.append(
        f"[结论] {verdict} —— {estimate_doc.get('reason')}"
    )
    lines.append("[冻结] 本模块只估算，不含任何 DELETE / VACUUM。")
    return "\n".join(lines)
