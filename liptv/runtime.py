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
"""

from __future__ import annotations

import dataclasses
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


class SingleInstanceLock:
    """跨 Windows/Linux 的最小可行单实例锁。

    用法::

        lock = SingleInstanceLock(path, stale_after_seconds=21600)
        lock.acquire()          # 拿不到就抛 LockError（含可解释的 reason）
        try:
            ...
            lock.heartbeat()    # 每轮开始/结束刷新一次
        finally:
            lock.release()      # 只删「token 还是自己的」那把锁
    """

    def __init__(
        self,
        path,
        *,
        stale_after_seconds: int = 21600,
        interval_seconds: int = 10800,
        version: str = "0.1.0",
        now: str | None = None,
    ):
        self.path = pathlib.Path(path)
        self.stale_after_seconds = max(1, int(stale_after_seconds))
        self.interval_seconds = int(interval_seconds)
        self.version = version
        self._now = now
        self._token: str | None = None
        self._info: LockInfo | None = None
        self._takeover_from: dict | None = None
        self._owns = False

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

    # ------------------------------------------------------------ 内部工具
    def _stamp(self) -> str:
        return self._now or utcnow_iso()

    def _read_metadata(self) -> tuple[dict | None, str]:
        """读锁文件。返回 (元数据或 None, 判定用的「心跳时间」ISO 字符串)。"""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None, ""
        try:
            mtime = self.path.stat().st_mtime
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
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(info.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        tmp = target.with_name(f"{target.name}.tmp{os.getpid()}")
        tmp.write_text(payload + "\n", encoding="utf-8", newline="\n")
        try:
            os.replace(tmp, target)
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

        data, heartbeat = self._read_metadata()
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
        if reason == LOCK_REASON_CREATED:
            self._takeover_from = None

    # ---------------------------------------------------------------- 维护
    def heartbeat(self) -> bool:
        """刷新心跳（原子替换）。只有仍持有锁时才有意义。"""
        if not self._owns or self._token is None:
            return False
        current, _ = self._read_metadata()
        if current is not None and current.get("token") != self._token:
            # 锁已被别人接管 —— 立刻放弃所有权，避免两个写入者同时干活
            self._owns = False
            return False
        assert self._info is not None
        self._info = dataclasses.replace(self._info, heartbeat_at=self._stamp())
        self._write(self.path, self._info)
        return True

    def release(self) -> bool:
        """释放锁；**只删自己的那一把**（token 不符则不删）。"""
        if not self._owns:
            return False
        self._owns = False
        current, _ = self._read_metadata()
        if current is not None and current.get("token") != self._token:
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


# ============================================================ 状态文件

def _atomic_write_json(path: pathlib.Path, payload: dict) -> dict:
    """原子写 JSON 到 path（同目录临时文件 + os.replace）。返回字节数与校验和。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    try:
        os.replace(tmp, path)
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
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
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
    """轮次循环：可注入 ``round_fn`` / ``clock`` / ``sleep``，测试不用真的等。"""

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
        """把一整个周期切成小片休眠，使停止请求能在 ``SLEEP_POLL_SECONDS`` 内生效。"""
        remaining = float(self.settings.interval_seconds)
        while remaining > 0 and not self._stop.is_set():
            chunk = min(SLEEP_POLL_SECONDS, remaining)
            self._sleep(chunk)
            remaining -= chunk

    # ------------------------------------------------------------- 单轮
    def run_once(self, *, next_run_at: str | None = None) -> dict:
        """跑一轮；**任何异常都被收敛进状态**，不向上抛（loop 因此不会被杀死）。"""
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
        self.stop_reason = None
        loop_started = self._clock()
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

        return {
            "rounds": self._rounds,
            "failed_rounds": self._failed_rounds,
            "stopped": self.stopped,
            "stop_reason": getattr(self, "stop_reason", None),
            "elapsed_seconds": round(max(0.0, self._clock() - loop_started), 3),
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
