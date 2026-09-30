"""Li IPTV Aggregator — 最小工具函数。"""

from __future__ import annotations

import datetime as _dt
import hashlib

UTC = _dt.timezone.utc


def utcnow_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串（秒级精度）。

    统一用 UTC + 固定格式，保证 TEXT 排序等价于时间排序。
    """
    return _dt.datetime.now(UTC).replace(microsecond=0).isoformat()


def dt_to_iso(value: _dt.datetime) -> str:
    """把 datetime 规范化为库内使用的 ISO-8601 UTC 字符串。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def iso_to_dt(value: str) -> _dt.datetime:
    """把库内的 ISO-8601 字符串解析回带时区的 datetime。"""
    parsed = _dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def sha256_hex(text: str) -> str:
    """URL 等文本的稳定哈希（去重辅助键，不是业务主键）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
