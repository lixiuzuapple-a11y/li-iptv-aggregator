"""线路评分与选择（V1 最小规则）。

依据 V1_RUNTIME_FLOW.md §5 / §6 与 PROJECT_LOG.md §6.4：
  * 7 日成功率高优先；
  * 连续失败强惩罚（并设硬阈值，超过则不输出该频道）；
  * 最近成功时间过久降级；
  * 启动速度为次要因素；
  * 清晰度 / 码率只在稳定性相近时参与。

V1 不落 stream_score 表：分数每次运行时从 probe_result 现算。
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import sqlite3

from .util import dt_to_iso, iso_to_dt, utcnow_iso

DEFAULT_WINDOW_DAYS = 7
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
DEFAULT_MIN_SUCCESSES = 1


@dataclasses.dataclass
class StreamScore:
    """单条 stream 在窗口内的评分事实。"""

    stream_id: int
    url: str
    status: str
    probe_count: int
    success_count: int
    failure_count: int
    success_rate: float
    consecutive_failures: int
    last_success_at: str | None
    last_checked_at: str | None
    median_startup_ms: int | None
    resolution_width: int | None
    resolution_height: int | None
    bitrate_kbps: int | None
    eligible: bool
    reason: str | None = None

    @property
    def pixels(self) -> int:
        width = self.resolution_width or 0
        height = self.resolution_height or 0
        return width * height

    def sort_key(self) -> tuple:
        """字典序排序键（越小越优先）。"""
        last_success = iso_to_dt(self.last_success_at).timestamp() if self.last_success_at else 0.0
        startup = self.median_startup_ms if self.median_startup_ms is not None else 10**9
        return (
            -self.success_rate,
            self.consecutive_failures,
            -last_success,
            startup,
            -self.pixels,
            -(self.bitrate_kbps or 0),
            self.stream_id,
        )


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return int((ordered[mid - 1] + ordered[mid]) / 2)


def score_stream(rows: list[sqlite3.Row], stream_row: sqlite3.Row,
                 *, max_consecutive_failures: int, min_successes: int) -> StreamScore:
    """根据已按 checked_at 倒序排列的 probe_result 计算单条 stream 的分数。"""
    probe_count = len(rows)
    success_count = sum(1 for r in rows if r["success"])
    failure_count = probe_count - success_count

    consecutive = 0
    for row in rows:  # rows 已按时间倒序
        if row["success"]:
            break
        consecutive += 1

    last_success_at = next((r["checked_at"] for r in rows if r["success"]), None)
    last_checked_at = rows[0]["checked_at"] if rows else None

    startup_values = [r["startup_ms"] for r in rows if r["success"] and r["startup_ms"] is not None]

    resolution_row = next(
        (r for r in rows if r["success"] and (r["resolution_width"] or r["resolution_height"])), None
    )
    bitrate_row = next((r for r in rows if r["success"] and r["bitrate_kbps"] is not None), None)

    success_rate = (success_count / probe_count) if probe_count else 0.0

    eligible = True
    reason: str | None = None
    if probe_count == 0:
        eligible, reason = False, "窗口内无测活记录"
    elif success_count < min_successes:
        eligible, reason = False, f"窗口内成功次数 {success_count} < 阈值 {min_successes}"
    elif consecutive >= max_consecutive_failures:
        eligible, reason = False, f"连续失败 {consecutive} 次，达到上限 {max_consecutive_failures}"

    return StreamScore(
        stream_id=int(stream_row["id"]),
        url=stream_row["url"],
        status=stream_row["status"],
        probe_count=probe_count,
        success_count=success_count,
        failure_count=failure_count,
        success_rate=success_rate,
        consecutive_failures=consecutive,
        last_success_at=last_success_at,
        last_checked_at=last_checked_at,
        median_startup_ms=_median(startup_values),
        resolution_width=resolution_row["resolution_width"] if resolution_row else None,
        resolution_height=resolution_row["resolution_height"] if resolution_row else None,
        bitrate_kbps=bitrate_row["bitrate_kbps"] if bitrate_row else None,
        eligible=eligible,
        reason=reason,
    )


def score_streams(
    conn: sqlite3.Connection,
    canonical_channel_id: int,
    *,
    now: str | _dt.datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    min_successes: int = DEFAULT_MIN_SUCCESSES,
) -> list[StreamScore]:
    """给某个 canonical_channel 下所有 enabled stream 打分，按优先级降序返回。

    Review-01：status='stale'（已失去全部来源）的 stream 不参与选线。
    """
    reference = iso_to_dt(now) if isinstance(now, str) else (now or _dt.datetime.now(_dt.timezone.utc))
    window_start = dt_to_iso(reference - _dt.timedelta(days=window_days))

    streams = conn.execute(
        "SELECT * FROM stream WHERE canonical_channel_id = ? AND enabled = 1 "
        "AND status <> 'stale' ORDER BY id",
        (canonical_channel_id,),
    ).fetchall()

    scores: list[StreamScore] = []
    for stream_row in streams:
        rows = conn.execute(
            "SELECT * FROM probe_result WHERE stream_id = ? AND checked_at >= ? "
            "ORDER BY checked_at DESC, id DESC",
            (stream_row["id"], window_start),
        ).fetchall()
        scores.append(
            score_stream(
                list(rows),
                stream_row,
                max_consecutive_failures=max_consecutive_failures,
                min_successes=min_successes,
            )
        )

    scores.sort(key=lambda s: (not s.eligible, s.sort_key()))
    return scores


def select_best_stream(
    conn: sqlite3.Connection,
    canonical_channel_id: int,
    **kwargs,
) -> StreamScore | None:
    """返回最佳可用线路；没有达到最低阈值的线路时返回 None。

    kwargs 透传给 score_streams（now / window_days / max_consecutive_failures / min_successes）。
    """
    scores = score_streams(conn, canonical_channel_id, **kwargs)
    for score in scores:
        if score.eligible:
            return score
    return None


def order_canonical_channels(
    rows: list[sqlite3.Row], group_order: list[str]
) -> list[sqlite3.Row]:
    """按「分类顺序 → priority → 名称」排列频道。

    priority 数值越小越靠前（默认 100）。
    不在配置分类列表里的分类排在已知分类之后，按名称兜底。
    """
    index = {name: i for i, name in enumerate(group_order)}
    unknown = len(group_order)

    def key(row: sqlite3.Row) -> tuple:
        category = row["category"] or ""
        return (index.get(category, unknown), row["priority"], row["name"])

    return sorted(rows, key=key)


def select_playlist(conn: sqlite3.Connection, *, group_order: list[str], **kwargs) -> dict:
    """为所有 enabled canonical_channel 选出播放列表。

    返回 {"entries": [...], "skipped": [...]}，skipped 记录无可用线路的频道。
    """
    rows = conn.execute(
        "SELECT * FROM canonical_channel WHERE enabled = 1"
    ).fetchall()
    ordered = order_canonical_channels(list(rows), group_order)

    entries: list[dict] = []
    skipped: list[dict] = []
    for row in ordered:
        best = select_best_stream(conn, int(row["id"]), **kwargs)
        if best is None:
            skipped.append({"canonical_channel_id": int(row["id"]), "name": row["name"]})
            continue
        entries.append(
            {
                "canonical_channel_id": int(row["id"]),
                "name": row["name"],
                "category": row["category"],
                "preferred_tvg_id": row["preferred_tvg_id"],
                "preferred_logo": row["preferred_logo"],
                "stream_id": best.stream_id,
                "url": best.url,
                "score": best,
            }
        )
    return {"entries": entries, "skipped": skipped, "generated_at": utcnow_iso()}
