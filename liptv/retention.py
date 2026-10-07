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

#: 速率基准取最近几个完整自然日。太短会被单日抖动带偏，太长会把
#: 冷启动日算进来（生产实测：10-05 冷启动半天只有 762 行，
#: 混进均值会把 11262 的稳态速率拉低近一半）。
BASIS_DAYS = 7


def daily_rows(conn: sqlite3.Connection, *, now: _dt.datetime | None = None,
               basis_days: int = BASIS_DAYS) -> dict:
    """真实日增行数。

    🚨 口径设计的关键判断（生产实测踩出来的）
    ------------------------------------------
    **预测未来增长必须用「最近一个完整自然日」的速率，不是历史平均。**

    生产实测：库里只有两天历史 —— 10-05（服务当天 14:25 才启动，
    半天数据 762 行）与 10-06（完整一天 11262 行）。任何形式的平均都会把
    冷启动日混进来：两天平均 = 6012，比真实稳态 11262 **低了 47%**；
    全部历史平均 = 3081，低了 73%。

    后果不是小数点误差：按 6012 算一年 122 MiB（低于阈值 ⇒ 判定
    「不需要 retention」），按 11262 算一年 1.6 GiB（**远超**阈值 ⇒
    应该设计 retention）。同一份数据，两个**相反**的运维结论。

    所以本函数给三个数，用途各不相同：
    ``latest_day_rows``
        **基准**。最近一个完整自然日的行数 —— 这就是当前稳态速率。
    ``mean_rows_per_day``
        参考。``basis_days`` 窗口内的均值，只用于看趋势是否平稳。
    ``max_rows_per_day``
        上界。窗口内最高的一天，用作保守估算。
    """
    reference = now or _dt.datetime.now(_dt.timezone.utc)
    rows = conn.execute(
        "SELECT substr(checked_at, 1, 10) AS d, count(*) AS n"
        " FROM probe_result WHERE checked_at IS NOT NULL"
        " GROUP BY d ORDER BY d DESC"
    ).fetchall()
    if not rows:
        return {"days_observed": 0, "latest_day": None, "latest_day_rows": None,
                "mean_rows_per_day": None, "max_rows_per_day": None,
                "active_days": 0, "basis_days": basis_days}

    counts = {r["d"]: r["n"] for r in rows}
    # 只取「已完整过去」的自然日：今天还在累积中，算进去会低估速率。
    today = reference.date().isoformat()
    complete = [(d, n) for d, n in counts.items() if d < today]
    # 按日期倒序（SQL 已排好），只留最近 basis_days 天。
    basis = complete[:max(1, int(basis_days))] if complete else \
        [(d, n) for d, n in counts.items()]

    latest_day, latest_rows = basis[0]
    return {
        "days_observed": len(counts),
        "active_days": len(basis),
        "basis_days": basis_days,
        "latest_day": latest_day,
        "latest_day_rows": latest_rows,
        "mean_rows_per_day": round(sum(n for _, n in basis) / len(basis), 1),
        "max_rows_per_day": max(n for _, n in basis),
        "rate_basis": "latest_complete_day",
    }


def estimate(conn: sqlite3.Connection, *,
             now: _dt.datetime | None = None,
             bytes_per_row: float | None = None,
             threshold_bytes: int = YEAR_BYTES_THRESHOLD,
             rate_source: str = "latest_complete_day") -> dict:
    """外推 30/180/365 天的行数与体积，并给出结论档位。

    ``rate_source``
        ``"latest_complete_day"``（默认）
            用最近一个完整自然日的行数。**这是唯一正确的预测基准** ——
            历史平均会被冷启动日污染，进而给出相反的运维结论（见
            :func:`daily_rows` 的 docstring）。
        ``"mean"``
            用窗口内均值。**仅供对照**，不要用它做运维决策。

    ``bytes_per_row`` 不传则**实测**：用 ``dbstat`` 的 ``sum(pgsize)``。
    """
    stats = daily_rows(conn, now=now)
    rate = (stats.get("latest_day_rows") if rate_source == "latest_complete_day"
            else stats.get("mean_rows_per_day"))

    if not rate or stats.get("active_days", 0) < 1:
        return {
            "available": False,
            "verdict": RETENTION_UNKNOWN,
            "reason": "probe_result 无完整自然日样本，不外推（§37）",
            "rate_source": rate_source,
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
            f"按实测 {rate:.0f} 行/天（基准：{rate_source}）、"
            f"{per_row:.0f} B/行外推，1 年约 {year_bytes / 1024 / 1024:.1f} MiB；"
            + ("超过阈值，建议设计 retention（本模块不执行任何删除）"
               if verdict == RETENTION_WORTH
               else "低于阈值，结论是不需要 retention")
        ),
        "rate_source": rate_source,
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
        conn.execute("SELECT sum(pgsize) FROM dbstat "
                     "WHERE name = 'probe_result'").fetchone()
    except sqlite3.Error:
        return "whole_db/rows（dbstat 不可用，含固定开销，偏保守）"
    return "dbstat:sum(pgsize of probe_result) / 行数"


def _measure_bytes_per_row(conn: sqlite3.Connection) -> float:
    """实测平均每行字节。

    优先用 SQLite 的 ``dbstat`` 虚表取 ``probe_result`` 的 ``sum(pgsize)`` ——
    整库大小除以行数会被**固定开销**放大（小库尤其离谱：实测一个只有几千行的
    测试库会算出 10KB/行，外推一年 37 MiB，纯属幻觉）。

    ⚠️ 必须用 ``sum(pgsize)`` 而不是 ``count(pageno) * page_size``：
    后者把索引页、overflow 页、freelist 页都按 page_size 计，会**高估**。
    实测差异可达数倍（生产实测 409.6 B/行 vs 真实值差 7 倍，
    差点让「不需要 retention」变成「需要 retention」的误判）。
    """
    total = conn.execute("SELECT count(*) FROM probe_result").fetchone()[0]
    if not total:
        return 64.0
    try:
        row = conn.execute(
            "SELECT sum(pgsize) FROM dbstat WHERE name = 'probe_result'"
        ).fetchone()
        used = row[0] if row and row[0] else 0
        if used:
            return max(1.0, used / total)
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
    source = estimate_doc.get("rate_source") or "?"
    lines = [
        f"[增长] 基准 {source} = {daily.get('latest_day_rows')} 行/天"
        f"（{daily.get('latest_day')}）；近 {daily.get('active_days')} 天"
        f"均值 {daily.get('mean_rows_per_day')}、最高 {daily.get('max_rows_per_day')}",
        f"  每行约 {estimate_doc.get('bytes_per_row', 0):.0f} B",
    ]
    for key in ("30d", "180d", "365d"):
        item = (estimate_doc.get("projections") or {}).get(key)
        if not item:
            continue
        lines.append(
            f"  {key}：{item['rows']:,} 行 / {item['bytes'] / 1024 / 1024:.1f} MiB")
    lines.append(f"[结论] {estimate_doc.get('verdict')} —— {estimate_doc.get('reason')}")
    lines.append("[冻结] 本模块只估算，不含任何 DELETE / VACUUM。")
    return "\n".join(lines)
