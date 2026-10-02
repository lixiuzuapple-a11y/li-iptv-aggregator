"""只读订阅服务的机器可判定健康检查（TASK-006）。

设计约束（与 TASK-004 / TASK-005 已冻结语义对齐）：

* ``/healthz`` **总是**返回 HTTP 200，业务状态在 JSON 的 ``status`` 字段里
  （``ok`` / ``stale`` / ``missing``）。因此「systemd active」或「HTTP 200」都**不等于**
  业务健康 —— 本模块一律要求 ``status == "ok"`` 才算健康。
* ``/live.m3u`` 在文件缺失或为空时返回 **503**（TASK-004 冻结口径）。本模块把
  503 如实判为**不健康**，绝不把「缺失」粉饰成成功。
* 只请求这两个只读路由；**不代理、不请求任何媒体流**。
* 默认只允许 loopback：对外暴露由外层 VPN / 反向代理负责（TASK-006 §7）。
* **永不输出播放列表正文**（里面含上游线路地址与短时签名）。返回值里只有
  字节数 / 状态码 / 状态字段。
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

#: 健康检查只看这两个路由（与 TASK-004 冻结路由一致）
DEFAULT_HEALTH_PATH = "/healthz"
DEFAULT_PLAYLIST_PATH = "/live.m3u"

#: ``/healthz`` 的 ``status`` 取值（TASK-004 冻结）
HEALTH_STATUS_OK = "ok"
HEALTH_STATUS_STALE = "stale"
HEALTH_STATUS_MISSING = "missing"

_DEFAULT_USER_AGENT = "liptv-healthcheck/1.0 (+local; python-urllib)"


class HealthError(RuntimeError):
    """健康检查本身无法进行（地址非法 / 非 loopback 且未显式放行）。"""


@dataclasses.dataclass(frozen=True)
class HealthResult:
    """一次健康检查的结构化结果（**不含任何 URL / token / 播放列表正文**）。"""

    ok: bool
    base_url: str
    health_http_status: int | None = None
    health_status: str | None = None
    freshness_source: str | None = None
    last_success_publish_at: str | None = None
    seconds_since_last_success: int | None = None
    playlist_http_status: int | None = None
    playlist_bytes: int | None = None
    attempts: int = 1
    elapsed_seconds: float = 0.0
    detail: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def is_loopback_host(host: str) -> bool:
    """判断主机名是否 loopback（``localhost`` / ``127.0.0.0/8`` / ``::1``）。"""
    text = (host or "").strip().strip("[]").lower()
    if text in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def normalize_base_url(url: str) -> str:
    """把用户给的地址规范成 ``scheme://host[:port]``（丢掉 path/query）。"""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise HealthError(f"健康检查地址无法解析：{url!r}（应形如 http://127.0.0.1:8080）")
    if parts.username or parts.password:
        raise HealthError("健康检查地址不得携带用户名/口令")
    netloc = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    if ":" in parts.hostname:  # IPv6 需要方括号
        netloc = f"[{parts.hostname}]" + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{netloc}"


def _http_get(url: str, *, timeout: float) -> tuple[int | None, bytes, str]:
    """返回 ``(状态码, 正文, 说明)``；网络层失败时状态码为 ``None``。"""
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": _DEFAULT_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 只打已知的本地只读地址
            # 只读回有限字节：健康 JSON 很小，播放列表只取头部用于判空
            body = response.read(4096)
            return int(response.status), body, ""
    except urllib.error.HTTPError as exc:
        try:
            exc.read(1024)
        except Exception:  # pragma: no cover - 连接已坏
            pass
        return int(exc.code), b"", f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, b"", f"连接失败：{type(exc.reason).__name__}"
    except OSError as exc:  # pragma: no cover - 极少数平台异常
        return None, b"", f"连接失败：{type(exc).__name__}"


def check_once(
    base_url: str,
    *,
    health_path: str = DEFAULT_HEALTH_PATH,
    playlist_path: str = DEFAULT_PLAYLIST_PATH,
    timeout: float = 2.0,
    require_playlist: bool = True,
) -> HealthResult:
    """打一次：先 ``/healthz``，再 ``/live.m3u``。

    健康条件（**必须同时满足**）：

    1. ``/healthz`` HTTP 200 且 JSON 可解析且 ``status == "ok"``；
    2. ``require_playlist`` 时 ``/live.m3u`` HTTP 200 且正文非空（503 ⇒ 不健康）。

    ``stale`` / ``missing`` 一律判为不健康，但不额外制造失败原因 —— 原始状态如实带回。
    """
    base = normalize_base_url(base_url)
    started = time.perf_counter()

    code, body, detail = _http_get(base + health_path, timeout=timeout)
    result = HealthResult(ok=False, base_url=base, health_http_status=code, detail=detail)

    health_status = None
    freshness_source = None
    last_success = None
    seconds_since = None
    if code == 200:
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            document = None
        if isinstance(document, dict):
            health_status = document.get("status")
            freshness = document.get("freshness") or {}
            if isinstance(freshness, dict):
                freshness_source = freshness.get("source")
                last_success = freshness.get("last_success_publish_at")
                seconds_since = freshness.get("seconds_since_last_success")

    playlist_code = None
    playlist_bytes = None
    if require_playlist:
        playlist_code, playlist_body, playlist_detail = _http_get(base + playlist_path, timeout=timeout)
        playlist_bytes = len(playlist_body) if playlist_body is not None else None
        if playlist_code is not None and playlist_code != 200:
            detail = detail or playlist_detail

    ok = code == 200 and health_status == HEALTH_STATUS_OK
    if ok and require_playlist:
        # 非空播放列表：503（缺失/空文件）或 0 字节都算不健康
        ok = bool(playlist_code == 200 and playlist_bytes)

    if not result.detail:
        if code is None:
            result.detail = "无法连接"
        elif health_status is None:
            result.detail = f"/healthz HTTP {code} 但正文不可解析"
        elif health_status != HEALTH_STATUS_OK:
            result.detail = f"业务状态为 {health_status}（不是 ok）"
        elif require_playlist and not ok:
            result.detail = f"/live.m3u HTTP {playlist_code}"

    return dataclasses.replace(
        result,
        ok=ok,
        health_status=health_status,
        freshness_source=freshness_source,
        last_success_publish_at=last_success,
        seconds_since_last_success=seconds_since,
        playlist_http_status=playlist_code,
        playlist_bytes=playlist_bytes,
        elapsed_seconds=round(time.perf_counter() - started, 3),
    )


def wait_until_healthy(
    base_url: str,
    *,
    timeout: float = 30.0,
    interval: float = 1.0,
    health_path: str = DEFAULT_HEALTH_PATH,
    playlist_path: str = DEFAULT_PLAYLIST_PATH,
    request_timeout: float = 2.0,
    require_playlist: bool = True,
    allow_non_loopback: bool = False,
    sleep=time.sleep,
) -> HealthResult:
    """轮询直到健康或超时；返回**最后一次**结果（超时即 ``ok=False``）。

    部署脚本用它做「start 之后必须真的健康才算成功」的闸门：``missing`` / ``stale``
    都不可能通过，因此「systemd active」不会被误当成业务成功。
    """
    base = normalize_base_url(base_url)
    host = urlsplit(base).hostname or ""
    if not allow_non_loopback and not is_loopback_host(host):
        raise HealthError(
            f"拒绝对非 loopback 地址做健康检查：{host!r}（默认只检查本机；"
            f"确实要检查远端请显式放行）"
        )

    deadline = time.perf_counter() + max(0.0, float(timeout))
    attempts = 0
    started = time.perf_counter()
    last: HealthResult | None = None
    while True:
        attempts += 1
        last = check_once(
            base,
            health_path=health_path,
            playlist_path=playlist_path,
            timeout=request_timeout,
            require_playlist=require_playlist,
        )
        if last.ok:
            break
        if time.perf_counter() >= deadline:
            break
        sleep(max(0.05, float(interval)))

    assert last is not None  # 循环至少跑一次
    return dataclasses.replace(
        last, attempts=attempts, elapsed_seconds=round(time.perf_counter() - started, 3)
    )


def health_base_url(host: str, port: int) -> str:
    """由 ``[server] host/port`` 生成健康检查基地址（``0.0.0.0`` 归一成 loopback）。"""
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    return normalize_base_url(f"http://{host}:{int(port)}")
