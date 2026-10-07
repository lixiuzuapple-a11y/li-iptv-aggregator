"""每日可靠性摘要（TASK-012 §19）。

产物：``/var/lib/li-iptv-aggregator/reliability-summary.json``（原子写）
人类可读输出一律走 :func:`render_human`（短，不刷屏 —— §19 明令「不要打印成
几百行日志」）。

四段结构（§19 逐条落地）
------------------------
``fixed``
    canonical_total / published / stable / degraded / failed / unknown /
    stream_total / stream_healthy / stream_failed / failover_count / recovered_count
``dynamic``
    **按 source** 分别给 fetched / published / precheck_fail / advisory_fail。
    刻意不给「dynamic 总数」—— TASK-011 §36 口径教训：混源计数没有可解释性。
``epg``
    last_success / age_seconds / channels / programmes / coverage
``runtime``
    last publish / service uptime / scheduler last round / last error / stale status

🚨 脱敏红线（§19 + 项目安全红线）
本模块**绝不**输出：完整 stream URL、signed query、Cookie、Authorization、
VPN 信息。频道名 / tvg-id / logo URL 是公开元数据，允许出现；URL 只允许
``scheme://host[:port]/...`` 形式（复用 :func:`liptv.publish.redact_stream_url`）。
所有「来源标识」一律用 ``source_name``（如 ``jsnzkpg-sports``），不用 URL。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib

from . import errors as errors_mod
from . import stability as stability_mod
from .util import dt_to_iso, iso_to_dt, utcnow_iso

SUMMARY_VERSION = 1

#: §19 固定输出路径建议值。调用方可覆盖（测试用临时路径）。
DEFAULT_SUMMARY_PATH = "/var/lib/li-iptv-aggregator/reliability-summary.json"

#: 判定「已发布 fixed」的口径：以最近一次 publish 摘要里的
#: fixed_count 为准，而不是「canonical 存在」。两者不是一回事。
_SKIP_LIST_MAX = 20


def _age_seconds(iso: str | None, now: _dt.datetime) -> float | None:
    if not iso:
        return None
    try:
        return max(0.0, (now - iso_to_dt(iso)).total_seconds())
    except (TypeError, ValueError):
        return None


def _fixed_section(conn, *, selection_kwargs: dict,
                   publish_summary: dict | None, probe_counts: dict) -> dict:
    """Fixed 段。数字全部来自 stability 派生 + 真实 publish 摘要。

    ``selection_kwargs`` 里已经带好了 ``now``（见 :func:`build_summary` 的对齐逻辑），
    这里**不再**单独传 ``now`` —— 两处都传会让 derive_all 收到重复关键字而报错，
    而且两处时间不一致会直接制造 §36 禁止的口径分裂。
    """
    channels = stability_mod.derive_all(conn, **selection_kwargs)
    agg = stability_mod.aggregate(channels)

    # 「已发布」只认 publish 摘要里的 fixed_count（真实写进 M3U 的条数）。
    published_fixed = None
    if publish_summary:
        published_fixed = publish_summary.get("fixed_count")
        if not isinstance(published_fixed, int):
            published_fixed = None

    # stream_healthy / stream_failed：按 §19 口径，从**已发布频道**的候选线统计，
    # 而不是全库。全库会把「已 skip 的频道的历史线路」算进来，虚高。
    published_ids = set()
    if publish_summary and publish_summary.get("entries"):
        for entry in publish_summary["entries"]:
            if isinstance(entry, dict) and entry.get("canonical_channel_id") is not None:
                published_ids.add(int(entry["canonical_channel_id"]))
    if not published_ids:
        # 没有摘要可依（首次运行 / 摘要被清）：退回「有可用线的频道」。
        published_ids = {cid for cid, ch in channels.items() if ch.stream_eligible > 0}

    # stream_healthy = 已发布频道名下「当前可用（eligible）」的 stream 数；
    # stream_failed = 同一批频道里「不可用」的 stream 数。
    # 口径刻意只算已发布频道 —— 全库统计会把「已 skip 频道的历史线路」算进来，虚高。
    stream_healthy = 0
    stream_failed = 0
    for cid in published_ids:
        ch = channels.get(cid)
        if ch is None:
            continue
        stream_healthy += len(ch.candidate_stream_ids)
        stream_failed += (ch.stream_total - ch.stream_eligible)

    skipped = (publish_summary or {}).get("fixed_skipped") or []
    if not isinstance(skipped, list):
        skipped = []
    skipped = [s for s in skipped if isinstance(s, dict)][:_SKIP_LIST_MAX]

    error_dist = probe_counts.get("error_distribution") or {}

    return {
        "canonical_inventory": agg["canonical_total"],
        "published": published_fixed if published_fixed is not None
        else sum(1 for ch in channels.values() if ch.stream_eligible > 0),
        "published_basis": "publish_summary" if published_fixed is not None
        else "eligible_derived",
        "stable": agg["stable"],
        "degraded": agg["degraded"],
        "failed": agg["failed"],
        "unknown": agg["unknown"],
        "stream_total": agg["stream_total"],
        "stream_healthy": stream_healthy,
        "stream_failed": stream_failed,
        "multi_stream": agg["multi_stream"],
        "multi_stream_with_backup": agg["multi_stream_with_backup"],
        "stable_rate": agg["stable_rate"],
        "single_stream": sorted(
            ch.name for ch in channels.values() if ch.stream_total == 1
        ),
        "skipped_now": [{"name": s.get("name")} for s in skipped],
        "error_distribution": error_dist,
        "unstable": [
            {
                "name": ch.name,
                "state": ch.state,
                "success_rate": round(ch.success_rate, 4),
                "stream_total": ch.stream_total,
                "stream_eligible": ch.stream_eligible,
                "top_error_category": ch.top_error_category,
                "reason": ch.reason,
            }
            for ch in sorted(
                channels.values(),
                key=lambda c: (c.state != stability_mod.FAILED,
                               c.success_rate, c.name),
            )
            if ch.state != stability_mod.STABLE
        ][:40],
    }


def _probe_totals(conn, *, now: _dt.datetime, window_days: int) -> dict:
    """窗口内 probe 统计：requested / success / failure / 启动耗时分位 / 错误分布。

    全部走**统一错误口径**（§9）—— 不再把 probe 的 11 类原样倒给报告。
    """
    window_start = dt_to_iso(now - _dt.timedelta(days=window_days))
    rows = conn.execute(
        "SELECT success, error_type, startup_ms FROM probe_result"
        " WHERE checked_at >= ? ORDER BY startup_ms",
        (window_start,),
    ).fetchall()

    total = len(rows)
    success = sum(1 for r in rows if r["success"])
    startups = sorted(r["startup_ms"] for r in rows
                      if r["success"] and r["startup_ms"] is not None)

    def percentile(pct: float) -> int | None:
        # §37：样本足够才算 p95。门槛与 §10 的 min_samples 一致，避免「2 个样本算分位」。
        if len(startups) < stability_mod.DEFAULT_MIN_SAMPLES:
            return None
        idx = min(len(startups) - 1, int(round((pct / 100.0) * (len(startups) - 1))))
        return int(startups[idx])

    def median() -> int | None:
        if not startups:
            return None
        mid = len(startups) // 2
        if len(startups) % 2 == 1:
            return int(startups[mid])
        return int((startups[mid - 1] + startups[mid]) / 2)

    return {
        "window_days": window_days,
        "requested": total,
        "success": success,
        "failure": total - success,
        "median_startup_ms": median(),
        "p95_startup_ms": percentile(95.0),
        "error_distribution": errors_mod.distribution(
            r["error_type"] for r in rows if not r["success"] and r["error_type"]
        ),
    }


def _dynamic_section(publish_summary: dict | None) -> dict:
    """Dynamic 段：**按 source** 分列（§19 + §36）。"""
    if not publish_summary:
        return {"available": False, "sources": []}

    sources = []
    report = publish_summary.get("dynamic_sources") or []
    summary = (publish_summary.get("dynamic_summary") or {}).get("sources") or []
    by_name = {s.get("name"): s for s in summary if isinstance(s, dict)}

    for item in report:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or "?"
        detail = by_name.get(name, {})
        sources.append({
            "name": name,
            "ok": bool(item.get("ok")),
            "status": item.get("status"),
            "error_category": errors_mod.normalize(item.get("error_category")),
            "fetched": detail.get("fetched"),
            "included": detail.get("included"),
            "published": detail.get("published"),
            "cross_source_duplicate": detail.get("cross_source_duplicate"),
            "discarded": detail.get("discarded"),
            "precheck_pass": detail.get("precheck_pass"),
            "precheck_fail": detail.get("precheck_fail"),
            "precheck_unknown": detail.get("precheck_unknown"),
            "authoritative": detail.get("authoritative"),
            "advisory_fail": detail.get("advisory_fail"),
        })

    return {
        "available": True,
        "published_counted": (publish_summary.get("dynamic_summary") or {}).get(
            "published_counted"),
        "selected_sources": (publish_summary.get("dynamic_summary") or {}).get(
            "selected_sources"),
        "failure_policy": (publish_summary.get("dynamic_summary") or {}).get(
            "failure_policy"),
        "fail_closed": bool(publish_summary.get("dynamic_fail_closed")),
        "sources": sources,
    }


def _epg_section(epg_status: dict | None, epg_live: dict | None) -> dict:
    if epg_status is None:
        return {"available": False}
    payload = {
        "available": True,
        "last_success_epoch": epg_status.get("last_success_epoch"),
        "channels": None,
        "programmes": None,
        "coverage": None,
        "last_error": epg_status.get("error"),
        "feeds_ok": None,
    }
    if epg_live:
        payload["channels"] = epg_live.get("channel_count")
        payload["programmes"] = epg_live.get("programme_count")
        payload["coverage"] = epg_live.get("channels_with_future_programme")
    feeds = epg_status.get("feeds") or []
    if feeds:
        payload["feeds_ok"] = sum(1 for f in feeds if isinstance(f, dict) and f.get("ok"))
        payload["feeds_total"] = len(feeds)
    return payload


def _runtime_section(runtime_status: dict | None, health: dict | None,
                     *, now: _dt.datetime) -> dict:
    rt = runtime_status or {}
    round_doc = rt.get("current_round") or {}
    last_publish = rt.get("last_success_publish_at")

    payload = {
        "available": bool(runtime_status),
        "last_success_publish_at": last_publish,
        "seconds_since_last_publish": _age_seconds(last_publish, now),
        "scheduler_round_id": round_doc.get("round_id"),
        "scheduler_last_finished_at": round_doc.get("finished_at"),
        "scheduler_outcome": round_doc.get("outcome"),
        "publish_status": round_doc.get("publish_status"),
        "last_error_category": errors_mod.normalize(
            round_doc.get("error", {}).get("category")
            if isinstance(round_doc.get("error"), dict) else None
        ),
        "lock_held_by_this_process": rt.get("lock", {}).get("acquired")
        if isinstance(rt.get("lock"), dict) else None,
    }
    if health:
        payload["service_uptime_seconds"] = (health.get("service") or {}).get(
            "uptime_seconds")
        payload["stale"] = (health.get("freshness") or {}).get("is_stale")
        payload["playlist_exists"] = (health.get("playlist") or {}).get("exists")
    return payload


def build_summary(
    conn,
    *,
    now: str | _dt.datetime | None = None,
    window_days: int = 7,
    selection_kwargs: dict | None = None,
    publish_summary: dict | None = None,
    runtime_status: dict | None = None,
    health: dict | None = None,
    epg_status: dict | None = None,
    epg_live: dict | None = None,
    scheduler_rounds: list | None = None,
) -> dict:
    """组装完整摘要（纯计算，**不写盘**）。

    所有外部输入都可为 ``None`` —— 缺什么就少报什么，但**不编造**：
    ``dynamic.available=false`` 就是「这轮没有 publish 摘要」，不是「动态健康」。
    """
    reference = (
        iso_to_dt(now) if isinstance(now, str)
        else (now or _dt.datetime.now(_dt.timezone.utc))
    )
    selection = {"window_days": window_days}
    selection.update(selection_kwargs or {})
    # selection 里若带了 now，取它（调用方显式注入的确定性时钟优先），
    # 否则用外层 now。只保留一份，避免 derive_all 收到重复 now。
    # 归一化成 datetime —— 否则字符串会一路传到 `_probe_totals` 的算术里炸掉。
    raw_reference = selection.pop("now", None) or reference
    reference = iso_to_dt(raw_reference) if isinstance(raw_reference, str) else raw_reference
    selection["now"] = reference

    probe_counts = _probe_totals(conn, now=reference, window_days=window_days)

    return {
        "schema": "li-iptv-aggregator/reliability-summary",
        "version": SUMMARY_VERSION,
        "generated_at": utcnow_iso(),
        "window_days": window_days,
        "fixed": _fixed_section(
            conn, selection_kwargs=selection,
            publish_summary=publish_summary, probe_counts=probe_counts,
        ),
        "probe": probe_counts,
        "dynamic": _dynamic_section(publish_summary),
        "epg": _epg_section(epg_status, epg_live),
        "runtime": _runtime_section(runtime_status, health, now=reference),
        "scheduler_rounds_recent": (scheduler_rounds or [])[-10:],
        "redaction": {
            "stream_urls": False,
            "signed_query": False,
            "cookie": False,
            "authorization": False,
            "vpn": False,
        },
        "note": (
            "固定频道的 stable/degraded/failed/unknown 是运行时派生状态"
            "（见 liptv/stability.py），不替代原始 probe_result 历史；"
            "stable_rate 的分母是全部 enabled canonical，UNKNOWN 不计入分子。"
            "上海 probe 只代表 shanghai-cloud，不代表用户家庭网络可播。"
        ),
    }


# ------------------------------------------------------------------ 人类可读

def render_human(summary: dict) -> str:
    """短输出（目标 ≤ 25 行）。§19：「不要打印成几百行日志」。"""
    lines: list[str] = []
    fixed = summary.get("fixed") or {}
    probe = summary.get("probe") or {}
    dyn = summary.get("dynamic") or {}
    epg = summary.get("epg") or {}
    rt = summary.get("runtime") or {}

    lines.append(
        f"[固定] 库存 {fixed.get('canonical_inventory')} / 已发布 {fixed.get('published')}"
        f"  稳定 {fixed.get('stable')} 退化 {fixed.get('degraded')}"
        f" 失败 {fixed.get('failed')} 未知 {fixed.get('unknown')}"
        f"  稳定率 {float(fixed.get('stable_rate') or 0) * 100:.1f}%"
    )
    lines.append(
        f"[线路] 共 {fixed.get('stream_total')}  可用 {fixed.get('stream_healthy')}"
        f"  不可用 {fixed.get('stream_failed')}  多线路频道 {fixed.get('multi_stream')}"
    )
    if probe:
        lines.append(
            f"[测活] 近 {probe.get('window_days')} 天 {probe.get('success')}"
            f"/{probe.get('requested')} 成功"
            f"  中位启动 {probe.get('median_startup_ms')}ms"
            f"  p95 {probe.get('p95_startup_ms') if probe.get('p95_startup_ms') is not None else '样本不足'}"
        )
    dist = probe.get("error_distribution") or {}
    if dist:
        top = sorted(dist.items(), key=lambda kv: -kv[1])[:4]
        lines.append("[错误] " + "  ".join(
            f"{k}={v}" for k, v in top))

    if dyn.get("available"):
        for src in dyn.get("sources") or []:
            lines.append(
                f"[动态] {src.get('name')}: 抓取 {src.get('fetched')}"
                f" 发布 {src.get('published')}"
                f" 预检 fail {src.get('precheck_fail') if src.get('precheck_fail') is not None else '-'}"
                f"{'（仅建议）' if src.get('authoritative') is False else ''}"
            )
    else:
        lines.append("[动态] 本轮无 publish 摘要，无法评估")

    if epg.get("available"):
        age = epg.get("last_success_epoch")
        lines.append(
            f"[节目单] 频道 {epg.get('channels')} 节目 {epg.get('programmes')}"
            f"  末次成功 epoch {age}"
            + (f"  错误 {epg['last_error']}" if epg.get("last_error") else "")
        )
    else:
        lines.append("[节目单] 无状态文件")

    if rt.get("available"):
        lines.append(
            f"[运行] 上次发布 {rt.get('last_success_publish_at')}"
            f"  最近轮次 {rt.get('scheduler_round_id')}"
            f"  结果 {rt.get('scheduler_outcome')}"
            f"  stale={rt.get('stale')}"
        )
    else:
        lines.append("[运行] 无 runtime-status.json")

    unstable = fixed.get("unstable") or []
    if unstable:
        head = "、".join(
            f"{u.get('name')}({stability_mod.state_label(u.get('state'))})"
            for u in unstable[:5]
        )
        lines.append(f"[需关注] {head}" + ("…" if len(unstable) > 5 else ""))

    # §7：每份人读输出都必须带上这句，否则读的人会把「上海云可播」当成
    # 「用户家里可播」—— 这是 TASK-010 §4 拆分 aggregator/playback context 的
    # 最后一公里，摘要层不能丢。
    lines.append("[口径] 测活来自 shanghai-cloud，不代表家庭网络可播；"
                 "UNKNOWN 不计入稳定分子。")

    return "\n".join(lines)


# ------------------------------------------------------------------ 原子写

def write_summary(summary: dict, path) -> dict:
    """原子写摘要。**写失败必须让调用方看到，但绝不影响 live.m3u**（F8）。

    用同目录 ``.tmp`` + :func:`os.replace`（runtime 的原子发布语义）。
    返回 ``{"written": bool, "path": str, "error": str | None, "bytes": int | None}``。
    """
    target = pathlib.Path(path)
    text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    data = text.encode("utf-8")
    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return {"written": False, "path": str(target),
                "error": f"{type(exc).__name__}: {exc}", "bytes": None}
    return {"written": True, "path": str(target), "error": None, "bytes": len(data)}
