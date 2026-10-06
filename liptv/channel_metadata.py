"""TASK-011 §4/§5：canonical 元数据映射层。

存在的唯一理由：**频道身份必须稳定**。

TASK-010 之前，输出 M3U 的频道身份完全由「这一轮恰好选中哪条stream」
决定。于是同一个频道在不同 source、不同时间产出的 ``tvg-id`` 会变，
Apple TV 这类播放器会把频道当成新频道，节目单匹配不上、Logo 也不稳定。

本模块把「身份」从「线路」里剥离出来：

- **canonical** 是唯一主键（与 seed、DB、selector 完全一致）；
- ``tvg_id`` 只来自：已验证 XMLTV channel id > 官方 EPG id >
  明确静态人工mapping > 内部稳定 id（最后兜底且必须显式声明）；
- metadata 缺失**只**意味着该字段留空，**绝不影响** stream 选择。

冻结规则（任务书 §5 逐条对应实现）：

===========================  ==========================================
禁止                        实现方式
===========================  ==========================================
URL hash 当 tvg-id          只接受人工填写的字符串，无任何自动推导
source_channel_id当 tvg-id  metadata 只接受 canonical 名字做主键
临时 source 名当 tvg-id     tvg_id 与 source 完全解耦
fuzzy 自动猜                无相似度匹配，canonical 名不命中就 miss
CCTV-5 与 CCTV-5+ 共用id   二者必须是不同 canonical 且 id 不同
===========================  ==========================================

冲突一律 fail-closed：同一个 canonical 出现两个不同 tvg_id、或两个
canonical 抢同一个 tvg_id，都直接抛错，绝不静默取胜者。理由与
``source_policy.py``、``fixed_aliases.toml`` 一致 —— 冲突必须人工消解。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ChannelMetadata",
    "MetadataError",
    "load_channel_metadata",
    "MetadataBook",
    "default_metadata",
    "validate_metadata_file",
    "TVG_ID_RE",
]

#: 内部稳定 id 的命名约束。刻意**不用**完整URL hash（任务书 §5 明令禁止），
#: 只允许「可读前缀 + 短数字后缀」，例如 ``liptv-cctv-5plus-1``。
TVG_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class MetadataError(ValueError):
    """元数据文件非法。**永远 fail-closed，绝不静默降级。**"""


@dataclass(frozen=True)
class ChannelMetadata:
    """一个 canonical 的元数据。字段全部可选，缺失即留空。"""

    canonical: str
    tvg_id: str | None = None
    tvg_name: str | None = None
    logo: str | None = None
    #: EPG 来源名（对应 ``source_policies``/epg 配置里的 key），非 URL。
    epg_source: str | None = None
    #: 该 canonical 在 XMLTV 里的 channel id（与 tvg_id 相同或更权威）。
    epg_channel_id: str | None = None
    group: str | None = None
    #: 该频道是否需要 VPN 才能播放（沿用 TASK-010 §13 的 context 语义）。
    playback_requires_vpn: bool = False
    notes: str = ""
    _extra: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def has_epg(self) -> bool:
        """是否绑定了 EPG。只有真绑了才算覆盖率分子（任务书 §7）。"""
        return bool(self.epg_channel_id)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def default_metadata(canonical: str) -> ChannelMetadata:
    """未登记 canonical 的兜底：全部字段留空。

    刻意**不**生成任何 tvg_id —— 任务书 §7 要求「不要为了 coverage
    建错误 mapping」，错误 EPG 比没有 EPG 更差。无 metadata 时省略
    ``tvg-id`` 属性（§14「缺 EPG 可省 tvg-id」），播放器会退化为按名字匹配。
    """
    return ChannelMetadata(canonical=canonical)


_BOOL_FIELDS = {"playback_requires_vpn"}
_KNOWN = {
    "canonical", "tvg_id", "tvg_name", "logo",
    "epg_source", "epg_channel_id", "group", "notes",
} | _BOOL_FIELDS


def normalize_entry(entry: dict[str, Any], *, source: str) -> ChannelMetadata:
    """校验并归一化一条 ``[[channel]]``。

    凭据类字段一律**拒绝**（任务书 §22 + §21 第 33 项）：把 Cookie/
    Authorization/token 写进 metadata 等于把凭据提交进 Git。
    """
    unknown = set(entry) - _KNOWN
    if unknown:
        raise MetadataError(
            f"{source}:未知字段 {sorted(unknown)}。"
            f"允许的字段只有 {sorted(_KNOWN)}"
        )
    banned = {
        k for k in entry
        if any(b in k.lower() for b in ("cookie", "auth", "token", "password", "passwd", "secret", "credential"))
    }
    if banned:
        raise MetadataError(
            f"{source}:字段 {sorted(banned)} 疑似凭据。metadata 只允许存"
            f"公开 metadata，VPN 凭据 / Cookie / token 一律禁止写入（会进 Git）"
        )

    canonical = _clean(entry.get("canonical"))
    if not canonical:
        raise MetadataError(f"{source}:每条 [[channel]] 必须有非空 canonical")

    for key in ("tvg_id", "tvg_name", "epg_source", "epg_channel_id", "group"):
        value = _clean(entry.get(key))
        if value is None:
            continue
        if key == "tvg_id":
            # 只校验字符集；**不**做任何自动推导或纠错。
            import re
            if not re.match(TVG_ID_RE, value):
                raise MetadataError(
                    f"{source}:canonical {canonical!r} 的 tvg_id {value!r} 非法。"
                    f"只允许 [A-Za-z0-9._-]，且长度 <= 64"
                )

    flag = entry.get("playback_requires_vpn", False)
    if not isinstance(flag, bool):
        raise MetadataError(f"{source}:canonical {canonical!r} 的 playback_requires_vpn 必须是 true/false")

    return ChannelMetadata(
        canonical=canonical,
        tvg_id=_clean(entry.get("tvg_id")),
        tvg_name=_clean(entry.get("tvg_name")),
        logo=_clean(entry.get("logo")),
        epg_source=_clean(entry.get("epg_source")),
        epg_channel_id=_clean(entry.get("epg_channel_id")),
        group=_clean(entry.get("group")),
        playback_requires_vpn=flag,
        notes=str(entry.get("notes") or "").strip(),
        _extra={k: v for k, v in entry.items() if k not in _KNOWN},
    )


class MetadataBook:
    """按 canonical 索引的元数据簿，构造后即通过全部一致性校验。"""

    def __init__(self, entries: list[ChannelMetadata], *, source: str = "<memory>") -> None:
        self.source = source
        self._by_canonical: dict[str, ChannelMetadata] = {}
        self._tvg_owner: dict[str, str] = {}

        for meta in entries:
            existing = self._by_canonical.get(meta.canonical)
            if existing is not None:
                raise MetadataError(
                    f"{source}:canonical {meta.canonical!r} 重复定义。"
                    f"冲突必须人工消解，拒绝后者覆盖前者"
                )
            # CCTV-5 / CCTV-5+ 这类必须各自独立，不允许共用 id。
            if meta.tvg_id:
                owner = self._tvg_owner.get(meta.tvg_id)
                if owner is not None and owner != meta.canonical:
                    raise MetadataError(
                        f"{source}:tvg_id {meta.tvg_id!r} 被 {owner!r} 与 "
                        f"{meta.canonical!r} 同时占用。同一 tvg-id 不允许对应"
                        f"两个 canonical（会把两个频道在播放器里合并成一个）"
                    )
                self._tvg_owner[meta.tvg_id] = meta.canonical
            self._by_canonical[meta.canonical] = meta

    def get(self, canonical: str) -> ChannelMetadata:
        """取元数据；未登记时返回全空的兜底（**不抛错**）。"""
        return self._by_canonical.get(canonical) or default_metadata(canonical)

    def __contains__(self, canonical: object) -> bool:
        return canonical in self._by_canonical

    def __len__(self) -> int:
        return len(self._by_canonical)

    @property
    def entries(self) -> list[ChannelMetadata]:
        return list(self._by_canonical.values())

    def coverage(self, canonicals: list[str]) -> dict[str, Any]:
        """统计 coverage —— 任务书 §7/§8 的硬指标口径。

        分子都是「真正登记了」的，未登记一律不计入分子，
        宁可 coverage 低也不虚报。
        """
        total = len(canonicals)
        with_id = [c for c in canonicals if self.get(c).tvg_id]
        with_logo = [c for c in canonicals if self.get(c).logo]
        with_epg = [c for c in canonicals if self.get(c).has_epg]
        pct = lambda n: round(100.0 * n / total, 1) if total else 0.0
        return {
            "total": total,
            "tvg_id": {"n": len(with_id), "pct": pct(len(with_id))},
            "logo": {"n": len(with_logo), "pct": pct(len(with_logo))},
            "epg": {"n": len(with_epg), "pct": pct(len(with_epg))},
            "missing_tvg_id": [c for c in canonicals if not self.get(c).tvg_id],
            "missing_logo": [c for c in canonicals if not self.get(c).logo],
            "missing_epg": [c for c in canonicals if not self.get(c).has_epg],
        }


def load_channel_metadata(path: Path | str | None) -> MetadataBook:
    """读取 metadata TOML。

    文件不存在返回**空簿**（不是错误）—— metadata 是增强层，
    缺它只是没有 tvg-id/logo，不该让整条流水线失败。
    文件存在但非法则直接抛错（fail-closed）。
    """
    if path is None:
        return MetadataBook([], source="<none>")
    p = Path(path)
    if not p.exists():
        return MetadataBook([], source=str(p))
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MetadataError(f"{p}:无法解析 TOML —— {exc}") from exc

    raw = data.get("channel") or []
    if not isinstance(raw, list):
        raise MetadataError(f"{p}:[[channel]] 必须是数组")
    return MetadataBook(
        [normalize_entry(e, source=str(p)) for e in raw if isinstance(e, dict)],
        source=str(p),
    )


def validate_metadata_file(path: Path | str) -> dict[str, Any]:
    """给 CLI / 运维用的独立校验入口，返回可读摘要。"""
    book = load_channel_metadata(path)
    ids = sorted({m.tvg_id for m in book.entries if m.tvg_id})
    return {
        "source": book.source,
        "canonical_count": len(book),
        "tvg_id_count": len(ids),
        "epg_count": sum(1 for m in book.entries if m.has_epg),
        "logo_count": sum(1 for m in book.entries if m.logo),
    }