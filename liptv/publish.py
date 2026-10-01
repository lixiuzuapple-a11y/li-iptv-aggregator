"""统一播放列表组合与安全本地发布（TASK-003）。

把两类来源合成**一个**本地 M3U 文件，并在写盘前完成全部校验：

* **固定频道** —— 复用 TASK-001 的 :func:`select.select_playlist`：基于历史
  ``probe_result`` 选线、每个 canonical 最多一条线路、**不绕过最低成功阈值**
  （从未探测过的 stream 不会被发布）；
* **动态赛事** —— 复用 TASK-002 的 :func:`ingest.preview_dynamic_source`
  （HTTP 限额 + M3U 结构校验）；只取**本次**实际成功获取的条目，绝不落库、
  绝不从旧 ``live.m3u`` 或历史临时快照回拼动态线路。
  纳入规则默认走**排除法**（剔除宣传与回放，其余联赛分组保留；回放按注释分区或分组名识别），
  只在显式配置了 ``include_groups`` 时才切换成严格白名单。

安全与事务（对应 TASK-003 §3）：

1. 写盘前完成固定选线 / 动态抓取 / 组合 / 完整校验；
2. 组合或校验失败 ⇒ 整次拒绝，当前与上一版文件**一字节不改**；
3. 动态源失败 ⇒ **fail-closed**：本轮动态条目**全部舍弃**（包括其他成功的来源），
   默认降级只发固定频道；``require_dynamic=True`` 时整次拒绝。
   状态 / 摘要 / 实际文件内容三者必须一致（QA-003B）；
4. 全空结果默认不覆盖已有列表（``DEGRADED_NO_PUBLISH``）；
5. 生成物本身会**反向解析校验**（头、条目数、URL、分组、无悬挂 #EXTINF）。

**本轮是单次静态文件**：在下一轮命令执行前无法自动过期，因此不能把「生成了一个文件」
描述成「24 小时可用的稳定订阅」。这一点会写进返回值与发布摘要。
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import time
from urllib.parse import urlsplit

from . import fetch as fetch_mod
from . import ingest as ingest_mod
from . import m3u as m3u_mod
from . import select as select_mod
from .util import sha256_hex, utcnow_iso

# ------------------------------------------------------------- 状态与错误码

STATUS_OK = "OK"
STATUS_DRY_RUN = "DRY_RUN"
STATUS_DEGRADED_FIXED_ONLY = "DEGRADED_FIXED_ONLY"
STATUS_DEGRADED_NO_PUBLISH = "DEGRADED_NO_PUBLISH"
STATUS_REJECTED_DYNAMIC_REQUIRED = "REJECTED_DYNAMIC_REQUIRED"
STATUS_REJECTED_EMPTY = "REJECTED_EMPTY"
STATUS_REJECTED_VALIDATION = "REJECTED_VALIDATION"
STATUS_REJECTED_IO = "REJECTED_IO"

#: 退出码分类：0 = 已发布（含降级发布）；2 = 未发布但输入本身没问题；1 = 失败/拒绝
EXIT_OK = 0
EXIT_NOT_PUBLISHED = 2
EXIT_REJECTED = 1

STATUS_EXIT = {
    STATUS_OK: EXIT_OK,
    STATUS_DRY_RUN: EXIT_OK,
    STATUS_DEGRADED_FIXED_ONLY: EXIT_OK,
    STATUS_DEGRADED_NO_PUBLISH: EXIT_NOT_PUBLISHED,
    STATUS_REJECTED_DYNAMIC_REQUIRED: EXIT_REJECTED,
    STATUS_REJECTED_EMPTY: EXIT_REJECTED,
    STATUS_REJECTED_VALIDATION: EXIT_REJECTED,
    STATUS_REJECTED_IO: EXIT_REJECTED,
}

PUBLISH_NOTE = (
    "本轮产物是**单次静态文件**：下一轮发布命令执行前不会自动过期，"
    "也不是 24 小时可用的稳定订阅。"
)

DEFAULT_DYNAMIC_GROUP_TITLE = "体育赛事（实时）"

# 动态赛事纳入规则的安全默认值（全部可在 config.toml 覆盖）
#
# 策略取向（TASK-003 QA-003A 修正）：**默认按「排除法」纳入**，而不是「白名单法」。
# 原因是真实上游（JSNZKPG）的 ``group-title`` 是**联赛名**（WNBA / 欧俱杯 / 国际友谊 …），
# 而「正在直播 / 赛事回放」是 M3U 内部的 **注释分区标记**，不是条目的 group-title。
# 旧默认把 ``正在直播`` 当白名单 → 真实源 0/88 全被排除，等于目标功能失效。
DEFAULT_DYNAMIC_FILTERS: dict[str, object] = {
    # 【留空 = 不启用白名单】只按下面的排除规则过滤，其余分组（即各联赛）一律保留。
    # 填了分组名会切换成**严格白名单模式**：只纳入列出的分组名，其余全部排除
    # （给「只想收某几个组」的用户；同样不需要硬编码任何联赛名单）。
    "include_groups": [],
    # 精确匹配的非赛事分组（宣传类）
    "exclude_groups": ["宣传", "公告", "推广", "广告"],
    # 分组名含这些关键词同样排除（如 ✈️TG频道 之类推广入口）；大小写不敏感
    "exclude_group_keywords": ["✈️", "TG频道", "TG 频道", "下载", "app"],
    # 回放：命中【注释分区名】或【分组名】都算回放，默认整类关闭
    "replay_sections": ["赛事回放", "回放", "录像", "重播"],
    "replay_groups": ["赛事回放", "回放", "录像", "重播"],
    "include_replay": False,
}

# 排除理由（写进报告，保证「过滤计数与理由」可解释）
REASON_EXCLUDED_GROUP = "excluded_group"
REASON_EXCLUDED_KEYWORD = "excluded_keyword"
REASON_NOT_IN_INCLUDE_LIST = "group_not_in_include_list"
REASON_REPLAY_SECTION = "replay_section_disabled"
REASON_REPLAY_DISABLED = "replay_disabled"
REASON_DUPLICATE = "duplicate_byte_identical"

REASON_LABELS = {
    REASON_EXCLUDED_GROUP: "命中排除分组（宣传/公告/推广/广告）",
    REASON_EXCLUDED_KEYWORD: "分组名命中排除关键词（推广入口）",
    REASON_NOT_IN_INCLUDE_LIST: "不在纳入分组白名单内（已显式配置 include_groups）",
    REASON_REPLAY_SECTION: "位于回放注释分区（include_replay=false）",
    REASON_REPLAY_DISABLED: "回放分组默认关闭（include_replay=false）",
    REASON_DUPLICATE: "同源内与前面条目完全重复（URL+显示名+原始分组字节相同）",
}

# 输出摘要里最多保留多少条排除样本（够排查即可，不写全表）
MAX_SAMPLES = 8

_URL_IN_TEXT_RE = re.compile(r"https?://\S+", re.I)


# ------------------------------------------------------------------ 小工具

def redact_text(text: str | None) -> str:
    """把自由文本里的 URL 一律替换掉（名称字段也可能夹带链接，不能直接进日志）。"""
    return _URL_IN_TEXT_RE.sub("<url>", text or "")


def redact_url_light(url: str | None) -> str:
    """比 :func:`ingest.redact_url` 更保守的脱敏：只留 scheme://host，path 也丢掉。

    动态条目的 playpath 里可能夹带签名材料，发布摘要里连 path 都不需要。
    """
    parts = urlsplit(url or "")
    if not parts.scheme or not parts.hostname:
        return "<unparsable-url>"
    return f"{parts.scheme}://{parts.hostname}"


def normalize_dynamic_filters(raw: dict | None) -> dict:
    """把配置里的 [publish.dynamic] 合并到默认值上，并做类型兜底。"""
    merged = dict(DEFAULT_DYNAMIC_FILTERS)
    list_keys = (
        "include_groups",
        "exclude_groups",
        "exclude_group_keywords",
        "replay_sections",
        "replay_groups",
    )
    for key, value in (raw or {}).items():
        if key in list_keys:
            if isinstance(value, (list, tuple)):
                merged[key] = [str(item) for item in value if str(item).strip()]
        elif key == "include_replay":
            merged[key] = bool(value)
    return merged


# -------------------------------------------------------------- 动态条目判定

@dataclasses.dataclass
class DynamicDecision:
    """一条动态条目的判定结果。"""

    index: int
    name: str
    url: str
    group: str | None
    tags: list[str]
    included: bool
    reason: str | None = None
    duplicate_of: int | None = None
    section: str | None = None


def classify_dynamic_entry(
    group: str | None, *, filters: dict, section: str | None = None
) -> tuple[bool, str | None]:
    """判定一条动态条目是否纳入；返回 (是否纳入, 排除理由)。

    规则刻意保持「简单、配置化、可解释」，顺序为：

    1. 分组名命中 ``exclude_groups``（精确） → 排除；
    2. 分组名命中 ``exclude_group_keywords``（子串，大小写不敏感） → 排除；
    3. 回放：分组名命中 ``replay_groups`` **或** ``section`` 命中 ``replay_sections``，
       且 ``include_replay=false`` → 排除（分组名优先取更具体的理由）；
    4. ``include_groups`` **非空**时才是严格白名单，不在表内的一律排除；
       **留空则不启用白名单**，前面的排除规则没拦住的（= 各联赛分组）一律纳入。

    **不写复杂赛事识别、不硬编码任何联赛名单**：默认只负责「剔除宣传与回放」。
    ``section`` 来自 :mod:`liptv.m3u` 解析出的注释分区（如 ``正在直播``/``赛事回放``），
    因此即使上游把回放放进分区而不写 group-title，也能被识别。
    """
    value = (group or "").strip()
    section_value = (section or "").strip()
    lowered = value.lower()

    if value and value in set(filters["exclude_groups"]):
        return False, REASON_EXCLUDED_GROUP
    for keyword in filters["exclude_group_keywords"]:
        if keyword and keyword.lower() in lowered:
            return False, REASON_EXCLUDED_KEYWORD
    if not filters["include_replay"]:
        # 先看条目自己的分组名（更具体），再看它落在哪个注释分区（兜底）。
        # 两者都判为回放，只是理由不同，便于排查上游到底是怎么标的。
        if value and value in set(filters["replay_groups"]):
            return False, REASON_REPLAY_DISABLED
        if section_value and section_value in set(filters["replay_sections"]):
            return False, REASON_REPLAY_SECTION
    include_groups = set(filters["include_groups"])
    if include_groups and value not in include_groups:
        return False, REASON_NOT_IN_INCLUDE_LIST
    return True, None


def decide_dynamic_entries(entries, *, filters: dict) -> tuple[list[DynamicDecision], dict[str, int]]:
    """逐条判定 + **同源内**字节级去重。

    去重作用域严格限制在「同一个动态来源内」，键是 ``(URL, 显示名, 原始分组)``：
    * 不跨来源去重（不同来源的同名赛事不得互相吞掉）；
    * 不把 [解说] 与 [原声] 合并（它们的 URL 与显示名都不同）；
    * 更不跨固定频道与动态赛事去重。

    判定时会带上条目所属的**注释分区**（``entry.section``），因此上游用
    ``# ===== 赛事回放 =====`` 分区而非分组名标回放时同样能正确排除。
    """
    decisions: list[DynamicDecision] = []
    seen: dict[tuple[str, str, str], int] = {}
    counts: dict[str, int] = {}

    for index, entry in enumerate(entries, start=1):
        section = getattr(entry, "section", None)
        key = (entry.url, entry.name, entry.group_title or "")
        if key in seen:
            decisions.append(
                DynamicDecision(
                    index=index,
                    name=entry.name,
                    url=entry.url,
                    group=entry.group_title,
                    tags=ingest_mod.extract_tags(entry.name),
                    included=False,
                    reason=REASON_DUPLICATE,
                    duplicate_of=seen[key],
                    section=section,
                )
            )
            counts[REASON_DUPLICATE] = counts.get(REASON_DUPLICATE, 0) + 1
            continue
        seen[key] = index

        included, reason = classify_dynamic_entry(
            entry.group_title, filters=filters, section=section
        )
        decisions.append(
            DynamicDecision(
                index=index,
                name=entry.name,
                url=entry.url,
                group=entry.group_title,
                tags=ingest_mod.extract_tags(entry.name),
                included=included,
                reason=reason,
                section=section,
            )
        )
        if not included and reason:
            counts[reason] = counts.get(reason, 0) + 1

    return decisions, counts


# -------------------------------------------------------------- 组合与校验

@dataclasses.dataclass
class Composition:
    """写盘前的完整组合结果（纯内存，不含任何文件操作）。"""

    channels: list[m3u_mod.M3UChannel]
    fixed_count: int
    dynamic_count: int
    skipped_fixed: list[dict]
    dynamic_report: list[dict]
    dynamic_excluded_reasons: dict[str, int]
    warnings: list[str]
    composition_errors: list[str]  # 组合层面的校验错误
    #: 是否因「任一动态来源失败」而触发 fail-closed（此时 channels 只含固定频道）。
    dynamic_fail_closed: bool = False
    #: fail-closed 时被**舍弃**的本轮动态条目数（已计入 dynamic_report 的 discarded）。
    dynamic_discarded: int = 0

    @property
    def total(self) -> int:
        return len(self.channels)


def _fixed_channels(entries) -> list[m3u_mod.M3UChannel]:
    return [
        m3u_mod.M3UChannel(
            key=("fixed", int(entry["canonical_channel_id"])),
            name=entry["name"],
            url=entry["url"],
            tvg_id=entry["preferred_tvg_id"],
            tvg_name=entry["name"],
            tvg_logo=entry["preferred_logo"],
            group_title=entry["category"],
        )
        for entry in entries
    ]


def _dynamic_channels(included: list[dict], *, group_title: str) -> list[m3u_mod.M3UChannel]:
    channels = []
    for item in included:
        channels.append(
            m3u_mod.M3UChannel(
                key=("dynamic", item["source_name"], item["index"]),
                name=item["name"],
                url=item["url"],
                tvg_id=None,
                tvg_name=item["name"],
                tvg_logo=None,
                group_title=group_title,
            )
        )
    return channels


def validate_composition(channels: list[m3u_mod.M3UChannel]) -> list[str]:
    """写盘前的组合校验：URL 非空、固定 canonical 不重复、分类非空、至少一条条目。"""
    errors: list[str] = []
    if not channels:
        return ["组合结果为空：没有任何固定频道或动态条目可发布"]

    seen_fixed: set[object] = set()
    for position, channel in enumerate(channels, start=1):
        if not (channel.url or "").strip():
            errors.append(f"第 {position} 条（{redact_text(channel.name)}）的 URL 为空")
        if not (channel.name or "").strip():
            errors.append(f"第 {position} 条的显示名为空")
        if channel.key is not None and channel.key[0] == "fixed":
            if channel.key in seen_fixed:
                errors.append(f"固定频道重复出现：canonical key={channel.key[1]}")
            seen_fixed.add(channel.key)
    return errors


def reverse_validate(text: str, channels: list[m3u_mod.M3UChannel]) -> list[str]:
    """把生成出来的文本**再解析一次**，确认生成物本身合法且与预期一致。"""
    errors: list[str] = []
    parsed = m3u_mod.parse_text(text)

    if not parsed.has_header:
        errors.append("生成文本缺少 #EXTM3U 头")
    if parsed.skipped.get("extinf_without_url"):
        errors.append(f"生成文本有 {parsed.skipped['extinf_without_url']} 条 #EXTINF 没有配套 URL")
    if parsed.skipped.get("url_without_extinf"):
        errors.append(f"生成文本有 {parsed.skipped['url_without_extinf']} 条 URL 没有配套 #EXTINF")
    if parsed.entry_count != len(channels):
        errors.append(f"反向解析得到 {parsed.entry_count} 条，预期 {len(channels)} 条")

    for position, (entry, channel) in enumerate(zip(parsed.entries, channels), start=1):
        if entry.url != (channel.url or "").strip():
            errors.append(f"第 {position} 条 URL 与预期不一致")
            break
        if entry.name != m3u_mod.normalize_channel_name(channel.name):
            errors.append(f"第 {position} 条显示名与预期不一致")
            break
        expected_group = m3u_mod.normalize_attr_value(channel.group_title)
        if (entry.group_title or None) != (expected_group or None):
            errors.append(f"第 {position} 条分组与预期不一致")
            break

    return errors


# --------------------------------------------------------------- 动态抓取层

def fetch_dynamic_sources(
    sources, *, limits, stamp: str, opener=None
) -> list[dict]:
    """逐个实时获取动态来源，只返回内存内结果。

    返回项的 ``entries`` 是**解析后的原始条目对象**（含真实 URL），
    ``raw_text`` 永不进入任何输出/摘要/日志 —— 调用方用完即弃。
    """
    results: list[dict] = []
    for source in sources:
        started = time.perf_counter()
        preview = ingest_mod.preview_dynamic_source(
            source, limits=limits, now=stamp, include_raw=True, opener=opener
        )
        raw_text = preview.pop("_raw_text", None)

        entries = []
        if preview["ok"] and raw_text:
            entries = m3u_mod.parse_text(raw_text).entries

        results.append(
            {
                "source_id": preview["source_id"],
                "source_name": preview["source_name"],
                "url_redacted": preview["url_redacted"],
                "ok": bool(preview["ok"]),
                "status": preview["status"],
                "error": preview["error"],
                "error_category": preview["error_category"],
                "fetched_entries": len(entries),
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "entries": entries,
                "raw_text": raw_text,
            }
        )
    return results


# ------------------------------------------------------------------ 组合入口

def build_composition(
    conn,
    *,
    group_order: list[str],
    selection_kwargs: dict | None = None,
    include_dynamic: bool = False,
    dynamic_sources=(),
    dynamic_filters: dict | None = None,
    dynamic_group_title: str = DEFAULT_DYNAMIC_GROUP_TITLE,
    limits: fetch_mod.FetchLimits | None = None,
    stamp: str | None = None,
    opener=None,
) -> Composition:
    """组装发布内容（**不写任何文件**）。

    固定选线永远执行；动态抓取只在 ``include_dynamic=True`` 时发生 ——
    因此默认路径完全不碰公网。
    """
    stamp = stamp or utcnow_iso()
    filters = normalize_dynamic_filters(dynamic_filters)

    selection = select_mod.select_playlist(conn, group_order=group_order, **(selection_kwargs or {}))
    fixed_channels = _fixed_channels(selection["entries"])

    dynamic_report: list[dict] = []
    dynamic_excluded: dict[str, int] = {}
    dynamic_included: list[dict] = []
    warnings: list[str] = []

    if include_dynamic and dynamic_sources:
        for result in fetch_dynamic_sources(
            dynamic_sources, limits=limits or fetch_mod.FetchLimits(), stamp=stamp, opener=opener
        ):
            decisions, counts = decide_dynamic_entries(result["entries"], filters=filters)
            for reason, number in counts.items():
                dynamic_excluded[reason] = dynamic_excluded.get(reason, 0) + number

            included_here = [d for d in decisions if d.included]
            for decision in included_here:
                dynamic_included.append(
                    {
                        "source_name": result["source_name"],
                        "index": decision.index,
                        "name": decision.name,
                        "url": decision.url,
                        "group": decision.group,
                        "tags": decision.tags,
                    }
                )

            excluded_samples = [
                {
                    "index": d.index,
                    "name": redact_text(d.name),
                    "group": d.group,
                    "section": d.section,
                    "reason": d.reason,
                    "why": REASON_LABELS.get(d.reason or "", d.reason),
                    "url": redact_url_light(d.url),
                    "duplicate_of": d.duplicate_of,
                }
                for d in decisions
                if not d.included
            ][:MAX_SAMPLES]

            sections_seen: dict[str, int] = {}
            for decision in decisions:
                key = decision.section or "<无分区>"
                sections_seen[key] = sections_seen.get(key, 0) + 1

            dynamic_report.append(
                {
                    "source_name": result["source_name"],
                    "source_id": result["source_id"],
                    "url_redacted": result["url_redacted"],
                    "ok": result["ok"],
                    "status": result["status"],
                    "error": result["error"],
                    "error_category": result["error_category"],
                    "duration_ms": result["duration_ms"],
                    "fetched_entries": result["fetched_entries"],
                    "included": len(included_here),
                    "discarded": 0,
                    "sections_seen": dict(sorted(sections_seen.items())),
                    "excluded_by_reason": {
                        REASON_LABELS.get(k, k): v for k, v in sorted(counts.items())
                    },
                    "excluded_samples": excluded_samples,
                }
            )
            if not result["ok"]:
                warnings.append(
                    f"动态来源 {result['source_name']} 获取失败 [{result['status']}]："
                    f"{redact_text(result['error'])}"
                )
            # 无论成功失败，用完即弃——原始文本绝不外流
            result["raw_text"] = None
            result["entries"] = []

    # ---- fail-closed（QA-003B）：只要有任一动态来源失败，本轮动态内容一律不发布 ----
    # 必须**同时**收窄 channels 与 dynamic_count，否则会出现「状态写 DEGRADED_FIXED_ONLY、
    # 文件里却带着动态线路」的自相矛盾（状态 / 摘要 / 实际内容三者必须一致）。
    failed_sources = [r for r in dynamic_report if not r["ok"]]
    dynamic_fail_closed = bool(include_dynamic and failed_sources)
    dynamic_discarded = 0
    if dynamic_fail_closed:
        dynamic_discarded = len(dynamic_included)
        dynamic_included = []
        for report in dynamic_report:
            report["discarded"] = int(report["included"])
            report["included"] = 0
        names = "、".join(r["source_name"] for r in failed_sources)
        warnings.append(
            f"动态来源失败（{names}）：已 fail-closed **只发布固定频道**，"
            f"本轮其余 {dynamic_discarded} 条动态条目一并舍弃"
            f"（不写入文件、也不复用上一次的签名线路）"
        )

    channels = fixed_channels + _dynamic_channels(
        dynamic_included, group_title=dynamic_group_title
    )
    if include_dynamic and dynamic_included == [] and not failed_sources:
        detail = ""
        if dynamic_excluded:
            top_reason, top_count = max(dynamic_excluded.items(), key=lambda kv: kv[1])
            detail = (
                f"；主要排除原因：{REASON_LABELS.get(top_reason, top_reason)}（{top_count} 条）"
            )
        warnings.append(
            "本轮没有纳入任何动态赛事条目（按动态空集处理，不带入任何历史动态线路）" + detail
        )

    return Composition(
        channels=channels,
        fixed_count=len(fixed_channels),
        dynamic_count=len(dynamic_included),
        skipped_fixed=selection["skipped"],
        dynamic_report=dynamic_report,
        dynamic_excluded_reasons=dynamic_excluded,
        warnings=warnings,
        composition_errors=validate_composition(channels),
        dynamic_fail_closed=dynamic_fail_closed,
        dynamic_discarded=dynamic_discarded,
    )


# ------------------------------------------------------------- 运行期产物护栏

def guard_runtime_output_path(target, *, repo_root=None) -> None:
    """拒绝把运行期产物写到「Git 工作树内且未被 .gitignore 忽略 / 已被跟踪」的位置。

    与 ``ingest.write_dynamic_snapshot`` 同一套判定（复用同一批函数，避免两处规则漂移）：
    工作树外 → 允许；工作树内 → 必须被忽略且未被跟踪。
    """
    resolved = pathlib.Path(target).resolve()
    root = (
        pathlib.Path(repo_root).resolve()
        if repo_root is not None
        else ingest_mod.find_git_worktree_root(resolved)
    )
    if root is None:
        return
    try:
        resolved.relative_to(root)
    except ValueError:
        return

    if not ingest_mod.is_ignored_by_gitignore(resolved, root):
        raise ValueError(
            f"拒绝写入 {resolved}：位于 Git 工作树 {root} 内但未被 .gitignore 忽略；"
            f"运行期产物请放在已被忽略的目录（如 out/）。"
        )
    if ingest_mod.is_tracked_by_git(resolved, root) is not False:
        raise ValueError(
            f"拒绝写入 {resolved}：该路径已被 Git 工作树 {root} 跟踪，或无法确认未被跟踪。"
        )


# ------------------------------------------------------------------ 发布主流程

def _write_summary(path, payload: dict) -> dict:
    """把发布摘要原子写到 path（先写同目录临时文件再替换）。"""
    target = pathlib.Path(path)
    guard_runtime_output_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = target.with_name(f"{target.stem}.tmp{target.suffix}")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, target)
    return {
        "path": str(target),
        "bytes": len(text.encode("utf-8")),
        "checksum": sha256_hex(text),
    }


def publish(
    conn,
    *,
    output_path,
    group_order: list[str],
    selection_kwargs: dict | None = None,
    keep_previous: bool = True,
    include_dynamic: bool = False,
    dynamic_sources=(),
    dynamic_filters: dict | None = None,
    dynamic_group_title: str = DEFAULT_DYNAMIC_GROUP_TITLE,
    require_dynamic: bool = False,
    limits: fetch_mod.FetchLimits | None = None,
    stamp: str | None = None,
    dry_run: bool = False,
    summary_path=None,
    opener=None,
) -> dict:
    """组合并（可选）发布统一播放列表。返回可直接序列化的结果字典。"""
    stamp = stamp or utcnow_iso()
    require_dynamic = bool(require_dynamic)
    if require_dynamic:
        include_dynamic = True

    payload: dict = {
        "published_at": stamp,
        "output_path": str(output_path),
        "dry_run": bool(dry_run),
        "require_dynamic": require_dynamic,
        "include_dynamic": bool(include_dynamic),
        "note": PUBLISH_NOTE,
    }

    try:
        composition = build_composition(
            conn,
            group_order=group_order,
            selection_kwargs=selection_kwargs,
            include_dynamic=include_dynamic,
            dynamic_sources=dynamic_sources,
            dynamic_filters=dynamic_filters,
            dynamic_group_title=dynamic_group_title,
            limits=limits,
            stamp=stamp,
            opener=opener,
        )
    except Exception as exc:  # noqa: BLE001 — 组合阶段任何异常都不许写出半成品
        payload.update(_rejected_payload(STATUS_REJECTED_VALIDATION, f"组合失败：{exc}"))
        return _finish(payload, summary_path=summary_path, dry_run=dry_run, write_summary=not dry_run)

    payload.update(
        {
            "fixed_count": composition.fixed_count,
            "dynamic_count": composition.dynamic_count,
            "channel_count": composition.total,
            "fixed_skipped": composition.skipped_fixed,
            "dynamic_sources": composition.dynamic_report,
            "dynamic_excluded_by_reason": {
                REASON_LABELS.get(k, k): v
                for k, v in sorted(composition.dynamic_excluded_reasons.items())
            },
            # fail-closed 的两个事实：是否触发、以及被舍弃的本轮动态条目数。
            # 有了它们，摘要里「状态=仅固定」与「实际文件里有没有动态线路」不可能再打架。
            "dynamic_fail_closed": composition.dynamic_fail_closed,
            "dynamic_discarded": composition.dynamic_discarded,
            "warnings": composition.warnings,
        }
    )

    dynamic_failed = any(not r["ok"] for r in composition.dynamic_report)

    # ---- 拒绝路径 1：要求动态内容但动态失败/缺失 ----
    if require_dynamic and (dynamic_failed or not composition.dynamic_report):
        reason = "动态来源获取失败" if dynamic_failed else "没有选中任何动态来源"
        payload.update(
            _rejected_payload(
                STATUS_REJECTED_DYNAMIC_REQUIRED,
                f"--require-dynamic 已指定，但{reason}；整次发布拒绝，当前与上一版文件保持不变",
            )
        )
        return _finish(payload, summary_path=summary_path, dry_run=dry_run, write_summary=not dry_run)

    # ---- 拒绝路径 2：动态失败 + 固定频道也为空 ----
    if dynamic_failed and composition.fixed_count == 0:
        payload.update(
            _rejected_payload(
                STATUS_DEGRADED_NO_PUBLISH,
                "动态来源失败且本次没有合格固定频道：不发布（也不沿用旧的动态签名线路）",
            )
        )
        payload["risk"] = (
            "已有 live.m3u 可能仍在被播放器读取，其动态线路随时失效；"
            "请修复动态来源或补齐固定频道后重跑 publish。"
        )
        return _finish(payload, summary_path=summary_path, dry_run=dry_run, write_summary=not dry_run)

    # ---- 拒绝路径 3：组合/校验层面不可发布 ----
    if composition.composition_errors:
        payload.update(
            _rejected_payload(
                STATUS_REJECTED_VALIDATION,
                "组合校验未通过：" + "；".join(composition.composition_errors),
            )
        )
        return _finish(payload, summary_path=summary_path, dry_run=dry_run, write_summary=not dry_run)

    # ---- 生成文本 + 反向校验（仍在写盘前） ----
    try:
        text = m3u_mod.generate_text(composition.channels)
    except m3u_mod.M3UError as exc:
        payload.update(_rejected_payload(STATUS_REJECTED_VALIDATION, f"生成 M3U 失败：{exc}"))
        return _finish(payload, summary_path=summary_path, dry_run=dry_run, write_summary=not dry_run)

    reverse_errors = reverse_validate(text, composition.channels)
    if reverse_errors:
        payload.update(
            _rejected_payload(
                STATUS_REJECTED_VALIDATION, "反向校验未通过：" + "；".join(reverse_errors)
            )
        )
        return _finish(payload, summary_path=summary_path, dry_run=dry_run, write_summary=not dry_run)

    payload["expected_checksum"] = sha256_hex(text)
    payload["expected_bytes"] = len(text.encode("utf-8"))

    # ---- 降级判定 ----
    # 注意：动态内容已在 build_composition 内被真正舍弃（channels/dynamic_count 已收窄），
    # 这里只是把状态与计数字段如实对齐，不存在「声称仅固定、文件里却有动态」的可能。
    if dynamic_failed and composition.fixed_count > 0:
        payload["status"] = STATUS_DEGRADED_FIXED_ONLY
    else:
        payload["status"] = STATUS_OK

    # ---- 写盘 ----
    if dry_run:
        payload["status"] = STATUS_DRY_RUN
        payload["published"] = False
        payload["exit_code"] = EXIT_OK
        return _finish(payload, summary_path=summary_path, dry_run=True, write_summary=False)

    try:
        stats = m3u_mod.write_m3u(
            composition.channels, output_path, keep_previous=keep_previous
        )
    except (OSError, m3u_mod.M3UError) as exc:
        payload.update(
            _rejected_payload(
                STATUS_REJECTED_IO,
                f"写盘失败（当前与上一版文件保持不变）：{exc}",
            )
        )
        return _finish(payload, summary_path=summary_path, dry_run=dry_run, write_summary=not dry_run)

    payload.update(
        {
            "published": True,
            "path": stats["path"],
            "previous": stats["previous"],
            "bytes": stats["bytes"],
            "checksum": stats["checksum"],
            "channel_count": stats["channel_count"],
            "exit_code": STATUS_EXIT[payload["status"]],
        }
    )
    return _finish(payload, summary_path=summary_path, dry_run=False, write_summary=True)


def _rejected_payload(status: str, reason: str) -> dict:
    return {
        "status": status,
        "published": False,
        "reason": reason,
        "exit_code": STATUS_EXIT[status],
    }


def _finish(payload: dict, *, summary_path, dry_run: bool, write_summary: bool) -> dict:
    """收尾：写发布摘要（不含任何完整签名 URL），并把摘要位置写回结果。"""
    payload.setdefault("published", False)
    payload.setdefault("exit_code", STATUS_EXIT.get(payload.get("status", ""), EXIT_REJECTED))
    payload["summary"] = None
    payload["summary_error"] = None

    if write_summary and summary_path:
        try:
            payload["summary"] = _write_summary(summary_path, payload)
        except (OSError, ValueError) as exc:
            # 摘要只是监测辅助，失败不影响已经发布的 live.m3u
            payload["summary_error"] = str(exc)
    return payload
