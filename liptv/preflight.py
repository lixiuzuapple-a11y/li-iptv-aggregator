"""动态赛事当前轮预检（TASK-012 §17）。

目的：**少把明显挂掉的赛事 URL 推给播放器**。就这一件事。

冻结条件（§17，逐条对应到代码里的禁止项）
------------------------------------------------
============================  ==========================================
冻结                          代码位置
============================  ==========================================
不写 probe_result 历史        :func:`precheck_entry` **只返回内存对象**，
                             全模块无任何 ``INSERT``/``repo.add_probe_result``
不进 fixed selector           本模块不被 :mod:`liptv.select` 引用，
                             也不写 ``stream`` 表
不保存旧 signed URL           :class:`PrecheckResult` **不持��� url 字段**，
                             只有 :func:`redact_url` 后的 host 形式
不做长时间深 probe           预算硬上限 :data:`DEFAULT_BUDGET`，
                             且任一阶段单请求超时独立受限
不转码 / 不代理               只做 HTTP GET + 读**前若干字节**，
                             绝不长时间持有连接、绝不转发媒体流
============================  ==========================================

三级判定：``PRECHECK_PASS`` / ``PRECHECK_FAIL`` / ``PRECHECK_UNKNOWN``。

``PRECHECK_UNKNOWN`` 是**刻意存在**的一档：网络抖动、源侧超时、无法在预算内
判定时都归它，而不是硬猜 FAIL。把抖动当 FAIL 会误杀好赛事（TASK-011 §13 的
27 条 segment-404 教训）。

authoritative / advisory（§18）
--------------------------------
:func:`apply_policy` 把来源 policy 映射成「结论能不能删条目」：

* ``authoritative=True``  → ``PRECHECK_FAIL`` 可以**排除本轮**该条目；
* ``authoritative=False`` → ``PRECHECK_FAIL`` **只能记录**，
  计入 ``advisory_fail``，绝不影响发布内容（KORICE 冻结规则）。
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit

from . import errors as errors_mod

PRECHECK_PASS = "PRECHECK_PASS"
PRECHECK_FAIL = "PRECHECK_FAIL"
PRECHECK_UNKNOWN = "PRECHECK_UNKNOWN"

ALL_RESULTS: tuple[str, ...] = (PRECHECK_PASS, PRECHECK_FAIL, PRECHECK_UNKNOWN)

RESULT_LABELS: dict[str, str] = {
    PRECHECK_PASS: "通过",
    PRECHECK_FAIL: "不通过（本轮可排除）",
    PRECHECK_UNKNOWN: "无法判定（不排除）",
}

# ------------------------------------------------------------------ 预算

#: 单条目各阶段超时（秒）。刻意短小 —— 这是「预检」不是「探测」。
DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_READ_TIMEOUT = 4.0
#: 读多少字节就够判类型。HLS master/media 的判定只需要头几百字节。
DEFAULT_HEAD_BYTES = 4096
#: 整轮总预算（秒）。条目多时按条均摊，**绝不**因为条目多就无限等。
DEFAULT_BUDGET = 45.0
#: 并发上限。默认保守；不在生产机上调高。
DEFAULT_MAX_CONCURRENCY = 4
#: 单条目总耗时上限（秒）。
DEFAULT_PER_ENTRY_SECONDS = 12.0

#: 分片请求最多读多少字节 —— 只为确认「拿到的是 TS 而不是 HTML」。
SEGMENT_HEAD_BYTES = 188

#: 标准网关语义状态码：**可能由中间代理产生**，不能直接当作「源站失败」。
#: 见 :func:`precheck_entry` 里 HTTPError 分支的说明（本机 FlClash 实测：
#: 不存在的端口会得到 502 而非 ConnectionRefused，会导致整批赛事被误删）。
_PROXY_INDUCIBLE_STATUS = frozenset({502, 503, 504})

_HLS_MARKERS = (b"#EXTM3U",)
_HTML_MARKERS = (b"<!doctype html", b"<html", b"<HTML")
_USER_AGENT = "liptv-aggregator-preflight/1 (+read-only; no media proxying)"


def redact_url(url: str | None) -> str:
    """只保留 ``scheme://host[:port]/...``。signed query **绝不**出现在结果里。"""
    parts = urlsplit(url or "")
    if not parts.scheme or not parts.hostname:
        return "<unparsable-url>"
    host = f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
    tail = "/..." if parts.path not in ("", "/") else ""
    return f"{parts.scheme}://{host}{tail}"


@dataclasses.dataclass
class PrecheckResult:
    """单条动态赛事 URL 的预检结论。

    ⚠️ **刻意不存 url**。要回溯用 :func:`redact_url` 后的 ``host``，或用
    ``entry_index`` + 调用方自己的内存列表。这样「signed URL 不落任何长期存储」
    是**结构性保证**，不是靠记得脱敏。
    """

    entry_index: int
    result: str
    host: str
    scheme: str | None
    error_category: str
    #: ``True``/``False``/``None``（UNKNOWN 时为 None）。来源 policy 决定它能否删条目。
    authoritative: bool | None = None
    duration_ms: int | None = None
    #: HLS 才填：master 里解析到的第一个 variant（**相对路径已解析成绝对**，但绝不返回）。
    segment_probed: bool = False
    segment_ok: bool | None = None
    detail: str | None = None

    @property
    def excluded(self) -> bool:
        """这条能否因预检失败而从**本轮发布**里排除。

        只有「FAIL + authoritative」才为 True —— advisory 源永远 False。
        """
        return self.result == PRECHECK_FAIL and bool(self.authoritative)

    def to_dict(self) -> dict:
        return {
            "entry_index": self.entry_index,
            "result": self.result,
            "host": self.host,
            "scheme": self.scheme,
            "error_category": self.error_category,
            "authoritative": self.authoritative,
            "duration_ms": self.duration_ms,
            "segment_probed": self.segment_probed,
            "segment_ok": self.segment_ok,
            "excluded": self.excluded,
            "detail": self.detail,
        }


@dataclasses.dataclass
class PrecheckSettings:
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    head_bytes: int = DEFAULT_HEAD_BYTES
    budget_seconds: float = DEFAULT_BUDGET
    per_entry_seconds: float = DEFAULT_PER_ENTRY_SECONDS
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    #: §17「总预算必须可控」：预算耗尽后，剩余条目一律 UNKNOWN（不猜）。
    stop_on_budget: bool = True

    @classmethod
    def from_mapping(cls, raw: dict | None) -> "PrecheckSettings":
        raw = dict(raw or {})
        return cls(
            connect_timeout=float(raw.get("connect_timeout", cls.connect_timeout)),
            read_timeout=float(raw.get("read_timeout", cls.read_timeout)),
            head_bytes=int(raw.get("head_bytes", cls.head_bytes)),
            budget_seconds=float(raw.get("budget_seconds", cls.budget_seconds)),
            per_entry_seconds=float(raw.get("per_entry_seconds",
                                             cls.per_entry_seconds)),
            max_concurrency=max(1, int(raw.get("max_concurrency",
                                               cls.max_concurrency))),
        )


def _open(url: str, *, settings: PrecheckSettings, opener=None):
    """发起一次**受限** GET，只读前 ``settings.head_bytes`` 字节。

    绝不 ``read()`` 到完 —— 流可能几百 MB。
    """
    if opener is not None:
        return opener(url, settings)

    request = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Range": f"bytes=0-{max(0, settings.head_bytes - 1)}",
    })
    timeout = min(settings.connect_timeout, settings.read_timeout) + settings.read_timeout
    response = urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
    try:
        body = response.read(settings.head_bytes)
        return response, body
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001 — 关闭失败不影响结论
            pass


def _first_segment(master_text: str, base_url: str) -> str | None:
    """从 master playlist 里取**第一个**非注释行，解析成绝对 URL。

    只取一个（§17：「最多检查一个 media playlist 和一个 segment」），
    刻意不做完整解析 —— 预检不需要理解整份 master。
    """
    for raw_line in master_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        return urljoin(base_url, line)
    return None


def precheck_entry(
    url: str,
    *,
    index: int = 0,
    settings: PrecheckSettings | None = None,
    opener=None,
    authoritative: bool | None = True,
) -> PrecheckResult:
    """预检**一条**赛事 URL。分四步，任何一步超时都算 UNKNOWN。

    1. scheme 合法性（``http``/``https``/``rtsp``/``rtmp`` 之外 → FAIL）；
    2. HTTP GET，只读头部字节 → 状态码 / content-type 判定；
    3. content-type 或内容特征判定是不是「媒体/播放列表」；
       **HTTP 200 一律不等于 PASS**（§9 明令，HTML_FAKE 就是这么来的）；
    4. 若是 HLS，最多再看**一个** media playlist + **一个** segment。

    刻意**不**做：ffprobe、不长时间下载、不解析整份 master、不转码、不代理。
    """
    cfg = settings or PrecheckSettings()
    scheme = (urlsplit(url or "").scheme or "").lower()
    host = (urlsplit(url or "").hostname or None)

    def done(result: str, category: str, **kw) -> PrecheckResult:
        return PrecheckResult(
            entry_index=index, result=result, host=host or "",
            scheme=scheme or None, error_category=category,
            authoritative=authoritative, **kw,
        )

    if scheme not in ("http", "https", "rtsp", "rtmp"):
        # scheme 不支持是**结构性**结论（这个 URL 根本不可能在播放器里播），
        # 不是网络抖动 ⇒ FAIL 而非 UNKNOWN。
        return done(PRECHECK_FAIL, errors_mod.CONNECT_ERROR,
                    detail=f"不支持的 scheme: {scheme or '(空)'}")

    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    # ---- 步骤 2：HTTP GET ----
    try:
        response, body = _open(url, settings=cfg, opener=opener)
    except urllib.error.HTTPError as exc:
        # HTTPError 带状态码，本来是最可靠的一类判定。
        #
        # 🚨 但**代理**会把「我连不上」改写成「上游返回 5xx」：本机开着
        # FlClash 时，一个根本不存在的端口会得到 ``HTTP 502 Bad Gateway``，
        # 而不是 ConnectionRefused。生产机目前没有代理，但**代码不能假设**——
        # 一旦哪天部署环境有代理/Gateway，整轮赛事就会被误判成「源站 5xx」而
        # **整批删掉**（authoritative 源）。这正是 §17 最怕的误杀。
        #
        # 因此：可由代理产生的「网关类」状态码一律降级为 UNKNOWN。
        # 判据是**状态码本身**（502/503/504 是标准网关语义），不是猜。
        if exc.code in _PROXY_INDUCIBLE_STATUS:
            return done(PRECHECK_UNKNOWN, errors_mod.CONNECT_ERROR,
                        duration_ms=elapsed(),
                        detail=f"HTTP {exc.code}（网关类状态码，可能来自中间代理，"
                               f"不作为源站失败依据）")
        category = errors_mod.classify_stream_failure(
            stage="playlist", http_status=exc.code, error=str(exc))
        return done(PRECHECK_FAIL, category, duration_ms=elapsed(),
                    detail=f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        text = str(reason).lower()
        if "timed out" in text or "timeout" in text:
            category = errors_mod.TIMEOUT
        elif "name or service not known" in text or "nodename" in text \
                or "getaddrinfo" in text or "temporary failure in name" in text:
            category = errors_mod.DNS_ERROR
        else:
            # 连接类问题在预检阶段无法区分「源挂了」和「我方网络抖」⇒ UNKNOWN。
            return done(PRECHECK_UNKNOWN, errors_mod.CONNECT_ERROR,
                        duration_ms=elapsed(), detail="连接失败（无法区分源/网络）")
        return done(PRECHECK_UNKNOWN, category, duration_ms=elapsed(),
                    detail="连接失败")

    try:
        status = getattr(response, "status", None) or getattr(
            response, "code", None)
        headers = getattr(response, "headers", None)
        ctype = (headers.get("Content-Type", "") if headers else "") or ""
    finally:
        pass

    if status is not None and int(status) >= 400:
        category = errors_mod.classify_stream_failure(
            stage="playlist", http_status=int(status), error=f"HTTP {status}")
        return done(PRECHECK_FAIL, category, duration_ms=elapsed(),
                    detail=f"HTTP {status}")

    # ---- 步骤 3：内容判定（200 ≠ PASS）----
    head = body[: cfg.head_bytes]
    lowered = head[:512].lower()
    ctype_low = ctype.lower()

    looks_html = (
        any(marker in lowered for marker in _HTML_MARKERS)
        or "text/html" in ctype_low
    )
    if looks_html:
        return done(PRECHECK_FAIL, errors_mod.HTML_FAKE, duration_ms=elapsed(),
                    detail=f"返回 HTML（content-type={ctype_low or '未声明'}）")

    is_hls = any(marker in head for marker in _HLS_MARKERS) \
        or "mpegurl" in ctype_low or "m3u" in ctype_low
    if not is_hls:
        # 非 HLS：可能是裸 TS/FLV 等直连流。本预检**不**验证它的媒体有效性
        # （那属于 fixed 的 ffprobe 职责），只确认「拿到了东西且不是 HTML」。
        if not head:
            return done(PRECHECK_FAIL, errors_mod.EMPTY_MEDIA, duration_ms=elapsed(),
                        detail="响应体为空")
        return done(PRECHECK_PASS, errors_mod.UNKNOWN, duration_ms=elapsed(),
                    detail="直连媒体（非 HLS，未做深验证）")

    # ---- 步骤 4：HLS —— 一个 media playlist + 一个 segment ----
    try:
        media_text = head.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — decode 失败不该炸预检
        media_text = ""

    segment_url = _first_segment(media_text, url)
    if segment_url is None:
        # 是 HLS 结构但一个分片行都没有 ⇒ 结构非法（EMPTY_MEDIA 语义）。
        return done(PRECHECK_FAIL, errors_mod.EMPTY_MEDIA, duration_ms=elapsed(),
                    detail="HLS 播放列表内无任何分片行")

    if elapsed() / 1000.0 > cfg.per_entry_seconds:
        return done(PRECHECK_UNKNOWN, errors_mod.TIMEOUT, duration_ms=elapsed(),
                    segment_probed=False,
                    detail="预算内未完成分片检查")

    segment_ok: bool | None = None
    detail = "HLS 结构正常"
    result = PRECHECK_PASS
    category = errors_mod.UNKNOWN

    try:
        seg_response, seg_body = _open(segment_url, settings=cfg, opener=opener)
    except urllib.error.HTTPError as exc:
        seg_status = exc.code
        if seg_status in _PROXY_INDUCIBLE_STATUS:
            result = PRECHECK_UNKNOWN
            category = errors_mod.CONNECT_ERROR
            detail = f"分片 HTTP {seg_status}（网关类，不作为源站失败依据）"
            segment_ok = None
        else:
            result = PRECHECK_FAIL
            category = errors_mod.classify_stream_failure(
                stage="segment", http_status=seg_status, error=str(exc), segment=True)
            detail = f"分片 HTTP {seg_status}"
            segment_ok = False
    except (urllib.error.URLError, OSError) as exc:
        text = str(getattr(exc, "reason", exc)).lower()
        result = PRECHECK_UNKNOWN
        category = errors_mod.TIMEOUT if ("timed out" in text or "timeout" in text) \
            else errors_mod.SEGMENT_UNREACHABLE
        detail = "分片不可达（无法区分源/网络）"
        segment_ok = None
    else:
        seg_status = getattr(seg_response, "status", None) or getattr(
            seg_response, "code", None)
        if seg_status is not None and int(seg_status) >= 400:
            result = PRECHECK_FAIL
            category = errors_mod.classify_stream_failure(
                stage="segment", http_status=int(seg_status), segment=True)
            detail = f"分片 HTTP {seg_status}"
            segment_ok = False
        else:
            probe_head = seg_body[:SEGMENT_HEAD_BYTES]
            if any(marker in probe_head[:256].lower() for marker in _HTML_MARKERS):
                result = PRECHECK_FAIL
                category = errors_mod.HTML_FAKE
                detail = "分片返回 HTML"
                segment_ok = False
            elif not seg_body:
                result = PRECHECK_FAIL
                category = errors_mod.EMPTY_MEDIA
                detail = "分片 0 字节"
                segment_ok = False
            else:
                # §17 只要求「拿到一个分片」。不校验 0x47 同步字节 ——
                # 那是 fixed 的 Level 3 深探测做的事，本轮明确不做。
                result = PRECHECK_PASS
                category = errors_mod.UNKNOWN
                detail = f"分片可达（{len(seg_body)}B）"
                segment_ok = True

    return done(result, category, duration_ms=elapsed(), segment_probed=True,
                segment_ok=segment_ok, detail=detail)


def precheck_entries(
    urls: list[str],
    *,
    settings: PrecheckSettings | None = None,
    opener=None,
    authoritative_for=None,
) -> list[PrecheckResult]:
    """并发预检一批 URL，**总预算硬上限**（§17「总预算必须可控」）。

    ``authoritative_for`` 是个 ``index -> bool|None`` 的判定函数（或可调用对象），
    用来按来源注入 authoritative / advisory（§18）。默认全部 authoritative=True。

    预算耗尽后，**剩余条目直接标 UNKNOWN 且不发请求** —— 宁可「不知道」，
    也不突破预算或把超时猜成 FAIL。
    """
    cfg = settings or PrecheckSettings()
    deadline = time.perf_counter() + cfg.budget_seconds
    results: list[PrecheckResult | None] = [None] * len(urls)

    def authoritative_at(index: int) -> bool | None:
        if authoritative_for is None:
            return True
        value = authoritative_for(index)
        return True if value is None else bool(value)

    def work(index: int) -> PrecheckResult:
        if time.perf_counter() >= deadline and cfg.stop_on_budget:
            return PrecheckResult(
                entry_index=index, result=PRECHECK_UNKNOWN, host="",
                scheme=None, error_category=errors_mod.TIMEOUT,
                authoritative=authoritative_at(index), duration_ms=0,
                detail="整轮预算耗尽，未检查",
            )
        return precheck_entry(
            urls[index], index=index, settings=cfg, opener=opener,
            authoritative=authoritative_at(index),
        )

    if not urls:
        return []

    workers = max(1, min(cfg.max_concurrency, len(urls)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, i): i for i in range(len(urls))}
        for future in concurrent.futures.as_completed(
            futures, timeout=max(1.0, cfg.budget_seconds + cfg.per_entry_seconds)
        ):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 — 单条异常不得拖垮整轮
                results[index] = PrecheckResult(
                    entry_index=index, result=PRECHECK_UNKNOWN, host="",
                    scheme=None, error_category=errors_mod.UNKNOWN,
                    authoritative=authoritative_at(index), duration_ms=None,
                    detail=f"预检异常：{type(exc).__name__}",
                )

    # 极端情况下（线程池超时）仍可能有 None —— fail-closed 成 UNKNOWN。
    return [
        r if r is not None else PrecheckResult(
            entry_index=i, result=PRECHECK_UNKNOWN, host="", scheme=None,
            error_category=errors_mod.UNKNOWN, authoritative=authoritative_at(i),
            duration_ms=None, detail="预检未完成",
        )
        for i, r in enumerate(results)
    ]


def apply_policy(results: list[PrecheckResult], results_authoritative: bool) -> dict:
    """把一组预检结论按来源 policy 归成**可执行的决定**。

    ``results_authoritative=False``（KORICE）时，**所有** ``PRECHECK_FAIL``
    都降级为 advisory：计入 ``advisory_fail``，``excluded`` 恒为 False。

    返回::

        {
          "excluded_indexes": [...],   # 本轮应剔除的条目下标
          "kept_indexes": [...],       # 保留（含 UNKNOWN）
          "counts": {"pass": n, "fail": n, "unknown": n},
          "advisory_fail": n,          # 仅统计，不影响 excluded
          "authoritative": bool,
        }
    """
    excluded: list[int] = []
    kept: list[int] = []
    counts = {key: 0 for key in ALL_RESULTS}
    advisory_fail = 0

    for item in results:
        counts[item.result] = counts.get(item.result, 0) + 1
        # policy 以**来源级**为准：单条传入的 authoritative 只是默认值。
        authoritative = results_authoritative and bool(item.authoritative is not False)
        if item.result == PRECHECK_FAIL:
            if authoritative:
                excluded.append(item.entry_index)
                continue
            advisory_fail += 1
        # UNKNOWN 永远保留（§17：不猜、不误杀）。
        kept.append(item.entry_index)

    return {
        "excluded_indexes": excluded,
        "kept_indexes": kept,
        "counts": counts,
        "advisory_fail": advisory_fail,
        "authoritative": bool(results_authoritative),
    }


def summarize(results: list[PrecheckResult], decision: dict | None = None) -> dict:
    """按来源汇总（进 publish dynamic_summary / reliability-summary）。

    **不含任何 URL**：只有 host、结果计数、错误类别分布。
    """
    counts = {key: 0 for key in ALL_RESULTS}
    cats = errors_mod.distribution(
        r.error_category for r in results if r.result != PRECHECK_PASS
    )
    for item in results:
        counts[item.result] = counts.get(item.result, 0) + 1

    payload = {
        "checked": len(results),
        "pass": counts[PRECHECK_PASS],
        "fail": counts[PRECHECK_FAIL],
        "unknown": counts[PRECHECK_UNKNOWN],
        "error_distribution": cats,
    }
    if decision:
        payload["excluded"] = len(decision.get("excluded_indexes") or [])
        payload["advisory_fail"] = decision.get("advisory_fail", 0)
        payload["authoritative"] = decision.get("authoritative")
    return payload
