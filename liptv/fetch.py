"""最小 HTTP(S) 文本拉取层（TASK-002）。

职责边界（刻意收窄）：
  * 只拉取**被登记来源的 M3U 文本**；
  * **绝不**对条目里的视频流 URL 发起任何请求、播放探测或 HEAD 试探；
  * 不做无限重定向、不读无限大响应、不无限等待；
  * 失败一律分成可枚举的类别，交给调用方决定是否落库。

本模块只依赖标准库，不引入第三方 HTTP 客户端。
"""

from __future__ import annotations

import dataclasses
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

# ---------------------------------------------------------------- 失败分类
# 这些字符串就是写入 source.last_fetch_status 的口径，测试与报告都以此为准。
ERROR_HTTP_STATUS = "HTTP_STATUS"          # 响应非 2xx
ERROR_NETWORK = "NETWORK_ERROR"            # DNS / 连接被拒 / TLS 等
ERROR_TIMEOUT = "TIMEOUT"                  # 连接或读取超时
ERROR_TOO_MANY_REDIRECTS = "TOO_MANY_REDIRECTS"
ERROR_TOO_LARGE = "RESPONSE_TOO_LARGE"     # 超过 max_bytes
ERROR_DECODE = "DECODE_ERROR"              # 文本解码失败
ERROR_INVALID_M3U = "INVALID_M3U"          # 结构上不是 M3U
ERROR_EMPTY_LIST = "EMPTY_LIST"            # 是 M3U 结构但没有任何条目
ERROR_UNKNOWN = "UNKNOWN_ERROR"
NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"  # 手工 smoke 用：本机网络不可达

ALL_ERROR_CATEGORIES = (
    ERROR_HTTP_STATUS,
    ERROR_NETWORK,
    ERROR_TIMEOUT,
    ERROR_TOO_MANY_REDIRECTS,
    ERROR_TOO_LARGE,
    ERROR_DECODE,
    ERROR_INVALID_M3U,
    ERROR_EMPTY_LIST,
    ERROR_UNKNOWN,
)

DEFAULT_USER_AGENT = "liptv/1.0 (+personal IPTV aggregator; python-urllib)"

# 只接受这两种协议；任何 file:// / ftp:// 等一律拒绝
ALLOWED_SCHEMES = ("http", "https")

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

_REDIRECT_STATUS = (301, 302, 303, 307, 308)


class FetchError(RuntimeError):
    """一次拉取/校验失败，携带可枚举的分类。"""

    def __init__(self, category: str, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.category = category
        self.http_status = http_status


@dataclasses.dataclass(frozen=True)
class FetchLimits:
    """拉取限制，全部来自配置（见 config.example.toml [fetch]）。"""

    timeout_seconds: float = 10.0
    max_bytes: int = 5_000_000
    max_redirects: int = 3
    user_agent: str = DEFAULT_USER_AGENT

    @classmethod
    def from_mapping(cls, mapping: dict | None) -> "FetchLimits":
        raw = mapping or {}
        return cls(
            timeout_seconds=float(raw.get("timeout_seconds", cls.timeout_seconds)),
            max_bytes=int(raw.get("max_bytes", cls.max_bytes)),
            max_redirects=int(raw.get("max_redirects", cls.max_redirects)),
            user_agent=str(raw.get("user_agent", cls.user_agent)),
        )


@dataclasses.dataclass
class FetchedText:
    """一次成功的文本拉取结果。"""

    url: str
    status: int
    content_type: str | None
    charset: str | None
    byte_count: int
    text: str


class _LimitedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """把 urllib 默认的 10 次重定向收敛为配置值。"""

    def __init__(self, max_redirects: int):
        super().__init__()
        self.max_redirections = int(max_redirects)
        self.max_repeats = 1


def _build_opener(limits: FetchLimits, url: str) -> urllib.request.OpenerDirector:
    """构造 opener。

    对 loopback（本地 mock / 手工演示）显式清空代理，避免本机 HTTP_PROXY 干扰；
    对外部地址保持 urllib 的默认代理行为（环境变量 HTTP_PROXY / HTTPS_PROXY）。
    """
    handlers: list[urllib.request.BaseHandler] = [
        _LimitedRedirectHandler(limits.max_redirects)
    ]
    hostname = (urlsplit(url).hostname or "").lower()
    if hostname in _LOOPBACK_HOSTS:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _decode(raw: bytes, charset: str | None) -> str:
    """按声明 charset 解码，失败再试 utf-8；仍失败 → DECODE_ERROR。"""
    declared = (charset or "utf-8").strip().lower() or "utf-8"
    if declared in ("utf8", "utf-8-sig"):
        declared = "utf-8"
    for candidate in dict.fromkeys([declared, "utf-8"]):
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    raise FetchError(
        ERROR_DECODE,
        f"响应体无法按 {declared!r}/utf-8 解码（{len(raw)} bytes）",
    )


def fetch_text(
    url: str,
    *,
    limits: FetchLimits | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> FetchedText:
    """拉取一个 URL 的文本内容。

    抛 FetchError（带 category）；成功时返回 FetchedText。
    这里**不会**去请求条目里的播放地址 —— 只请求传入的这一个 URL。
    """
    limits = limits or FetchLimits()

    parts = urlsplit(url or "")
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname:
        raise FetchError(
            ERROR_NETWORK,
            f"只接受 http/https 且带主机的 URL，收到：{url!r}",
        )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": limits.user_agent,
            "Accept": "application/x-mpegURL, application/vnd.apple.mpegurl, "
                      "audio/x-mpegurl, text/plain, */*",
        },
    )
    director = opener or _build_opener(limits, url)

    try:
        with director.open(request, timeout=limits.timeout_seconds) as response:
            status = int(getattr(response, "status", 0) or 0)
            headers = response.headers
            content_type = headers.get_content_type() if headers else None
            charset = headers.get_content_charset() if headers else None
            # 多读 1 字节，用于判定「超过上限」
            raw = response.read(limits.max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in _REDIRECT_STATUS:
            raise FetchError(
                ERROR_TOO_MANY_REDIRECTS,
                f"重定向次数超过上限 {limits.max_redirects}（最后一次状态 {exc.code}）",
                http_status=int(exc.code),
            ) from exc
        raise FetchError(
            ERROR_HTTP_STATUS,
            f"HTTP {exc.code} {exc.reason}",
            http_status=int(exc.code),
        ) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
            raise FetchError(
                ERROR_TIMEOUT, f"请求超时（{limits.timeout_seconds}s）：{reason}"
            ) from exc
        raise FetchError(ERROR_NETWORK, f"网络错误：{reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise FetchError(
            ERROR_TIMEOUT, f"请求超时（{limits.timeout_seconds}s）：{exc}"
        ) from exc
    except OSError as exc:  # 连接被拒、DNS 失败等
        raise FetchError(ERROR_NETWORK, f"网络错误：{exc}") from exc

    if len(raw) > limits.max_bytes:
        raise FetchError(
            ERROR_TOO_LARGE,
            f"响应体超过上限 {limits.max_bytes} bytes（已读取 {limits.max_bytes + 1}+）",
        )

    return FetchedText(
        url=url,
        status=status,
        content_type=content_type,
        charset=charset,
        byte_count=len(raw),
        text=_decode(raw, charset),
    )
