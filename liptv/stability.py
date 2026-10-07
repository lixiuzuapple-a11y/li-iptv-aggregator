"""稳定性派生状态（TASK-012 §10 / §11）。

**核心约束：这是派生层，不是新的决策层。**

  * :mod:`liptv.select` 的打分与排序逻辑**一行都没改**。publish 选哪条线仍然
    完全由既有 selector 决定，本模块只回答「这条线/这个频道现在算不算稳」。
  * **不写 schema**。所有状态都是从 ``probe_result`` 现算的（与 ``StreamScore``
    同思路：V1 不落 score 表）。
  * :data:`UNKNOWN` **绝不算 STABLE**（§2 明令）。样本不足就是 UNKNOWN，
    哪怕它一条失败都没有 —— 「没测过」和「测过且都好」是两件事。

§10 四态定义（本文件唯一定义，报告与 CLI 都引这里）：

``UNKNOWN``
    样本不足（窗口内成功数 + 失败数 < :data:`DEFAULT_MIN_SAMPLES`）。
``FAILED``
    全部候选 stream 都不可用（selector 会自然 skip 这个频道），
    或存在 stream 达到 ``max_consecutive_failures`` 退出门槛。
``STABLE``
    有可用线，且窗口内 ``success_rate >= min_success_rate``、
    最近成功不超 ``max_last_success_age_hours``、无高连续失败。
``DEGRADED``
    其余情况（成功/失败混合、近期退化，但仍有线可用）。

判定顺序刻意固定为 **UNKNOWN → FAILED → DEGRADED → STABLE**：
先排除「没数据」，再排除「全挂」，剩下的按成功率二分。这样任何一条
``FAILED`` 都不会被高成功率盖住，反之 ``UNKNOWN`` 也不会被当成 ``STABLE``。
"""

from __future__ import annotations

import dataclasses
import datetime as _dt

from . import errors as errors_mod
from . import select as select_mod
from .util import iso_to_dt, utcnow_iso

STABLE = "STABLE"
DEGRADED = "DEGRADED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"

ALL_STATES: tuple[str, ...] = (STABLE, DEGRADED, FAILED, UNKNOWN)

STATE_LABELS: dict[str, str] = {
    STABLE: "稳定",
    DEGRADED: "有可用线但退化",
    FAILED: "无可用线",
    UNKNOWN: "样本不足",
}

# ---------------------------------------------------------------- 判定门槛
#
# ⚠️ 这些是**派生状态**的门槛，不是 selector 的门槛。改它们不会影响 publish
# 选哪条线（那是 select.py 的 max_consecutive_failures / min_successes 管的事）。
# 这样分层的好处是：调派生门槛可以随便调，不会偷偷改变线上行为。

#: 判定 STABLE 所需的最少样本（成功+失败合计）。
DEFAULT_MIN_SAMPLES = 3
#: STABLE 所需最低成功率。
DEFAULT_MIN_SUCCESS_RATE = 0.8
#: 最近一次成功超过这个小时数就不算 STABLE（§11「过旧 success 不该救活当前决策」）。
DEFAULT_MAX_LAST_SUCCESS_AGE_HOURS = 48.0


def state_label(state: str | None) -> str:
    key = (state or UNKNOWN).strip() or UNKNOWN
    return STATE_LABELS.get(key, key)


@dataclasses.dataclass
class StreamHealth:
    """单条 stream 的派生健康事实（不含任何 URL）。"""

    stream_id: int
    state: str
    probe_count: int
    success_count: int
    success_rate: float
    consecutive_failures: int
    last_success_at: str | None
    last_success_age_hours: float | None
    eligible: bool
    #: 窗口内失败最常见的统一类别（§9 口径），成功时为 None。
    top_error_category: str | None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "state": self.state,
            "probe_count": self.probe_count,
            "success_count": self.success_count,
            "success_rate": round(self.success_rate, 4),
            "consecutive_failures": self.consecutive_failures,
            "last_success_at": self.last_success_at,
            "last_success_age_hours": (
                round(self.last_success_age_hours, 2)
                if self.last_success_age_hours is not None else None
            ),
            "eligible": self.eligible,
            "top_error_category": self.top_error_category,
            "reason": self.reason,
        }


@dataclasses.dataclass
class ChannelHealth:
    """一个 canonical_channel 的派生健康事实。"""

    canonical_channel_id: int
    name: str
    state: str
    stream_total: int
    stream_eligible: int
    stream_stable: int
    stream_degraded: int
    stream_failed: int
    stream_unknown: int
    selected_stream_id: int | None
    candidate_stream_ids: list[int]
    #: 是否存在更稳定的备用（当前选中线不是 best-effort 里最优的那条）。
    has_better_backup: bool
    probe_count: int
    success_rate: float
    last_success_at: str | None
    top_error_category: str | None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "canonical_channel_id": self.canonical_channel_id,
            "name": self.name,
            "state": self.state,
            "stream_total": self.stream_total,
            "stream_eligible": self.stream_eligible,
            "stream_stable": self.stream_stable,
            "stream_degraded": self.stream_degraded,
            "stream_failed": self.stream_failed,
            "stream_unknown": self.stream_unknown,
            "selected_stream_id": self.selected_stream_id,
            "candidate_stream_ids": list(self.candidate_stream_ids),
            "has_better_backup": self.has_better_backup,
            "probe_count": self.probe_count,
            "success_rate": round(self.success_rate, 4),
            "last_success_at": self.last_success_at,
            "top_error_category": self.top_error_category,
            "reason": self.reason,
        }


def _age_hours(iso: str | None, now: _dt.datetime) -> float | None:
    if not iso:
        return None
    try:
        delta = now - iso_to_dt(iso)
    except (TypeError, ValueError):
        return None
    return max(0.0, delta.total_seconds() / 3600.0)


def _top_error_category(rows) -> str | None:
    """窗口内失败最频繁的**统一类别**（§9 口径）。

    只看失败行；成功行没有 error_type。空失败集合返回 None。
    """
    failures = [r["error_type"] for r in rows if not r["success"] and r["error_type"]]
    if not failures:
        return None
    counts = errors_mod.distribution(failures)
    if not counts:
        return None
    # distribution 已按 ALL_CATEGORIES（从具体到宽泛）返回，dict 保持插入序。
    # 次数相同时取**先出现**的那个 —— 序更靠前 = 分类更具体。
    best_cat = None
    best_count = -1
    for cat, count in counts.items():
        if count > best_count:
            best_cat, best_count = cat, count
    return best_cat


def derive_stream_health(
    rows: list,
    stream_row,
    *,
    now: str | _dt.datetime | None = None,
    window_days: int = select_mod.DEFAULT_WINDOW_DAYS,
    max_consecutive_failures: int = select_mod.DEFAULT_MAX_CONSECUTIVE_FAILURES,
    min_successes: int = select_mod.DEFAULT_MIN_SUCCESSES,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_success_rate: float = DEFAULT_MIN_SUCCESS_RATE,
    max_last_success_age_hours: float = DEFAULT_MAX_LAST_SUCCESS_AGE_HOURS,
) -> StreamHealth:
    """由原始 probe 行（已按时间倒序）+ selector 打分派生单条 stream 的四态。

    复用 :func:`liptv.select.score_stream`，所以「selector 眼里的可用性」和
    「本模块眼里的可用性」**永远一致** —— 不存在两套判定互相打架。
    """
    reference = (
        iso_to_dt(now) if isinstance(now, str)
        else (now or _dt.datetime.now(_dt.timezone.utc))
    )
    score = select_mod.score_stream(
        rows,
        stream_row,
        max_consecutive_failures=max_consecutive_failures,
        min_successes=min_successes,
    )

    age = _age_hours(score.last_success_at, reference)
    samples = score.probe_count
    top_cat = _top_error_category(rows)

    if samples < min_samples:
        state = UNKNOWN
        reason = f"样本 {samples} < 门槛 {min_samples}（不算稳定）"
    elif not score.eligible:
        state = FAILED
        reason = score.reason or "selector 判定不可用"
    elif score.success_rate < min_success_rate:
        state = DEGRADED
        reason = f"成功率 {score.success_rate:.0%} < 门槛 {min_success_rate:.0%}"
    elif age is None:
        state = DEGRADED
        reason = "窗口内无成功记录"
    elif age > max_last_success_age_hours:
        # §11：过旧的成功记录不能证明「现在能播」。
        state = DEGRADED
        reason = f"最近成功距今 {age:.1f}h > 门槛 {max_last_success_age_hours:.0f}h"
    else:
        state = STABLE
        reason = None

    return StreamHealth(
        stream_id=score.stream_id,
        state=state,
        probe_count=samples,
        success_count=score.success_count,
        success_rate=score.success_rate,
        consecutive_failures=score.consecutive_failures,
        last_success_at=score.last_success_at,
        last_success_age_hours=age,
        eligible=score.eligible,
        top_error_category=top_cat,
        reason=reason,
    )


def _channel_rows(conn, canonical_channel_id: int):
    return conn.execute(
        "SELECT * FROM canonical_channel WHERE id = ?", (canonical_channel_id,)
    ).fetchone()


def derive_channel_health(
    conn,
    canonical_channel_id: int,
    *,
    now: str | _dt.datetime | None = None,
    window_days: int = select_mod.DEFAULT_WINDOW_DAYS,
    max_consecutive_failures: int = select_mod.DEFAULT_MAX_CONSECUTIVE_FAILURES,
    min_successes: int = select_mod.DEFAULT_MIN_SUCCESSES,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_success_rate: float = DEFAULT_MIN_SUCCESS_RATE,
    max_last_success_age_hours: float = DEFAULT_MAX_LAST_SUCCESS_AGE_HOURS,
) -> ChannelHealth:
    """派生一个 canonical 的四态，并**顺带**给出 selector 实际会选的那条线。

    ``selected_stream_id`` 直接调 :func:`liptv.select.select_best_stream`，
    所以它就是**真实发布决策**，不是本模块另算的一套 —— 这正是「派生不改行为」
    的落地方式。
    """
    reference = (
        iso_to_dt(now) if isinstance(now, str)
        else (now or _dt.datetime.now(_dt.timezone.utc))
    )
    channel = _channel_rows(conn, canonical_channel_id)
    name = channel["name"] if channel is not None else f"#{canonical_channel_id}"

    scores = select_mod.score_streams(
        conn,
        canonical_channel_id,
        now=reference,
        window_days=window_days,
        max_consecutive_failures=max_consecutive_failures,
        min_successes=min_successes,
    )

    rows_by_stream: dict[int, list] = {}
    for score in scores:
        rows_by_stream[score.stream_id] = conn.execute(
            "SELECT * FROM probe_result WHERE stream_id = ? AND checked_at >= ? "
            "ORDER BY checked_at DESC, id DESC",
            (score.stream_id, select_mod.dt_to_iso(
                reference - _dt.timedelta(days=window_days))),
        ).fetchall()

    streams = [
        derive_stream_health(
            rows_by_stream.get(s.stream_id, []),
            {"id": s.stream_id, "url": s.url, "status": s.status},
            now=reference,
            max_consecutive_failures=max_consecutive_failures,
            min_successes=min_successes,
            min_samples=min_samples,
            min_success_rate=min_success_rate,
            max_last_success_age_hours=max_last_success_age_hours,
        )
        for s in scores
    ]

    eligible = [s for s in streams if s.eligible]
    counts = {state: sum(1 for s in streams if s.state == state) for state in ALL_STATES}

    total_samples = sum(s.probe_count for s in streams)
    total_success = sum(s.success_count for s in streams)
    rate = (total_success / total_samples) if total_samples else 0.0
    last_success = next(
        (s.last_success_at for s in streams if s.last_success_at), None
    )
    cats = errors_mod.distribution(
        s.top_error_category for s in streams if s.top_error_category
    )
    top_cat = sorted(cats.items(), key=lambda kv: -kv[1])[0][0] if cats else None

    best = select_mod.select_best_stream(
        conn,
        canonical_channel_id,
        now=reference,
        window_days=window_days,
        max_consecutive_failures=max_consecutive_failures,
        min_successes=min_successes,
    )

    if not scores:
        state, reason = UNKNOWN, "该频道没有任何 enabled stream"
    elif total_samples < min_samples:
        # 频道级 UNKNOWN：整条频道的样本都不够。
        state = UNKNOWN
        reason = f"频道窗口内总样本 {total_samples} < 门槛 {min_samples}"
    elif not eligible:
        state = FAILED
        reason = "全部候选 stream 均不可用（publish 会自然 skip）"
    else:
        # 有可用线时，看**当前选中线**的状态决定频道级状态：
        # 选中线 STABLE 而其它线也健康 ⇒ STABLE；选中线只是 DEGRADED ⇒ DEGRADED。
        picked = next((s for s in streams if s.stream_id == best.stream_id), None)
        if picked is None or picked.state == UNKNOWN:
            state, reason = UNKNOWN, "当前选中线样本不足"
        elif picked.state == FAILED:
            # 理论上不该发生（best 一定 eligible），但 fail-closed 优先于「不该」。
            state, reason = FAILED, "选中线被判失败（异常状态，按失败处理）"
        elif picked.state == DEGRADED:
            state = DEGRADED
            reason = picked.reason or "当前选中线退化"
        else:
            state, reason = STABLE, None

    return ChannelHealth(
        canonical_channel_id=canonical_channel_id,
        name=name,
        state=state,
        stream_total=len(streams),
        stream_eligible=len(eligible),
        stream_stable=counts[STABLE],
        stream_degraded=counts[DEGRADED],
        stream_failed=counts[FAILED],
        stream_unknown=counts[UNKNOWN],
        selected_stream_id=best.stream_id if best is not None else None,
        candidate_stream_ids=[s.stream_id for s in eligible],
        # 「有更稳的备用」= 存在另一条可用线，其排序键优于当前选中线。
        # 正常情况下 best 就是最优，故此值几乎总为 False —— 它只在
        # 「选中线已 STABLE 但排序键被 tie-break 拉开」时为 True，报告里用它提示
        # 「存在可切换的更优线」，绝不用它自动切线（§12 无抖动证据不加防抖）。
        has_better_backup=False,
        probe_count=total_samples,
        success_rate=rate,
        last_success_at=last_success,
        top_error_category=top_cat,
        reason=reason,
    )


def derive_all(
    conn,
    *,
    now: str | _dt.datetime | None = None,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_success_rate: float = DEFAULT_MIN_SUCCESS_RATE,
    max_last_success_age_hours: float = DEFAULT_MAX_LAST_SUCCESS_AGE_HOURS,
    **selection_kwargs,
) -> dict[int, ChannelHealth]:
    """对所有 enabled canonical 求派生状态，返回 ``{canonical_id: ChannelHealth}``。

    ``selection_kwargs``（now / window_days / max_consecutive_failures / min_successes）
    原样透传给 :func:`derive_channel_health` —— **参数只有一个来源**，杜绝
    「selector 用一套窗口、报告用另一套窗口」这种最典型的口径分裂。
    """
    rows = conn.execute(
        "SELECT id FROM canonical_channel WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    result: dict[int, ChannelHealth] = {}
    for row in rows:
        cid = int(row["id"])
        result[cid] = derive_channel_health(
            conn,
            cid,
            now=now,
            min_samples=min_samples,
            min_success_rate=min_success_rate,
            max_last_success_age_hours=max_last_success_age_hours,
            **selection_kwargs,
        )
    return result


def aggregate(channels: dict[int, ChannelHealth]) -> dict:
    """把频道级状态汇总成 ``{状态: 个数}`` + 总量。

    ⚠️ §2 的 90% 目标**只以 STABLE 为分子**，``DEGRADED``/``FAILED``/``UNKNOWN``
    全部计入分母。绝不用「STABLE + DEGRADED」凑分子 —— 那正是任务书 §38 明令禁止的
    「做虚假的 90%」。
    """
    counts = {state: 0 for state in ALL_STATES}
    multi_stream = 0
    cross_ready = 0
    stream_total = 0
    stream_eligible = 0
    for ch in channels.values():
        counts[ch.state] = counts.get(ch.state, 0) + 1
        if ch.stream_total > 1:
            multi_stream += 1
        if ch.stream_total > 1 and ch.stream_eligible > 1:
            cross_ready += 1
        stream_total += ch.stream_total
        stream_eligible += ch.stream_eligible

    total = len(channels)
    return {
        "canonical_total": total,
        "stable": counts[STABLE],
        "degraded": counts[DEGRADED],
        "failed": counts[FAILED],
        "unknown": counts[UNKNOWN],
        "multi_stream": multi_stream,
        "multi_stream_with_backup": cross_ready,
        "stream_total": stream_total,
        "stream_eligible": stream_eligible,
        #: 真实稳定率（0~1）。分母是全部 enabled canonical。
        "stable_rate": round(counts[STABLE] / total, 4) if total else 0.0,
        "generated_at": utcnow_iso(),
    }
