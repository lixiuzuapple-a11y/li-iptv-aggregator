"""来源编排层（TASK-002）：两类来源的严格分离。

**fixed_m3u** —— 相对稳定的公开频道列表：
    fetch → 校验 → 在**单一来源的事务**里应用快照
    （本次出现的创建/更新、本次消失的置 active=0、重新出现的恢复 active=1）。
    失败（网络/HTTP/解码/非法/空列表）**绝不触碰库存**，只写 fetch 状态。

**dynamic_event_m3u** —— 动态赛事列表（如 JSNZKPG）：
    每次显式获取都重新请求原始 URL，只返回**临时**解析结果与摘要；
    不写 canonical_channel / stream，不参与 /live.m3u；
    摘要默认脱敏（去 query、截断 path），避免泄漏短时签名参数。
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import time
from urllib.parse import urlsplit

from . import fetch as fetch_mod
from . import m3u as m3u_mod
from . import repo
from .util import sha256_hex, utcnow_iso

# 两类来源的规范 kind 取值
KIND_FIXED = "fixed_m3u"
KIND_DYNAMIC = "dynamic_event_m3u"
KIND_LOCAL = "local-m3u"

KNOWN_KINDS = (KIND_FIXED, KIND_DYNAMIC, KIND_LOCAL)

_TAG_RE = re.compile(r"\[([^\[\]]{1,32})\]")

DYNAMIC_NOTE = "短时快照：动态赛事条目不做长期持久化，不参与固定频道 /live.m3u 输出。"


# ------------------------------------------------------------------ 校验

def validate_m3u_text(text: str) -> m3u_mod.ParseResult:
    """把响应文本按 M3U 校验，失败时抛 FetchError（INVALID_M3U / EMPTY_LIST）。"""
    parsed = m3u_mod.parse_text(text or "")
    if parsed.entry_count > 0:
        return parsed

    stripped = (text or "").strip()
    if not stripped:
        raise fetch_mod.FetchError(fetch_mod.ERROR_EMPTY_LIST, "响应体为空")
    upper = stripped.upper()
    if "#EXTM3U" not in upper and "#EXTINF" not in upper:
        raise fetch_mod.FetchError(
            fetch_mod.ERROR_INVALID_M3U,
            f"内容不含 #EXTM3U / #EXTINF，不是 M3U 结构（{len(stripped)} 字符）",
        )
    raise fetch_mod.FetchError(fetch_mod.ERROR_EMPTY_LIST, "M3U 结构存在但没有任何有效条目")


# ------------------------------------------------------------------ 脱敏

def redact_url(url: str, *, max_path: int = 40) -> str:
    """把 URL 脱敏成可安全进日志/报告的形式。

    * query 全部丢弃（只报参数个数）—— 短时签名参数（txSecret / txTime / token 等）不会外泄；
    * path 过长时截断；
    * fragment 一律不显示。
    """
    parts = urlsplit(url or "")
    if not parts.scheme or not parts.hostname:
        return "<unparsable-url>"
    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    path = parts.path or ""
    if len(path) > max_path:
        path = path[:max_path] + "…"
    query = ""
    if parts.query:
        query = f"?<redacted:{len(parts.query.split('&'))}-param(s)>"
    return f"{parts.scheme}://{netloc}{path}{query}"


def url_extension(url: str) -> str:
    """线路类型原样文本（按 URL 后缀推断）。"""
    path = urlsplit(url or "").path.lower()
    for ext in (".m3u8", ".m3u", ".flv", ".mp4", ".ts", ".mpd"):
        if path.endswith(ext):
            return ext
    return "(none)"


def extract_tags(name: str) -> list[str]:
    """原样提取名称里的方括号标记（如 [解说] / [原声]）。"""
    return _TAG_RE.findall(name or "")


def summarize_entries(entries) -> list[dict]:
    """把解析出的条目汇总成**不含完整播放 URL** 的摘要列表。"""
    summary = []
    for index, entry in enumerate(entries, start=1):
        summary.append(
            {
                "index": index,
                "name": entry.name,
                "group_title": entry.group_title,
                "tags": extract_tags(entry.name),
                "url_redacted": redact_url(entry.url),
                "url_ext": url_extension(entry.url),
                "url_scheme": urlsplit(entry.url).scheme or None,
            }
        )
    return summary


# --------------------------------------------------------------- fixed_m3u

def ingest_fixed_source(
    conn,
    source,
    *,
    limits: fetch_mod.FetchLimits | None = None,
    now: str | None = None,
    opener=None,
) -> dict:
    """抓取并应用单个 fixed 来源的快照。

    成功：在单一事务里应用快照 + 写 fetch 状态。
    失败：只写 fetch 状态，**库存零改动**。
    """
    limits = limits or fetch_mod.FetchLimits()
    stamp = now or utcnow_iso()
    source_id = int(source["id"])
    source_name = source["name"]
    url = source["url"]

    started = time.perf_counter()
    base = {
        "source_id": source_id,
        "source_name": source_name,
        "kind": source["kind"],
        "url": url,
        "url_redacted": redact_url(url or ""),
    }

    if not url:
        category = fetch_mod.ERROR_NETWORK
        message = "来源未配置 URL，跳过抓取"
        repo.mark_source_fetched(conn, source_id, category, now=stamp)
        conn.commit()
        return {
            **base,
            "ok": False,
            "status": category,
            "error": message,
            "error_category": category,
            "http_status": None,
            "bytes": 0,
            "entries": 0,
            "created": 0,
            "updated": 0,
            "deactivated": 0,
            "reactivated": 0,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    try:
        fetched = fetch_mod.fetch_text(url, limits=limits, opener=opener)
        parsed = validate_m3u_text(fetched.text)
    except fetch_mod.FetchError as exc:
        repo.mark_source_fetched(conn, source_id, exc.category, now=stamp)
        conn.commit()
        return {
            **base,
            "ok": False,
            "status": exc.category,
            "error": str(exc),
            "error_category": exc.category,
            "http_status": exc.http_status,
            "bytes": 0,
            "entries": 0,
            "created": 0,
            "updated": 0,
            "deactivated": 0,
            "reactivated": 0,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    # ---- 单一来源事务边界：库存变更与 fetch 状态一起提交或一起不提交 ----
    try:
        conn.execute("SAVEPOINT liptv_fixed_ingest")
        stats = repo.apply_source_snapshot(conn, source_id, parsed.entries, now=stamp)
        repo.mark_source_fetched(conn, source_id, "ok", now=stamp)
        conn.execute("RELEASE liptv_fixed_ingest")
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — 明确回滚而不是留下半截数据
        conn.execute("ROLLBACK TO liptv_fixed_ingest")
        conn.execute("RELEASE liptv_fixed_ingest")
        conn.commit()
        return {
            **base,
            "ok": False,
            "status": fetch_mod.ERROR_UNKNOWN,
            "error": f"应用快照失败并已回滚：{exc}",
            "error_category": fetch_mod.ERROR_UNKNOWN,
            "http_status": fetched.status,
            "bytes": fetched.byte_count,
            "entries": parsed.entry_count,
            "created": 0,
            "updated": 0,
            "deactivated": 0,
            "reactivated": 0,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    return {
        **base,
        "ok": True,
        "status": "ok",
        "error": None,
        "error_category": None,
        "http_status": fetched.status,
        "content_type": fetched.content_type,
        "bytes": fetched.byte_count,
        "entries": parsed.entry_count,
        "has_header": parsed.has_header,
        "skipped": parsed.skipped,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        **stats,
    }


# ------------------------------------------------------- dynamic_event_m3u

def preview_dynamic_source(
    source,
    *,
    limits: fetch_mod.FetchLimits | None = None,
    now: str | None = None,
    opener=None,
    include_raw: bool = False,
) -> dict:
    """实时获取动态赛事源，只返回临时结果。**不接触数据库、不写任何库存。**

    `include_raw=True` 时在 `_raw_text` 键里额外返回原始文本，仅供调用方
    显式落盘使用；CLI 在输出任何摘要/JSON 之前必须把它摘掉，
    否则带签名参数的播放地址会泄漏到日志（见 tests/test_dynamic.py）。
    """
    limits = limits or fetch_mod.FetchLimits()
    stamp = now or utcnow_iso()
    url = source["url"]
    base = {
        "source_id": int(source["id"]) if source["id"] is not None else None,
        "source_name": source["name"],
        "kind": source["kind"],
        "url": url,
        "url_redacted": redact_url(url or ""),
        "fetched_at": stamp,
        "persisted": False,
        "note": DYNAMIC_NOTE,
    }

    started = time.perf_counter()
    if not url:
        return {
            **base,
            "ok": False,
            "status": fetch_mod.ERROR_NETWORK,
            "error": "动态来源未配置 URL",
            "error_category": fetch_mod.ERROR_NETWORK,
            "entry_count": 0,
            "entries": [],
            "groups": {},
            "duration_ms": 0,
            "_raw_text": None,
        }

    try:
        fetched = fetch_mod.fetch_text(url, limits=limits, opener=opener)
        parsed = validate_m3u_text(fetched.text)
    except fetch_mod.FetchError as exc:
        return {
            **base,
            "ok": False,
            "status": exc.category,
            "error": str(exc),
            "error_category": exc.category,
            "http_status": exc.http_status,
            "entry_count": 0,
            "entries": [],
            "groups": {},
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "_raw_text": None,
        }

    groups: dict[str, int] = {}
    for entry in parsed.entries:
        key = entry.group_title or "(no-group)"
        groups[key] = groups.get(key, 0) + 1

    return {
        **base,
        "ok": True,
        "status": "ok",
        "error": None,
        "error_category": None,
        "http_status": fetched.status,
        "content_type": fetched.content_type,
        "bytes": fetched.byte_count,
        "has_header": parsed.has_header,
        "skipped": parsed.skipped,
        "entry_count": parsed.entry_count,
        "entries": summarize_entries(parsed.entries),
        "groups": groups,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "_raw_text": fetched.text if include_raw else None,
    }


def write_dynamic_snapshot(
    text: str,
    out_path: str | pathlib.Path,
    *,
    allowed_dir: str | pathlib.Path,
) -> dict:
    """把动态快照写到受 .gitignore 保护的运行目录。

    安全约束：目标必须落在 allowed_dir（默认 out/tmp，被 .gitignore 忽略）之内，
    否则拒绝写入 —— 防止带短时签名 URL 的快照被误提交进 Git。
    """
    target = pathlib.Path(out_path).resolve()
    allowed = pathlib.Path(allowed_dir).resolve()
    if target != allowed and allowed not in target.parents:
        raise ValueError(
            f"拒绝写入 {target}：动态快照只能落在受忽略的运行目录 {allowed} 之内"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return {
        "path": str(target),
        "bytes": len(text.encode("utf-8")),
        "checksum": sha256_hex(text),
        "note": DYNAMIC_NOTE,
    }


def default_dynamic_out_path(template_dir: str | pathlib.Path, source_name: str, now: str) -> str:
    """生成默认快照文件名：out/tmp/<source>-<UTC 时间戳>.m3u"""
    safe = re.sub(r"[^A-Za-z0-9_.\-]+", "_", source_name or "dynamic")
    stamp = (now or utcnow_iso()).replace(":", "").replace("+", "Z")
    return str(pathlib.Path(template_dir) / f"{safe}-{stamp}.m3u")


def is_ignored_by_gitignore(path: str | pathlib.Path, repo_root: str | pathlib.Path) -> bool:
    """粗判路径是否会被仓库 .gitignore 忽略（用于自检与报告）。"""
    root = pathlib.Path(repo_root)
    try:
        relative = pathlib.Path(path).resolve().relative_to(root.resolve())
    except ValueError:
        return False
    ignore_file = root / ".gitignore"
    if not ignore_file.exists():
        return False
    name = os.path.basename(str(relative).replace("\\", "/"))
    top = str(relative).replace("\\", "/").split("/", 1)[0]
    for raw_line in ignore_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line in (top, f"{top}/") or line.rstrip("/") == top:
            return True
        if line == name:
            return True
    return False


@dataclasses.dataclass
class SourceRef:
    """解析后的来源引用（DB 行 或 仅配置来源）。"""

    id: int | None
    name: str
    kind: str
    url: str | None
    enabled: int

    def as_row(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "url": self.url,
                "enabled": self.enabled}
