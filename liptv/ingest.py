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
import shutil
import struct
import subprocess
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
    """把响应文本按 M3U 校验，失败时抛 FetchError（INVALID_M3U / EMPTY_LIST）。

    **完整性校验（QA-002A）**：一个「部分成功的截断前缀」——即解析出了若干有效条目、
    但末尾（或中间）仍有 `#EXTINF` 没有配套播放地址——属于明显不完整的 M3U。
    这种文本绝不能被当作**完整快照**去驱动「本次未出现 → 置 active=0」的下线逻辑，
    否则一次截断的上游响应会静默误下线旧频道（违反「失败不得污染库存」）。

    注意：这里**不**用「条目数量变少」判据 —— 真正结构完整的删台必须照常生效，
    由调用方按快照语义把消失的条目置为 inactive。
    """
    parsed = m3u_mod.parse_text(text or "")
    dangling_extinf = parsed.skipped.get("extinf_without_url", 0)

    if parsed.entry_count > 0:
        if dangling_extinf > 0:
            raise fetch_mod.FetchError(
                fetch_mod.ERROR_INVALID_M3U,
                f"解析到 {parsed.entry_count} 条有效条目，但仍有 {dangling_extinf} 条 "
                f"#EXTINF 没有配套播放地址；判定为截断/不完整的 M3U，"
                f"拒绝作为完整快照应用（避免误下线已有条目）",
            )
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
    repo_root: str | pathlib.Path | None = None,
) -> dict:
    """把动态快照写到**受 Git 保护**的运行目录。

    TASK-002 的安全约束，这里**三重强制**（QA-002B / QA-002C）：

    1. 目标必须落在 ``allowed_dir``（即配置的 ``fetch.dynamic_tmp_dir``）之内；
    2. 目标位于某个 Git 工作树内 → 必须命中该树的 ``.gitignore`` 规则，否则拒绝；
       目标不在任何 Git 工作树内 → Git 根本不会跟踪它，允许写入；
    3. 目标**已被该 Git 工作树跟踪**（在它的索引里）→ 一律拒绝。

    第 2 条单独用是不够的：``dynamic_tmp_dir`` 可以被配置覆盖成仓库内**未被忽略**
    的目录（例如 ``SOURCES/``），那样带短时签名参数的完整快照就会进入 Git 跟踪范围。

    第 3 条单独用也是不够的（QA-002C）：``.gitignore`` **只对尚未被跟踪的文件生效**。
    一旦某个快照文件历史上被 ``git add -f`` 强制加进索引（例如 ``out/tmp/signed.m3u``），
    之后往同名文件写入就会直接变成**待提交的已跟踪变更**，.gitignore 拦不住。
    所以即使目标落在被忽略的目录里，也要再问一次 Git 索引。

    ``repo_root`` 显式给出时按它判定；默认按目标路径自动向上探测 Git 工作树。
    """
    target = pathlib.Path(out_path).resolve()
    allowed = pathlib.Path(allowed_dir).resolve()
    if target != allowed and allowed not in target.parents:
        raise ValueError(
            f"拒绝写入 {target}：动态快照只能落在受忽略的运行目录 {allowed} 之内"
        )

    root = (
        pathlib.Path(repo_root).resolve()
        if repo_root is not None
        else _find_git_worktree_root(target)
    )
    tracked: bool | None = None
    if root is not None and _is_within(target, root):
        if not is_ignored_by_gitignore(target, root):
            raise ValueError(
                f"拒绝写入 {target}：该路径位于 Git 工作树 {root} 内，但未被 .gitignore 忽略；"
                f"带短时签名参数的快照不得进入 Git 跟踪范围。"
                f"请把 fetch.dynamic_tmp_dir 指向已被忽略的目录（如 out/tmp），"
                f"或改用不在任何 Git 工作树内的目录。"
            )

        tracked = is_tracked_by_git(target, root)
        if tracked is None:
            raise ValueError(
                f"拒绝写入 {target}：该路径位于 Git 工作树 {root} 内，但**无法确认它未被跟踪**"
                f"（既取不到可用的 git 可执行文件，也读不出该工作树的索引）。"
                f"带短时签名参数的快照在无法证明安全时只能拒绝。"
                f"可设置环境变量 LIPTV_GIT_EXECUTABLE 指向 git 后重试。"
            )
        if tracked:
            raise ValueError(
                f"拒绝写入 {target}：该路径**已被 Git 工作树 {root} 跟踪**（存在于其索引中）。"
                f".gitignore 对已跟踪文件不生效，写入会把带短时签名参数的快照变成"
                f"待提交的已跟踪变更。请改用未被跟踪且被忽略的文件名"
                f"（默认的 out/tmp/<source>-<UTC 时间戳>.m3u 满足该条件）。"
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return {
        "path": str(target),
        "bytes": len(text.encode("utf-8")),
        "checksum": sha256_hex(text),
        "note": DYNAMIC_NOTE,
        # 供报告/自检使用：不在任何 Git 工作树内时为 None（该路径不存在被跟踪风险）
        "git_worktree": str(root) if root is not None else None,
        "git_ignored": is_ignored_by_gitignore(target, root) if root is not None else None,
        "git_tracked": tracked,
    }


def default_dynamic_out_path(template_dir: str | pathlib.Path, source_name: str, now: str) -> str:
    """生成默认快照文件名：out/tmp/<source>-<UTC 时间戳>.m3u"""
    safe = re.sub(r"[^A-Za-z0-9_.\-]+", "_", source_name or "dynamic")
    stamp = (now or utcnow_iso()).replace(":", "").replace("+", "Z")
    return str(pathlib.Path(template_dir) / f"{safe}-{stamp}.m3u")


# ------------------------------------------------- 快照落盘路径的 Git 安全

def _is_within(path: str | pathlib.Path, root: str | pathlib.Path) -> bool:
    """path 是否位于 root 之内（含两者相等）。"""
    try:
        pathlib.Path(path).resolve().relative_to(pathlib.Path(root).resolve())
        return True
    except ValueError:
        return False


def _find_git_worktree_root(path: str | pathlib.Path) -> pathlib.Path | None:
    """自 path 向上寻找包含 .git 的目录（Git 工作树根）；找不到返回 None。"""
    current = pathlib.Path(path).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _read_gitignore_rules(base_dir: pathlib.Path) -> list[tuple[str, bool, bool]]:
    """读取一个目录下的 .gitignore，返回 [(pattern, negated, dir_only), ...]。"""
    ignore_file = base_dir / ".gitignore"
    if not ignore_file.is_file():
        return []
    try:
        raw_lines = ignore_file.read_text(encoding="utf-8-sig").splitlines()
    except OSError:  # pragma: no cover — 读不到就当没有规则（不静默放行）
        return []

    rules: list[tuple[str, bool, bool]] = []
    for raw in raw_lines:
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("\\#"):
            line = line[1:]
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        line = line.strip()
        dir_only = line.endswith("/")
        if dir_only:
            line = line.rstrip("/")
        if not line:
            continue
        rules.append((line, negated, dir_only))
    return rules


def _gitignore_pattern_regex(pattern: str) -> re.Pattern[str]:
    """把一条 .gitignore 模式编译成正则（覆盖本项目用到的常见语法）。

    支持 ``*`` / ``?`` / ``**`` 通配、前导 ``/`` 或内含 ``/`` 的锚定、目录模式；
    不支持字符类与转义序列 —— 对 out/ data/ *.sqlite3 这类模式足够。
    """
    anchored = pattern.startswith("/") or "/" in pattern
    if pattern.startswith("/"):
        pattern = pattern[1:]

    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index:index + 2] == "**":
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    body = "".join(out)
    if anchored:
        return re.compile("^" + body + "$")
    return re.compile("(?:^|.*/)" + body + "$")


def is_ignored_by_gitignore(path: str | pathlib.Path, repo_root: str | pathlib.Path) -> bool:
    """判定 path 是否会被 repo_root 的 .gitignore 规则忽略（QA-002B）。

    规则来源：工作树根，以及从根到目标父目录的每一级目录下的 .gitignore。
    判定方式：目标本身或它的任一父目录命中「最后一条适用规则」，且该规则不是否定规则
    （父目录被忽略 ⇒ 其中的文件同样被忽略）。

    目标不在 repo_root 内时返回 False —— 调用方据此判断是否要拒绝写入。
    """
    root = pathlib.Path(repo_root).resolve()
    target = pathlib.Path(path).resolve()
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        return False
    if not parts:
        return False

    # (相对 root 的 posix 目录串, 规则表)；"." 表示工作树根
    sources: list[tuple[str, list[tuple[str, bool, bool]]]] = [
        (".", _read_gitignore_rules(root))
    ]
    accumulated: list[str] = []
    for part in parts[:-1]:
        accumulated.append(part)
        sources.append(("/".join(accumulated), _read_gitignore_rules(root.joinpath(*accumulated))))

    for depth in range(1, len(parts) + 1):
        prefix = "/".join(parts[:depth])
        is_dir = depth < len(parts)  # 不是最后一段 ⇒ 一定是目录
        decision: bool | None = None

        for base_rel, rules in sources:
            if not rules:
                continue
            if base_rel == ".":
                rel = prefix
            elif prefix == base_rel:
                rel = ""
            elif prefix.startswith(base_rel + "/"):
                rel = prefix[len(base_rel) + 1:]
            else:
                continue
            if not rel:
                continue
            for pattern, negated, dir_only in rules:
                if dir_only and not is_dir:
                    continue
                if _gitignore_pattern_regex(pattern).search(rel):
                    decision = not negated

        if decision:  # 该前缀落在被忽略范围内 ⇒ 目标也被忽略
            return True
    return False


# ------------------------------- 动态快照落盘路径的 Git 索引安全（QA-002C）

# 本机（以及不少 Windows 机器）不把 git 放进 PATH，允许用环境变量显式指定。
_GIT_EXECUTABLE_ENV = ("LIPTV_GIT_EXECUTABLE", "GIT_EXECUTABLE")


def _resolve_git_executable() -> str | None:
    """定位 git 可执行文件；取不到返回 None（调用方回退到直接解析索引）。"""
    for key in _GIT_EXECUTABLE_ENV:
        candidate = os.environ.get(key)
        if candidate and pathlib.Path(candidate).is_file():
            return candidate
    found = shutil.which("git")
    if found:
        return found
    if os.name == "nt":
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)"),
            os.path.join(os.environ.get("LOCALAPPDATA", "") or "", "Programs"),
        ):
            if not base:
                continue
            for tail in (("Git", "cmd", "git.exe"), ("Git", "bin", "git.exe")):
                candidate = pathlib.Path(base).joinpath(*tail)
                if candidate.is_file():
                    return str(candidate)
    return None


def _resolve_git_dir(root: pathlib.Path) -> pathlib.Path | None:
    """取该工作树的 git 目录。

    ``.git`` 可能是目录（普通工作树），也可能是内容为 ``gitdir: <path>`` 的文件
    （``git worktree add`` 出来的附属工作树、子模块）——两种形态都要认，
    否则 nested worktree 会被误判为「无法确认」。
    """
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        try:
            content = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if content.lower().startswith("gitdir:"):
            raw = content.split(":", 1)[1].strip()
            candidate = pathlib.Path(raw)
            if not candidate.is_absolute():
                candidate = root / candidate
            candidate = candidate.resolve()
            if candidate.is_dir():
                return candidate
    return None


def _query_git_index(git_exe: str, root: pathlib.Path, rel_posix: str) -> bool | None:
    """让 git 自己回答：该相对路径是否在索引里（``git ls-files --error-unmatch``）。

    输出一律丢弃（``DEVNULL``）—— 判定完全不依赖任何输出内容，
    因此不会把仓库里的文件名或私密内容带进日志/报告。
    路径以独立参数传入（不经过 shell），带空格的 OneDrive 路径也安全。

    返回 True（已跟踪）/ False（未匹配）/ None（无法判定，交给索引回退）。
    """
    try:
        proc = subprocess.run(
            [git_exe, "-C", str(root), "ls-files", "--error-unmatch", "--", rel_posix],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None          # 128 = 不是 Git 仓库 / 其他异常 ⇒ 交由索引文件回退


def _decode_git_varint(data: bytes, offset: int) -> tuple[int, int]:
    """解 git 的可变长整数（index v4 的路径前缀压缩在用）。"""
    if offset >= len(data):
        raise IndexError("index 数据在变长整数处截断")
    value = data[offset] & 0x7F
    cursor = offset + 1
    while data[cursor - 1] & 0x80:
        if cursor >= len(data):
            raise IndexError("index 数据在变长整数处截断")
        value += 1
        value = (value << 7) + (data[cursor] & 0x7F)
        cursor += 1
    return value, cursor


def _git_index_contains(git_dir: pathlib.Path, rel_posix: str) -> bool | None:
    """直接读 ``<git_dir>/index`` 判断路径是否已跟踪（git 不可用时的回退）。

    支持 index v2 / v3 / v4。遇到不认识的版本、split index（``link`` 扩展，条目被
    拆到 ``sharedindex.<sha>``）或结构异常时返回 **None**（= 无法证明未跟踪，
    调用方按保守策略拒绝写入），绝不在读不懂时静默放行。
    """
    index_file = git_dir / "index"
    if not index_file.is_file():
        return False        # 索引尚未建立 ⇒ 不可能有任何已跟踪文件
    try:
        data = index_file.read_bytes()
    except OSError:
        return None
    if len(data) < 12 or data[:4] != b"DIRC":
        return None
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3, 4):
        return None

    wanted = rel_posix.encode("utf-8", "surrogateescape")
    offset = 12
    previous = b""
    for _ in range(count):
        if offset + 62 > len(data):
            return None
        flags = struct.unpack(">H", data[offset + 60:offset + 62])[0]
        fixed = 62 + (2 if (version >= 3 and flags & 0x4000) else 0)   # 扩展标志
        if version == 4:
            try:
                strip_len, cursor = _decode_git_varint(data, offset + fixed)
            except IndexError:
                return None
            end = data.find(b"\x00", cursor)
            if end < 0 or strip_len > len(previous):
                return None
            name = previous[: len(previous) - strip_len] + data[cursor:end]
            previous = name
            offset = end + 1
        else:
            name_start = offset + fixed
            end = data.find(b"\x00", name_start)
            if end < 0:
                return None
            name = data[name_start:end]
            # 条目在 v2/v3 里按 8 字节对齐（相对条目起点），名字以 NUL 结尾
            offset += (fixed + (end - name_start) + 1 + 7) & ~7
        if name == wanted:
            return True

    if data[offset:offset + 4] == b"link":
        return None         # split index：条目分散在 sharedindex，不做猜测
    return False


def is_tracked_by_git(path: str | pathlib.Path, repo_root: str | pathlib.Path) -> bool | None:
    """判定 path 是否**已被 repo_root 这个 Git 工作树跟踪**（QA-002C）。

    返回 True / False / None（无法判定）。

    为什么需要它：``.gitignore`` 只对**尚未被跟踪**的文件生效。已经进过索引的文件
    （典型情形是历史上被 ``git add -f`` 强制加入）必须另行拦截，
    否则写入会把带短时签名参数的动态快照变成待提交的已跟踪变更。

    判定顺序：先问 git 可执行文件（与索引天然一致，且天然支持 nested worktree），
    再回退到直接解析 ``.git/index``。路径不在 repo_root 内时返回 False
    （不在该工作树里，就不可能被它跟踪）。
    """
    root = pathlib.Path(repo_root).resolve()
    target = pathlib.Path(path).resolve()
    try:
        rel = target.relative_to(root)
    except ValueError:
        return False
    if not rel.parts:
        return False

    rel_posix = rel.as_posix()
    git_exe = _resolve_git_executable()
    if git_exe is not None:
        verdict = _query_git_index(git_exe, root, rel_posix)
        if verdict is not None:
            return verdict

    git_dir = _resolve_git_dir(root)
    if git_dir is None:
        return None
    return _git_index_contains(git_dir, rel_posix)


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
