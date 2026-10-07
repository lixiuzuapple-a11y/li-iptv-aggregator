"""统一错误分类（TASK-012 §9）。

设计约束（照抄任务书，不自行发挥）：

  * **不改 schema V1**。probe_result / fetch / precheck 各自已有的 ``error_type`` /
    ``category`` 字符串一律**原样保留**，本模块只做「派生归一」——
    :func:`normalize` 把它们映射到本文件定义的**一套**术语。
  * probe、report、status **共用同一口径**：任何地方要说「这条流为什么挂」，
    都必须经过 :func:`normalize`，不允许各自造词。
  * 归一时**绝不发明事实**：输入里没有的信息（HTTP 状态码、正文特征）不会被猜出来。
    缺证据就落到 :data:`UNKNOWN` 或语义最接近的上位类。

§9 规定的统一术语（14 个）::

    DNS_ERROR  CONNECT_ERROR  TIMEOUT  HTTP_4XX  HTTP_5XX
    HTML_FAKE  INVALID_PLAYLIST  INVALID_MEDIA  SEGMENT_UNREACHABLE
    SIGNED_DOWNSTREAM_UNREACHABLE  EMPTY_MEDIA  GEO_BLOCK_SUSPECTED
    ENVIRONMENT_ERROR  UNKNOWN

两处与旧口径的**有意**差异，写在这里免得以后有人当 bug 修：

  1. 旧 ``probe.ERROR_TLS``（``TLS_ERROR``）不在 §9 列表里。TLS 握手失败在真实
     原因上就是「连不上」，且几乎总是伴随证书/协议不匹配，归到
     ``CONNECT_ERROR``；只有当调用方能明确区分时才允许直接写 ``CONNECT_ERROR``。
     映射表把 TLS 明确并入 CONNECT_ERROR，不新造类别。
  2. 旧 ``probe.ERROR_HTTP`` 把 4xx/5xx 混成一个词，而 TASK-011 已经证明
     「playlist 返回 200 但内容是 HTML」是本项目最常见的坏流形态，必须能和
     4xx/5xx 分开看，否则 §26 的 fake-200 统计做不出来。所以
     :func:`classify_probe_failure` 在**有** ``http_status`` 时精确分 4xx/5xx，
     没有时（ffprobe 常常不给状态码）保守落 ``HTTP_4XX`` 之外的上位选择——
     具体见该函数 docstring。
"""

from __future__ import annotations

# ------------------------------------------------------------------ §9 统一术语

DNS_ERROR = "DNS_ERROR"
CONNECT_ERROR = "CONNECT_ERROR"
TIMEOUT = "TIMEOUT"
HTTP_4XX = "HTTP_4XX"
HTTP_5XX = "HTTP_5XX"
HTML_FAKE = "HTML_FAKE"
INVALID_PLAYLIST = "INVALID_PLAYLIST"
INVALID_MEDIA = "INVALID_MEDIA"
SEGMENT_UNREACHABLE = "SEGMENT_UNREACHABLE"
SIGNED_DOWNSTREAM_UNREACHABLE = "SIGNED_DOWNSTREAM_UNREACHABLE"
EMPTY_MEDIA = "EMPTY_MEDIA"
GEO_BLOCK_SUSPECTED = "GEO_BLOCK_SUSPECTED"
ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"
UNKNOWN = "UNKNOWN"

#: §9 全集。顺序即报告里的展示顺序（从具体到宽泛）。
ALL_CATEGORIES: tuple[str, ...] = (
    DNS_ERROR,
    CONNECT_ERROR,
    TIMEOUT,
    HTTP_4XX,
    HTTP_5XX,
    HTML_FAKE,
    INVALID_PLAYLIST,
    INVALID_MEDIA,
    SEGMENT_UNREACHABLE,
    SIGNED_DOWNSTREAM_UNREACHABLE,
    EMPTY_MEDIA,
    GEO_BLOCK_SUSPECTED,
    ENVIRONMENT_ERROR,
    UNKNOWN,
)

#: 人读标签。给报告 / reliability-status / CLI 表格用，避免到处散落中文。
CATEGORY_LABELS: dict[str, str] = {
    DNS_ERROR: "域名解析失败",
    CONNECT_ERROR: "连接失败（拒绝/重置/无路由/TLS）",
    TIMEOUT: "超时",
    HTTP_4XX: "HTTP 4xx",
    HTTP_5XX: "HTTP 5xx",
    HTML_FAKE: "假成功（HTTP 200 但内容不是媒体/播放列表）",
    INVALID_PLAYLIST: "播放列表结构非法",
    INVALID_MEDIA: "媒体无效（无法解析出音视频）",
    SEGMENT_UNREACHABLE: "分片不可达",
    SIGNED_DOWNSTREAM_UNREACHABLE: "签名下游不可达",
    EMPTY_MEDIA: "空媒体（0 字节 / 无数据）",
    GEO_BLOCK_SUSPECTED: "疑似地域限制",
    ENVIRONMENT_ERROR: "环境级故障（非本条流的问题）",
    UNKNOWN: "未归类",
}

#: 环境级类别：出现它意味着**这一轮整体不可信**，绝不批量写成「每条流都挂」。
ENVIRONMENT_CATEGORIES: frozenset[str] = frozenset({ENVIRONMENT_ERROR})


def label(category: str | None) -> str:
    """给人类看的短标签；未登记的类别原样返回（便于发现拼写错误）。"""
    key = (category or UNKNOWN).strip() or UNKNOWN
    return CATEGORY_LABELS.get(key, key)


def is_environment(category: str | None) -> bool:
    """该类别是否代表环境级故障。"""
    return (category or "").strip() in ENVIRONMENT_CATEGORIES


# ------------------------------------------------------------------ 旧 → 统一

#: probe.py 的 11 类 → §9 术语。
#:
#: ``HTTP_ERROR`` / ``OUTPUT_INVALID`` / ``PROCESS_ERROR`` 都是**没有 HTTP 状态码**
#: 时 ffprobe 能给出的全部信息，映射时不做 4xx/5xx 的无证据细分。
_PROBE_MAP: dict[str, str] = {
    "FFPROBE_NOT_FOUND": ENVIRONMENT_ERROR,
    "FFPROBE_START_FAILED": ENVIRONMENT_ERROR,
    "TIMEOUT": TIMEOUT,
    "DNS_ERROR": DNS_ERROR,
    "CONNECT_ERROR": CONNECT_ERROR,
    # §9 无 TLS 类别：握手失败在真实原因上属「连不上」。
    "TLS_ERROR": CONNECT_ERROR,
    # 精确细分留给 :func:`classify_probe_failure`（那里可能有状态码）。
    "HTTP_ERROR": HTTP_4XX,
    "INVALID_MEDIA": INVALID_MEDIA,
    "PROCESS_ERROR": CONNECT_ERROR,
    "OUTPUT_INVALID": INVALID_PLAYLIST,
    "UNKNOWN": UNKNOWN,
}

#: fetch.py 的 9 类 → §9 术语。
_FETCH_MAP: dict[str, str] = {
    "HTTP_STATUS": HTTP_4XX,  # 有状态码时由 classify_http_status 细分
    "NETWORK_ERROR": CONNECT_ERROR,
    "TIMEOUT": TIMEOUT,
    "TOO_MANY_REDIRECTS": HTTP_4XX,
    "RESPONSE_TOO_LARGE": INVALID_PLAYLIST,
    "DECODE_ERROR": INVALID_PLAYLIST,
    "INVALID_M3U": INVALID_PLAYLIST,
    "EMPTY_LIST": EMPTY_MEDIA,
    "UNKNOWN_ERROR": UNKNOWN,
}


def classify_http_status(status: int | None) -> str:
    """按 HTTP 状态码归类。**没有状态码就返回 UNKNOWN**，绝不猜 4xx/5xx。"""
    if status is None:
        return UNKNOWN
    try:
        code = int(status)
    except (TypeError, ValueError):
        return UNKNOWN
    if 400 <= code < 500:
        return HTTP_4XX
    if 500 <= code < 600:
        return HTTP_5XX
    # 2xx / 3xx 不该出现在「失败」语境里；仍归 UNKNOWN 而不是硬塞 4xx。
    return UNKNOWN


def normalize(raw: str | None, *, http_status: int | None = None) -> str:
    """把任意来源的错误类别归一到 §9 术语。

    ``raw`` 允许是本模块已定义的值（幂等）、probe 的旧值、fetch 的旧值，
    或完全陌生的字符串（→ ``UNKNOWN``）。

    ``http_status`` 只在 ``raw`` **本身没有**类别信息时用来细分
    ``HTTP_STATUS`` / ``HTTP_ERROR``；已明确是 DNS/TIMEOUT 的不会被状态码覆盖。
    """
    key = (raw or "").strip()

    if key in ALL_CATEGORIES:
        return key

    if key in _PROBE_MAP or key in _FETCH_MAP:
        mapped = _PROBE_MAP.get(key) or _FETCH_MAP[key]
        if key in ("HTTP_ERROR", "HTTP_STATUS") and http_status is not None:
            detailed = classify_http_status(http_status)
            if detailed != UNKNOWN:
                return detailed
        return mapped

    # 大小写兜底：旧代码/手写配置里出现过小写形式。
    upper = key.upper()
    if upper in ALL_CATEGORIES:
        return upper
    if upper in _PROBE_MAP:
        return _PROBE_MAP[upper]
    if upper in _FETCH_MAP:
        return _FETCH_MAP[upper]

    return UNKNOWN


def _http_status_from_text(text: str) -> int | None:
    """从 ffprobe stderr 里抠出真实状态码；抠不到就返回 None（绝不猜）。

    真实 ffprobe 6.x 的两种文案都要认::

        Server returned 404 Not Found
        HTTP error 404 Not Found
    """
    for marker in ("server returned ", "http error "):
        idx = text.find(marker)
        if idx < 0:
            continue
        digits = ""
        for ch in text[idx + len(marker):]:
            if ch.isdigit():
                digits += ch
            elif digits:
                break
            else:
                break
        if digits:
            try:
                return int(digits)
            except ValueError:
                return None
    return None


def _geo_suspected(text: str) -> bool:
    """文案层面的地域限制证据（403/451 只在同时像「拒绝」时才判 geo）。"""
    return (
        "geo block" in text
        or "geo restricted" in text
        or "geo-restricted" in text
        or "forbidden by geo" in text
        or "not available in your country" in text
        or "outside the region" in text
    )


def classify_probe_failure(
    *,
    timed_out: bool,
    error_type: str | None,
    stderr: str = "",
    http_status: int | None = None,
    segment: bool = False,
) -> str:
    """在 :func:`normalize` 之上叠加 ffprobe stderr 的**明确特征**。

    只在 stderr 出现**明确字面证据**时才升级分类，绝不「推测」。
    识别顺序（先具体后宽泛）：

    ============================  ==========================================
    证据                          归类
    ============================  ==========================================
    超时标志                      ``TIMEOUT``
    状态码 403/451 或 geo 文案     ``GEO_BLOCK_SUSPECTED``
    ``<html``/``<!doctype``/html ctype  ``HTML_FAKE``
    404/410 且 ``segment=True``    ``SEGMENT_UNREACHABLE``
    4xx / 5xx 状态码              ``HTTP_4XX`` / ``HTTP_5XX``
    其余旧 error_type             :func:`normalize`
    ============================  ==========================================

    ⚠️ ``404 → SEGMENT_UNREACHABLE`` 只在**调用方明确声明 ``segment=True``** 时
    才生效。对主播放列表 URL 而言 404 就是普通 4xx —— TASK-011 §13 的教训正是
    「``27 条 playlist 正常但分片全 404``」，不区分就会把「源站整条挂掉」和
    「分片索引对不上」混成同一类。

    ⚠️ 403 判成 ``GEO_BLOCK_SUSPECTED`` 而非 ``HTTP_4XX``：对本项目（上海云 +
    家庭网络）而言 403 绝大多数就是地域/防盗链，不是「频道不存在」。
    这是**可解释的判断规则**，不是 ML 猜测，且有 regression test 固定住。
    """
    if timed_out:
        return TIMEOUT

    text = (stderr or "").lower()
    status = http_status if http_status is not None else _http_status_from_text(text)

    if _geo_suspected(text) or status in (403, 451):
        return GEO_BLOCK_SUSPECTED

    if "<html" in text or "<!doctype html" in text or "text/html" in text:
        return HTML_FAKE

    if segment and status in (404, 410):
        return SEGMENT_UNREACHABLE

    detailed = classify_http_status(status)
    if detailed != UNKNOWN:
        return detailed

    return normalize(error_type, http_status=status)


def classify_stream_failure(
    *,
    stage: str,
    error: str = "",
    http_status: int | None = None,
    content_type: str = "",
    body_head: str = "",
    segment: bool = False,
) -> str:
    """把「轻量 HTTP 层检查」的失败归类（preflight 与深 probe 共用）。

    与 :func:`classify_probe_failure` 的区别：这里我们自己拿到了 HTTP 响应，
    所以**可以**依据真实 status / content-type 判定，而不必从 ffprobe 文本里猜。

    ``stage`` 取值：

    * ``resolve`` —— DNS 阶段失败 ⇒ DNS_ERROR / TIMEOUT；
    * ``connect`` —— TCP/TLS 阶段失败 ⇒ CONNECT_ERROR / TIMEOUT；
    * ``playlist`` —— 主播放列表请求失败；
    * ``media``    —— HLS media playlist 失败；
    * ``segment``  —— 分片请求失败（``segment=True`` 是显式声明，不是从 stage 推断，
      因为调用方可能已经知道这是分片 URL）。

    ``body_head`` 只取**响应体开头少量字符**用于识别 HTML；调用方负责截断，
    本函数不会去读全量 body，也不会把它写进任何输出。
    """
    stage = (stage or "").strip().lower()
    text = (error or "").lower()

    if stage == "resolve":
        if "timed out" in text or "timeout" in text:
            return TIMEOUT
        return DNS_ERROR

    if stage == "connect":
        if "timed out" in text or "timeout" in text:
            return TIMEOUT
        return CONNECT_ERROR

    if http_status is not None:
        # 403/451 与 :func:`classify_probe_failure` 保持一致 → GEO_BLOCK_SUSPECTED。
        # §9 要求 probe / preflight / report **同一口径**；同名不同义比不准更糟。
        # 类别名本身带 SUSPECTED —— 它表达「疑似」，不宣称已证明是地域限制。
        if int(http_status) in (403, 451):
            return GEO_BLOCK_SUSPECTED
        detailed = classify_http_status(http_status)
        if detailed != UNKNOWN:
            if segment and detailed == HTTP_4XX and int(http_status) in (404, 410):
                return SEGMENT_UNREACHABLE
            return detailed

    head = (body_head or "")[:400].lower()
    ctype = (content_type or "").lower()
    if "html" in ctype or "<html" in head or "<!doctype" in head:
        return HTML_FAKE

    if stage == "segment":
        return SEGMENT_UNREACHABLE
    if stage in ("playlist", "media"):
        return INVALID_PLAYLIST

    return UNKNOWN


def distribution(categories) -> dict[str, int]:
    """把任意可迭代的类别序列整理成 ``{类别: 次数}``。

    * 空类别（``None`` / 空串）计入 ``UNKNOWN`` —— 报告里「有一批失败但没写类别」
      必须看得见，不能悄悄消失。
    * 只保留出现过的类别，顺序按 :data:`ALL_CATEGORIES`，保证报告稳定可比。
    """
    counts: dict[str, int] = {}
    for item in categories or ():
        key = normalize(item if isinstance(item, str) else None)
        counts[key] = counts.get(key, 0) + 1
    return {k: counts[k] for k in ALL_CATEGORIES if k in counts}
