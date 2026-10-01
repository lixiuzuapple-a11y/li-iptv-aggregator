"""本地调度运行期：单实例锁 + 轮次状态 + 新鲜度（TASK-004）。

把 TASK-003 的「手工跑一次 :func:`liptv.publish.publish`」推进成**可长期运行的本地进程**。
本模块只做三件事，且刻意保持可注入、可离线测试：

1. :class:`SingleInstanceLock` —— 同一套 data/output 目录只允许一个 scheduler。
   原子创建 + PID/启动时间/心跳元数据；stale 判定**可解释**（见下）。
2. :class:`StatusStore` —— 脱敏的轮次状态 JSON（原子写、条数封顶）。
3. :func:`execute_round` / :class:`Scheduler` —— 一轮运行链与循环；
   ``clock`` / ``sleep`` / ``round_fn`` 全部可注入，测试不需要真的等几小时。

边界（与 TASK-004 验收场景一致）：

* **不伪造 probe 结果**：固定频道只走既有 ``select_playlist``，只认数据库里真实的
  ``probe_result``；本模块从不写 probe 表。
* **不自动创造 canonical/binding**：只调 :func:`liptv.repo.sync_streams` 归集，
  不做任何模糊强绑。
* **不代理视频流**：本模块只读 M3U 文本；条目里的播放地址永远不会被请求。
* **动态失败沿用 TASK-003 fail-closed**：绝不从旧文件回拼动态签名 URL。

单实例锁的 stale 策略（**不能只因文件存在就永远锁死，也不能无法判断就抢锁**）：

============ ==================== ========================== ==================
持有者位置   进程存活（可判定）      心跳年龄                    判定
============ ==================== ========================== ==================
本机         存活                  新鲜                        HELD（拒绝）
本机         存活                  过期                        HELD_SUSPECT（拒绝，
                                                                 并打印 PID 与清理办法）
本机         已退出                ——                          STALE（安全接管）
他机         无法判定              新鲜                        HELD（拒绝）
他机         无法判定              过期                        STALE（按心跳接管）
元数据损坏   无法判定              ——                          新鲜 ⇒ HELD；过期 ⇒ STALE
============ ==================== ========================== ==================

接管采用「``<lock>.steal`` 独占创建 → ``os.replace`` 原子替换」两步，避免两个实例同时抢锁。

锁心跳（**必须真正接进调度生命周期**，否则长时间运行会被误判 stale 而出现两个写入者）：

* 职责分工：:class:`Scheduler` 在**每轮开始 / 每轮结束**各刷新一次；
  :meth:`Scheduler._sleep_between_rounds` 在长休眠期间按周期刷新；
  :class:`LockHeartbeat` 后台线程覆盖「单轮本身很久」（例如慢速抓取）的窗口。
* 周期由 :func:`resolve_heartbeat_interval` 统一决定，恒 ≤ ``stale_after_seconds × 0.25``，
  因此心跳年龄最多只会逼近 stale 阈值的 1/4。
* **失去锁就必须停**：只要 :meth:`SingleInstanceLock.heartbeat` 返回 ``False``
  （token 已被替换 / 锁文件被删或损坏 / 写盘失败），调度器立刻 ``request_stop(LOCK_REASON_LOST)``，
  **不再进入下一轮、不再执行任何 fetch/publish**。
* 保守语义：无法确认 ownership 时既不覆盖未知锁、也不删除别人的锁（见 ``release`` / ``heartbeat``）。
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import os
import pathlib
import secrets
import socket
import threading
import time
from datetime import datetime, timezone

from . import fetch as fetch_mod
from . import ingest as ingest_mod
from . import publish as publish_mod
from . import repo
from .util import UTC, dt_to_iso, iso_to_dt, utcnow_iso

# ------------------------------------------------------------------ 退出码
#: 与 publish（0/1/2）不冲突：3 = 已被另一实例锁定，本轮**没有**执行任何 fetch/publish。
EXIT_OK = 0
EXIT_ROUND_FAILED = 1
EXIT_LOCKED = 3

LOCK_REASON_CREATED = "created"
LOCK_REASON_STALE_DEAD_PID = "stale_dead_pid"
LOCK_REASON_STALE_HEARTBEAT = "stale_heartbeat"
LOCK_REASON_HELD_LIVE_PID = "held_by_live_process"
LOCK_REASON_HELD_SUSPECT = "held_by_live_process_with_stale_heartbeat"
LOCK_REASON_HELD_REMOTE = "held_by_remote_host"
LOCK_REASON_HELD_UNREADABLE = "held_metadata_unreadable"
LOCK_REASON_HELD_STEAL_RACE = "held_concurrent_steal"
LOCK_REASON_TAKEN_OVER = "stale_taken_over"
#: 运行过程中**失去**锁（心跳发现 token 已被替换 / 锁文件被删或损坏 / 刷新失败）。
#: 与 ``EXIT_LOCKED`` 的区别：那是「一开始就没拿到」，这是「拿到后中途丢了」。
LOCK_REASON_LOST = "lock_lost"

#: 心跳周期：下限避免疯狂刷盘，上限保证再长的轮次间隔也不会被误判 stale。
HEARTBEAT_MIN_INTERVAL_SECONDS = 0.25
HEARTBEAT_MAX_INTERVAL_SECONDS = 300.0

#: 心跳年龄「允许逼近 stale 阈值」的比例：实际周期恒 ≤ stale × 该比例（即至少 4 倍余量）。
HEARTBEAT_STALE_MARGIN_RATIO = 0.25

#: 「已发布」= 文件确实被写了。用于判定 last_success_publish_at。
PUBLISHED_STATUSES = (
    publish_mod.STATUS_OK,
    publish_mod.STATUS_DEGRADED_FIXED_ONLY,
)

#: /healthz 与调度共用的新鲜度状态
FRESHNESS_OK = "ok"
FRESHNESS_STALE = "stale"
FRESHNESS_MISSING = "missing"

#: HTTP 服务在 live.m3u 不存在时返回的状态码（冻结口径：服务在、内容不在）
HTTP_PLAYLIST_MISSING = 503

STATUS_SCHEMA_VERSION = 1
DEFAULT_STATUS_HISTORY = 5

#: 长时间休眠切片：保证 Ctrl+C / SIGTERM 能在 ~0.25s 内被感知并干净退出
#: （否则一次 ``time.sleep(3h)`` 会让「可控停止」形同虚设）。
SLEEP_POLL_SECONDS = 0.25


# ==================================================================== 配置

@dataclasses.dataclass
class RuntimeSettings:
    """``[runtime]`` 段。字段默认值与 :data:`liptv.config.DEFAULT_CONFIG` 保持一致。"""

    interval_seconds: int = 10800
    run_on_start: bool = True
    lock_path: str = "out/liptv.lock"
    status_path: str = "out/runtime-status.json"
    stale_after_seconds: int = 21600
    status_history: int = DEFAULT_STATUS_HISTORY
    include_dynamic: bool = False
    dynamic_sources: tuple[str, ...] = ()
    require_dynamic: bool = False

    @classmethod
    def from_mapping(cls, raw: dict | None) -> "RuntimeSettings":
        raw = dict(raw or {})
        interval = max(1, int(raw.get("interval_seconds", cls.interval_seconds)))
        stale = max(1, int(raw.get("stale_after_seconds", cls.stale_after_seconds)))
        sources = raw.get("dynamic_sources", cls.dynamic_sources) or ()
        return cls(
            interval_seconds=interval,
            run_on_start=bool(raw.get("run_on_start", cls.run_on_start)),
            lock_path=str(raw.get("lock_path", cls.lock_path)),
            status_path=str(raw.get("status_path", cls.status_path)),
            stale_after_seconds=stale,
            status_history=max(1, int(raw.get("status_history", cls.status_history))),
            include_dynamic=bool(raw.get("include_dynamic", cls.include_dynamic)),
            dynamic_sources=tuple(str(item) for item in sources),
            require_dynamic=bool(raw.get("require_dynamic", cls.require_dynamic)),
        )


@dataclasses.dataclass
class ServerSettings:
    """``[server]`` 段。字段默认值与 :data:`liptv.config.DEFAULT_CONFIG` 保持一致。"""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    playlist_path: str = "/live.m3u"
    health_path: str = "/healthz"

    @classmethod
    def from_mapping(cls, raw: dict | None) -> "ServerSettings":
        raw = dict(raw or {})
        return cls(
            enabled=bool(raw.get("enabled", cls.enabled)),
            host=str(raw.get("host", cls.host)),
            port=int(raw.get("port", cls.port)),
            playlist_path=_normalize_route(raw.get("playlist_path", cls.playlist_path)),
            health_path=_normalize_route(raw.get("health_path", cls.health_path)),
        )


def _normalize_route(value: object) -> str:
    """把配置里的路由规范成以 ``/`` 开头的形式（只影响展示与匹配，不参与磁盘路径拼接）。"""
    text = str(value or "").strip() or "/"
    return text if text.startswith("/") else "/" + text


# ============================================================ 单实例锁

class LockError(RuntimeError):
    """无法取得单实例锁；``reason`` 是可枚举的判定结果。"""

    def __init__(self, reason: str, message: str, *, holder: dict | None = None):
        super().__init__(message)
        self.reason = reason
        self.holder = holder or {}


def _pid_alive(pid: int) -> bool | None:
    """判断 PID 是否存活。**绝不用 os.kill(pid, 0) 猜 Windows**（那会真的杀进程）。

    Windows 走 ``OpenProcess`` + ``WaitForSingleObject(0)``（只查询，不影响目标进程）；
    POSIX 走 ``os.kill(pid, 0)``。无法判断时返回 ``None``。
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # 存在但不属于当前用户
        return True
    except OSError:
        return None
    return True


def _pid_alive_windows(pid: int) -> bool | None:  # pragma: no cover - 平台分支
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    WAIT_TIMEOUT = 0x00000102
    WAIT_OBJECT_0 = 0x00000000
    ERROR_ACCESS_DENIED = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE

    handle = kernel32.OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        # 5 = 无权限（进程存在）；87 = 参数错误（PID 不存在）
        return True if ctypes.get_last_error() == ERROR_ACCESS_DENIED else False
    try:
        rc = kernel32.WaitForSingleObject(handle, 0)
        if rc == WAIT_TIMEOUT:
            return True
        if rc == WAIT_OBJECT_0:
            return False
        return None
    finally:
        kernel32.CloseHandle(handle)


#: Windows 上 ``os.replace`` 的「可重试」错误码：5=拒绝访问（目标被打开）、32=被占用。
_TRANSIENT_REPLACE_WINERRORS = (5, 32)
_REPLACE_RETRY_ATTEMPTS = 12
_REPLACE_RETRY_DELAY_SECONDS = 0.1

#: 临时文件名的唯一序号。**必须唯一**：心跳线程与业务线程会并发写同一个锁文件，
#: 若两个线程算出同一个临时名，就会出现「你写一半我替换 → FileNotFoundError」。
_TMP_SEQ = itertools.count()


def _unique_tmp_path(target: pathlib.Path) -> pathlib.Path:
    """同目录下的唯一临时文件名（含 pid + 全局递增序号，跨线程/跨进程都不撞）。"""
    return target.with_name(f"{target.name}.tmp{os.getpid()}-{next(_TMP_SEQ)}")


def _is_transient_replace_error(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in _TRANSIENT_REPLACE_WINERRORS


#: 共享读取的重试口径（比写入短：读不上就尽快失败，由调用方保守处理）。
_READ_RETRY_ATTEMPTS = 8
_READ_RETRY_DELAY_SECONDS = 0.02


def _read_shared_bytes(path) -> bytes:
    """读文件，并**允许别人同时替换它**（Windows 共享语义）。

    为什么不能用 ``Path.read_bytes()``：默认打开的句柄不允许别人 ``os.replace`` 覆盖该文件
    （对方会拿到 ``WinError 5``）；反过来，当替换正在进行时，默认读取自己也可能瞬时拿到
    ``ERROR_ACCESS_DENIED``。锁文件现在被「每轮 + 每次心跳 + 其它实例的 acquire」频繁读写，
    两边必须都能容忍对方，否则一次抖动就会被误判成「心跳失败」而让长跑停摆。

    因此统一走 ``CreateFileW(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)``
    —— 与 ``liptv.server`` 读只读播放列表是同一手法（两处独立实现，成因相同）。
    瞬时的 5/32 会做几次极短重试；文件确实不存在则抛 ``FileNotFoundError``。
    """
    if os.name != "nt":
        with open(path, "rb") as handle:  # pragma: no cover - 平台分支
            return handle.read()

    import ctypes
    import msvcrt
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    ERROR_FILE_NOT_FOUND = 2
    ERROR_PATH_NOT_FOUND = 3

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    target = str(path)
    handle = None
    for attempt in range(_READ_RETRY_ATTEMPTS):
        handle = kernel32.CreateFileW(
            target, GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
        )
        if handle and handle != INVALID_HANDLE_VALUE:
            break
        last_error = ctypes.get_last_error()
        if last_error in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
            raise FileNotFoundError(ERROR_FILE_NOT_FOUND, "锁文件不存在", target)
        if last_error not in _TRANSIENT_REPLACE_WINERRORS or attempt + 1 >= _READ_RETRY_ATTEMPTS:
            raise OSError(last_error, f"无法共享读取 {target}")
        time.sleep(_READ_RETRY_DELAY_SECONDS)
    if not handle or handle == INVALID_HANDLE_VALUE:  # pragma: no cover - 循环必然 break 或 raise
        raise OSError(0, f"无法共享读取 {target}")

    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:   # 关闭流 = 关闭句柄
        return stream.read()


def _replace_with_retry(src, dst) -> None:
    """``os.replace`` + 针对「目标瞬时被打开」的窄口径有界重试。

    Windows 上只要目标文件被**任何**句柄打开（含同进程的读者），``os.replace`` 就会抛
    ``PermissionError[WinError 5]``。锁文件现在**每轮 + 每次心跳**都要重写，而另一个实例的
    ``acquire`` 可能正好在同一瞬间读它 —— 不重试的话，一次抖动就会让长时间运行的 scheduler
    误判「心跳失败」而停摆。重试**只**对 winerror 5/32 生效，其它 ``OSError`` 立即上抛。

    与 ``liptv.m3u`` 中发布路径的重试口径一致（同一物理约束，两处独立实现）。
    """
    for attempt in range(_REPLACE_RETRY_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if not _is_transient_replace_error(exc) or attempt + 1 >= _REPLACE_RETRY_ATTEMPTS:
                raise
            time.sleep(_REPLACE_RETRY_DELAY_SECONDS)


@dataclasses.dataclass
class LockInfo:
    """锁文件的元数据。"""

    pid: int
    hostname: str
    token: str
    created_at: str
    heartbeat_at: str
    interval_seconds: int
    version: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def resolve_heartbeat_interval(stale_after_seconds: int, interval_seconds: int) -> float:
    """算出锁心跳周期（秒）。

    约束：

    * **必须显著小于 stale 阈值** —— 取 ``stale_after_seconds × 0.25``，即至少 4 倍余量；
    * 也不该超过轮次间隔的 1/4，否则「一轮很快、休眠很久」的场景仍会走满阈值；
    * 夹在 ``[HEARTBEAT_MIN_INTERVAL_SECONDS, HEARTBEAT_MAX_INTERVAL_SECONDS]`` 内，
      避免极小间隔疯狂刷盘、也避免超长间隔（例如 6h stale 下 300s 已经很宽裕）。

    例：``stale=21600, interval=10800`` → ``300s``；``stale=10, interval=60`` → ``2.5s``。
    """
    stale = max(1.0, float(stale_after_seconds))
    interval = max(1.0, float(interval_seconds))
    raw = min(stale * HEARTBEAT_STALE_MARGIN_RATIO, interval * 0.25, HEARTBEAT_MAX_INTERVAL_SECONDS)
    return max(HEARTBEAT_MIN_INTERVAL_SECONDS, raw)


class SingleInstanceLock:
    """跨 Windows/Linux 的最小可行单实例锁。

    用法::

        lock = SingleInstanceLock(path, stale_after_seconds=21600)
        lock.acquire()          # 拿不到就抛 LockError（含可解释的 reason）
        try:
            ...
            lock.heartbeat()    # 每轮开始/结束 + 长休眠期间周期刷新（由 Scheduler 驱动）
        finally:
            lock.release()      # 只删「token 还是自己的」那把锁

    ``now`` 既可以是固定时间戳字符串，也可以是一个返回 ISO 字符串的**可调用对象**
    （测试里配合 fake clock 用；两者互斥，传可调用对象时以它为准）。
    """

    def __init__(
        self,
        path,
        *,
        stale_after_seconds: int = 21600,
        interval_seconds: int = 10800,
        version: str = "0.1.0",
        now=None,
    ):
        self.path = pathlib.Path(path)
        self.stale_after_seconds = max(1, int(stale_after_seconds))
        self.interval_seconds = int(interval_seconds)
        self.version = version
        if callable(now):
            self._now: str | None = None
            self._now_fn = now
        else:
            self._now = now
            self._now_fn = None
        self._token: str | None = None
        self._info: LockInfo | None = None
        self._takeover_from: dict | None = None
        self._owns = False
        #: 进程内串行化锁文件 I/O。心跳线程与业务线程会同时碰这把锁文件，
        #: 必须避免「一边 os.replace、一边 open」这种自伤（Windows 上会直接 WinError 5）。
        #: RLock：_read_metadata / _write 会被 acquire/heartbeat/release 嵌套调用。
        self._io = threading.RLock()
        #: 保守放弃所有权的原因（token 被替换 / 锁文件不可读）；None = 未丢
        self._lost_reason: str | None = None
        #: 最近一次心跳失败的文字说明（写盘失败等），仅用于日志/诊断
        self.last_error: str | None = None

    # ------------------------------------------------------------ 只读属性
    @property
    def acquired(self) -> bool:
        return self._owns

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def info(self) -> LockInfo | None:
        return self._info

    @property
    def taken_over_from(self) -> dict | None:
        """若本次是接管了一把 stale 锁，返回原持有者元数据（供日志/报告使用）。"""
        return self._takeover_from

    @property
    def ownership_lost(self) -> bool:
        """是否因为「无法确认锁归属」而保守放弃所有权（心跳 token 被换 / 锁文件不可读）。"""
        return self._lost_reason is not None

    @property
    def lost_reason(self) -> str | None:
        return self._lost_reason

    # ------------------------------------------------------------ 内部工具
    def _stamp(self) -> str:
        if self._now_fn is not None:
            return self._now_fn()
        return self._now or utcnow_iso()

    def _read_metadata(self) -> tuple[dict | None, str]:
        """读锁文件。返回 (元数据或 None, 判定用的「心跳时间」ISO 字符串)。

        读不出内容（不存在 / 空 / 损坏 / 缺 ``pid``）一律返回 ``(None, …)``；
        只有「重试之后仍然打不开文件」（磁盘/权限异常）才抛出 ``OSError``，
        由调用方决定语义（``acquire`` 拒绝抢锁、``heartbeat`` 停止、``release`` 不删）。
        """
        with self._io:
            try:
                raw = _read_shared_bytes(self.path)
            except FileNotFoundError:
                return None, ""
            try:
                mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                return None, ""
            except OSError:  # pragma: no cover - 竞态
                mtime = time.time()
        fallback = dt_to_iso(datetime.fromtimestamp(mtime, timezone.utc))
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, fallback
        if not isinstance(data, dict) or "pid" not in data:
            return None, fallback
        heartbeat = str(data.get("heartbeat_at") or data.get("created_at") or fallback)
        return data, heartbeat

    def _heartbeat_age(self, heartbeat: str) -> float:
        try:
            return max(0.0, (iso_to_dt(self._stamp()) - iso_to_dt(heartbeat)).total_seconds())
        except (ValueError, TypeError):  # 时间戳坏掉 → 当成「无法判断」，按最老处理
            return float("inf")

    def _write(self, target: pathlib.Path, info: LockInfo) -> None:
        with self._io:
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(info.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            tmp = _unique_tmp_path(target)
            tmp.write_text(payload + "\n", encoding="utf-8", newline="\n")
            try:
                _replace_with_retry(tmp, target)
            finally:
                if tmp.exists():  # pragma: no cover - 仅异常路径
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

    def _new_info(self) -> LockInfo:
        stamp = self._stamp()
        return LockInfo(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            token=secrets.token_hex(8),
            created_at=stamp,
            heartbeat_at=stamp,
            interval_seconds=self.interval_seconds,
            version=self.version,
        )

    # ---------------------------------------------------------------- 获取
    def acquire(self) -> LockInfo:
        """尝试取得锁；失败抛 :class:`LockError`（不执行任何业务动作）。"""
        publish_mod.guard_runtime_output_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        info = self._new_info()

        if self._try_create(info):
            self._install(info, reason=LOCK_REASON_CREATED)
            return info

        try:
            data, heartbeat = self._read_metadata()
        except OSError as exc:
            # 文件在，但连读都读不了（权限/占用）：无法判断持有者 ⇒ 保守拒绝，绝不抢锁。
            raise LockError(
                LOCK_REASON_HELD_UNREADABLE,
                f"锁文件 {self.path} 存在但无法读取（{exc}）：无法判断持有者，拒绝抢锁。",
                holder={"path": str(self.path), "error": str(exc)},
            ) from exc
        age = self._heartbeat_age(heartbeat)
        stale = age >= self.stale_after_seconds

        if data is None:
            # 元数据损坏：新鲜 ⇒ 不敢抢；过期 ⇒ 按 mtime 判 stale
            if not stale:
                raise LockError(
                    LOCK_REASON_HELD_UNREADABLE,
                    f"锁文件 {self.path} 内容无法解析，且修改时间距今仅 {int(age)}s "
                    f"（< {self.stale_after_seconds}s）：无法判断持有者，拒绝抢锁。",
                    holder={"path": str(self.path), "heartbeat_age_seconds": int(age)},
                )
            self._takeover(info, data=None, reason=LOCK_REASON_STALE_HEARTBEAT)
            return info

        same_host = str(data.get("hostname") or "") == socket.gethostname()
        pid = int(data.get("pid") or 0)
        metadata = {k: data.get(k) for k in ("pid", "hostname", "created_at", "heartbeat_at")}
        metadata["heartbeat_age_seconds"] = None if age == float("inf") else int(age)

        if same_host:
            alive = _pid_alive(pid)
            if alive is False:
                self._takeover(info, data=data, reason=LOCK_REASON_STALE_DEAD_PID)
                return info
            if alive is True and stale:
                # 进程还在但长时间没心跳：**不抢**，而是给出明确的清理指引
                raise LockError(
                    LOCK_REASON_HELD_SUSPECT,
                    f"锁被本机进程 pid={pid} 持有，但它已 {int(age)}s 未刷新心跳"
                    f"（> {self.stale_after_seconds}s）。为避免两个写入者同时改库存，"
                    f"本实例不抢锁；请确认该进程状态后手动删除 {self.path}。",
                    holder=metadata,
                )
            raise LockError(
                LOCK_REASON_HELD_LIVE_PID,
                f"锁被本机进程 pid={pid} 持有（心跳 {int(age)}s 前）。"
                f"同一套 data/output 目录不允许两个 scheduler，本实例退出。",
                holder=metadata,
            )

        # 他机持有：无法判定进程存活 → 只看心跳新鲜度
        if stale:
            self._takeover(info, data=data, reason=LOCK_REASON_STALE_HEARTBEAT)
            return info
        raise LockError(
            LOCK_REASON_HELD_REMOTE,
            f"锁被主机 {data.get('hostname')!r} 的进程 pid={pid} 持有，"
            f"心跳仅 {int(age)}s 前；无法验证远端进程存活，因此不抢锁。",
            holder=metadata,
        )

    def _try_create(self, info: LockInfo) -> bool:
        """原子创建锁文件（O_CREAT|O_EXCL）。成功则写元数据。"""
        with self._io:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return False
            os.close(fd)
            self._write(self.path, info)
            return True

    def _takeover(self, info: LockInfo, *, data: dict | None, reason: str) -> None:
        """接管一把 stale 锁：先独占创建 ``.steal`` 占位，再原子替换。"""
        steal_path = self.path.with_name(self.path.name + ".steal")
        try:
            fd = os.open(steal_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LockError(
                LOCK_REASON_HELD_STEAL_RACE,
                f"检测到锁已过期，但另一个实例正在接管（{steal_path} 已存在）；"
                f"本实例退出，不参与抢锁。",
            ) from None
        try:
            os.close(fd)
            try:
                mtime = self.path.stat().st_mtime
            except OSError:  # pragma: no cover - 竞态
                mtime = time.time()
            self._takeover_from = {
                "reason": reason,
                "previous": data or {"metadata": "unreadable"},
                "lock_age_seconds": int(
                    max(0.0, time.time() - mtime)
                ),
                "stale_after_seconds": self.stale_after_seconds,
            }
            self._write(self.path, info)
        finally:
            try:
                steal_path.unlink()
            except OSError:
                pass
        self._install(info, reason=reason)

    def _install(self, info: LockInfo, *, reason: str) -> None:
        self._token = info.token
        self._info = info
        self._owns = True
        self._lost_reason = None
        self.last_error = None
        if reason == LOCK_REASON_CREATED:
            self._takeover_from = None

    # ---------------------------------------------------------------- 维护
    def _lose_ownership(self, reason: str) -> None:
        """保守放弃所有权：**不删除、不覆盖**任何锁文件（因为无法证明它是自己的）。"""
        self._owns = False
        self._lost_reason = reason

    def heartbeat(self) -> bool:
        """刷新心跳（原子替换）。返回 ``False`` = **已无法确认锁归属，必须停止**。

        fail-closed 语义（与 :meth:`release` 一致）：

        * 元数据读不出（文件缺失 / 空文件 / 损坏 JSON / 缺 ``pid`` 字段）⇒ 放弃所有权、
          **绝不覆盖**未知锁，返回 ``False``；
        * 元数据能读但 ``token`` 不是自己的 ⇒ 锁已被他人接管，放弃所有权，返回 ``False``；
        * 只有确认 ``token`` 仍是自己的，才写回新的 ``heartbeat_at``。

        写盘失败（``OSError``）时**不**放弃所有权（文件里仍是自己的 token，只是刷新失败），
        但仍返回 ``False``：调用方必须停止，不能带着可能过期的锁继续写。

        「文件打不开」（``OSError``，区别于「打得开但内容不可解析」）同样**不**放弃所有权：
        那是暂时性 I/O 故障，不代表锁易主；但也返回 ``False`` 让调用方停下来。
        """
        if not self._owns or self._token is None:
            return False
        try:
            current, _ = self._read_metadata()
        except OSError as exc:
            self.last_error = f"读取锁文件失败：{exc}"
            return False
        if not isinstance(current, dict):
            self.last_error = "锁文件缺失或内容无法解析"
            self._lose_ownership(LOCK_REASON_LOST)
            return False
        if current.get("token") != self._token:
            self.last_error = "锁文件 token 已被其它实例替换"
            self._lose_ownership(LOCK_REASON_LOST)
            return False
        assert self._info is not None
        self._info = dataclasses.replace(self._info, heartbeat_at=self._stamp())
        try:
            self._write(self.path, self._info)
        except OSError as exc:  # 刷新失败：保留所有权（文件还是自己的），但调用方必须停
            self.last_error = f"心跳写盘失败：{exc}"
            return False
        self.last_error = None
        return True

    def release(self) -> bool:
        """释放锁；**只删自己的那一把**。

        fail-closed：只有**能成功解析锁文件、且其中的 ``token`` 与本实例一致**时才 ``unlink``。
        以下情况一律**不删**并返回 ``False``（宁可留下一把需要人工清理的锁，
        也绝不误删另一个实例正在使用的锁）：

        * 文件已不存在；
        * 内容为空 / 损坏 JSON / 缺 ``pid`` 字段（无法证明是自己的）；
        * 能解析但 ``token`` 不同（已被他人接管）；
        * 文件存在但读不出来（I/O 故障，同样无法证明归属）。
        """
        if not self._owns:
            return False
        self._owns = False
        try:
            current, _ = self._read_metadata()
        except OSError as exc:
            self.last_error = f"释放前读取锁文件失败：{exc}"
            return False
        if not isinstance(current, dict):
            return False
        if current.get("token") != self._token:
            return False
        try:
            with self._io:
                self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:  # pragma: no cover - 权限等异常
            self.last_error = f"删除锁文件失败：{exc}"
            return False

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


class LockHeartbeat:
    """后台按固定周期刷新单实例锁心跳（覆盖「单轮本身很久」的窗口）。

    为什么需要它：只在轮次边界刷新心跳时，一次很慢的抓取（或一次长轮次）会让
    ``heartbeat_at`` 长时间不动，共享目录/多主机场景下就会被判 stale 并接管 ——
    这正是 QA-004A 的失败模式。线程按 ``interval_seconds`` 独立推进，与轮次解耦。

    一旦 :meth:`SingleInstanceLock.heartbeat` 返回 ``False``（丢掉锁 / 刷新失败），
    线程立即停止并回调 ``on_lost``（调度器据此 ``request_stop``）。
    线程是 daemon：进程退出不会因为它在等而挂住。
    """

    def __init__(
        self,
        lock: SingleInstanceLock,
        *,
        interval_seconds: float,
        on_lost=None,
        logger=None,
    ):
        self.lock = lock
        self.interval_seconds = max(0.01, float(interval_seconds))
        self._on_lost = on_lost
        self._logger = logger if logger is not None else (lambda _msg: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: 统计（供状态/测试断言）
        self.beats = 0
        self.misses = 0

    # ------------------------------------------------------------- 生命周期
    def start(self) -> "LockHeartbeat":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._loop, name="liptv-lock-heartbeat", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, *, join_timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(join_timeout)

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def __enter__(self) -> "LockHeartbeat":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.stop()

    # ---------------------------------------------------------------- 内部
    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                ok = self.lock.heartbeat()
            except Exception as exc:  # noqa: BLE001 — 线程绝不能因为异常静默死掉
                self.lock.last_error = f"心跳线程异常：{type(exc).__name__}: {exc}"
                ok = False
            if ok:
                self.beats += 1
                continue
            self.misses += 1
            self._stop.set()
            self._logger(
                f"[runtime] 锁心跳中断：{self.lock.last_error or '无法确认锁归属'}；"
                f"调度器将停止，不再执行后续 fetch/publish"
            )
            if self._on_lost is not None:
                self._on_lost()
            return


# ============================================================ 状态文件

# ---------------------------------------------- 让「替换」容忍并发读者（QA-004C）
#
# 实测（2026-10-01，Windows 10 / Python 3.13，证据见 REPORTS/TASK-004-REPORT.md §13.2）：
#
#   API                            读者 FILE_SHARE_READ    读者 FILE_SHARE_READ|WRITE|DELETE
#   MoveFileEx(REPLACE_EXISTING)          WinError 5                WinError 5   ← os.replace 走这条
#   ReplaceFileW                          WinError 32               OK
#
# 结论：``os.replace`` **根本不认** ``FILE_SHARE_DELETE`` —— 目标只要被任何句柄打开就失败；
# 而 ``ReplaceFileW`` 认。``runtime-status.json`` 会被 ``/healthz`` 高频读取，
# 所以状态文件的替换必须走 ``ReplaceFileW``（只加读侧重试实测仍有 2/500 残留失败）。
# 锁文件与 ``live.m3u`` 沿用各自已验证的路径，本轮不动。

#: 状态文件替换的重试口径：延迟比锁文件短得多（状态写频率高、单次成本极低、读者持有
#: 句柄的时间在微秒级），总预算 ≈ 0.4s，仍然「有界」。
_STATUS_REPLACE_ATTEMPTS = 40
_STATUS_REPLACE_DELAY_SECONDS = 0.01

#: 状态文件读取的「瞬时不存在」重试：``ReplaceFileW`` 换名的一瞬间目标可能短暂不在。
_STATUS_READ_ATTEMPTS = 6
_STATUS_READ_DELAY_SECONDS = 0.01


def _replace_file_sharing_readers(src, dst) -> None:
    """原子替换，且**尊重读者句柄的 FILE_SHARE_DELETE**。

    Windows 上走 ``ReplaceFileW``（被替换文件必须已存在，因此目标不存在时退化为
    ``os.replace``）；其它平台直接用 ``os.replace``。失败时抛出的 ``OSError`` 带
    ``winerror``，好让外层的窄口径重试识别 5/32。
    """
    if os.name != "nt" or not os.path.exists(dst):
        os.replace(src, dst)
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ReplaceFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID,
    ]
    kernel32.ReplaceFileW.restype = wintypes.BOOL

    if not kernel32.ReplaceFileW(str(dst), str(src), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def _replace_status_json(src, dst) -> None:
    """状态 JSON 的原子替换：``ReplaceFileW`` + 窄口径有界重试（只对 winerror 5/32）。"""
    for attempt in range(_STATUS_REPLACE_ATTEMPTS):
        try:
            _replace_file_sharing_readers(src, dst)
            return
        except OSError as exc:
            if not _is_transient_replace_error(exc) or attempt + 1 >= _STATUS_REPLACE_ATTEMPTS:
                raise
            time.sleep(_STATUS_REPLACE_DELAY_SECONDS)


def _atomic_write_json(path: pathlib.Path, payload: dict) -> dict:
    """原子写 JSON 到 path（同目录临时文件 + 原子替换）。返回字节数与校验和。

    替换走 :func:`_replace_status_json`（Windows: ``ReplaceFileW``）：本文件会被
    ``/healthz`` 高频读取（见 ``StatusStore.read``），而 ``os.replace`` 在 Windows 上
    只要目标被**任何**句柄打开就抛 ``WinError 5`` —— 状态更新会被整片丢弃。
    重试口径仍然很窄：只对 winerror 5/32 生效，权限、磁盘满、非法路径等其它
    ``OSError`` 一律**立即上抛**，绝不被吞掉。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = _unique_tmp_path(path)
    tmp.write_text(text, encoding="utf-8", newline="\n")
    try:
        _replace_status_json(tmp, path)
    finally:
        if tmp.exists():  # pragma: no cover - 仅异常路径
            try:
                tmp.unlink()
            except OSError:
                pass
    encoded = text.encode("utf-8")
    return {"bytes": len(encoded), "checksum": publish_mod.sha256_hex(text)}


class StatusStore:
    """运行期状态 JSON（脱敏、原子写、最近轮次条数封顶）。"""

    def __init__(self, path, *, max_rounds: int = DEFAULT_STATUS_HISTORY, version: str = "0.1.0"):
        self.path = pathlib.Path(path)
        self.max_rounds = max(1, int(max_rounds))
        self.version = version

    # ------------------------------------------------------------------ IO
    def read(self) -> dict:
        """读状态 JSON；**读句柄不得阻塞写入侧的原子替换**。

        ``/healthz`` 每次请求都会走到这里，而 scheduler 每轮（以及 ``set_next_run``）都会
        原子替换同一个文件。若这里用 ``Path.read_text()``，默认打开的句柄不允许别人替换
        目标 —— 高频健康检查会把状态写入整片顶失败（大G实测 500 次写里 291 次
        ``WinError 5``），于是健康端点长期展示旧的 ``last_success_publish_at``。

        因此改走 ``_read_shared_bytes``（``FILE_SHARE_READ|WRITE|DELETE``，与锁文件和只读
        播放列表同一手法）。共享读是**必要**条件，真正让写入不再被顶失败的是
        ``_replace_status_json`` 里的 ``ReplaceFileW``（实测表见该函数上方）。
        换名的一瞬间目标可能短暂不存在，这里对 ``FileNotFoundError`` 做几次极短重试。
        其它任何读不到 / 读不动的异常都退化成「没有状态」，由调用方按 missing / stale
        保守处理，绝不构造假数据。
        """
        for attempt in range(_STATUS_READ_ATTEMPTS):
            try:
                raw = _read_shared_bytes(self.path).decode("utf-8")
                break
            except FileNotFoundError:
                if attempt + 1 >= _STATUS_READ_ATTEMPTS:
                    return {}
                time.sleep(_STATUS_READ_DELAY_SECONDS)
            except OSError:
                return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def write(self, document: dict) -> dict:
        publish_mod.guard_runtime_output_path(self.path)
        document = dict(document)
        document["schema"] = STATUS_SCHEMA_VERSION
        document["version"] = self.version
        rounds = list(document.get("rounds") or [])[-self.max_rounds :]
        document["rounds"] = rounds
        document["updated_at"] = utcnow_iso()
        return _atomic_write_json(self.path, document)

    def record_round(self, entry: dict) -> dict:
        """记一轮：更新 current / rounds（封顶）/ 下次计划时间；只在**已发布**时推进 last_success。"""
        document = self.read()
        rounds = list(document.get("rounds") or [])
        rounds.append(entry)
        document["rounds"] = rounds[-self.max_rounds :]
        document["current_round"] = entry
        document["last_run_at"] = entry.get("finished_at") or entry.get("started_at")
        document["last_run_outcome"] = entry.get("outcome")
        document["last_run_error_category"] = (entry.get("error") or {}).get("category")
        if entry.get("published") and entry.get("publish_status") in PUBLISHED_STATUSES:
            document["last_success_publish_at"] = entry.get("finished_at")
            document["last_success_publish_status"] = entry.get("publish_status")
        if entry.get("next_run_at") is not None:
            document["next_run_at"] = entry["next_run_at"]
        document["started_at"] = document.get("started_at") or entry.get("started_at")
        document["pid"] = os.getpid()
        self.write(document)
        return document

    def set_next_run(self, next_run_at: str | None) -> None:
        document = self.read()
        document["next_run_at"] = next_run_at
        self.write(document)


# ============================================================ 一轮运行链

def _summarize_fetch_result(result: dict) -> dict:
    """把 ingest 的抓取结果裁成**不含任何 URL**的摘要。"""
    return {
        "source_name": result.get("source_name"),
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "error_category": result.get("error_category"),
        "error": publish_mod.redact_text(result.get("error")),
        "entries": int(result.get("entries") or 0),
        "created": int(result.get("created") or 0),
        "updated": int(result.get("updated") or 0),
        "deactivated": int(result.get("deactivated") or 0),
        "reactivated": int(result.get("reactivated") or 0),
        "duration_ms": int(result.get("duration_ms") or 0),
    }


def resolve_dynamic_sources(conn, tokens) -> list[dict]:
    """把 ``[runtime] dynamic_sources`` / CLI 的登记名解析成来源行。

    优先级与 TASK-003 ``publish`` 的 ``--dynamic`` **完全一致**（勿改）：
    显式 token（可重复，允许指向已登记但**未启用**的源）→ 留空时回落数据库里
    所有 **enabled** 的 ``dynamic_event_m3u`` 源。任何情况下都只认已登记的源，
    绝不凭空造 URL。
    """
    explicit = [str(token) for token in (tokens or ()) if str(token).strip()]
    resolved: list[dict] = []

    if explicit:
        for token in explicit:
            row = repo.resolve_source(conn, token)
            if row is None:
                raise ValueError(f"找不到动态来源：{token}")
            if row["kind"] != ingest_mod.KIND_DYNAMIC:
                raise ValueError(
                    f"来源 {row['name']!r} 的 kind 是 {row['kind']!r}，不是 "
                    f"{ingest_mod.KIND_DYNAMIC}；运行时动态源只接受动态赛事源。"
                )
            resolved.append(
                {"id": int(row["id"]), "name": row["name"], "kind": row["kind"],
                 "url": row["url"]}
            )
        return resolved

    for row in repo.list_sources_by_kind(conn, ingest_mod.KIND_DYNAMIC, enabled_only=True):
        resolved.append(
            {"id": int(row["id"]), "name": row["name"], "kind": row["kind"],
             "url": row["url"]}
        )
    return resolved


def execute_round(
    conn,
    *,
    output_path,
    group_order,
    selection_kwargs: dict | None = None,
    keep_previous: bool = True,
    include_dynamic: bool = False,
    dynamic_tokens=(),
    dynamic_filters: dict | None = None,
    dynamic_group_title: str | None = None,
    require_dynamic: bool = False,
    limits: fetch_mod.FetchLimits | None = None,
    summary_path=None,
    stamp: str | None = None,
    opener=None,
    fetch_fixed: bool = True,
) -> dict:
    """执行一轮完整运行链（**不涉及锁与 HTTP**，便于单测）。

    顺序严格按 TASK-004 §1：① 抓取所有 enabled ``fixed_m3u`` → ② ``stream-sync``
    → ③ 按配置显式决定是否拉动态源 → ④ 调用 TASK-003 的统一 ``publish``。
    """
    stamp = stamp or utcnow_iso()
    started_at = stamp
    started = time.perf_counter()

    fetch_results: list[dict] = []
    fetch_errors: list[dict] = []

    # ---- ① 抓取：单个来源失败不得阻塞其它来源 ----
    if fetch_fixed:
        targets = repo.list_sources_by_kind(conn, ingest_mod.KIND_FIXED, enabled_only=True)
        for source in targets:
            try:
                result = ingest_mod.ingest_fixed_source(
                    conn, source, limits=limits, now=stamp, opener=opener
                )
            except Exception as exc:  # noqa: BLE001 — 一条来源炸掉不能带走整轮
                try:
                    conn.rollback()
                except Exception:  # pragma: no cover - 连接已坏就没什么可回滚的
                    pass
                result = {
                    "source_id": int(source["id"]),
                    "source_name": source["name"],
                    "ok": False,
                    "status": fetch_mod.ERROR_UNKNOWN,
                    "error": f"抓取时抛出未预期异常：{exc}",
                    "error_category": fetch_mod.ERROR_UNKNOWN,
                }
            summary = _summarize_fetch_result(result)
            fetch_results.append(summary)
            if not summary["ok"]:
                fetch_errors.append(
                    {
                        "source_name": summary["source_name"],
                        "status": summary["status"],
                        "error_category": summary["error_category"],
                    }
                )

    # ---- ② stream-sync：只归集，**不**创造 canonical/binding ----
    stream_sync = repo.sync_streams(conn, now=stamp)
    conn.commit()

    # ---- ③④ 组合与发布（TASK-003 语义原样复用） ----
    dynamic_sources: list[dict] = []
    dynamic_error: str | None = None
    if include_dynamic:
        try:
            dynamic_sources = resolve_dynamic_sources(conn, dynamic_tokens)
        except ValueError as exc:
            dynamic_error = str(exc)

    if dynamic_error:
        publish_summary = {
            "status": publish_mod.STATUS_REJECTED_VALIDATION,
            "exit_code": publish_mod.EXIT_REJECTED,
            "published": False,
            "reason": dynamic_error,
        }
    else:
        result = publish_mod.publish(
            conn,
            output_path=output_path,
            group_order=group_order,
            selection_kwargs=selection_kwargs,
            keep_previous=keep_previous,
            include_dynamic=include_dynamic,
            dynamic_sources=dynamic_sources,
            dynamic_filters=dynamic_filters,
            dynamic_group_title=dynamic_group_title
            or publish_mod.DEFAULT_DYNAMIC_GROUP_TITLE,
            require_dynamic=require_dynamic,
            limits=limits,
            stamp=stamp,
            summary_path=summary_path,
            opener=opener,
        )
        publish_summary = {
            "status": result.get("status"),
            "exit_code": int(result.get("exit_code", publish_mod.EXIT_REJECTED)),
            "published": bool(result.get("published")),
            "fixed_count": int(result.get("fixed_count") or 0),
            "dynamic_count": int(result.get("dynamic_count") or 0),
            "channel_count": int(result.get("channel_count") or 0),
            "dynamic_fail_closed": bool(result.get("dynamic_fail_closed")),
            "dynamic_discarded": int(result.get("dynamic_discarded") or 0),
            "checksum": result.get("checksum") or result.get("expected_checksum"),
            "bytes": result.get("bytes") or result.get("expected_bytes"),
            "reason": publish_mod.redact_text(result.get("reason")),
            "summary_error": result.get("summary_error"),
        }

    errors = list(fetch_errors)
    if dynamic_error:
        errors.append({"source_name": None, "status": "DYNAMIC_RESOLVE_FAILED",
                       "error_category": fetch_mod.ERROR_UNKNOWN})

    outcome = _round_outcome(publish_summary, errors)
    return {
        "started_at": started_at,
        # 轮次时间戳统一取本轮 stamp（CLI 可用 --now 注入），保证状态文件可复现；
        # 真实耗时另由 duration_ms 记录。
        "finished_at": stamp,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "fetch": {
            "requested": len(fetch_results),
            "ok": sum(1 for item in fetch_results if item["ok"]),
            "failed": len(fetch_errors),
            "sources": fetch_results,
        },
        "stream_sync": dict(stream_sync or {}),
        "publish": publish_summary,
        "published": bool(publish_summary.get("published")),
        "publish_status": publish_summary.get("status"),
        "dynamic_fail_closed": bool(publish_summary.get("dynamic_fail_closed")),
        "errors": errors,
        "outcome": outcome,
    }


def _round_outcome(publish_summary: dict, errors: list[dict]) -> str:
    status = publish_summary.get("status")
    if publish_summary.get("published") and status == publish_mod.STATUS_OK:
        return "ok"
    if publish_summary.get("published"):
        return "degraded"
    if errors or status:
        return "failed"
    return "failed"


def round_exit_code(entry: dict) -> int:
    """把一轮结果折算成退出码：未发布但输入没问题 = 2，其余失败 = 1。"""
    publish = entry.get("publish") or {}
    code = publish.get("exit_code")
    if isinstance(code, int):
        return code
    return EXIT_ROUND_FAILED if entry.get("errors") else EXIT_OK


# ============================================================== 调度器

class Scheduler:
    """轮次循环：可注入 ``round_fn`` / ``clock`` / ``sleep``，测试不用真的等。

    传入 ``lock`` 后，本类会承担**锁的生命周期维护**（QA-004A）：

    * 每轮开始 / 结束各 :meth:`SingleInstanceLock.heartbeat` 一次；
    * 长休眠期间按 :func:`resolve_heartbeat_interval` 决定的周期刷新；
    * ``run()``（以及命令行 ``--once``）期间由 :class:`LockHeartbeat` 后台线程兜底，
      覆盖「单轮本身很久」；
    * 任何一次心跳返回 ``False`` ⇒ ``request_stop(LOCK_REASON_LOST)``，
      **不再进入下一轮、不再执行 fetch/publish**。

    不传 ``lock`` 时行为与 TASK-004 首版完全一致（纯轮次循环，不做任何锁操作）。
    """

    def __init__(
        self,
        *,
        settings: RuntimeSettings,
        round_fn,
        clock=time.monotonic,
        sleep=time.sleep,
        status_store: StatusStore | None = None,
        logger=None,
        now_fn=utcnow_iso,
        lock: SingleInstanceLock | None = None,
        heartbeat_interval_seconds: float | None = None,
        heartbeat_thread: bool = True,
    ):
        self.settings = settings
        self.round_fn = round_fn
        self._clock = clock
        self._sleep = sleep
        self.status = status_store or StatusStore(
            settings.status_path, max_rounds=settings.status_history
        )
        self._logger = logger if logger is not None else (lambda _msg: None)
        self._now_fn = now_fn
        self._stop = threading.Event()
        self._rounds = 0
        self._failed_rounds = 0
        self.stop_reason: str | None = None
        self._lock = lock
        self._heartbeat_thread_enabled = bool(heartbeat_thread)
        self._keeper: LockHeartbeat | None = None
        self._heartbeat_interval: float | None = None
        self._heartbeat_count = 0
        self._thread_beats = 0
        #: 运行中是否失去过锁（心跳判定的结果，会体现在 loop 汇总与 CLI 输出里）
        self.lock_lost = False
        if lock is not None:
            self._heartbeat_interval = (
                float(heartbeat_interval_seconds)
                if heartbeat_interval_seconds is not None
                else resolve_heartbeat_interval(
                    settings.stale_after_seconds, settings.interval_seconds
                )
            )
            if float(self._heartbeat_interval) <= 0:
                raise ValueError("heartbeat_interval_seconds 必须为正数")

    # ------------------------------------------------------------- 锁心跳
    @property
    def heartbeat_interval(self) -> float | None:
        """当前生效的心跳周期（秒）；未接锁时为 ``None``。"""
        return self._heartbeat_interval

    @property
    def heartbeat_count(self) -> int:
        """本实例成功刷新心跳的次数（不含后台线程自身的计数）。"""
        return self._heartbeat_count

    def heartbeat_now(self, context: str = "manual") -> bool:
        """刷新一次锁心跳并判归属；返回 ``False`` 表示**已失去锁，必须停止**。"""
        if self._lock is None:
            return True
        try:
            ok = self._lock.heartbeat()
        except OSError as exc:  # pragma: no cover - _write 已自行收敛，双层保险
            self._logger(f"[runtime] 心跳刷新异常（{context}）：{exc}")
            ok = False
        if ok:
            self._heartbeat_count += 1
            return True
        self.lock_lost = True
        detail = self._lock.last_error or "无法确认锁归属"
        self._logger(
            f"[runtime] 锁心跳失败（{context}）：{detail}；"
            f"本实例停止，不再执行后续 fetch/publish"
        )
        self.request_stop(LOCK_REASON_LOST)
        return False

    def start_heartbeat(self) -> LockHeartbeat | None:
        """启动后台心跳线程（幂等）。``--once`` 单轮也有长抓取，故同样适用。"""
        if self._lock is None or not self._heartbeat_thread_enabled:
            return None
        if not self._lock.acquired:
            # 已经不再持有锁：不启动线程，直接标记为丢失（绝不继续跑）
            self.lock_lost = True
            self.request_stop(LOCK_REASON_LOST)
            return None
        if self._keeper is not None and self._keeper.running:
            return self._keeper

        def _on_lost() -> None:
            self.lock_lost = True
            self.request_stop(LOCK_REASON_LOST)

        keeper = LockHeartbeat(
            self._lock,
            interval_seconds=float(self._heartbeat_interval or HEARTBEAT_MAX_INTERVAL_SECONDS),
            on_lost=_on_lost,
            logger=self._logger,
        )
        self._keeper = keeper
        keeper.start()
        self._logger(
            f"[runtime] 锁心跳线程已启动：每 {keeper.interval_seconds:g}s 一次"
            f"（stale 阈值 {self.settings.stale_after_seconds}s）"
        )
        return keeper

    def stop_heartbeat(self) -> None:
        """停止后台心跳线程（幂等）；成功刷新次数会累加进 :attr:`heartbeat_beats`。"""
        keeper = self._keeper
        self._keeper = None
        if keeper is not None:
            keeper.stop()
            self._thread_beats += int(keeper.beats)

    @property
    def heartbeat_beats(self) -> int:
        """后台线程累计成功刷新次数（没起过线程则为 0）。"""
        live = int(getattr(self._keeper, "beats", 0))
        return self._thread_beats + live

    # ------------------------------------------------------------- 控制面
    def request_stop(self, reason: str = "requested") -> None:
        self.stop_reason = reason
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def rounds_run(self) -> int:
        return self._rounds

    @property
    def failed_rounds(self) -> int:
        return self._failed_rounds

    def _next_run_at(self) -> str:
        target = datetime.now(UTC).timestamp() + self.settings.interval_seconds
        return dt_to_iso(datetime.fromtimestamp(target, timezone.utc))

    def _sleep_between_rounds(self) -> None:
        """把一整个周期切成小片休眠。

        * 停止请求能在 ``SLEEP_POLL_SECONDS`` 内生效；
        * **休眠期间同样按周期刷新锁心跳**（QA-004A）：否则 interval 远大于 stale 阈值时，
          一次长休眠就足以让锁被判 stale，从而出现两个写入者。
        """
        remaining = float(self.settings.interval_seconds)
        since_beat = 0.0
        while remaining > 0 and not self._stop.is_set():
            chunk = min(SLEEP_POLL_SECONDS, remaining)
            self._sleep(chunk)
            remaining -= chunk
            since_beat += chunk
            if self._heartbeat_interval is None:
                continue
            if since_beat + 1e-9 >= self._heartbeat_interval:
                since_beat = 0.0
                if not self.heartbeat_now("sleep"):
                    return

    # ------------------------------------------------------------- 单轮
    def _record_skipped_round(self, *, next_run_at: str | None) -> dict:
        """锁已丢失：记录一条「跳过」的轮次（**绝不调用 round_fn**）并请求停止。"""
        round_id = f"{self._now_fn()}#{self._rounds + 1}"
        stamp = self._now_fn()
        entry = {
            "round_id": round_id,
            "round_number": self._rounds + 1,
            "started_at": stamp,
            "finished_at": stamp,
            "duration_ms": 0,
            "skipped": True,
            "skip_reason": LOCK_REASON_LOST,
            "fetch": {"requested": 0, "ok": 0, "failed": 0, "sources": []},
            "stream_sync": {},
            "publish": {},
            "published": False,
            "publish_status": None,
            "dynamic_fail_closed": False,
            "errors": [
                {"source_name": None, "status": LOCK_REASON_LOST,
                 "error_category": LOCK_REASON_LOST}
            ],
            "outcome": "lock_lost",
            "exit_code": EXIT_ROUND_FAILED,
            "next_run_at": next_run_at,
        }
        self._logger(
            f"[runtime] 跳过第 {self._rounds + 1} 轮：已失去单实例锁，"
            f"本轮不执行 fetch/publish"
        )
        self.request_stop(LOCK_REASON_LOST)
        try:
            self.status.record_round(entry)
        except (OSError, ValueError) as exc:  # 状态文件写不出不该杀死 loop
            self._logger(f"[runtime] 状态写入失败（忽略）：{exc}")
        return entry

    def run_once(self, *, next_run_at: str | None = None) -> dict:
        """跑一轮；**任何异常都被收敛进状态**，不向上抛（loop 因此不会被杀死）。

        进入轮次前若持有单实例锁，会先刷新并核对锁归属；一旦发现锁已不属于自己，
        本轮**不调用 ``round_fn``**（即不抓取、不发布），直接记录一条 ``lock_lost``
        轮次并请求停止。
        """
        if self._lock is not None and not self.heartbeat_now("round_start"):
            return self._record_skipped_round(next_run_at=next_run_at)

        round_id = f"{self._now_fn()}#{self._rounds + 1}"
        self._rounds += 1
        self._logger(f"[runtime] round {self._rounds} start ({round_id})")
        try:
            entry = dict(self.round_fn(round_id=round_id))
        except Exception as exc:  # noqa: BLE001 — 单轮异常绝不允许杀死 loop
            self._failed_rounds += 1
            entry = {
                "started_at": self._now_fn(),
                "finished_at": self._now_fn(),
                "duration_ms": 0,
                "fetch": {"requested": 0, "ok": 0, "failed": 0, "sources": []},
                "stream_sync": {},
                "publish": {},
                "published": False,
                "publish_status": None,
                "outcome": "failed",
                "errors": [
                    {"source_name": None, "status": type(exc).__name__,
                     "error_category": type(exc).__name__}
                ],
            }
            entry["error"] = {
                "type": type(exc).__name__,
                "message": publish_mod.redact_text(str(exc)),
                "category": type(exc).__name__,
            }
            self._logger(f"[runtime] round {self._rounds} FAILED: {type(exc).__name__}: {exc}")
        # 轮次结束（无论成功/异常）都刷新一次心跳：把 heartbeat_at 对齐到轮次边界，
        # 并在这一轮丢掉锁时立刻停止（本轮已经写完的产物不回收，但绝不进入下一轮）。
        if self._lock is not None:
            self.heartbeat_now("round_end")
        entry.setdefault("round_id", round_id)
        entry["round_number"] = self._rounds
        entry["exit_code"] = round_exit_code(entry)
        entry["next_run_at"] = next_run_at
        try:
            self.status.record_round(entry)
        except (OSError, ValueError) as exc:  # 状态文件写不出不该杀死 loop
            self._logger(f"[runtime] 状态写入失败（忽略）：{exc}")
        return entry

    # ------------------------------------------------------------- 循环
    def run(self, *, max_rounds: int | None = None) -> dict:
        """长期循环。``max_rounds`` 仅供测试/一次演示使用。"""
        if not self._stop.is_set():
            # 只在「还没被停」时才重置：若进入 run 之前就已经判出失去锁
            # （例如 cmd_run 的 start_heartbeat 发现锁已易主），不能把结论抹掉。
            self.stop_reason = None
            self.lock_lost = False
        loop_started = self._clock()
        # 后台心跳覆盖「单轮本身很久」；run_once 内部另有轮次边界刷新。
        self.start_heartbeat()
        try:
            if not self.settings.run_on_start and not self._stop.is_set():
                self._sleep_between_rounds()

            while not self._stop.is_set():
                if max_rounds is not None and self._rounds >= max_rounds:
                    break
                will_continue = not (max_rounds is not None and self._rounds + 1 >= max_rounds)
                entry = self.run_once(next_run_at=self._next_run_at() if will_continue else None)
                if self._stop.is_set() or not will_continue:
                    break
                self._sleep_between_rounds()
        finally:
            self.stop_heartbeat()

        return {
            "rounds": self._rounds,
            "failed_rounds": self._failed_rounds,
            "stopped": self.stopped,
            "stop_reason": getattr(self, "stop_reason", None),
            "elapsed_seconds": round(max(0.0, self._clock() - loop_started), 3),
            "lock_lost": self.lock_lost,
            "heartbeat_count": self._heartbeat_count,
            "heartbeat_beats": self.heartbeat_beats,
        }


# ============================================================== 新鲜度

def compute_health(
    *,
    playlist_path,
    status_path=None,
    now: str | None = None,
    stale_after_seconds: int = 21600,
    version: str = "0.1.0",
    started_at: str | None = None,
) -> dict:
    """健康/新鲜度快照。**只输出状态，不含任何 stream URL / 来源清单**。"""
    playlist = pathlib.Path(playlist_path)
    now_dt = iso_to_dt(now) if now else datetime.now(UTC)

    exists = playlist.is_file()
    file_bytes = None
    last_modified = None
    checksum = None
    if exists:
        try:
            stat = playlist.stat()
            file_bytes = int(stat.st_size)
            last_modified = dt_to_iso(datetime.fromtimestamp(stat.st_mtime, timezone.utc))
        except OSError:  # pragma: no cover - 竞态
            exists = False

    status_doc = StatusStore(status_path).read() if status_path else {}
    last_success = status_doc.get("last_success_publish_at")
    source = "runtime_status" if last_success else None
    if not last_success and last_modified:
        last_success = last_modified
        source = "playlist_mtime"

    seconds_since = None
    if last_success:
        seconds_since = int(max(0.0, (now_dt - iso_to_dt(last_success)).total_seconds()))

    if not exists:
        state = FRESHNESS_MISSING
    elif seconds_since is None or seconds_since > int(stale_after_seconds):
        state = FRESHNESS_STALE
    else:
        state = FRESHNESS_OK

    last_round = status_doc.get("current_round") or {}
    uptime = None
    if started_at:
        uptime = int(max(0.0, (now_dt - iso_to_dt(started_at)).total_seconds()))

    return {
        "status": state,
        "service": {
            "version": version,
            "started_at": started_at,
            "uptime_seconds": uptime,
            "read_only": True,
        },
        "playlist": {
            "exists": exists,
            "bytes": file_bytes,
            "last_modified": last_modified,
        },
        "freshness": {
            "last_success_publish_at": last_success,
            "seconds_since_last_success": seconds_since,
            "stale_after_seconds": int(stale_after_seconds),
            "is_stale": state == FRESHNESS_STALE,
            "source": source,
        },
        "last_run": {
            "round_id": last_round.get("round_id"),
            "outcome": last_round.get("outcome"),
            "finished_at": last_round.get("finished_at"),
            "publish_status": last_round.get("publish_status"),
            "error_category": last_round.get("error", {}).get("category")
            if isinstance(last_round.get("error"), dict)
            else None,
        },
        "note": (
            "只读订阅服务：仅提供当前已发布的 M3U 与健康状态，不代理任何视频流；"
            "stale 仅表示「距上次成功发布较久」，不代表文件不可播。"
        ),
    }


# ============================================================== 便捷入口

def validate_server_binding(host: str) -> str | None:
    """非 loopback 绑定返回一句安全提示（不改行为，只提醒）。"""
    normalized = (host or "").strip().lower()
    loopback = {"127.0.0.1", "::1", "localhost"}
    if normalized in loopback:
        return None
    if normalized in {"0.0.0.0", "::", ""}:
        return (
            f"server.host = {host!r} 会把只读订阅暴露给**整个局域网/所有网卡**；"
            f"确认网络可信后再使用（默认应为 127.0.0.1）。"
        )
    return f"server.host = {host!r} 不是 loopback，请确认这是有意为之。"
