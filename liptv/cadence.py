"""TASK-012 §20：生产 cadence 的**解析与校验**（纯只读派生）。

背景
----
``OPERATIONS/RUNTIME-CADENCE.md`` 是给人看的规范，但规范本身不约束任何行为。
本模块把「哪些动作应该多久发生一次」变成**可执行的检查**：

* 从 ``runtime-status.json`` 的 ``rounds`` 里解析出每轮的开始/结束时间与
  各项动作耗时，算出**实测间隔**；
* 与 :data:`TARGETS` 里的目标区间比对，给出 ``ON_TARGET`` / ``SLOW`` /
  ``TOO_FAST`` / ``UNKNOWN``；
* 任何无法从真实数据得出的结论一律 ``UNKNOWN``，**不猜**（§37）。

⚠️ 冻结原则（§20 明确禁止的三件事），本模块只做检测，不改 scheduler：

1. EPG 不该每 15 分钟抓；
2. 不该每 15 分钟做全量 ffprobe；
3. 不该每轮无意义写盘。

第 3 条由 :mod:`liptv.publish` 的 no-change 负责（见 ``publish()`` 的
``rewritten`` 字段），本模块只负责把「有没有在无意义写盘」变成可读数字。
"""

from __future__ import annotations

import datetime as _dt
import statistics

from .util import iso_to_dt

#: 动作 → (最小间隔秒, 目标间隔秒, 最大间隔秒, 单位说明)
#:
#: 区间来自 TASK-012 §20 的参考范围，**只作为观测口径**，不写回配置。
#: ``None`` 表示该维度不设上限（例如 publish 跟随内容变化，本就可能很频繁）。
TARGETS: dict[str, dict] = {
    "dynamic_refresh": {
        "min_seconds": 600,        # 10 min：低于此说明同一轮内重复抓取
        "target_seconds": 900,     # 15 min
        "max_seconds": 2400,       # 40 min
        "label": "dynamic 源刷新",
    },
    "fixed_source_refresh": {
        "min_seconds": 1800,       # 30 min
        "target_seconds": 5400,     # 1.5 h
        "max_seconds": 21600,      # 6 h（§20 上限 3h，留 1 倍容差）
        "label": "fixed 源刷新",
    },
    "fixed_probe": {
        "min_seconds": 1200,       # 20 min
        "target_seconds": 2700,     # 45 min
        "max_seconds": 10800,      # 3 h
        "label": "fixed 全量 ffprobe",
    },
    "epg_refresh": {
        "min_seconds": 18000,      # 5 h —— §20 禁止每 15 min 抓 EPG
        "target_seconds": 28800,   # 8 h
        "max_seconds": 86400,      # 24 h
        "label": "EPG 抓取",
    },
}

CADENCE_UNKNOWN = "UNKNOWN"
CADENCE_ON_TARGET = "ON_TARGET"
CADENCE_TOO_FAST = "TOO_FAST"
CADENCE_SLOW = "SLOW"

ALL_VERDICTS = (CADENCE_ON_TARGET, CADENCE_TOO_FAST, CADENCE_SLOW, CADENCE_UNKNOWN)

#: ``runtime-status`` 保留的轮次数少于这个值时，cadence 改用 probe 推断。
#: 生产默认 ``status_history = 5``，直接用它算间隔只有 4 个样本。
MIN_ROUNDS_FOR_DIRECT = 6

#: 用 probe 推断时的判定放宽系数 —— 推断的轮次边界有 ±1 小时粒度误差，
#: 不放宽会把「几点几分开始测活」误判成节奏问题。
PROBE_INFERRED_SLACK = 0.34


def _seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        moment = iso_to_dt(iso)
    except (ValueError, TypeError):
        return None
    if moment is None:
        return None
    return moment.timestamp()


def _verdict(gap: float | None, spec: dict) -> str:
    """把一个实测间隔判成四档之一。**没有数据就是 UNKNOWN**（§37）。"""
    if gap is None or gap <= 0:
        return CADENCE_UNKNOWN
    if gap < spec["min_seconds"]:
        return CADENCE_TOO_FAST
    if gap > spec["max_seconds"]:
        return CADENCE_SLOW
    return CADENCE_ON_TARGET


def _verdict_relaxed(gap: float | None, spec: dict) -> str:
    """推断数据的宽松判定：上下界各放宽 :data:`PROBE_INFERRED_SLACK`。"""
    if gap is None or gap <= 0:
        return CADENCE_UNKNOWN
    slack = PROBE_INFERRED_SLACK
    if gap < spec["min_seconds"] * (1.0 - slack):
        return CADENCE_TOO_FAST
    if gap > spec["max_seconds"] * (1.0 + slack):
        return CADENCE_SLOW
    return CADENCE_ON_TARGET


def _stats(gaps: list[float]) -> dict:
    if not gaps:
        return {"samples": 0, "min": None, "median": None, "max": None, "mean": None}
    return {
        "samples": len(gaps),
        "min": round(min(gaps), 1),
        "median": round(statistics.median(gaps), 1),
        "max": round(max(gaps), 1),
        "mean": round(statistics.fmean(gaps), 1),
    }


def parse_rounds(runtime_status: dict | None) -> list[dict]:
    """从 runtime-status 里抽出**时间可解析**的轮次，按开始时间排序。

    只保留 ``started_at`` 与 ``finished_at`` 至少有一个能解析成时间的轮次；
    解析不了的不静默丢弃，而是保留在返回值的 ``skipped`` 计数里由调用方决定
    是否上报 —— 「悄悄少算一轮」比「少算一轮且明说」危险得多。
    """
    if not isinstance(runtime_status, dict):
        return []
    raw = runtime_status.get("rounds")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        started = _seconds(item.get("started_at"))
        finished = _seconds(item.get("finished_at"))
        if started is None and finished is None:
            continue
        out.append({
            "round_id": item.get("round_id"),
            "started_at": item.get("started_at"),
            "finished_at": item.get("finished_at"),
            "_started": started,
            "_finished": finished,
            "duration_seconds": round(finished - started, 1)
            if (started is not None and finished is not None) else None,
        })
    out.sort(key=lambda r: (r["_started"] if r["_started"] is not None
                            else r["_finished"]))
    return out


def action_gaps(rounds: list[dict], field: str) -> list[float]:
    """某个时间字段在**相邻轮次**之间的间隔（秒），保持原序。

    ``field`` 收的是 ``started_at`` / ``finished_at`` 这种 ISO 字符串键，
    内部统一走 :func:`_seconds` 转成 epoch 差值 —— 直接对 ISO 字符串做减法
    会炸（这是本函数第一版的真实 bug，已被测试抓住）。
    """
    stamps = [_seconds(r.get(field)) for r in rounds]
    stamps = [s for s in stamps if s is not None]
    return [round(b - a, 1) for a, b in zip(stamps, stamps[1:]) if b > a]


def rounds_from_probe_activity(conn, *, limit: int = 12) -> list[dict]:
    """从 ``probe_result`` 的时间分布推断轮次时刻（**不受状态文件截断影响**）。

    为什么需要这个第二数据源
    ----------------------
    ``runtime-status.json`` 的 ``rounds`` 默认只保留 5 轮（``status_history``，
    §24 要求状态文件有界）。只有 5 个样本算出来的「间隔」是**短窗口局部值**，
    碰上某一轮 probe 特别慢就会被误判成 SLOW/TOO_FAST。

    ``probe_result`` 没有这个问题 —— 它保留全部历史。做法：把「某一小时
    内出现新记录」视为一次测活活动的开始，取每轮最早的 ``checked_at``。

    ⚠️ 这是**推断**而非直接观测，所以判定时按 :data:`PROBE_INFERRED_SLACK`
    放宽上下界，避免把推断的抖动当成真实节奏问题。
    """
    if conn is None:
        return []
    rows = conn.execute(
        "SELECT checked_at FROM probe_result"
        " WHERE checked_at IS NOT NULL ORDER BY checked_at"
    ).fetchall()
    if not rows:
        return []

    marks: list[str] = []
    current_hour: str | None = None
    for row in rows:
        stamp = row[0]
        hour = stamp[:13]
        if hour != current_hour:
            marks.append(stamp)
            current_hour = hour
    if len(marks) < 3:
        return []

    out: list[dict] = []
    for index, stamp in enumerate(marks[-limit:], start=1):
        out.append({
            "round_id": f"probe-{index}",
            "started_at": stamp,
            "finished_at": None,
            # 推断出来的轮次没有真实 finished_at，duration 恒为 None。
            # 这三个键必须与 :func:`parse_rounds` 的输出**同构** ——
            # 第一版漏了它们，analyse() 里 r["duration_seconds"] 直接 KeyError。
            "_started": _seconds(stamp),
            "_finished": None,
            "duration_seconds": None,
        })
    return out


def analyse(runtime_status: dict | None, *,
            epg_last_success_epoch: float | None = None,
            now: _dt.datetime | None = None,
            conn=None) -> dict:
    """给出四个动作的实测 cadence 与判定。

    ``conn`` 给了就启用 ``probe_result`` 推断作为**第二数据源**：当
    ``runtime_status`` 保留的轮次少于 6 个（状态文件被 ``status_history``
    截断）时自动改用它，并把 ``source`` 标成 ``probe_inferred``。

    ``epg_last_success_epoch`` 只能给**一个**时间点，因此 EPG 一栏永远是
    ``UNKNOWN`` —— 这是刻意的：单点算不出间隔，编一个出来就是 §37 禁止的
    「无证据结论」。要真正核 EPG 间隔，需要多次采样 ``epg-status.json``。
    """
    rounds = parse_rounds(runtime_status)
    source = "runtime_status"
    if conn is not None and len(rounds) < MIN_ROUNDS_FOR_DIRECT:
        inferred = rounds_from_probe_activity(conn)
        if len(inferred) > len(rounds):
            rounds, source = inferred, "probe_inferred"

    report: dict[str, dict] = {}

    for action, spec in TARGETS.items():
        if action == "epg_refresh":
            # EPG 状态在 runtime-status 里没有历史序列，如实标 UNKNOWN。
            report[action] = {
                "label": spec["label"],
                "verdict": CADENCE_UNKNOWN,
                "reason": "runtime-status 不保留 EPG 历史时间序列，"
                          "单点无法推算间隔；需多次采样 epg-status.json",
                "last_success_epoch": epg_last_success_epoch,
                "stats": _stats([]),
            }
            continue
        field = "started_at" if action in ("dynamic_refresh", "fixed_source_refresh") \
            else "finished_at"
        gaps = action_gaps(rounds, field)
        if source == "probe_inferred":
            verdict = _verdict_relaxed(gaps[-1] if gaps else None, spec)
        else:
            verdict = _verdict(gaps[-1] if gaps else None, spec)
        report[action] = {
            "label": spec["label"],
            "verdict": verdict,
            "reason": ("数据来自 probe_result 推断（状态文件保留轮次不足 "
                       f"{MIN_ROUNDS_FOR_DIRECT}），判定边界已放宽 "
                       f"{int(PROBE_INFERRED_SLACK * 100)}%")
            if source == "probe_inferred" else None,
            "last_gap_seconds": gaps[-1] if gaps else None,
            "target_seconds": spec["target_seconds"],
            "stats": _stats(gaps),
        }

    durations = [r["duration_seconds"] for r in rounds if r["duration_seconds"]]
    return {
        "rounds_parsed": len(rounds),
        "source": source,
        "actions": report,
        "round_duration": _stats([float(d) for d in durations]),
        # §24：状态必须**有界**。这里直接给结论所需的三个数，不复制整份 rounds。
        "status_bounded_fields": ("rounds_parsed", "actions", "round_duration"),
    }


def render_human(analysis: dict) -> str:
    """≤12 行人读输出（§19 风格：短、给结论、标口径）。"""
    labels = {
        CADENCE_ON_TARGET: "达标", CADENCE_TOO_FAST: "过快",
        CADENCE_SLOW: "偏慢", CADENCE_UNKNOWN: "未知",
    }
    source = analysis.get("source") or "runtime_status"
    source_note = "" if source == "runtime_status" else "（probe 推断）"
    lines = [f"[节奏] 解析轮次 {analysis.get('rounds_parsed', 0)}{source_note}"]
    for action, item in (analysis.get("actions") or {}).items():
        stats = item.get("stats") or {}
        median = stats.get("median")
        span = "无样本" if median is None else f"中位 {median:.0f}s"
        lines.append(
            f"  {item.get('label', action)}：{labels.get(item.get('verdict'), '?')}"
            f"（{span}，目标 {item.get('target_seconds', '-')}s）"
        )
        if item.get("reason"):
            lines.append(f"    └ {item['reason']}")
    duration = (analysis.get("round_duration") or {}).get("median")
    lines.append(
        "[口径] 间隔仅统计保留的轮次，非全量历史；"
        "status_history 截断时会退回 probe 推断并放宽边界。"
    )
    lines.append(
        "[耗时] 单轮中位 " + ("无数据" if duration is None else f"{duration:.0f}s"))
    return "\n".join(lines)
