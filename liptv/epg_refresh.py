"""TASK-011 §9/§17：EPG 抓取 → 校验 → 原子落盘的完整 refresh 流程。

单独成模块的理由：把「抓取」与「解析/校验」严格分开，才能保证
**任何抓取侧故障都不可能损坏已有的 LKG 文件**。

refresh 的不变量（每一条都对应任务书的一项硬要求）：

1. 抓取失败（网络/HTTP/超时/超大）⇒ **不碰**输出文件，返回失败原因；
2. XML 解析失败 / 空 XML / channel id 冲突 / 没有节目单⇒ 同上；
3. 合并结果必须**通过质量门禁**才写盘；
4. 写盘用临时文件 + ``os.replace``，读者永远看不到半文件；
5. refresh **完全独立于 M3U 发布**（§17「EPG refresh 失败不应阻断
   live.m3u publish」）—— 本模块不 import publish，也不碰 playlist。

刷新节奏由调用方（scheduler / cron）决定，本模块**不含定时器**。
"""

from __future__ import annotations

import datetime as dt
import pathlib
import time
from typing import Any

from . import epg as epg_mod
from . import fetch as fetch_mod

__all__ = ["EpgSource", "RefreshResult", "refresh_epg", "load_sources"]

#: 单个 feed 的大小上限。EPG feed 实测 3~8 MB，16 MB 留足余量又防止
#: 遇到错误响应体（例如 HTML 错误页被当 XML）时无限读。
MAX_FEED_BYTES = 16 * 1024 * 1024

#: 抓取超时。EPG feed 较大，30s 是「不该卡住主流程」的量级（§17）。
FETCH_TIMEOUT_SECONDS = 30


class EpgSource:
    """一个 EPG feed 来源（公开 URL + 展示名）。"""

    def __init__(self, key: str, url: str, *, title: str = "") -> None:
        self.key = key
        self.url = url
        self.title = title or key

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"EpgSource({self.key!r}, host={self.url.split('/')[2]!r})"


class RefreshResult:
    """refresh 的结构化结果，供 CLI / 状态文件 / 测试共用。"""

    def __init__(
        self,
        *,
        ok: bool,
        written: bool = False,
        feeds: list[dict] | None = None,
        quality: dict | None = None,
        error: str | None = None,
        lkg_preserved: bool = False,
    ) -> None:
        self.ok = ok
        self.written = written
        self.feeds = feeds or []
        self.quality = quality or {}
        self.error = error
        #: 失败时是否**确实**保留了原文件（供测试与报告核对）。
        self.lkg_preserved = lkg_preserved

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "written": self.written,
            "feeds": self.feeds,
            "quality": self.quality,
            "error": self.error,
            "lkg_preserved": self.lkg_preserved,
        }


def load_sources(config: dict) -> list[EpgSource]:
    """从 config 的 ``[epg]`` 段读取 feed 列表。

    配置形如::

        [epg]
        sources = [
          { key = "fanmingming", url = "https://cdn.jsdelivr.net/.../e.xml" },
        ]

    刻意支持两种写法（字符串列表 / 表列表），因为 TOML 里内联表列表
    可读性更好，而字符串列表写起来更省事。两种都不接受「空列表」——
    空列表意味着「不启用 EPG」，不是错误。
    """
    section = (config or {}).get("epg") or {}
    raw = section.get("sources") or []
    out: list[EpgSource] = []
    for item in raw:
        if isinstance(item, str):
            out.append(EpgSource(key=f"src{len(out) + 1}", url=item))
        elif isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            out.append(
                EpgSource(
                    key=str(item.get("key") or f"src{len(out) + 1}"),
                    url=url,
                    title=str(item.get("title") or ""),
                )
            )
    return out


def _fetch_one(src: EpgSource) -> tuple[epg_mod.ParsedFeed, dict]:
    """抓取并解析单个 feed。**不抛异常**，失败信息进 dict。"""
    info: dict[str, Any] = {"key": src.key, "url_host": src.url.split("/")[2]}
    limits = fetch_mod.FetchLimits(
        timeout_seconds=FETCH_TIMEOUT_SECONDS,
        max_bytes=MAX_FEED_BYTES,
        user_agent=fetch_mod.DEFAULT_USER_AGENT,
    )
    try:
        fetched = fetch_mod.fetch_text(src.url, limits=limits)
    except fetch_mod.FetchError as exc:
        info.update(ok=False, error=str(exc), category=getattr(exc, "category", None))
        return epg_mod.ParsedFeed(), info

    feed = epg_mod.parse_xmltv(fetched.text)
    info.update(
        ok=feed.quality.well_formed,
        bytes=fetched.byte_count,
        channels=feed.quality.channel_count,
        programmes=feed.quality.programme_count,
        error=feed.quality.error,
    )
    return feed, info


def refresh_epg(
    *,
    sources: list[EpgSource],
    output_path: str | pathlib.Path,
    restrict_to: set[str] | None = None,
    now: dt.datetime | None = None,
) -> RefreshResult:
    """抓取 → 合并 → 校验 → 原子落盘。

    ``restrict_to`` 给出时只保留这些 channel id（播放器只会按M3U 里的
    tvg-id 找节目，输出无关频道只会让文件变大）。

    失败时**保证** ``output_path`` 的原有内容一字不变。
    """
    target = pathlib.Path(output_path)
    existed_before = target.exists()
    infos: list[dict] = []
    feeds: list[epg_mod.ParsedFeed] = []

    for src in sources:
        feed, info = _fetch_one(src)
        infos.append(info)
        if feed.quality.well_formed and feed.quality.channel_count:
            feeds.append(feed)

    if not feeds:
        # 一个都没成功 ⇒ 绝不覆盖 LKG。
        return RefreshResult(
            ok=False,
            feeds=infos,
            error="所有 EPG 源都不可用或解析失败",
            lkg_preserved=existed_before,
        )

    merged = epg_mod.merge_feeds(feeds, restrict_to=restrict_to)

    if not merged.quality.usable:
        #🚨 交集为 0 是最常见的一种「不可用」，且原因与「源坏了」完全不同：
        # 源本身有几千个频道，但它们的 channel id 与我们 metadata 里登记的
        # epg_channel_id 一个都对不上（实测 epg.pw 用纯数字 id539631…，
        # 而 fanmingming / iptv-org 用中文 id CCTV1 / 湖南卫视）。
        # 这种情况**必须**说清楚，否则运维会以为「源挂了」而去换源 ——
        # 其实换任何源都一样，只有换 channel id 体系才有用。
        if restrict_to and merged.quality.channel_count == 0:
            total_seen = sum(f.quality.channel_count for f in feeds)
            samples = sorted({cid for f in feeds for cid in list(f.channels)[:3]})[:5]
            return RefreshResult(
                ok=False,
                feeds=infos,
                quality=merged.quality.as_dict(),
                error=(
                    f"源里有 {total_seen} 个 channel，但与 metadata 登记的 "
                    f"{len(restrict_to)} 个 epg_channel_id **交集为 0**。"
                    f"源本身的 channel id 示例：{samples}。"
                    f"这是 id 体系不匹配（如 epg.pw 用纯数字 id、fanmingming 用"
                    f"中文 id），不是源故障 —— 换源无用，需要人工建立 id 映射表。"
                ),
                lkg_preserved=existed_before,
            )
        return RefreshResult(
            ok=False,
            feeds=infos,
            quality=merged.quality.as_dict(),
            error=(
                "合并结果未通过质量门禁"
                f"（channel={merged.quality.channel_count}, "
                f"programme={merged.quality.programme_count}, "
                f"未来48h有节目={merged.quality.channels_with_future_programme}）"
            ),
            lkg_preserved=existed_before,
        )

    payload = epg_mod.render_xmltv(merged)
    if not payload.strip():
        return RefreshResult(
            ok=False, feeds=infos, quality=merged.quality.as_dict(),
            error="渲染结果为空", lkg_preserved=existed_before,
        )

    try:
        epg_mod.write_epg_atomic(payload, target)
    except OSError as exc:
        # 落盘失败同样不动 LKG（临时文件已在 write_epg_atomic 内清理）。
        return RefreshResult(
            ok=False, feeds=infos, quality=merged.quality.as_dict(),
            error=f"写入失败：{type(exc).__name__}: {exc}",
            lkg_preserved=existed_before,
        )

    return RefreshResult(
        ok=True,
        written=True,
        feeds=infos,
        quality=merged.quality.as_dict(),
        lkg_preserved=False,
    )


def build_status(
    result: RefreshResult,
    *,
    output_path: str | pathlib.Path,
    epg_channel_count: int | None = None,
    metadata_coverage: dict | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """构造状态 payload（§18 的可观测性字段）。

    刻意**不含**任何会「让视频订阅不可用」的信息 —— 状态只是给人看的。
    """
    stamp = now if now is not None else time.time()
    payload: dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "epg_path": str(output_path),
        "ok": result.ok,
        "written": result.written,
        "feeds": result.feeds,
        "quality": result.quality,
        "error": result.error,
    }
    if result.ok and result.written:
        payload["last_success_epoch"] = stamp
    if epg_channel_count is not None:
        payload["metadata_coverage"] = metadata_coverage or {}
    return payload