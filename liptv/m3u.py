"""最小 M3U parser / generator。

V1 只支持播放器订阅最常用的部分：
  #EXTM3U
  #EXTINF:<duration> tvg-id="" tvg-name="" tvg-logo="" group-title="" ,Display Name
  <stream url>
  #EXTGRP:<group>            （作为 group-title 的补充来源）

刻意不做的事：header 指令（#EXTVLCOPT / #KODIPROP 等）、EPG 关联、catchup。
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re

from .util import sha256_hex

EXTINF_PREFIX = "#EXTINF:"
EXTGRP_PREFIX = "#EXTGRP:"

# 支持 key="value" 与 key=value 两种写法
_ATTR_RE = re.compile(r'([A-Za-z0-9_.\-]+)=(?:"([^"]*)"|([^\s,]+))')

# 生成 M3U 时输出的属性顺序
_GENERATED_ATTRS = ("tvg-id", "tvg-name", "tvg-logo", "group-title")


class M3UError(ValueError):
    """M3U 内容或生成参数非法。"""


@dataclasses.dataclass
class M3UEntry:
    """一条从 M3U 解析出来的原始条目。"""

    name: str
    url: str
    tvg_id: str | None = None
    tvg_name: str | None = None
    tvg_logo: str | None = None
    group_title: str | None = None
    duration: str | None = None
    line_no: int = 0


@dataclasses.dataclass
class ParseResult:
    """解析结果 + 统计信息。"""

    entries: list[M3UEntry] = dataclasses.field(default_factory=list)
    has_header: bool = False
    total_lines: int = 0
    skipped: dict[str, int] = dataclasses.field(default_factory=dict)

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def skipped_count(self) -> int:
        return sum(self.skipped.values())


@dataclasses.dataclass
class M3UChannel:
    """一条准备写进输出 M3U 的线路。

    key 只用于查重（例如 canonical_channel.id），不会写进文件。
    """

    name: str
    url: str
    key: object | None = None
    tvg_id: str | None = None
    tvg_name: str | None = None
    tvg_logo: str | None = None
    group_title: str | None = None


def _split_extinf(payload: str) -> tuple[str, str, dict[str, str]]:
    """切分 #EXTINF 内容 → (duration, display_name, attrs)。

    显示名是「第一个引号外的逗号」之后的部分，因此属性值里含逗号不会切错。
    """
    in_quotes = False
    split_at = -1
    for idx, ch in enumerate(payload):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            split_at = idx
            break

    if split_at == -1:
        head, display = payload, ""
    else:
        head, display = payload[:split_at], payload[split_at + 1:]

    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(head):
        key = match.group(1).lower()
        value = match.group(2) if match.group(2) is not None else match.group(3)
        attrs[key] = value

    duration = head.split(" ", 1)[0].strip() if head.strip() else None
    return duration or None, display.strip(), attrs


def parse_text(text: str) -> ParseResult:
    """解析 M3U 文本。"""
    result = ParseResult()
    lines = text.splitlines()
    result.total_lines = len(lines)

    pending: M3UEntry | None = None
    pending_group: str | None = None

    def bump(reason: str) -> None:
        result.skipped[reason] = result.skipped.get(reason, 0) + 1

    for offset, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue

        if line.upper().startswith("#EXTM3U"):
            result.has_header = True
            continue

        if line.upper().startswith(EXTINF_PREFIX):
            if pending is not None:
                bump("extinf_without_url")
            duration, display, attrs = _split_extinf(line[len(EXTINF_PREFIX):])
            pending = M3UEntry(
                name=display,
                url="",
                tvg_id=attrs.get("tvg-id"),
                tvg_name=attrs.get("tvg-name"),
                tvg_logo=attrs.get("tvg-logo"),
                group_title=attrs.get("group-title"),
                duration=duration,
                line_no=offset,
            )
            continue

        if line.upper().startswith(EXTGRP_PREFIX):
            # #EXTGRP 可能出现在 #EXTINF 之前或之后，统一按「最近一次声明」处理，
            # 在读到 URL 时再回填（见下方 apply）。
            pending_group = line[len(EXTGRP_PREFIX):].strip()
            continue

        if line.startswith("#"):
            bump("ignored_directive")
            continue

        # 非注释行 = 播放地址
        if pending is None:
            bump("url_without_extinf")
            continue

        pending.url = line
        if not pending.group_title and pending_group:
            pending.group_title = pending_group
        result.entries.append(pending)
        pending = None

    if pending is not None:
        bump("extinf_without_url")

    return result


def parse_file(path: str | pathlib.Path) -> ParseResult:
    """从文件解析 M3U（UTF-8，容错 BOM）。"""
    text = pathlib.Path(path).read_text(encoding="utf-8-sig")
    return parse_text(text)


# ------------------------------------------------------------------ 生成

def _clean_attr(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.replace('"', "'").replace("\r", " ").replace("\n", " ").strip()
    return cleaned or None


def _clean_name(value: str) -> str:
    return (value or "").replace("\r", " ").replace("\n", " ").strip()


def generate_text(channels: list[M3UChannel]) -> str:
    """生成 M3U 文本，并在生成前做基础校验。"""
    seen: set[object] = set()
    lines: list[str] = ["#EXTM3U"]

    for index, channel in enumerate(channels):
        url = (channel.url or "").strip()
        if not url:
            raise M3UError(f"第 {index + 1} 个条目 URL 为空，拒绝生成")

        if channel.key is not None:
            if channel.key in seen:
                raise M3UError(f"重复的条目 key={channel.key!r}，拒绝生成")
            seen.add(channel.key)

        attrs: list[str] = []
        for attr in _GENERATED_ATTRS:
            value = _clean_attr(getattr(channel, attr.replace("-", "_")))
            if value:
                attrs.append(f'{attr}="{value}"')

        name = _clean_name(channel.name)
        head = "#EXTINF:-1" + (" " + " ".join(attrs) if attrs else "")
        lines.append(f"{head},{name}")
        lines.append(url)

    return "\n".join(lines) + "\n"


def write_m3u(
    channels: list[M3UChannel],
    path: str | pathlib.Path,
    *,
    keep_previous: bool = True,
) -> dict[str, object]:
    """原子写出 M3U。

    顺序：先校验并生成文本 → 备份当前文件为 *.previous.m3u → os.replace 原子替换。
    任何一步失败都不会破坏现有文件（last-known-good 保留）。
    """
    target = pathlib.Path(path)
    text = generate_text(channels)  # 校验失败会在这里抛错，不会动到磁盘
    target.parent.mkdir(parents=True, exist_ok=True)

    previous: pathlib.Path | None = None
    if keep_previous and target.exists():
        previous = target.with_name(f"{target.stem}.previous{target.suffix}")
        previous.write_bytes(target.read_bytes())

    tmp = target.with_name(f"{target.stem}.tmp{target.suffix}")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, target)

    return {
        "path": str(target),
        "previous": str(previous) if previous else None,
        "channel_count": len(channels),
        "bytes": len(text.encode("utf-8")),
        "checksum": sha256_hex(text),
    }
