"""TASK-011 §9/§15/§16：EPG 抓取、缓存、校验与合并。

设计前提（全部来自任务书，且都在代码里落成硬约束）：

- **不自造节目单**（§2A）。只从公开 XMLTV feed 抓，合并后原样输出。
- **失败保留 last-known-good**（§9/§19/§20 F1~F3）。任何 HTTP 失败、
  XML 解析失败、空 XML、channel id 冲突，一律**不覆盖**已有文件。
- **原子替换**（§9/§23第 23 条）。先写临时文件再 ``os.replace``，
  读者永远看不到半文件。
- **XML 无效不覆盖 LKG**（§9）。校验在写盘**之前**完成。
- **refresh 失败不阻断 live.m3u publish**（§17）。本模块**完全不碰**
  M3U 与 scheduler 的发布路径，只提供独立可调用的 refresh。

合并语义（§15）刻意保守：

- 同一 canonical 只允许**单一 authoritative EPG source**，不做 ranking；
- channel id 相同但 display-name 不同 ⇒ 取先到的（顺序固定 ⇒ 结果确定）；
- programme 按 ``(channel, start, title)`` 去重，**后到的不覆盖先到的**
  （保证「EPG merge deterministic」，§21 第 21/22 项）；
- programme 引用了未知 channel ⇒ **过滤掉并计数**，绝不生成悬空引用
  （§20 F6 / §16）。
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = [
    "EpgError",
    "EpgQuality",
    "ParsedFeed",
    "parse_xmltv",
    "merge_feeds",
    "render_xmltv",
    "validate_xmltv_text",
    "write_epg_atomic",
    "load_epg_status",
    "save_epg_status",
    "EPG_XML_DECL",
]

EPG_XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'


class EpgError(ValueError):
    """EPG 数据或配置非法。**永远 fail-closed。**"""


# ---------------------------------------------------------------- 抓取结果

@dataclass
class EpgQuality:
    """EPG 质量报告（任务书 §16 的检查项逐条对应字段）。"""

    well_formed: bool = False
    channel_count: int = 0
    programme_count: int = 0
    duplicate_channel_ids: int = 0
    orphan_programmes: int = 0
    bad_time_programmes: int = 0
    #: 当前时间附近（±窗口）有节目的频道数
    channels_with_current_programme: int = 0
    #: 未来 48h 内有节目的频道数
    channels_with_future_programme: int = 0
    #: 最远节目的时间（ISO 字符串），用于识别「过期几个月的 stale feed」
    latest_programme_end: str | None = None
    error: str | None = None

    @property
    def usable(self) -> bool:
        """是否值得写盘。

        判据刻意保守 —— **错误 EPG 比没有 EPG 更差**（§7原话）：

        - XML 必须 well-formed；
        - 必须真的有 channel（空 XML 不算成功，§16）；
        - 必须真的有 programme（没有节目单的 EPG 对播放器毫无意义）；
        - 至少一半以上的 channel 要在未来 48h 内有节目，
          否则判为 stale feed（过期几个月的 feed 不能用）。
        """
        if not self.well_formed or self.channel_count == 0:
            return False
        if self.programme_count == 0:
            return False
        if self.channels_with_future_programme * 2 < self.channel_count:
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "well_formed": self.well_formed,
            "channel_count": self.channel_count,
            "programme_count": self.programme_count,
            "duplicate_channel_ids": self.duplicate_channel_ids,
            "orphan_programmes": self.orphan_programmes,
            "bad_time_programmes": self.bad_time_programmes,
            "channels_with_current_programme": self.channels_with_current_programme,
            "channels_with_future_programme": self.channels_with_future_programme,
            "latest_programme_end": self.latest_programme_end,
            "error": self.error,
            "usable": self.usable,
        }


@dataclass
class ParsedFeed:
    """一个已解析的 XMLTV feed。"""

    channels: dict[str, dict] = field(default_factory=dict)
    programmes: list[dict] = field(default_factory=list)
    quality: EpgQuality = field(default_factory=EpgQuality)

    @property
    def channel_ids(self) -> set[str]:
        return set(self.channels)


# ---------------------------------------------------------------- 时间解析

_TS_FORMATS = (
    "%Y%m%d%H%M%S %z",
    "%Y%m%d%H%M%S",
    "%Y%m%d%H%M",
    "%Y%m%d",
)


def parse_timestamp(raw: str | None) -> dt.datetime | None:
    """解析 XMLTV timestamp。**无法解析返回 None，绝不抛异常。**

    XMLTV 的时间格式有带/不带时区两种；带时区偏移的（``+0800``）必须
    保留 —— 任务书 §15 要求「timezone 保留」。
    """
    if not raw:
        return None
    text = raw.strip()
    for fmt in _TS_FORMATS:
        try:
            parsed = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        # 不带时区的按 EPG 惯例解释为**本机本地时间**（生产机是 UTC+8）。
        return parsed if parsed.tzinfo else parsed.astimezone()
    return None


def _fmt_ts(value: dt.datetime) -> str:
    """反向格式化。统一输出带偏移的 ``%Y%m%d%H%M%S %z``（§16 timezone 可解析）。"""
    if value.tzinfo is None:
        value = value.astimezone()
    return value.strftime("%Y%m%d%H%M%S %z")


# ---------------------------------------------------------------- 解析

def parse_xmltv(data: bytes | str, *, now: dt.datetime | None = None) -> ParsedFeed:
    """解析 XMLTV 文本，返回结构化 feed + 质量报告。

    **不抛异常**：解析失败时返回 ``quality.well_formed=False`` 且
    ``quality.error`` 有值，由调用方决定是否写盘（保持 LKG）。
    """
    now = now or dt.datetime.now().astimezone()
    feed = ParsedFeed()
    q = feed.quality

    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            q.error = f"非 UTF-8 编码: {exc}"
            return feed
    else:
        text = data

    if not text.strip():
        q.error = "空 XML"
        return feed

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        q.error = f"XML 解析失败: {exc}"
        return feed

    q.well_formed = True

    seen_ids: set[str] = set()
    for node in root.findall("channel"):
        cid = (node.get("id") or "").strip()
        if not cid:
            continue
        if cid in seen_ids:
            q.duplicate_channel_ids += 1
            continue  # channel id必须 unique（§16）
        seen_ids.add(cid)
        icon_node = node.find("icon")
        feed.channels[cid] = {
            # 🚨 ``display-name`` 在 XMLTV 里是**子元素**而不是 attribute。
            # 误用 ``node.get("display-name")`` 会让每个频道名静默退化成
            # channel id 本身 —— 而且 roundtrip 依然自洽，看不出任何异常。
            # 这类错误只有拿真实 feed 交叉核对才能发现。
            "display_name": (
                (node.findtext("display-name") or "").strip()
                or (node.get("display-name") or "").strip()
                or cid
            ),
            # ``icon`` 同理：URL 在 ``src`` attribute 上。
            "icon": (
                (icon_node.get("src") or "").strip() if icon_node is not None else None
            ) or None,
        }
    q.channel_count = len(feed.channels)

    future_cutoff = now + dt.timedelta(hours=48)
    past_cutoff = now - dt.timedelta(hours=6)
    has_future: set[str] = set()
    has_current: set[str] = set()
    latest: dt.datetime | None = None

    for node in root.findall("programme"):
        channel = (node.get("channel") or "").strip()
        start = parse_timestamp(node.get("start"))
        stop = parse_timestamp(node.get("stop"))
        if start is None or stop is None:
            q.bad_time_programmes += 1
            continue
        if stop < start:
            q.bad_time_programmes += 1
            continue
        if channel not in seen_ids:
            q.orphan_programmes += 1
            continue  # F6：过滤悬空引用，绝不生成坏 XML
        feed.programmes.append({
            "channel": channel,
            "start": start,
            "stop": stop,
            "title": (node.findtext("title") or "").strip(),
            "desc": (node.findtext("desc") or "").strip(),
        })
        if stop > past_cutoff and start <= future_cutoff:
            has_future.add(channel)
            if start <= now < stop:
                has_current.add(channel)
        if latest is None or stop > latest:
            latest = stop

    q.programme_count = len(feed.programmes)
    q.channels_with_future_programme = len(has_future)
    q.channels_with_current_programme = len(has_current)
    q.latest_programme_end = _fmt_ts(latest) if latest else None
    return feed


# ---------------------------------------------------------------- 合并

def merge_feeds(feeds: Iterable[ParsedFeed], *, restrict_to: set[str] | None = None) -> ParsedFeed:
    """按固定顺序合并多个 feed（§15）。

    - ``restrict_to`` 给定时只保留这些 channel id —— 用于「canonical →
      单一 authoritative EPG source」：播放器只会按M3U 里的 tvg-id 找节目，
      输出无关频道只会让文件变大、匹配变慢。
    - 合并**完全确定**：同 id的 channel 取先到者；同
      ``(channel, start, title)`` 的 programme 取先到者。
    """
    merged = ParsedFeed()
    seen_programmes: set[tuple[str, str, str]] = set()
    q = merged.quality
    q.well_formed = True

    for feed in feeds:
        if not feed.quality.well_formed:
            # 单个 feed 坏了就跳过，不影响其它源（§15 多源合并）。
            continue
        for cid, meta in feed.channels.items():
            if restrict_to is not None and cid not in restrict_to:
                continue
            if cid not in merged.channels:
                merged.channels[cid] = dict(meta)
        for prog in feed.programmes:
            if restrict_to is not None and prog["channel"] not in restrict_to:
                continue
            key = (prog["channel"], _fmt_ts(prog["start"]), prog["title"])
            if key in seen_programmes:
                continue  # duplicate programme 去重（§15）
            seen_programmes.add(key)
            merged.programmes.append(dict(prog))

    merged.programmes.sort(key=lambda p: (p["channel"], p["start"], p["title"]))
    q.channel_count = len(merged.channels)
    q.programme_count = len(merged.programmes)
    q.orphan_programmes = 0  # 合并时已过滤
    now = dt.datetime.now().astimezone()
    future_cutoff = now + dt.timedelta(hours=48)
    past_cutoff = now - dt.timedelta(hours=6)
    has_future: set[str] = set()
    has_current: set[str] = set()
    latest: dt.datetime | None = None
    for prog in merged.programmes:
        if prog["stop"] > past_cutoff and prog["start"] <= future_cutoff:
            has_future.add(prog["channel"])
            if prog["start"] <= now < prog["stop"]:
                has_current.add(prog["channel"])
        if latest is None or prog["stop"] > latest:
            latest = prog["stop"]
    q.channels_with_future_programme = len(has_future)
    q.channels_with_current_programme = len(has_current)
    q.latest_programme_end = _fmt_ts(latest) if latest else None
    return merged


# ---------------------------------------------------------------- 渲染

def _esc(value: str) -> str:
    """XML 文本转义。``&`` 必须第一个处理，否则会二次转义。"""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _esc_attr(value: str) -> str:
    return _esc(value).replace('"', "&quot;")


def render_xmltv(feed: ParsedFeed) -> bytes:
    """渲染成 XMLTV 字节串（带 XML 声明与 UTF-8 编码）。"""
    out: list[str] = [EPG_XML_DECL, "<tv>\n"]
    for cid in sorted(feed.channels):
        meta = feed.channels[cid]
        out.append(
            f'  <channel id="{_esc_attr(cid)}">\n'
            f'    <display-name>{_esc(meta["display_name"])}</display-name>\n'
        )
        if meta.get("icon"):
            out.append(f'    <icon src="{_esc_attr(meta["icon"])}" />\n')
        out.append("  </channel>\n")
    for prog in feed.programmes:
        out.append(
            f'  <programme start="{_esc_attr(_fmt_ts(prog["start"]))}" '
            f'stop="{_esc_attr(_fmt_ts(prog["stop"]))}" '
            f'channel="{_esc_attr(prog["channel"])}">\n'
            f'    <title lang="zh">{_esc(prog["title"])}</title>\n'
        )
        if prog.get("desc"):
            out.append(f'    <desc lang="zh">{_esc(prog["desc"])}</desc>\n')
        out.append("  </programme>\n")
    out.append("</tv>\n")
    return "".join(out).encode("utf-8")


def validate_xmltv_text(text: str | bytes) -> tuple[bool, EpgQuality]:
    """独立校验入口（CLI / 测试用）。返回 ``(是否可用, 质量报告)``。"""
    feed = parse_xmltv(text)
    return feed.quality.usable, feed.quality


# ---------------------------------------------------------------- 原子写

def write_epg_atomic(data: bytes, path: str | pathlib.Path) -> None:
    """原子写出 EPG（§9「原子替换，避免半文件」）。

    先写同目录临时文件 → ``fsync`` → ``os.replace``。同目录保证
    ``os.replace`` 在同一文件系统内，是真正的原子操作。
    """
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    tmp = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        # 失败时**绝不能**留下半个文件。
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 状态

def load_epg_status(path: str | pathlib.Path) -> dict[str, Any]:
    """读 EPG 状态文件。文件缺失/损坏返回空 dict（状态信息**永远不该**
    成为「EPG 是否可用」的判据 —— 它只是可观测性，§18）。"""
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    try:
        import json

        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_epg_status(payload: dict[str, Any], path: str | pathlib.Path) -> None:
    import json

    write_epg_atomic(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), path
    )


def epg_age_seconds(status: dict[str, Any], *, now: float | None = None) -> float | None:
    """EPG 文件年龄（秒）。用于 §18的 ``epg_age`` 与 stale 判定。"""
    stamp = status.get("last_success_epoch")
    if not isinstance(stamp, (int, float)):
        return None
    return max(0.0, (now if now is not None else time.time()) - float(stamp))