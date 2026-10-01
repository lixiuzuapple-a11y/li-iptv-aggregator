"""真实固定频道流测活（TASK-005）。

职责边界（刻意收窄，见 TASK-005 §4）：

  * 只对**已入库的固定库存 stream** 调用本机 ``ffprobe`` 做短时、受控、**只读**探测；
  * 绝不代理 / 转码 / 录制 / 持久化任何媒体内容，也绝不去请求条目以外的地址；
  * 绝不写猜测值：无法可靠获得的字段一律 ``NULL``（见 :meth:`ProbeObservation.to_repo_kwargs`）；
  * ``ffprobe`` 缺失 / 启动失败属于**环境级**错误：本轮 0 条 probe_result，绝不批量写「播放失败」；
  * 只依赖标准库（``subprocess`` + ``concurrent.futures``），不引入第三方 ffmpeg 封装。

调用安全（TASK-005 §4）：

  * ``argv`` 数组 + ``shell=False``：单条 URL 永远只是**一个** argv 元素，不做任何 shell 拼接；
  * 单条流有总超时；超时后 ``terminate`` → 等待 → ``kill`` → 再等待（绝不留孤儿进程）；
  * stdout / stderr 都在**内存里**按上限截断，超出部分继续读掉后丢弃（既不落盘也不会把子进程堵死）；
  * 落库与日志都不保存完整 URL / query token / stderr 全文。

并发（TASK-005 §7）：worker 线程只跑子进程并返回 :class:`ProbeObservation`；
**所有 SQLite 写入都在主线程**完成，worker 不共享 connection。
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import os
import subprocess
import threading
import time
from urllib.parse import urlsplit

from . import publish as publish_mod
from . import repo
from .util import utcnow_iso

# ------------------------------------------------------------------ 错误分类（TASK-005 §6）

ERROR_FFPROBE_NOT_FOUND = "FFPROBE_NOT_FOUND"
ERROR_FFPROBE_START_FAILED = "FFPROBE_START_FAILED"
ERROR_TIMEOUT = "TIMEOUT"
ERROR_DNS = "DNS_ERROR"
ERROR_CONNECT = "CONNECT_ERROR"
ERROR_TLS = "TLS_ERROR"
ERROR_HTTP = "HTTP_ERROR"
ERROR_INVALID_MEDIA = "INVALID_MEDIA"
ERROR_PROCESS = "PROCESS_ERROR"
ERROR_OUTPUT_INVALID = "OUTPUT_INVALID"
ERROR_UNKNOWN = "UNKNOWN"

ALL_ERROR_TYPES = (
    ERROR_FFPROBE_NOT_FOUND,
    ERROR_FFPROBE_START_FAILED,
    ERROR_TIMEOUT,
    ERROR_DNS,
    ERROR_CONNECT,
    ERROR_TLS,
    ERROR_HTTP,
    ERROR_INVALID_MEDIA,
    ERROR_PROCESS,
    ERROR_OUTPUT_INVALID,
    ERROR_UNKNOWN,
)

#: 环境级错误：ffprobe 本身不可用，与「这条流好不好」无关。
#: 出现这类错误时**绝不**写 probe_result（否则等于把环境故障伪装成流健康历史）。
ENVIRONMENT_ERROR_TYPES = (ERROR_FFPROBE_NOT_FOUND, ERROR_FFPROBE_START_FAILED)

# ------------------------------------------------------------------ 轮次阶段（TASK-005 §9）

STAGE_DISABLED = "disabled"     # [probe] enabled = false：0 次 ffprobe，行为同 TASK-004
STAGE_OK = "ok"                 # 环境正常，且本轮尝试的流全部成功
STAGE_DEGRADED = "degraded"     # 环境正常，但有流失败（正常的健康历史，不污染）
STAGE_FAILED = "failed"         # 环境级故障：0 条写入，整轮不得报告为完全 OK

# ------------------------------------------------------------------ 退出码（probe-check / probe-run）

EXIT_OK = 0
#: 环境级故障（ffprobe 缺失 / 无法启动）—— 与 publish 的 0/1/2、lock 的 3 不冲突。
EXIT_ENVIRONMENT = 1

# ------------------------------------------------------------------ 输出上限

#: ffprobe 的 JSON 正常只有几 KB；256 KiB 已是极宽松的上限。
STDOUT_CAP_BYTES = 262_144
#: stderr 只用于**内存内**分类与脱敏诊断，绝不落库。
STDERR_CAP_BYTES = 8_192
#: 进日志/JSON 的 stderr 片段长度上限（先脱敏再截断）
STDERR_EXCERPT_CHARS = 400
#: 能力检查（``ffprobe -version``）用的独立超时
CAPABILITY_TIMEOUT_SECONDS = 8.0
#: 进程被要求退出后，给它的宽限时间（秒）
TERMINATE_GRACE_SECONDS = 2.0
#: 等待子进程时的唤醒周期（秒）：**必须**留出这个心跳，否则「停止请求」要等到
#: 某条探测自己超时才会被感知（实测 4 条 30s 超时的流会让 Ctrl+C 迟滞整整 30s）。
#: 与 runtime 的 SLEEP_POLL_SECONDS 同量级。
STOP_POLL_SECONDS = 0.25


# ==================================================================== 配置

@dataclasses.dataclass
class ProbeSettings:
    """``[probe]`` 段。字段默认值与 :data:`liptv.config.DEFAULT_CONFIG["probe"]` 保持一致。"""

    enabled: bool = False
    name: str = "windows-local"
    location: str = "Windows"
    ffprobe_path: str = "ffprobe"
    timeout_seconds: float = 12.0
    analyze_seconds: float = 4.0
    max_concurrency: int = 4
    per_round_limit: int = 0
    #: 插在可执行文件之后的额外 argv。它来自「``ffprobe_path`` 写成数组」的配置形式::
    #:
    #:     [probe]
    #:     ffprobe_path = ["python", "tools/fake_ffprobe.py"]   # 包装脚本 / 离线测试替身
    #:
    #: 数组形式**只**是「可执行文件 + 前置参数」的展开，仍然走 argv 直传（``shell=False``），
    #: 不做任何 shell 解析。写字符串时它恒为空。
    #: 之所以需要它：离线测试必须复用**真实的子进程调用路径**，而 Windows 上没有可编译的
    #: 假 ffprobe；``.cmd`` 包装器实测会篡改参数（``&`` 会被 cmd 拆开），已被排除。
    extra_argv: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: dict | None) -> "ProbeSettings":
        raw = dict(raw or {})
        command = raw.get("ffprobe_path", cls.ffprobe_path)
        if isinstance(command, (list, tuple)):
            parts = tuple(str(item) for item in command if str(item).strip())
        else:
            parts = (str(command).strip(),) if str(command or "").strip() else ()
        if not parts:
            parts = (cls.ffprobe_path,)
        return cls(
            enabled=bool(raw.get("enabled", cls.enabled)),
            name=str(raw.get("name", cls.name)).strip() or cls.name,
            location=(str(raw["location"]) if raw.get("location") is not None
                      else cls.location),
            ffprobe_path=parts[0],
            timeout_seconds=_positive_float(raw.get("timeout_seconds"), cls.timeout_seconds),
            analyze_seconds=_bounded_float(
                raw.get("analyze_seconds"), cls.analyze_seconds, low=0.5, high=30.0
            ),
            max_concurrency=max(1, int(raw.get("max_concurrency", cls.max_concurrency))),
            per_round_limit=max(0, int(raw.get("per_round_limit", cls.per_round_limit))),
            extra_argv=parts[1:],
        )

    @property
    def command(self) -> tuple[str, ...]:
        """实际执行的完整命令前缀（可执行文件 + 前置参数）。"""
        return (self.ffprobe_path, *self.extra_argv)

    @property
    def display_command(self) -> str:
        """给日志/报告用的可读命令（只含命令本身，绝不含 URL）。"""
        return " ".join(self.command)


def _positive_float(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if number > 0 else float(default)


def _bounded_float(value, default: float, *, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return min(max(number, low), high)


# ============================================================ URL 脱敏（§11）

def redact_stream_url(url: str | None) -> str:
    """把流 URL 脱敏成 ``scheme://host[:port]/...``。

    刻意**只**保留 scheme 与主机（含端口），path 一律折叠成 ``/...``，
    query / fragment / userinfo 全部丢弃 —— 短时签名参数绝不出现在日志、JSON、
    报告或运行期状态里。
    """
    parts = urlsplit(url or "")
    if not parts.scheme or not parts.hostname:
        return "<unparsable-url>"
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    tail = "/..." if parts.path and parts.path not in ("", "/") else ""
    return f"{parts.scheme}://{host}{tail}"


def url_protocol(url: str | None) -> str | None:
    """按**实际 URL scheme** 判定 protocol 字段；不去猜任何 HTTP 状态码。"""
    scheme = (urlsplit(url or "").scheme or "").strip().lower()
    return scheme or None


def _sanitize_text(text: str | None, url: str | None) -> str:
    """先按原样替换掉 URL，再交给 publish 的通用 URL 脱敏，最后截断。"""
    body = text or ""
    if url:
        body = body.replace(url, redact_stream_url(url))
    body = publish_mod.redact_text(body)
    body = " ".join(body.split())
    return body[:STDERR_EXCERPT_CHARS]


# ==================================================== ffprobe argv 与进程调用

def _probe_size_bytes(analyze_seconds: float) -> int:
    """``-probesize``（字节）：随分析时长放大，但**有上界**，避免长时间拉流。"""
    return max(32_768, min(int(analyze_seconds * 500_000), 5_000_000))


def build_argv(settings: ProbeSettings, url: str) -> list[str]:
    """构造探测单条流的 argv 数组。

    * ``shell=False`` + argv 数组 ⇒ URL 永远只是一个参数，shell 元字符没有意义；
    * ``-nostdin`` 防止 ffprobe 抢我们的标准输入；
    * ``-analyzeduration`` / ``-probesize`` 限制它自己的分析量（短时、受控）。
    """
    return [
        *settings.command,
        "-hide_banner",
        "-nostdin",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-analyzeduration", str(int(settings.analyze_seconds * 1_000_000)),
        "-probesize", str(_probe_size_bytes(settings.analyze_seconds)),
        url,
    ]


def _version_argv(settings: ProbeSettings) -> list[str]:
    return [*settings.command, "-hide_banner", "-version"]


def _creation_flags() -> int:
    """Windows 上不给子进程开控制台窗口；其它平台为 0。"""
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)


@dataclasses.dataclass
class ProcessOutcome:
    """一次子进程调用的**结构化**结果（不含任何 URL）。"""

    started: bool
    returncode: int | None = None
    #: 子进程 PID（仅用于「有没有留下孤儿」这类校验与诊断，不入库）
    pid: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    elapsed_ms: int = 0
    timed_out: bool = False
    start_error_type: str | None = None
    start_error_message: str | None = None


class ProcessRegistry:
    """在跑的 ffprobe 句柄登记表：停止请求可以一次性把它们全部终止。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()

    def add(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)

    def discard(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def terminate_all(self) -> int:
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            _terminate(proc)
        return len(procs)


def _drain_capped(stream, cap: int) -> tuple[bytes, bool]:
    """把 ``stream`` 读到底，但**只在内存里保留前 cap 字节**。

    超出部分照样读掉（丢弃）—— 否则管道塞满会把子进程堵死。
    """
    kept: list[bytes] = []
    stored = 0
    total = 0
    while True:
        chunk = stream.read(65_536)
        if not chunk:
            break
        total += len(chunk)
        if stored < cap:
            piece = chunk[: cap - stored]
            kept.append(piece)
            stored += len(piece)
    return b"".join(kept), total > cap


def _terminate(proc: subprocess.Popen, *, grace: float = TERMINATE_GRACE_SECONDS) -> None:
    """terminate → wait → kill → wait。任何一步失败都不上抛（收尾路径）。"""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=grace)
        return
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=grace)
    except (subprocess.TimeoutExpired, OSError):
        pass


def run_process(
    argv: list[str],
    *,
    timeout_seconds: float,
    stdout_cap: int = STDOUT_CAP_BYTES,
    stderr_cap: int = STDERR_CAP_BYTES,
    registry: ProcessRegistry | None = None,
) -> ProcessOutcome:
    """受控地跑一个子进程。

    ``shell=False``；stdout / stderr 用两个后台线程**限量**读取；
    ``timeout_seconds`` 到点则 terminate/kill 并 wait/reap —— 绝不留孤儿 ffprobe。
    """
    started = time.perf_counter()
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv 数组 + shell=False，无 shell 拼接
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=_creation_flags(),
        )
    except FileNotFoundError as exc:
        return ProcessOutcome(
            started=False,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            start_error_type=ERROR_FFPROBE_NOT_FOUND,
            start_error_message=str(exc),
        )
    except OSError as exc:
        return ProcessOutcome(
            started=False,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            start_error_type=ERROR_FFPROBE_START_FAILED,
            start_error_message=str(exc),
        )

    results: dict[str, object] = {}

    def _reader(key: str, stream, cap: int) -> None:
        try:
            data, truncated = _drain_capped(stream, cap)
        except (OSError, ValueError):  # pragma: no cover - 管道被强行关闭
            data, truncated = b"", False
        results[key] = data
        results[key + "_truncated"] = truncated

    if registry is not None:
        registry.add(proc)
    out_thread = threading.Thread(target=_reader, args=("stdout", proc.stdout, stdout_cap), daemon=True)
    err_thread = threading.Thread(target=_reader, args=("stderr", proc.stderr, stderr_cap), daemon=True)
    out_thread.start()
    err_thread.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(proc)
    finally:
        if registry is not None:
            registry.discard(proc)

    out_thread.join(timeout=TERMINATE_GRACE_SECONDS * 2)
    err_thread.join(timeout=TERMINATE_GRACE_SECONDS * 2)

    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except (OSError, AttributeError):  # pragma: no cover
            pass

    return ProcessOutcome(
        started=True,
        returncode=proc.returncode,
        pid=proc.pid,
        stdout=results.get("stdout", b"") or b"",
        stderr=results.get("stderr", b"") or b"",
        stdout_truncated=bool(results.get("stdout_truncated")),
        stderr_truncated=bool(results.get("stderr_truncated")),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        timed_out=timed_out,
    )


# ============================================================ ffprobe 能力检查

@dataclasses.dataclass
class FfprobeCapability:
    """``ffprobe -version`` 的结果。**只检查可执行文件，不请求任何 stream。**"""

    ok: bool
    path: str
    version_line: str | None = None
    error_type: str | None = None
    message: str | None = None
    exit_code: int | None = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "path": self.path,
            "version": self.version_line,
            "error_type": self.error_type,
            "message": self.message,
            "exit_code": self.exit_code,
            "elapsed_ms": self.elapsed_ms,
        }


def check_ffprobe(settings: ProbeSettings, *, timeout_seconds: float | None = None) -> FfprobeCapability:
    """检查 ffprobe 可执行文件与版本；不请求任何 stream（TASK-005 §3）。

    超时用**独立**的 :data:`CAPABILITY_TIMEOUT_SECONDS`：能力检查是「环境是否可用」的
    健康判断，不该被单条流的 ``timeout_seconds`` 拖累（否则把流超时配小就会把
    环境误判成故障）。冷启动（首次拉起解解释器 / 杀软扫描）也可能要几秒。
    """
    budget = float(timeout_seconds if timeout_seconds is not None
                   else CAPABILITY_TIMEOUT_SECONDS)
    outcome = run_process(_version_argv(settings), timeout_seconds=budget,
                          stdout_cap=16_384, stderr_cap=STDERR_CAP_BYTES)
    if not outcome.started:
        return FfprobeCapability(
            ok=False,
            path=settings.ffprobe_path,
            error_type=outcome.start_error_type or ERROR_FFPROBE_NOT_FOUND,
            message=outcome.start_error_message,
            elapsed_ms=outcome.elapsed_ms,
        )
    text = outcome.stdout.decode("utf-8", errors="replace").strip()
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), None)
    if outcome.timed_out:
        return FfprobeCapability(
            ok=False, path=settings.ffprobe_path, error_type=ERROR_TIMEOUT,
            message=f"ffprobe -version 超过 {budget:g}s 未返回",
            exit_code=outcome.returncode, elapsed_ms=outcome.elapsed_ms,
        )
    if outcome.returncode != 0 or not first_line:
        return FfprobeCapability(
            ok=False, path=settings.ffprobe_path,
            error_type=ERROR_FFPROBE_START_FAILED,
            message=_sanitize_text(outcome.stderr.decode("utf-8", errors="replace"), None)
            or f"ffprobe -version 退出码 {outcome.returncode}",
            exit_code=outcome.returncode, elapsed_ms=outcome.elapsed_ms,
        )
    return FfprobeCapability(
        ok=True, path=settings.ffprobe_path,
        version_line=first_line[:200], exit_code=outcome.returncode,
        elapsed_ms=outcome.elapsed_ms,
    )


# ============================================================ stderr 错误分类

_DNS_PATTERNS = (
    "failed to resolve hostname",
    "name or service not known",
    "getaddrinfo",
    "temporary failure in name resolution",
    "nodename nor servname",
    "no address associated with hostname",
)
_TLS_PATTERNS = (
    "tls error",
    "ssl error",
    "unable to negotiate tls",
    "handshake failed",
    "certificate verify failed",
    "ssl certificate",
    "self-signed certificate",
    "ssl_",
    "openssl",
)
_CONNECT_PATTERNS = (
    "connection refused",
    "connection timed out",
    "connection reset",
    "network is unreachable",
    "no route to host",
    "immediate exit requested",
    "input/output error",
)
_HTTP_PATTERNS = (
    "server returned",
    "http error",
    "not found",
    "forbidden",
    "unauthorized",
    "method not allowed",
    "too many requests",
    "service unavailable",
)
_INVALID_PATTERNS = (
    "invalid data found when processing input",
    "moov atom not found",
    "could not find codec parameters",
    "invalid argument",
    "end of file",
    "protocol not found",
    "unsupported",
    "no such file or directory",
)


def _matches(haystack: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in haystack for pattern in patterns)


def classify_failure(*, timed_out: bool, returncode: int | None, stderr: str) -> str:
    """把一次失败的探测归到**稳定、可解释**的类别。

    只在 stderr 里找**明确特征**；找不到特征就用更宽的 ``CONNECT_ERROR`` /
    ``PROCESS_ERROR``，绝不假装精确。判定顺序：HTTP → DNS → TLS → 连接 → 媒体无效。
    """
    if timed_out:
        return ERROR_TIMEOUT
    text = (stderr or "").lower()
    if _matches(text, _HTTP_PATTERNS):
        return ERROR_HTTP
    if _matches(text, _DNS_PATTERNS):
        return ERROR_DNS
    if _matches(text, _TLS_PATTERNS):
        return ERROR_TLS
    if _matches(text, _CONNECT_PATTERNS):
        return ERROR_CONNECT
    if _matches(text, _INVALID_PATTERNS):
        return ERROR_INVALID_MEDIA
    return ERROR_PROCESS if returncode not in (None, 0) else ERROR_UNKNOWN


# ============================================================ ffprobe JSON 解析

@dataclasses.dataclass
class MediaInfo:
    """从头到尾都只承载**真实存在**的测量值。"""

    resolution_width: int | None = None
    resolution_height: int | None = None
    bitrate_kbps: int | None = None


def _as_positive_int(value) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_media_info(raw: bytes) -> tuple[MediaInfo | None, str | None, str | None]:
    """解析 ffprobe JSON。

    返回 ``(info, error_type, message)``：
      * JSON 解不出来 / 结构不是对象 → ``OUTPUT_INVALID``；
      * 解析成功但没有任何 audio/video 流（空媒体 / 明显无效） → ``INVALID_MEDIA``；
      * 成功 → ``(MediaInfo, None, None)``。

    只取真实存在的字段：分辨率仅来自 video 流的 width/height，
    码率仅来自 format / 流的 ``bit_rate``（拿不到就留 ``None``，不猜）。
    """
    text = raw.decode("utf-8", errors="replace")
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, ERROR_OUTPUT_INVALID, f"ffprobe 输出不是合法 JSON：{exc}"
    if not isinstance(document, dict):
        return None, ERROR_OUTPUT_INVALID, "ffprobe 输出顶层不是 JSON 对象"

    streams = document.get("streams")
    if streams is None:
        return None, ERROR_OUTPUT_INVALID, "ffprobe 输出缺少 streams 字段"
    if not isinstance(streams, list):
        return None, ERROR_OUTPUT_INVALID, "ffprobe 输出的 streams 不是数组"

    audio_video = [
        item for item in streams
        if isinstance(item, dict) and item.get("codec_type") in ("audio", "video")
    ]
    if not audio_video:
        return None, ERROR_INVALID_MEDIA, "没有任何 audio / video 流（空媒体或无效数据）"

    info = MediaInfo()
    for item in audio_video:
        if item.get("codec_type") != "video":
            continue
        width = _as_positive_int(item.get("width"))
        height = _as_positive_int(item.get("height"))
        if width and height:
            info.resolution_width, info.resolution_height = width, height
            break

    for candidate in (document.get("format"), *audio_video):
        if not isinstance(candidate, dict):
            continue
        bit_rate = _as_positive_int(candidate.get("bit_rate"))
        if bit_rate:
            info.bitrate_kbps = max(1, round(bit_rate / 1000))
            break

    return info, None, None


# ============================================================ 单条流观测结果

@dataclasses.dataclass
class ProbeObservation:
    """一条流的探测结果。**只承载真实测量值或 NULL。**"""

    stream_id: int
    #: 冗余一份 canonical 名，纯粹为了让 CLI / 演示的输出可读（不入库）
    canonical_name: str | None = None
    success: bool = False
    error_type: str | None = None
    startup_ms: int | None = None
    resolution_width: int | None = None
    resolution_height: int | None = None
    bitrate_kbps: int | None = None
    protocol: str | None = None
    #: 以下三项**恒为 None**：ffprobe 既不能可靠给出 HTTP 状态码，
    #: 也不能把一次连接拆成 DNS / TCP / TLS 阶段。宁缺勿猜（TASK-005 §2）。
    ipv_family: str | None = None
    http_status: int | None = None
    connect_ms: int | None = None
    #: 仅诊断用，不落库
    exit_code: int | None = None
    duration_ms: int | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    cancelled: bool = False
    message: str | None = None
    redacted_url: str | None = None

    @property
    def environment_error(self) -> bool:
        return self.error_type in ENVIRONMENT_ERROR_TYPES

    def to_repo_kwargs(self) -> dict:
        """映射到 ``repo.add_probe_result`` 的关键字（字段名与 schema V1 一一对应）。"""
        return {
            "stream_id": self.stream_id,
            "success": self.success,
            "error_type": self.error_type,
            "http_status": self.http_status,
            "connect_ms": self.connect_ms,
            "startup_ms": self.startup_ms,
            "resolution_width": self.resolution_width,
            "resolution_height": self.resolution_height,
            "bitrate_kbps": self.bitrate_kbps,
            "protocol": self.protocol,
            "ipv_family": self.ipv_family,
        }

    def to_public_dict(self) -> dict:
        """给 CLI / 演示用的展示形态：**URL 已脱敏**。"""
        resolution = None
        if self.resolution_width and self.resolution_height:
            resolution = f"{self.resolution_width}x{self.resolution_height}"
        return {
            "stream_id": self.stream_id,
            "canonical_name": self.canonical_name,
            "success": self.success,
            "error_type": self.error_type,
            "cancelled": self.cancelled,
            "startup_ms": self.startup_ms,
            "resolution": resolution,
            "bitrate_kbps": self.bitrate_kbps,
            "protocol": self.protocol,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "url": self.redacted_url,
            "message": self.message,
        }


# ============================================================ 单条流：探测

def probe_stream(
    url: str,
    *,
    stream_id: int,
    settings: ProbeSettings,
    cancel: threading.Event | None = None,
    registry: ProcessRegistry | None = None,
) -> ProbeObservation:
    """探测**一条**流的可播性；任何异常都被收敛成一条 observation，绝不外抛。"""
    observation = ProbeObservation(stream_id=stream_id, redacted_url=redact_stream_url(url))
    if cancel is not None and cancel.is_set():
        observation.cancelled = True
        return observation

    try:
        outcome = run_process(
            build_argv(settings, url),
            timeout_seconds=settings.timeout_seconds,
            registry=registry,
        )
    except Exception as exc:  # noqa: BLE001 - 单条流绝不能带走整轮
        observation.error_type = ERROR_UNKNOWN
        observation.message = _sanitize_text(f"{type(exc).__name__}: {exc}", url)
        return observation

    observation.duration_ms = outcome.elapsed_ms
    observation.stdout_truncated = outcome.stdout_truncated
    observation.stderr_truncated = outcome.stderr_truncated

    if not outcome.started:
        observation.error_type = outcome.start_error_type or ERROR_FFPROBE_START_FAILED
        observation.message = _sanitize_text(outcome.start_error_message, url)
        return observation

    if cancel is not None and cancel.is_set():
        observation.cancelled = True
        return observation

    observation.exit_code = outcome.returncode
    stderr_text = outcome.stderr.decode("utf-8", errors="replace")

    if outcome.timed_out:
        observation.error_type = ERROR_TIMEOUT
        observation.message = f"超过 {settings.timeout_seconds:g}s 未得到有效媒体信息，已终止"
        return observation

    if outcome.returncode != 0:
        observation.error_type = classify_failure(
            timed_out=False, returncode=outcome.returncode, stderr=stderr_text
        )
        observation.message = _sanitize_text(stderr_text, url) or f"ffprobe 退出码 {outcome.returncode}"
        return observation

    info, error_type, message = parse_media_info(outcome.stdout)
    if error_type is not None:
        observation.error_type = error_type
        # 输出异常时 stderr 往往有关键线索；两者都脱敏后再截断
        observation.message = _sanitize_text(
            stderr_text or message or "ffprobe 输出无效", url
        )
        return observation

    # 成功：startup_ms = 从启动 ffprobe 到「已产出满足成功条件的媒体信息」的墙钟耗时。
    # 这里用子进程从 spawn 到 exit 的实测耗时（JSON 解析在微秒级，不计入）。
    observation.success = True
    observation.startup_ms = outcome.elapsed_ms
    assert info is not None  # parse_media_info 成功时必然给 info
    observation.resolution_width = info.resolution_width
    observation.resolution_height = info.resolution_height
    observation.bitrate_kbps = info.bitrate_kbps
    observation.protocol = url_protocol(url)
    return observation


# ============================================================ 一轮测活

def disabled_summary(settings: ProbeSettings | None = None) -> dict:
    """``[probe] enabled = false`` 时的轮次摘要：明确「一次 ffprobe 都没跑」。"""
    return {
        "enabled": False,
        "stage": STAGE_DISABLED,
        "dry_run": False,
        "probe_name": settings.name if settings else None,
        "probe_location": settings.location if settings else None,
        "probe_id": None,
        "ffprobe": None,
        "requested": 0,
        "succeeded": 0,
        "failed": 0,
        "written": 0,
        "cancelled": 0,
        "skipped": 0,
        "error_counts": {},
        "error_type": None,
        "environment_error": False,
        "results": [],
        "reason": "probe.enabled = false：本轮不调用任何 ffprobe",
    }


def _effective_limit(settings: ProbeSettings, limit: int | None) -> int | None:
    if limit is not None and int(limit) > 0:
        return int(limit)
    return settings.per_round_limit or None


def _should_cancel(cancel: threading.Event | None, should_stop) -> bool:
    if cancel is not None and cancel.is_set():
        return True
    if should_stop is not None:
        try:
            return bool(should_stop())
        except Exception:  # noqa: BLE001 - 判断本身出错不能变成「一直跑」
            return True
    return False


def _collect_targets(settings, targets, cancel, should_stop, registry) -> tuple[list[ProbeObservation], bool]:
    """并发跑完所有目标，返回 ``(observations, stop_hit)``。

    停止请求出现时：不再提交新任务、终止在跑的子进程、把未开始的标记为 cancelled。
    """
    observations: list[ProbeObservation] = []
    pending: dict[concurrent.futures.Future, int] = {}
    stop_hit = False

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=settings.max_concurrency, thread_name_prefix="liptv-probe"
    ) as pool:
        iterator = iter(targets)

        def submit_next() -> bool:
            for row in iterator:
                if _should_cancel(cancel, should_stop):
                    return False
                future = pool.submit(
                    probe_stream,
                    row["url"],
                    stream_id=int(row["id"]),
                    settings=settings,
                    cancel=cancel,
                    registry=registry,
                )
                pending[future] = int(row["id"])
                return True
            return False

        for _ in range(settings.max_concurrency):
            if not submit_next():
                break
        if not pending and _should_cancel(cancel, should_stop):
            stop_hit = True

        while pending:
            # timeout 是**必需的**：没有它就只能等到「某条探测自己结束」才有机会
            # 检查停止请求（长超时的流会让 Ctrl+C 迟滞到接近 timeout_seconds）。
            done, _not_done = concurrent.futures.wait(
                list(pending),
                timeout=STOP_POLL_SECONDS,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                stream_id = pending.pop(future)
                observations.append(_safe_result(future, stream_id))

            if _should_cancel(cancel, should_stop):
                stop_hit = True
                registry.terminate_all()
                break

            while len(pending) < settings.max_concurrency and submit_next():
                pass

        # 走到这里说明是「停止请求导致的提前收尾」（正常收尾时 pending 必然是空的）。
        # **被我们杀掉的探测结果不可信**：它的失败来自 terminate/kill，不是流本身的问题，
        # 因此一律按「未完成」处理 —— 绝不写进 probe_result 污染健康历史。
        for future, stream_id in list(pending.items()):
            if future.cancel():
                observations.append(ProbeObservation(stream_id=stream_id, cancelled=True))
                continue
            try:
                result = future.result(
                    timeout=settings.timeout_seconds + TERMINATE_GRACE_SECONDS * 2
                )
            except Exception:  # noqa: BLE001 - 收尾阶段不再上抛
                observations.append(ProbeObservation(stream_id=stream_id, cancelled=True))
                continue
            observations.append(
                ProbeObservation(
                    stream_id=stream_id,
                    canonical_name=result.canonical_name,
                    cancelled=True,
                    message="已因停止请求终止，结果不参与落库",
                )
            )

    observations.sort(key=lambda item: item.stream_id)
    return observations, stop_hit


def _safe_result(future: concurrent.futures.Future, stream_id: int) -> ProbeObservation:
    try:
        return future.result()
    except Exception as exc:  # noqa: BLE001 - worker 内部已兜底，这里是双保险
        return ProbeObservation(
            stream_id=stream_id, error_type=ERROR_UNKNOWN,
            message=_sanitize_text(f"{type(exc).__name__}: {exc}", None),
        )


def run_round(
    conn,
    *,
    settings: ProbeSettings,
    now: str | None = None,
    stream_id: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    cancel: threading.Event | None = None,
    should_stop=None,
    capability: FfprobeCapability | None = None,
) -> dict:
    """执行一轮真实测活：能力检查 → 选目标 → 并发探测 → **主线程**写库。

    写库语义（TASK-005 §8）：
      * 每个 stream × 本 probe 节点每轮**最多 1 条** probe_result；历史只追加；
      * ``probe.last_seen_at`` 由 :func:`repo.add_probe_result` 在**真实落库成功后**推进；
      * ``dry_run`` 不写数据库；
      * 环境级故障（ffprobe 缺失 / 起不来，或本轮**全部**探测都因环境错误失败）
        ⇒ **0 条写入**，``stage = failed``，明确失败，不污染 selector 历史。

    返回值同时供 CLI（含 ``results`` 明细）与 scheduler（由 :func:`summarize_for_status`
    裁成不含 URL 的子摘要）使用。
    """
    if not settings.enabled:
        return disabled_summary(settings)

    if capability is None:
        capability = check_ffprobe(settings)

    summary = disabled_summary(settings)
    summary.update(
        enabled=True,
        stage=STAGE_OK,
        dry_run=bool(dry_run),
        ffprobe=capability.to_dict(),
        reason=None,
    )

    if not capability.ok:
        summary.update(
            stage=STAGE_FAILED,
            environment_error=True,
            error_type=capability.error_type or ERROR_FFPROBE_NOT_FOUND,
            reason=(
                "ffprobe 不可用（环境级故障）：本轮不写任何 probe_result，"
                "也不要把它误解成「所有流都播不了」"
            ),
        )
        return summary

    targets = repo.list_probe_candidates(
        conn, stream_id=stream_id, limit=_effective_limit(settings, limit)
    )
    summary["requested"] = len(targets)
    if not targets:
        summary["reason"] = (
            "没有可测活的固定库存 stream（disabled / stale / orphan / "
            "dynamic_event_m3u 来源都已被排除）"
        )
        return summary

    registry = ProcessRegistry()
    observations, stop_hit = _collect_targets(
        settings, targets, cancel, should_stop, registry
    )
    # canonical 名只在主线程补齐（worker 不碰数据库行）
    names = {int(row["id"]): row["canonical_name"] for row in targets}
    for item in observations:
        item.canonical_name = names.get(item.stream_id)

    attempted = [item for item in observations if not item.cancelled]
    cancelled = len(observations) - len(attempted)
    summary["cancelled"] = cancelled

    env_failures = [item for item in attempted if item.environment_error]
    if attempted and len(env_failures) == len(attempted):
        summary.update(
            stage=STAGE_FAILED,
            environment_error=True,
            error_type=env_failures[0].error_type,
            succeeded=0,
            failed=0,
            written=0,
            skipped=len(targets) - len(observations),
            error_counts=_error_counts(attempted),
            environment_failed_streams=[item.stream_id for item in attempted],
            results=[item.to_public_dict() for item in observations],
            reason=(
                "本轮全部探测都因 ffprobe 环境错误失败：0 条 probe_result，"
                "避免把环境故障写成一整批「流失败」"
            ),
        )
        return summary

    succeeded = sum(1 for item in attempted if item.success)
    written = 0
    if not dry_run:
        probe_id = repo.ensure_probe(conn, settings.name, settings.location, now=now)
        summary["probe_id"] = probe_id
        checked_at = now or utcnow_iso()
        for item in attempted:
            repo.add_probe_result(
                conn, probe_id=probe_id, checked_at=checked_at, **item.to_repo_kwargs()
            )
            written += 1
        conn.commit()

    summary.update(
        stage=STAGE_OK if succeeded == len(attempted) else STAGE_DEGRADED,
        succeeded=succeeded,
        failed=len(attempted) - succeeded,
        written=written,
        skipped=len(targets) - len(attempted) - cancelled,
        error_counts=_error_counts(attempted),
        error_type=None,
        results=[item.to_public_dict() for item in observations],
    )
    if stop_hit:
        summary["reason"] = "收到停止请求：已终止在跑的 ffprobe，不再启动新的探测"
    return summary


def _error_counts(observations: list[ProbeObservation]) -> dict:
    counts: dict[str, int] = {}
    for item in observations:
        if item.success or not item.error_type:
            continue
        counts[item.error_type] = counts.get(item.error_type, 0) + 1
    return counts


# ============================================================ 状态摘要（无 URL）

#: 状态文件里最多列出多少个失败 stream id（避免无界增长；id 本身不敏感）
STATUS_FAILED_STREAM_LIMIT = 20


def summarize_for_status(summary: dict | None) -> dict:
    """把一轮测活摘要裁成可以进 ``runtime-status.json`` 的形态。

    **刻意不含**：任何 URL、stderr 片段、逐条结果明细。
    这是 TASK-004「运行期状态不含任何 URL / 签名」的边界延续。
    """
    data = summary or disabled_summary()
    failed_ids: list[int] = []
    for item in data.get("results") or []:
        if item.get("success") or item.get("cancelled") or not item.get("error_type"):
            continue
        failed_ids.append(int(item["stream_id"]))
    return {
        "enabled": bool(data.get("enabled")),
        "stage": data.get("stage"),
        "probe_name": data.get("probe_name"),
        "probe_id": data.get("probe_id"),
        "dry_run": bool(data.get("dry_run")),
        "requested": int(data.get("requested") or 0),
        "succeeded": int(data.get("succeeded") or 0),
        "failed": int(data.get("failed") or 0),
        "written": int(data.get("written") or 0),
        "cancelled": int(data.get("cancelled") or 0),
        "skipped": int(data.get("skipped") or 0),
        "error_type": data.get("error_type"),
        "error_counts": dict(data.get("error_counts") or {}),
        "failed_stream_ids": failed_ids[:STATUS_FAILED_STREAM_LIMIT],
    }
