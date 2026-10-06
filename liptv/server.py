"""只读 HTTP 订阅服务（TASK-004）。

给播放器一个固定地址：

* ``GET/HEAD /live.m3u`` —— 返回当前已发布的 M3U 文件；
* ``GET /healthz`` —— 只返回「服务在不在 / 文件在不在 / 距上次成功发布多久」，
  **不含任何 stream URL、签名、数据库内容或来源清单**；
* 其余路径一律 ``404``。

刻意的安全边界：

* **绝不代理视频流**：本模块只读那一个 M3U 文件，永远不会去请求条目里的播放地址；
* **路由是固定映射**：只做「请求路径 == 配置里的确切字符串」比较，从不把 URL 拼成磁盘路径，
  因此 ``/../``、``%2e%2e%2f``、绝对路径等都自然落进 404，不存在路径穿越面；
* 不启用目录浏览，不暴露数据库 / ``*.previous.m3u`` / 发布摘要原文 / 其它磁盘文件；
* 默认只绑 ``127.0.0.1``；绑到 ``0.0.0.0`` 需要用户显式配置（CLI 会打印安全提示）。

写盘与读取的并发安全：发布侧用 ``os.replace`` 原子替换，读取侧每次请求**完整读一遍**文件，
因此播放器只会拿到「旧的完整文件」或「新的完整文件」，不会拿到半文件。
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import threading
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import runtime as runtime_mod
from .util import utcnow_iso

PLAYLIST_CONTENT_TYPE = "application/vnd.apple.mpegurl"
#: TASK-011 §10：XMLTV 的 Content-Type。带 charset，避免中文节目名乱码。
XML_CONTENT_TYPE = "application/xml; charset=utf-8"
JSON_CONTENT_TYPE = "application/json; charset=utf-8"
TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"

#: 文件不存在 / 空文件时冻结为 503（服务在、内容不可用）——不生成空列表冒充成功。
PLAYLIST_UNAVAILABLE_STATUS = runtime_mod.HTTP_PLAYLIST_MISSING

_NOT_FOUND_BODY = b"not found\n"
_UNAVAILABLE_BODY = b"playlist not available\n"


# ------------------------------------------------- 与原子替换并存的共享读
#
# 实测（2026-10-01，Windows 10 / Python 3.13）：若读者用普通 ``open()`` 持有目标文件，
# 发布侧的 ``os.replace`` 会直接抛 ``PermissionError [WinError 5]`` —— 也就是说
# 「HTTP 服务读取发布文件」会把 scheduler 的原子替换**顶失败**。
# 因此读取侧统一走「带 FILE_SHARE_DELETE 的共享读」：句柄不阻止 rename/delete，
# 而且已经打开的句柄始终指向**它打开时的那一个完整版本**，天然只会看到旧整版或新整版。

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3


def _open_shared_read(path) -> int:
    """打开一个只读 fd，且**不阻塞**其它进程对该文件的 rename / replace。"""
    if os.name != "nt":
        return os.open(path, os.O_RDONLY)

    import ctypes  # noqa: PLC0415 - 只在 Windows 分支需要
    import msvcrt  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE

    handle = kernel32.CreateFileW(
        str(path),
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == wintypes.HANDLE(-1).value or not handle:
        err = ctypes.get_last_error()
        if err in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
            raise FileNotFoundError(err, os.strerror(err), str(path))
        raise OSError(err, f"CreateFileW 失败（WinError {err}）", str(path))
    # 所有权移交给 fd，之后用 os.close 关闭
    return msvcrt.open_osfhandle(handle, os.O_RDONLY)  # type: ignore[arg-type]


def read_playlist(path) -> tuple[bytes, os.stat_result]:
    """完整读一遍发布文件，返回 (内容, 该版本的 stat)。

    内容与 stat 取自**同一个句柄**，因此 ETag / Last-Modified 与读到的字节必然自洽。
    """
    fd = _open_shared_read(path)
    try:
        stat = os.fstat(fd)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 262144)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), stat
    finally:
        os.close(fd)


def _validate_route(value: str, *, name: str) -> str:
    """校验路由是「以 / 开头的干净路径」，避免把奇怪的字符串写进路由表。"""
    route = str(value or "").strip()
    if not route.startswith("/"):
        raise ValueError(f"{name} 必须以 / 开头，收到：{value!r}")
    if route != "/" and route.endswith("/"):
        route = route.rstrip("/")
    if any(bad in route for bad in ("..", "\\", "?", "#", "\x00")):
        raise ValueError(f"{name} 含非法片段，收到：{value!r}")
    return route


@dataclasses.dataclass
class ServerState:
    """HTTP 服务的只读运行状态（handler 从这里取，不碰全局变量）。"""

    playlist_file: pathlib.Path
    status_file: pathlib.Path | None = None
    playlist_route: str = "/live.m3u"
    health_route: str = "/healthz"
    #: TASK-011 §10：EPG（XMLTV）文件。``None`` 表示不提供该端点。
    epg_file: pathlib.Path | None = None
    epg_route: str = "/epg.xml"
    stale_after_seconds: int = 21600
    version: str = "0.1.0"
    started_at: str | None = None
    quiet: bool = True


class _SubscriptionHandler(BaseHTTPRequestHandler):
    server_version = "liptv-subscription"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 响应工具
    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        head: bool,
        extra: list[tuple[str, str]] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in extra or []:
            self.send_header(key, value)
        self.end_headers()
        if not head and body:
            self.wfile.write(body)

    # ---------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        self._route(head=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._route(head=True)

    def _route(self, *, head: bool) -> None:
        state: ServerState = self.server.state  # type: ignore[attr-defined]
        if not getattr(self.server, "quiet", True):
            print(f"[server] {self.command} {self.path}", flush=True)

        # 只比较「路径部分」且必须**完全相等** —— 绝不做磁盘路径拼接
        path = urlsplit(self.path).path
        if path == state.playlist_route:
            self._serve_playlist(state, head=head)
            return
        if path == state.health_route:
            self._serve_health(state, head=head)
            return
        if state.epg_file is not None and path == state.epg_route:
            self._serve_epg(state, head=head)
            return
        self._send(404, _NOT_FOUND_BODY, TEXT_CONTENT_TYPE, head=head)

    # ---------------------------------------------------------------- EPG
    def _serve_epg(self, state: ServerState, *, head: bool) -> None:
        """TASK-011 §10：返回 XMLTV。

        语义要点：

        - **只读**，且不经过任何转换 —— 磁盘上是什么就发什么；
        - 文件不存在 ⇒ **404 + 明确说明**，绝不返回空文件或半文件
          （§10「无 EPG 时明确 404/503，不返回半文件」）；
        - 文件存在但读不出来（权限/IO）⇒ **503**，同样不返回半文件；
        - 不影响 /live.m3u 与 /healthz（各自独立分支）。
        """
        target = state.epg_file
        assert target is not None  # 由 _route 保证
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            self._send(
                404,
                b"EPG not generated yet. Run: liptv epg-refresh\n",
                TEXT_CONTENT_TYPE,
                head=head,
            )
            return
        except OSError as exc:
            self._send(
                503,
                f"EPG file unavailable: {type(exc).__name__}\n".encode("utf-8"),
                TEXT_CONTENT_TYPE,
                head=head,
            )
            return
        # 🚨 空文件绝不返回 200 —— 播放器会把「0 个节目」当成「今天没节目」。
        if not data.strip():
            self._send(
                503,
                b"EPG file is empty; refusing to serve a half-built guide.\n",
                TEXT_CONTENT_TYPE,
                head=head,
            )
            return
        self._send(200, data, XML_CONTENT_TYPE, head=head)

    # ---------------------------------------------------------------- 播放列表
    def _serve_playlist(self, state: ServerState, *, head: bool) -> None:
        target = state.playlist_file
        if head:
            # HEAD 不读正文，但必须报出与 GET 完全一致的 Content-Length。
            try:
                stat = target.stat()
            except OSError:
                self._send(PLAYLIST_UNAVAILABLE_STATUS, _UNAVAILABLE_BODY,
                           TEXT_CONTENT_TYPE, head=True)
                return
            if not stat.st_size:
                self._send(PLAYLIST_UNAVAILABLE_STATUS, _UNAVAILABLE_BODY,
                           TEXT_CONTENT_TYPE, head=True)
                return
            self._send_head(200, stat.st_size, PLAYLIST_CONTENT_TYPE, extra=[
                ("Last-Modified", formatdate(stat.st_mtime, usegmt=True)),
                ("ETag", _etag(stat)),
            ])
            return

        try:
            data, stat = read_playlist(target)
        except OSError:
            self._send(PLAYLIST_UNAVAILABLE_STATUS, _UNAVAILABLE_BODY,
                       TEXT_CONTENT_TYPE, head=False)
            return
        if not data:
            # 空文件绝不冒充成功
            self._send(PLAYLIST_UNAVAILABLE_STATUS, _UNAVAILABLE_BODY,
                       TEXT_CONTENT_TYPE, head=False)
            return
        extra = [
            ("Last-Modified", formatdate(stat.st_mtime, usegmt=True)),
            ("ETag", _etag(stat)),
        ]
        # Content-Length 一律以**本次实际读到的字节**为准，且与该句柄的 stat 自洽
        self._send(200, data, PLAYLIST_CONTENT_TYPE, head=False, extra=extra)

    def _send_head(
        self, status: int, content_length: int, content_type: str, *, extra=None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(int(content_length)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in extra or []:
            self.send_header(key, value)
        self.end_headers()

    # ---------------------------------------------------------------- 健康
    def _serve_health(self, state: ServerState, *, head: bool) -> None:
        payload = runtime_mod.compute_health(
            playlist_path=state.playlist_file,
            status_path=state.status_file,
            now=utcnow_iso(),
            stale_after_seconds=state.stale_after_seconds,
            version=state.version,
            started_at=state.started_at,
        )
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self._send(200, body, JSON_CONTENT_TYPE, head=head)

    # ---------------------------------------------------------------- 日志
    def log_message(self, fmt: str, *args) -> None:  # pragma: no cover - 噪声控制
        if getattr(self.server, "quiet", True):
            return
        print("[server] " + (fmt % args), flush=True)


def _etag(stat) -> str:
    """用 (mtime_ns, size) 生成弱标识；不读取文件内容，GET/HEAD 结果一致。"""
    return f'"{stat.st_mtime_ns}-{stat.st_size}"'


def make_http_server(
    *,
    host: str,
    port: int,
    playlist_file,
    status_file=None,
    playlist_route: str = "/live.m3u",
    health_route: str = "/healthz",
    epg_file=None,
    epg_route: str = "/epg.xml",
    stale_after_seconds: int = 21600,
    version: str = "0.1.0",
    started_at: str | None = None,
    quiet: bool = True,
) -> ThreadingHTTPServer:
    """构造（未启动的）HTTP 服务；``port=0`` 时由系统分配空闲端口。"""
    playlist_route = _validate_route(playlist_route, name="server.playlist_path")
    health_route = _validate_route(health_route, name="server.health_path")
    if playlist_route == health_route:
        raise ValueError("server.playlist_path 与 server.health_path 不能相同")

    # TASK-011 §10：EPG 端点。epg_file 为 None 时**完全不注册**该路由，
    # 于是行为与 TASK-010 完全一致（404），零回归。
    epg_path = None
    epg_resolved = _validate_route(epg_route, name="server.epg_path")
    if epg_file is not None:
        epg_path = pathlib.Path(epg_file)
        if epg_resolved in (playlist_route, health_route):
            raise ValueError("server.epg_path 不能与 playlist/health 路径相同")

    state = ServerState(
        playlist_file=pathlib.Path(playlist_file),
        status_file=pathlib.Path(status_file) if status_file else None,
        playlist_route=playlist_route,
        health_route=health_route,
        epg_file=epg_path,
        epg_route=epg_resolved,
        stale_after_seconds=int(stale_after_seconds),
        version=version,
        started_at=started_at or utcnow_iso(),
        quiet=quiet,
    )

    httpd = ThreadingHTTPServer((host, int(port)), _SubscriptionHandler)
    httpd.daemon_threads = True
    httpd.state = state  # type: ignore[attr-defined]
    httpd.quiet = quiet  # type: ignore[attr-defined]
    return httpd


class SubscriptionServer:
    """把 :func:`make_http_server` 包成「可启停」的对象（供 run --serve / serve 使用）。"""

    def __init__(self, **kwargs):
        self._httpd = make_http_server(**kwargs)
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- 只读属性
    @property
    def host(self) -> str:
        return str(self._httpd.server_address[0])

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        host = self.host
        if ":" in host:  # IPv6
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @property
    def state(self) -> ServerState:
        return self._httpd.state  # type: ignore[attr-defined]

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------- 生命周期
    def start(self) -> threading.Thread:
        """在后台线程里开始服务（daemon，不阻塞调用方）。"""
        if self._thread is not None:
            return self._thread
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="liptv-http", daemon=True
        )
        self._thread.start()
        return self._thread

    def serve_forever(self) -> None:
        """前台阻塞服务（CLI ``serve`` 用）。"""
        self._httpd.serve_forever()

    def stop(self) -> None:
        """关闭监听并等待线程退出；可重复调用。"""
        try:
            self._httpd.shutdown()
        except Exception:  # noqa: BLE001 - 未启动时 shutdown 会抛，忽略
            pass
        try:
            self._httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "SubscriptionServer":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()
