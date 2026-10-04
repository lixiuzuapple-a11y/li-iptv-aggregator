"""单机 Linux 生产部署内核（TASK-006）。

把已验收的本地链路固化成一台 Linux 主机上的长期运行服务：

```
system boot → systemd → <venv>/python -m liptv run --serve --config /etc/li-iptv-aggregator/config.toml
           → fixed fetch → stream-sync → ffprobe → selector → publish → /live.m3u + /healthz
```

本模块**只**负责部署与运维（目录/权限、release、unit、备份、升级/回滚、体检），
不重新设计业务算法，不引入多节点，不做视频代理。

冻结的目录模型（TASK-006 §2，可用 ``--root`` 换成测试前缀）：

```
<root>/opt/li-iptv-aggregator/       只读应用代码：releases/<id>/ + venv/ + current + deploy-state.json
<root>/etc/li-iptv-aggregator/       配置（config.toml，权限最小化，安装后不再覆盖）
<root>/var/lib/li-iptv-aggregator/   SQLite / live.m3u / status / summary / backups
<root>/var/cache/li-iptv-aggregator/ 可丢弃缓存（动态赛事快照）
<root>/run/liptv-aggregator/         短生命周期锁（tmpfs，重启即清空）
<root>/etc/systemd/system/li-iptv.service
```

关键设计决定（都写进了执行报告，供独立验收复核）：

1. **release 指针是普通文本文件 + 渲染过的 unit**，不用 ``current`` symlink。
   symlink 在某些文件系统/权限下建不出来，还会多一层间接；而 unit 里的
   ``Environment=PYTHONPATH=<releases/<id>>`` 本来就是「指向活动 release」的显式声明。
   切换 release = 原子改 ``current`` 文件 + 重渲染 unit + ``daemon-reload``。
2. **幂等**：文件内容相同就跳过、已存在的生产配置与数据库**永不覆盖**。
3. **安装 venv 不依赖网络**：``python -m venv`` 用本地 ensurepip；项目本身以
   「release 目录 + PYTHONPATH」方式生效（不需要 ``pip install`` 联网取构建依赖）。
   需要 pip 级安装时用 ``--method pip``，那是有网环境下的显式选择。
4. **升级/回滚永不碰数据库内容**；DB 恢复只能显式执行 ``restore-db --yes``。
5. 所有写盘路径都过一遍 :func:`liptv.publish.guard_runtime_output_path`：
   落在「Git 工作树内且未被忽略 / 已被跟踪」的位置一律**拒绝**，部署不能绕开护栏。
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import time

try:  # pragma: no cover - 平台分支
    import grp
    import pwd
except ImportError:  # pragma: no cover - Windows
    grp = None
    pwd = None

from . import backup as backup_mod
from . import config as config_mod
from . import db as db_mod
from . import doctor as doctor_mod
from . import health as health_mod
from . import publish as publish_mod

# ==================================================================== 常量

APP_NAME = "li-iptv-aggregator"
UNIT_NAME = "li-iptv.service"
RUNTIME_DIR_NAME = "li-iptv-aggregator"
UNIT_TEMPLATE_NAME = "li-iptv.service.in"
CONFIG_TEMPLATE_NAME = "config.production.example.toml"

DEFAULT_USER = "liptv"
DEFAULT_GROUP = "liptv"

#: 部署专属退出码（与 ``run`` 的 0/1/2/3 语义**不共用**，见 DEPLOYMENT.md）
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PREFLIGHT = 2
EXIT_HEALTH = 3

#: 打包元数据读不到时的 Python 下限兜底
FALLBACK_MIN_PYTHON = (3, 11)

#: 进 release 目录的内容（生产运行只需要这些；测试与报告不进生产）
RELEASE_INCLUDES = (
    "liptv",
    "tools",
    "schema",
    "deploy",
    "config",
    "pyproject.toml",
    "README.md",
    "DEPLOYMENT.md",
    "V1_RUNTIME_FLOW.md",
)

#: 绝不进 release 的东西
RELEASE_EXCLUDE_NAMES = {"__pycache__", ".git", ".pytest_cache", "out", "data", "venv", ".venv"}
RELEASE_EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".sqlite3", ".sqlite3-journal", ".log")

#: 允许出现在 release id 里的字符
_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: state 文件里保留多少条事件
STATE_EVENT_HISTORY = 20
#: 保留多少个历史 release 目录（current / previous 永不删）
RELEASE_KEEP = 3

#: 渲染配置时的敏感模式（出现即拒绝安装）
_FORBIDDEN_CONFIG_PATTERNS = (
    (re.compile(r"\bhttps?://[^\s\"']*\?[^\s\"']*", re.IGNORECASE), "带 query 的 URL"),
    (re.compile(r"\b(?:txSecret|token|access_key|api[_-]?key|authorization)\s*=", re.IGNORECASE),
     "疑似凭据字段"),
)


class DeployError(RuntimeError):
    """部署动作无法继续（调用方据此给出退出码）。"""


# ==================================================================== 布局

@dataclasses.dataclass(frozen=True)
class Layout:
    """生产路径模型。``root`` 是 DESTDIR 式前缀（生产为 ``/``，测试为临时目录）。"""

    root: pathlib.Path
    user: str = DEFAULT_USER
    group: str = DEFAULT_GROUP

    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise DeployError(f"--root 必须是绝对路径：{self.root}")

    # ------------------------------------------------------------ 目录
    @property
    def app_dir(self) -> pathlib.Path:
        return self.root / "opt" / APP_NAME

    @property
    def etc_dir(self) -> pathlib.Path:
        return self.root / "etc" / APP_NAME

    @property
    def lib_dir(self) -> pathlib.Path:
        return self.root / "var" / "lib" / APP_NAME

    @property
    def cache_dir(self) -> pathlib.Path:
        return self.root / "var" / "cache" / APP_NAME

    @property
    def run_dir(self) -> pathlib.Path:
        return self.root / "run" / RUNTIME_DIR_NAME

    @property
    def systemd_dir(self) -> pathlib.Path:
        return self.root / "etc" / "systemd" / "system"

    @property
    def releases_dir(self) -> pathlib.Path:
        return self.app_dir / "releases"

    @property
    def venv_dir(self) -> pathlib.Path:
        return self.app_dir / "venv"

    @property
    def backups_dir(self) -> pathlib.Path:
        return self.lib_dir / "backups"

    # ------------------------------------------------------------ 文件
    @property
    def config_path(self) -> pathlib.Path:
        return self.etc_dir / "config.toml"

    @property
    def unit_path(self) -> pathlib.Path:
        return self.systemd_dir / UNIT_NAME

    @property
    def current_pointer(self) -> pathlib.Path:
        return self.app_dir / "current"

    @property
    def state_path(self) -> pathlib.Path:
        return self.app_dir / "deploy-state.json"

    @property
    def db_path(self) -> pathlib.Path:
        return self.lib_dir / "liptv.sqlite3"

    @property
    def output_path(self) -> pathlib.Path:
        return self.lib_dir / "live.m3u"

    @property
    def status_path(self) -> pathlib.Path:
        return self.lib_dir / "runtime-status.json"

    @property
    def summary_path(self) -> pathlib.Path:
        return self.lib_dir / "publish-summary.json"

    @property
    def lock_path(self) -> pathlib.Path:
        return self.run_dir / "liptv.lock"

    @property
    def dynamic_tmp_dir(self) -> pathlib.Path:
        return self.cache_dir / "tmp"

    @property
    def service_log_path(self) -> pathlib.Path:
        return self.cache_dir / "service.log"

    def release_dir(self, release_id: str) -> pathlib.Path:
        return self.releases_dir / release_id

    @property
    def venv_python(self) -> pathlib.Path:
        """venv 里的解释器（POSIX 是 bin/python，Windows 仅测试用 Scripts/python.exe）。"""
        if os.name == "posix":
            return self.venv_dir / "bin" / "python"
        return self.venv_dir / "Scripts" / "python.exe"

    @property
    def venv_pip(self) -> pathlib.Path:
        if os.name == "posix":
            return self.venv_dir / "bin" / "pip"
        return self.venv_dir / "Scripts" / "pip.exe"

    # ------------------------------------------------------------ 属主意图
    @property
    def root_owner(self) -> str:
        """代码 / release / venv / 指针 / state / unit：**root 拥有**，服务用户只读。"""
        return "root:root"

    @property
    def config_owner(self) -> str:
        """生产配置：**root 拥有**、服务用户所在组可读（``root:liptv``），服务用户**不可写**。"""
        return f"root:{self.group}"

    @property
    def data_owner(self) -> str:
        """数据 / 缓存 / 运行期状态：服务用户拥有（唯一可写者）。"""
        return f"{self.user}:{self.group}"

    # ------------------------------------------------------------ 权限意图
    def mode_table(self, release_id: str | None = None) -> list[dict]:
        """权限**意图**表（POSIX 上会被真实应用；其它平台只记录意图）。

        TASK-006 冻结的属主模型（QA-006A）—— 低权限服务账号**不得**拥有应用代码与生产配置：

        * ``root:root``：``/opt/<app>`` 下的 app / releases / release / venv /
          ``current`` 指针 / ``deploy-state.json``，以及 systemd unit —— 服务用户只读；
        * ``root:<group>``：``/etc/<app>`` 与 ``config.toml`` —— 服务用户所在组可读、不可写；
        * ``<user>:<group>``：``/var/lib``（含 SQLite、``live.m3u``、backups）、
          ``/var/cache``、``/run`` —— 服务用户可写。

        同一份表被 ``_apply_modes()`` 真正执行（chown/chmod），也被报告与回归断言读取，
        所以「意图」与「实际」只有这一个来源。
        """
        root_owner = self.root_owner
        config_owner = self.config_owner
        data_owner = self.data_owner
        rows = [
            # --- 代码与 venv：root 拥有，服务用户只读
            {"path": str(self.app_dir), "mode": 0o755, "owner": root_owner, "kind": "dir"},
            {"path": str(self.releases_dir), "mode": 0o755, "owner": root_owner, "kind": "dir"},
            {"path": str(self.venv_dir), "mode": 0o755, "owner": root_owner, "kind": "dir"},
            {"path": str(self.current_pointer), "mode": 0o644, "owner": root_owner, "kind": "file"},
            {"path": str(self.state_path), "mode": 0o640, "owner": root_owner, "kind": "file"},
            # --- 生产配置：root 拥有、服务用户组可读
            {"path": str(self.etc_dir), "mode": 0o750, "owner": config_owner, "kind": "dir"},
            {"path": str(self.config_path), "mode": 0o640, "owner": config_owner, "kind": "file"},
            # --- 数据与运行期：服务用户拥有
            {"path": str(self.lib_dir), "mode": 0o750, "owner": data_owner, "kind": "dir"},
            {"path": str(self.db_path), "mode": 0o640, "owner": data_owner, "kind": "file"},
            {"path": str(self.backups_dir), "mode": 0o700, "owner": data_owner, "kind": "dir"},
            {"path": str(self.cache_dir), "mode": 0o750, "owner": data_owner, "kind": "dir"},
            {"path": str(self.dynamic_tmp_dir), "mode": 0o750, "owner": data_owner, "kind": "dir"},
            {"path": str(self.run_dir), "mode": 0o750, "owner": data_owner, "kind": "dir"},
            # --- systemd unit：root 拥有
            {"path": str(self.unit_path), "mode": 0o644, "owner": root_owner, "kind": "file"},
        ]
        if release_id:
            rows.insert(2, {
                "path": str(self.release_dir(release_id)), "mode": 0o755,
                "owner": root_owner, "kind": "dir",
            })
        return rows


def default_root() -> pathlib.Path:
    """生产前缀：POSIX 为 ``/``；其它平台必须显式给 ``--root``（拒绝误改开发机）。"""
    if os.name == "posix":
        return pathlib.Path("/")
    raise DeployError(
        "本机不是 POSIX 系统：生产安装只能在 Linux 上执行。"
        "在本机做离线验证请显式给 --root <临时目录>。"
    )


def build_layout(root: str | pathlib.Path | None, *, user: str | None = None,
                 group: str | None = None) -> Layout:
    resolved = default_root() if root in (None, "") else pathlib.Path(root).expanduser()
    resolved = pathlib.Path(os.path.abspath(str(resolved)))
    return Layout(root=resolved, user=user or DEFAULT_USER, group=group or DEFAULT_USER)


# ==================================================================== 工具

def _atomic_write_text(path: pathlib.Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    if mode is not None and os.name == "posix":
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def _read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sanitize_release_id(value: str) -> str:
    """把任意字符串收敛成安全的 release id（目录名）。"""
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]", "-", text)
    text = text.strip("-.")
    if not text or not _RELEASE_ID_RE.match(text):
        raise DeployError(f"release id 非法：{value!r}")
    return text


def git_revision(source_dir: pathlib.Path) -> str | None:
    """尽力取源目录的 Git 提交 SHA（取不到返回 None，绝不让部署失败）。"""
    git = os.environ.get("LIPTV_GIT_EXECUTABLE") or shutil.which("git")
    if not git or not (source_dir / ".git").exists():
        return None
    try:
        out = subprocess.run(
            [git, "-C", str(source_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - 环境异常
        return None
    if out.returncode != 0:
        return None
    sha = (out.stdout or "").strip()
    return sha if re.fullmatch(r"[0-9a-f]{7,64}", sha) else None


def resolve_release_id(source_dir: pathlib.Path, explicit: str | None, *,
                       now: float | None = None) -> str:
    """确定 release id：显式值优先，否则 ``<git sha 前 12 位>-<UTC 时间戳>``。"""
    if explicit and explicit.strip() and explicit.strip().lower() != "auto":
        return sanitize_release_id(explicit)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    sha = git_revision(source_dir)
    return sanitize_release_id(f"{sha[:12]}-{stamp}" if sha else f"local-{stamp}")


def min_python_version(source_dir: pathlib.Path) -> tuple[int, int]:
    """从 ``pyproject.toml`` 的 ``requires-python`` 读 Python 下限（唯一事实源）。"""
    pyproject = source_dir / "pyproject.toml"
    if not pyproject.is_file():
        return FALLBACK_MIN_PYTHON
    try:
        import tomllib

        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
        spec = str((data.get("project") or {}).get("requires-python") or "")
    except Exception:  # pragma: no cover - 坏文件
        return FALLBACK_MIN_PYTHON
    match = re.search(r">=\s*(\d+)\.(\d+)", spec)
    if not match:
        return FALLBACK_MIN_PYTHON
    return int(match.group(1)), int(match.group(2))


def _guard(path: pathlib.Path) -> None:
    """所有写盘路径都必须过运行期产物护栏（工作树内未忽略/已跟踪 ⇒ 拒绝）。"""
    try:
        publish_mod.guard_runtime_output_path(path)
    except ValueError as exc:
        raise DeployError(f"拒绝写入 {path}：{exc}") from exc


def _posix(path) -> str:
    """写进 TOML / unit 时一律用正斜杠。

    Linux 上是恒等变换；Windows 上也必须这样 —— TOML 基本字符串里的 ``\\`` 是转义符，
    反斜杠路径会直接把配置写成不可解析（仓库既有惯例）。
    """
    return pathlib.Path(path).as_posix()


def _is_absolute_path(value: str) -> bool:
    """配置里的路径是否绝对。

    必须**同时**接受两种绝对形式，否则会在某一端误判：

    * POSIX 绝对路径（``/var/lib/...``）—— 生产形态。Windows 上 ``os.path.isabs`` 会
      把它判成**非**绝对（缺盘符），所以不能只用 ``os.path.isabs``；
    * 带盘符的 Windows 路径（``C:/Users/...``）—— 本机离线验证 ``--root`` 的形态。
      Linux 上 ``os.path.isabs`` 同样会判成非绝对，所以不能只用 ``startswith("/")``。
    """
    return bool(value) and (value.startswith("/") or os.path.isabs(value))


def _cmd_for(spec: str | None, default: str) -> list[str] | None:
    """把可执行文件规格变成 argv 前缀。

    ``*.py`` 视为脚本，用当前解释器执行 —— 这是**离线测试/演示的接缝**（fake systemctl），
    也方便运维指向自定义包装器；其余情况直接执行。
    """
    target = spec or default
    if not target:
        return None
    if target.endswith(".py"):
        return [sys.executable, target]
    found = shutil.which(target) if not os.path.isabs(target) else target
    if not found or not os.path.exists(found):
        return None
    return [found]


# 只有这两个状态能明确代表「服务稳定地停着」。systemd 的过渡态
# （activating / deactivating / reloading / maintenance）以及 unknown / 空 / 查询失败
# **都不等于**稳定 inactive，一律归 None 保持 doctor 严格判定。
_SERVICE_INACTIVE_STATES = frozenset({"inactive", "failed"})


def _service_active_state(raw: object) -> bool | None:
    """把 systemd service state 归一化成三态布尔（QA-007B-1）。

    ``ServiceManager.query()["active"]`` 在生产里是 ``systemctl is-active`` 的
    **stdout 字符串**，也可能是布尔（``ProcessServiceManager``）或 ``None``
    （``NullServiceManager`` / 查询失败）。归一化规则**故意收窄**：

    ==========================  ======  ==========================================
    输入                         输出    理由
    ==========================  ======  ==========================================
    ``True`` / ``"active"``      True    明确在跑
    ``False`` / ``"inactive"``   False   明确停着（RuntimeDirectory 已被回收）
    ``"failed"``                 False   与 inactive 等价：unit 已停，目录已被回收
    ``"activating"``             None    🚫 **过渡态**：可能下一秒就 active，
                                           此时不能假设 RuntimeDirectory 已回收
    ``"deactivating"``           None    🚫 同上，停止进行中，目录回收时机未定
    ``"reloading"``              None    🚫 过渡态
    ``"maintenance"``            None    🚫 过渡态
    ``"unknown"`` / 其它字符串   None    判不出来
    ``""`` / ``None`` / 非字符串  None    查不到
    ==========================  ======  ==========================================

    🚨 **不要**写成 ``raw.strip() == "active"``：那会把**所有**非 active 的
    非空字符串（含 ``activating`` 等过渡态）一律压成 False，等于宣称
    「服务已明确停止」而实际并未如此，于是 doctor 对 RuntimeDirectory 缺失
    采用了宽松语义。Review 01 要求的 fail-closed 三态设计不允许这样。

    非字符串的其它真值类型（如 int 1）刻意不映射为 True —— 只有显式的
    ``True`` 与 ``"active"`` 才算明确在跑。
    """
    if raw is True:
        return True
    if raw is False:
        return False
    if not isinstance(raw, str):
        return None
    state = raw.strip().lower()
    if state == "active":
        return True
    if state in _SERVICE_INACTIVE_STATES:
        return False
    return None


# ==================================================================== 记录器

class _Recorder:
    """记录（并可选执行）文件级动作；dry-run 时只记录，做到「plan == install 的动作表」。"""

    def __init__(self, *, dry_run: bool):
        self.dry_run = dry_run
        self.actions: list[dict] = []
        self.messages: list[str] = []

    # ------------------------------------------------------------ 查询
    @property
    def changed(self) -> list[str]:
        return [a["path"] for a in self.actions if a["status"] == "applied"]

    @property
    def skipped(self) -> list[dict]:
        return [a for a in self.actions if a["status"] == "skipped"]

    def note(self, message: str) -> None:
        self.messages.append(message)

    def _add(self, *, kind: str, path: pathlib.Path, status: str, mode: int | None = None,
             owner: str | None = None, reason: str | None = None) -> None:
        self.actions.append({
            "kind": kind, "path": str(path), "status": status,
            "mode": f"{mode:04o}" if isinstance(mode, int) else None,
            "owner": owner, "reason": reason,
        })

    # ------------------------------------------------------------ 动作
    def mkdir(self, path: pathlib.Path, *, mode: int, owner: str | None = None) -> None:
        _guard(path)
        if path.is_dir():
            if os.name == "posix":
                current = path.stat().st_mode & 0o7777
                if current != mode:
                    if self.dry_run:
                        self._add(kind="chmod", path=path, status="planned", mode=mode, owner=owner)
                    else:
                        os.chmod(path, mode)
                        self._add(kind="chmod", path=path, status="applied", mode=mode, owner=owner)
                    return
            self._add(kind="dir", path=path, status="skipped", mode=mode,
                      reason="目录已存在且权限一致")
            return
        if self.dry_run:
            self._add(kind="mkdir", path=path, status="planned", mode=mode, owner=owner)
            return
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(path, mode)
        self._add(kind="mkdir", path=path, status="applied", mode=mode, owner=owner)

    def write_text(self, path: pathlib.Path, text: str, *, mode: int, owner: str | None = None,
                   overwrite: bool = True, what: str = "文件") -> bool:
        _guard(path)
        if path.exists() and not overwrite:
            self._add(kind="file", path=path, status="skipped", mode=mode, owner=owner,
                      reason=f"{what}已存在，未覆盖")
            return False
        if path.is_file():
            try:
                if _read_text(path) == text:
                    self._add(kind="file", path=path, status="skipped", mode=mode, owner=owner,
                              reason="内容相同")
                    return True
            except OSError:  # pragma: no cover - 读不动就按需重写
                pass
        if self.dry_run:
            self._add(kind="write", path=path, status="planned", mode=mode, owner=owner)
            return True
        _atomic_write_text(path, text, mode=mode if os.name == "posix" else None)
        self._add(kind="write", path=path, status="applied", mode=mode, owner=owner)
        return True

    def copy_tree(self, src: pathlib.Path, dst: pathlib.Path, *, owner: str | None = None) -> None:
        _guard(dst)
        if dst.exists():
            self._add(kind="release", path=dst, status="skipped",
                      reason="release 目录已存在（不原地覆盖）")
            return
        if self.dry_run:
            self._add(kind="release", path=dst, status="planned", owner=owner)
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*sorted(RELEASE_EXCLUDE_NAMES)),
                        symlinks=False)
        self._add(kind="release", path=dst, status="applied", owner=owner)

    def chown(self, path: pathlib.Path, owner: str, *, enabled: bool) -> None:
        """记录/执行一次 chown。

        注意：**每条分支都带上 ``owner``**（含跳过分支）—— 离线平台上「仅记录归属意图」
        也必须能被人和测试读到，否则报告与实现就会各说各话（QA-006A）。
        """
        if not enabled:
            self._add(kind="chown", path=path, status="skipped", owner=owner,
                      reason="未启用（--no-chown）")
            return
        if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
            self._add(kind="chown", path=path, status="skipped", owner=owner,
                      reason="非 root 或非 POSIX：仅记录归属意图")
            return
        user, _, group = owner.partition(":")
        uid = gid = -1
        try:
            if user and pwd is not None:
                uid = pwd.getpwnam(user).pw_uid
            if group and grp is not None:
                gid = grp.getgrnam(group).gr_gid
        except KeyError:
            self._add(kind="chown", path=path, status="skipped", owner=owner,
                      reason=f"用户/组不存在：{owner}（先运行 install 创建用户）")
            return
        if self.dry_run:
            self._add(kind="chown", path=path, status="planned", owner=owner)
            return
        try:
            os.chown(path, uid, gid)
        except OSError as exc:  # pragma: no cover - 权限异常
            self._add(kind="chown", path=path, status="skipped", reason=f"chown 失败：{exc}")
            return
        self._add(kind="chown", path=path, status="applied", owner=owner)


# ==================================================================== 服务管理

class ServiceManager:
    """服务托管抽象：``systemd``（生产）/ ``process``（离线验证与演示）/ ``none``。"""

    name = "none"

    def daemon_reload(self) -> dict:
        return {"action": "daemon-reload", "status": "skipped", "reason": "未启用服务托管"}

    def enable(self) -> dict:
        return {"action": "enable", "status": "skipped", "reason": "未启用服务托管"}

    def start(self) -> dict:
        return {"action": "start", "status": "skipped", "reason": "未启用服务托管"}

    def stop(self) -> dict:
        return {"action": "stop", "status": "skipped", "reason": "未启用服务托管"}

    def query(self) -> dict:
        return {"managed": False, "active": None, "enabled": None}

    def cleanup(self) -> None:
        return None


class NullServiceManager(ServiceManager):
    """只落文件、不碰服务（纯静态安装 / 权限演练用）。"""


class _CommandServiceManager(ServiceManager):
    """按 argv 前缀执行外部命令的基类。"""

    def __init__(self, argv_prefix: list[str]):
        self.argv_prefix = list(argv_prefix)
        self.log: list[dict] = []

    def _run(self, *args: str, timeout: float = 30.0) -> dict:
        argv = [*self.argv_prefix, *args]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                  check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            result = {"action": args[0] if args else "", "status": "error",
                      "argv": argv, "detail": str(exc)}
        else:
            result = {
                "action": args[0] if args else "",
                "status": "ok" if proc.returncode == 0 else "error",
                "argv": argv,
                "returncode": proc.returncode,
                "stdout": (proc.stdout or "").strip()[:200],
                "stderr": (proc.stderr or "").strip()[:200],
            }
        self.log.append(result)
        return result

    def query(self) -> dict:
        active = self._run("is-active", UNIT_NAME)
        enabled = self._run("is-enabled", UNIT_NAME)
        return {
            "managed": True,
            "active": active.get("stdout") or None,
            "enabled": enabled.get("stdout") or None,
            "active_ok": active.get("status") == "ok",
        }


class SystemdServiceManager(_CommandServiceManager):
    """``systemctl`` 驱动。生产用它；离线测试用 ``tools/fake_systemctl.py`` 顶替。"""

    name = "systemd"

    def __init__(self, argv_prefix: list[str]):
        super().__init__(argv_prefix)

    def daemon_reload(self) -> dict:
        return self._run("daemon-reload")

    def enable(self) -> dict:
        return self._run("enable", UNIT_NAME)

    def start(self) -> dict:
        return self._run("start", UNIT_NAME)

    def stop(self) -> dict:
        # 服务本来就没跑时 systemctl 返回非 0，这不算失败
        return self._run("stop", UNIT_NAME)


class ProcessServiceManager(ServiceManager):
    """离线/演示用：直接以子进程跑 ``run --serve``（等价于 unit 的 ExecStart）。

    只用于**本机离线验证**：目标机上的真实托管始终是 systemd。生产日志走 journald，
    这里的 ``service.log`` 只存在于演示/测试环境。
    """

    name = "process"

    def __init__(self, *, layout: Layout, python: str, release_dir: pathlib.Path,
                 config_path: pathlib.Path, log_path: pathlib.Path | None = None):
        self.layout = layout
        self.python = python
        self.release_dir = release_dir
        self.config_path = config_path
        self.log_path = log_path or layout.service_log_path
        self.proc: subprocess.Popen | None = None
        self._log_handle = None

    def _argv(self) -> list[str]:
        return [self.python, "-m", "liptv", "run", "--serve", "--config", str(self.config_path)]

    def start(self) -> dict:
        self.layout.app_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.release_dir)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self._log_handle = self.log_path.open("w", encoding="utf-8", newline="\n")
        try:
            self.proc = subprocess.Popen(
                self._argv(), cwd=str(self.layout.app_dir), env=env,
                stdout=self._log_handle, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._log_handle.close()
            self._log_handle = None
            return {"action": "start", "status": "error", "detail": str(exc)}
        return {"action": "start", "status": "ok", "pid": self.proc.pid, "argv": self._argv()}

    def stop(self) -> dict:
        if self.proc is None:
            return {"action": "stop", "status": "skipped", "reason": "本管理器未启动过进程"}
        proc = self.proc
        self.proc = None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
                proc.kill()
                proc.wait(timeout=10)
        code = proc.returncode
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        return {"action": "stop", "status": "ok", "returncode": code}

    def query(self) -> dict:
        if self.proc is None:
            return {"managed": True, "active": "inactive", "enabled": "static",
                    "pid": None, "log": str(self.log_path)}
        code = self.proc.poll()
        return {
            "managed": True,
            "active": "active" if code is None else "failed",
            "enabled": "static",
            "pid": self.proc.pid,
            "returncode": code,
            "log": str(self.log_path),
        }

    def cleanup(self) -> None:
        if self.proc is not None:
            self.stop()


def build_service_manager(options: "DeployOptions", *, release_dir: pathlib.Path,
                          python: str) -> ServiceManager:
    kind = (options.service_manager or "none").lower()
    if kind == "none":
        return NullServiceManager()
    if kind == "process":
        config_path = options.layout.config_path
        return ProcessServiceManager(
            layout=options.layout, python=python, release_dir=release_dir,
            config_path=config_path,
        )
    if kind == "systemd":
        prefix = _cmd_for(options.systemctl, "systemctl")
        if prefix is None:
            raise DeployError(
                "需要 systemctl 才能托管服务；本机找不到 systemctl。"
                "（离线验证请用 --service-manager process 或 none）"
            )
        return SystemdServiceManager(prefix)
    raise DeployError(f"未知的 --service-manager：{options.service_manager!r}")


# ==================================================================== 选项

@dataclasses.dataclass
class DeployOptions:
    layout: Layout
    source_dir: pathlib.Path
    release_id: str | None = None
    python: str | None = None
    method: str = "copy"
    no_venv: bool = False
    force: bool = False
    dry_run: bool = False
    start: bool = False
    init_db: bool = True
    service_manager: str = "none"
    systemctl: str | None = None
    useradd: str | None = None
    create_user: bool = False
    chown: bool = True
    health_url: str | None = None
    skip_health: bool = False
    health_timeout: float = 30.0
    health_interval: float = 1.0
    retention: int = backup_mod.DEFAULT_RETENTION
    templates_dir: pathlib.Path | None = None
    json: bool = False
    now: float | None = None


# ==================================================================== 部署器

class Deployer:
    """install / upgrade / rollback / status / backup / restore-db 的统一入口。"""

    def __init__(self, options: DeployOptions):
        self.options = options
        self.layout = options.layout
        self.rec = _Recorder(dry_run=options.dry_run)
        self.steps: list[dict] = []
        self.notes: list[str] = []
        self.service: ServiceManager = NullServiceManager()
        self.python = options.python or sys.executable

    # ------------------------------------------------------------ 小工具
    def _step(self, name: str, status: str, **detail) -> dict:
        entry = {"step": name, "status": status, **detail}
        self.steps.append(entry)
        return entry

    def _step_info(self, name: str, info: dict) -> dict:
        """把「子动作返回的 dict」记成一步（``status`` 从 dict 里取，避免关键字冲突）。"""
        payload = dict(info or {})
        status = str(payload.pop("status", "ok"))
        return self._step(name, status, **payload)

    def _service_action(self, name: str, action) -> dict:
        """服务动作：dry-run 一律不执行（plan 模式不能碰真实 systemd）。"""
        if self.options.dry_run:
            return self._step(name, "planned")
        return self._step_info(name, action())

    @property
    def templates_dir(self) -> pathlib.Path:
        if self.options.templates_dir:
            return pathlib.Path(self.options.templates_dir)
        env = os.environ.get("LIPTV_DEPLOY_TEMPLATES")
        if env:
            return pathlib.Path(env)
        # 源码树 / release 目录里的 deploy/
        candidate = pathlib.Path(__file__).resolve().parent.parent / "deploy"
        if candidate.is_dir():
            return candidate
        raise DeployError(f"找不到部署模板目录（期望 {candidate}）")

    def _template(self, name: str) -> str:
        path = self.templates_dir / ("systemd/" + name if name.endswith(".service.in") else name)
        if not path.is_file():
            raise DeployError(f"缺少模板文件：{path}")
        return _read_text(path)

    def _base_payload(self, action: str) -> dict:
        return {
            "action": action,
            "status": "OK",
            "prefix": str(self.layout.root),
            "app_dir": str(self.layout.app_dir),
            "release_id": None,
            "previous_release": None,
            "actions": [],
            "changed": [],
            "skipped": [],
            "steps": [],
            "notes": [],
            "messages": [],
            "service": None,
            "backup": None,
            "health": None,
            "doctor": None,
            "exit_code": EXIT_OK,
        }

    def _finish(self, payload: dict, status: str, exit_code: int) -> dict:
        payload["status"] = status
        payload["exit_code"] = int(exit_code)
        payload["actions"] = self.rec.actions
        payload["changed"] = self.rec.changed
        payload["skipped"] = [
            {"path": a["path"], "reason": a["reason"]} for a in self.rec.skipped
        ]
        payload["steps"] = self.steps
        payload["notes"] = list(self.notes)
        payload["messages"] = list(self.rec.messages)
        # dry-run（plan）**不查询**服务状态：``SystemdServiceManager.query()`` 会真的执行
        # ``systemctl is-active/is-enabled``，而 plan 的承诺是「不碰任何外部系统」。
        payload["service"] = (
            None if self.options.dry_run
            else (self.service.query() if isinstance(self.service, ServiceManager) else None)
        )
        return payload

    # ------------------------------------------------------------ 状态文件
    def load_state(self) -> dict:
        path = self.layout.state_path
        if not path.is_file():
            return {"schema": 1, "current": None, "previous": None, "events": []}
        try:
            data = json.loads(_read_text(path))
        except (OSError, json.JSONDecodeError):  # pragma: no cover
            return {"schema": 1, "current": None, "previous": None, "events": []}
        if not isinstance(data, dict):
            return {"schema": 1, "current": None, "previous": None, "events": []}
        data.setdefault("events", [])
        return data

    def save_state(self, state: dict) -> None:
        state = dict(state)
        state["schema"] = 1
        state["events"] = list(state.get("events") or [])[-STATE_EVENT_HISTORY:]
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _guard(self.layout.state_path)
        _atomic_write_text(
            self.layout.state_path,
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            mode=0o640,
        )

    def _record_event(self, state: dict, action: str, **detail) -> dict:
        state = dict(state)
        state["events"] = [*list(state.get("events") or []), {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "action": action,
            **detail,
        }][-STATE_EVENT_HISTORY:]
        return state

    def active_release(self) -> str | None:
        pointer = self.layout.current_pointer
        if pointer.is_file():
            try:
                text = _read_text(pointer).strip()
            except OSError:  # pragma: no cover
                text = ""
            if text and _RELEASE_ID_RE.match(text):
                return text
        return self.load_state().get("current")

    def active_release_dir(self) -> pathlib.Path | None:
        release = self.active_release()
        if not release:
            return None
        path = self.layout.release_dir(release)
        return path if path.is_dir() else None

    # ------------------------------------------------------------ 体检/前置
    def preflight(self, *, require_config: bool, require_installed: bool) -> list[str]:
        """返回问题列表（空 = 通过）。**不写任何文件。**"""
        problems: list[str] = []
        source = self.options.source_dir
        if not (source / "liptv" / "__init__.py").is_file():
            problems.append(f"--source 不是本项目源码树（缺少 liptv/__init__.py）：{source}")
        if not (source / "pyproject.toml").is_file():
            problems.append(f"--source 缺少 pyproject.toml：{source}")
        try:
            self.templates_dir  # noqa: B018 - 触发解析
        except DeployError as exc:
            problems.append(str(exc))

        floor = min_python_version(source)
        try:
            out = subprocess.run(
                [self.python, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            text = (out.stdout or "").strip()
            major, _, minor = text.partition(".")
            if out.returncode != 0 or not major.isdigit():
                problems.append(f"无法确定 Python 版本：{self.python}（{out.stderr.strip()[:120]}）")
            elif (int(major), int(minor)) < floor:
                problems.append(
                    f"Python {text} 低于打包元数据要求的下限 "
                    f"{floor[0]}.{floor[1]}（pyproject.toml requires-python）；"
                    f"Debian 12 自带 3.11，请装 python3.12 或改用 Ubuntu 24.04 LTS"
                )
        except (OSError, subprocess.SubprocessError) as exc:
            problems.append(f"无法执行 Python 解释器 {self.python}：{exc}")

        if require_config and not self.layout.config_path.is_file():
            problems.append(
                f"缺少生产配置 {self.layout.config_path}（请先 install，或用 --config 指定）"
            )
        if require_installed:
            if self.active_release_dir() is None:
                problems.append("尚未安装任何 release（请先 install）")
            if not self.layout.venv_dir.is_dir() and not self.options.no_venv:
                problems.append(f"缺少 venv：{self.layout.venv_dir}")

        if os.name != "posix":
            self.notes.append("本机不是 POSIX：权限/属主只记录**意图**，不实际 apply。")
        if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0:
            self.notes.append(
                "当前不是 root：创建系统用户、chown、写入 /etc/systemd/system 都会失败或被跳过；"
                "生产请用 sudo 执行（离线验证请用 --root 指向临时前缀）。"
            )
        return problems

    def _read_config(self) -> dict:
        return config_mod.load_config(self.layout.config_path)

    # ------------------------------------------------------------ 渲染
    def render_unit(self, *, release_id: str, python: str | None = None) -> str:
        template = self._template(UNIT_TEMPLATE_NAME)
        # 模板在 [Unit] 之前有说明性注释（给人看的），不渲染进最终 unit。
        # 注意：必须匹配**独立的节标题行**，不能用 ``"@" in text`` / ``text.index("[Unit]")``
        # 这种子串查找 —— 模板注释里也会写到 "[Unit]" 字样，子串查找会把注释切一半留下来
        # （TASK-006 首版就是这样，导致 unit 里混入说明文字与未替换的 ``@@``）。
        marker = "[Unit]"
        lines = template.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.strip() == marker:
                template = "".join(lines[index:])
                break
        venv_python = python or str(self.layout.venv_python)
        replacements = {
            "@@USER@@": self.layout.user,
            "@@GROUP@@": self.layout.group,
            "@@APP_DIR@@": _posix(self.layout.app_dir),
            "@@RELEASE_DIR@@": _posix(self.layout.release_dir(release_id)),
            "@@VENV_PYTHON@@": _posix(venv_python),
            "@@CONFIG_PATH@@": _posix(self.layout.config_path),
            "@@LIB_DIR@@": _posix(self.layout.lib_dir),
            "@@CACHE_DIR@@": _posix(self.layout.cache_dir),
            "@@RUN_DIR@@": _posix(self.layout.run_dir),
            "@@RUNTIME_DIR_NAME@@": RUNTIME_DIR_NAME,
        }
        text = template
        for key, value in replacements.items():
            text = text.replace(key, value)
        leftovers = re.findall(r"@@[A-Z_]+@@", text)
        if leftovers:
            raise DeployError(f"unit 模板存在未替换占位符：{sorted(set(leftovers))}")
        return text

    def render_config(self) -> str:
        template = self._template(CONFIG_TEMPLATE_NAME)
        replacements = {
            "@@DB_PATH@@": _posix(self.layout.db_path),
            "@@OUTPUT_PATH@@": _posix(self.layout.output_path),
            "@@SUMMARY_PATH@@": _posix(self.layout.summary_path),
            "@@STATUS_PATH@@": _posix(self.layout.status_path),
            "@@LOCK_PATH@@": _posix(self.layout.lock_path),
            "@@DYNAMIC_TMP_DIR@@": _posix(self.layout.dynamic_tmp_dir),
        }
        text = template
        for key, value in replacements.items():
            text = text.replace(key, value)
        leftovers = re.findall(r"@@[A-Z_]+@@", text)
        if leftovers:
            raise DeployError(f"配置模板存在未替换占位符：{sorted(set(leftovers))}")
        return text

    @staticmethod
    def validate_config_text(text: str) -> list[str]:
        """检查渲染后的配置：全绝对路径、无凭据、无带 query 的 URL、来源与测活默认关闭。

        按**小节**判定（``[server] enabled = true`` 是生产必需，不能误伤）：
        ``[probe] enabled = true``、任何 ``[[sources]]`` 里的 ``enabled = true``、
        以及 ``include_dynamic/require_dynamic = true`` 都是一票否决。
        """
        problems: list[str] = []
        section = ""
        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            body = line.split("#", 1)[0].strip()
            if body.startswith("[") and body.endswith("]"):
                section = body.strip("[]").strip()
                continue
            for pattern, label in _FORBIDDEN_CONFIG_PATTERNS:
                if pattern.search(body):
                    problems.append(f"第 {lineno} 行出现{label}：{body[:80]}")
            match = re.match(r"^(path|m3u_path|summary_path|status_path|lock_path|"
                             r"dynamic_tmp_dir)\s*=\s*\"([^\"]*)\"", body)
            if match and not _is_absolute_path(match.group(2)):
                problems.append(f"第 {lineno} 行不是绝对路径：{body[:80]}")
            if re.match(r"^enabled\s*=\s*true\b", body, re.IGNORECASE):
                if section in {"probe", "sources", "publish", "runtime"}:
                    problems.append(
                        f"第 {lineno} 行在 [{section}] 里启用了来源或测活"
                        f"（生产模板必须默认关闭）：{body[:80]}"
                    )
            if re.match(r"^(include_dynamic|require_dynamic)\s*=\s*true\b", body, re.IGNORECASE):
                problems.append(f"第 {lineno} 行启用了动态赛事源（生产模板必须默认关闭）：{body[:80]}")
        return problems

    # ------------------------------------------------------------ 用户
    def ensure_user(self) -> dict:
        """创建低权限系统用户（可选，需要 root；离线环境跳过并说明）。"""
        if not self.options.create_user:
            return {"action": "create-user", "status": "skipped", "reason": "未指定 --create-user"}
        if os.name != "posix":
            return {"action": "create-user", "status": "skipped", "reason": "非 POSIX"}
        if pwd is not None:
            try:
                pwd.getpwnam(self.layout.user)
            except KeyError:
                pass
            else:
                return {"action": "create-user", "status": "skipped", "reason": "用户已存在"}
        prefix = _cmd_for(self.options.useradd, "useradd")
        if prefix is None:
            return {"action": "create-user", "status": "skipped",
                    "reason": "找不到 useradd；请手工创建系统用户"}
        argv = [*prefix, "--system", "--no-create-home", "--home-dir", str(self.layout.lib_dir),
                "--shell", "/usr/sbin/nologin", "--user-group", self.layout.user]
        if self.options.dry_run:
            return {"action": "create-user", "status": "planned", "argv": argv}
        result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60)
        return {
            "action": "create-user",
            "status": "ok" if result.returncode == 0 else "error",
            "argv": argv,
            "returncode": result.returncode,
            "stderr": (result.stderr or "").strip()[:200],
        }

    # ------------------------------------------------------------ 落盘
    def _apply_modes(self, *, release_id: str | None) -> None:
        for row in self.layout.mode_table(release_id):
            path = pathlib.Path(row["path"])
            if not path.exists():
                continue
            if row["kind"] == "dir":
                self.rec.mkdir(path, mode=row["mode"])
            else:
                if os.name == "posix" and not self.options.dry_run:
                    try:
                        current = path.stat().st_mode & 0o7777
                        if current != row["mode"]:
                            os.chmod(path, row["mode"])
                    except OSError:  # pragma: no cover
                        pass
            self.rec.chown(path, row["owner"], enabled=self.options.chown)

    def _create_layout_dirs(self) -> None:
        """创建目录骨架。

        属主意图**与 ``mode_table()`` 逐项一致**（QA-006A）：这里记录的 owner 必须等于
        最终 ``_apply_modes()`` 会 chown 成的属主，否则报告与实际就会互相矛盾。
        """
        for path, mode, owner in (
            (self.layout.app_dir, 0o755, self.layout.root_owner),
            (self.layout.releases_dir, 0o755, self.layout.root_owner),
            (self.layout.etc_dir, 0o750, self.layout.config_owner),
            (self.layout.lib_dir, 0o750, self.layout.data_owner),
            (self.layout.backups_dir, 0o700, self.layout.data_owner),
            (self.layout.cache_dir, 0o750, self.layout.data_owner),
            (self.layout.dynamic_tmp_dir, 0o750, self.layout.data_owner),
            (self.layout.run_dir, 0o750, self.layout.data_owner),
        ):
            self.rec.mkdir(path, mode=mode, owner=owner)
        if self.options.service_manager == "systemd":
            self.rec.mkdir(self.layout.systemd_dir, mode=0o755, owner=self.layout.root_owner)

    def _create_venv(self) -> dict:
        layout = self.layout
        if self.options.no_venv:
            self.notes.append("--no-venv：直接使用给定解释器，不创建 venv（离线/演练用）。")
            return {"action": "venv", "status": "skipped", "reason": "--no-venv"}
        if layout.venv_python.exists():
            return {"action": "venv", "status": "skipped", "reason": "venv 已存在"}
        _guard(layout.venv_dir)
        if self.options.dry_run:
            return {"action": "venv", "status": "planned", "path": str(layout.venv_dir)}
        layout.venv_dir.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [self.python, "-m", "venv", str(layout.venv_dir)],
            capture_output=True, text=True, check=False, timeout=600,
        )
        if result.returncode != 0:
            return {
                "action": "venv", "status": "error", "returncode": result.returncode,
                "stderr": (result.stderr or "").strip()[:300],
            }
        return {"action": "venv", "status": "ok", "path": str(layout.venv_dir)}

    def _install_project(self, release_dir: pathlib.Path) -> dict:
        """把项目装进 venv。

        * ``copy``（默认）：release 目录 + unit 里的 ``PYTHONPATH``，**不需要网络**；
        * ``pip``：``<venv>/pip install --no-deps <release>``，需要能取到构建依赖（有网）。
        """
        if self.options.no_venv:
            return {"action": "install-project", "status": "skipped",
                    "reason": "--no-venv：由 PYTHONPATH 指向 release"}
        if self.options.method == "copy":
            return {"action": "install-project", "status": "skipped",
                    "reason": "copy 方式：unit 的 PYTHONPATH 指向 release 目录（不联网）"}
        if self.options.method != "pip":
            raise DeployError(f"未知的安装方式：{self.options.method!r}")
        pip = self.layout.venv_pip
        argv = [str(pip), "install", "--no-deps", "--disable-pip-version-check", str(release_dir)]
        if self.options.dry_run:
            return {"action": "install-project", "status": "planned", "argv": argv}
        result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=900)
        return {
            "action": "install-project",
            "status": "ok" if result.returncode == 0 else "error",
            "returncode": result.returncode,
            "stderr": (result.stderr or "").strip()[:300],
        }

    def _init_database(self) -> dict:
        db_path = self.layout.db_path
        if db_path.is_file():
            return {"action": "init-db", "status": "skipped", "reason": "数据库已存在，绝不重建"}
        if not self.options.init_db:
            return {"action": "init-db", "status": "skipped", "reason": "--no-init-db"}
        _guard(db_path)
        if self.options.dry_run:
            return {"action": "init-db", "status": "planned", "path": str(db_path)}
        conn = db_mod.connect(db_path)
        try:
            version = db_mod.init_db(conn)
        finally:
            conn.close()
        return {"action": "init-db", "status": "ok", "path": str(db_path), "schema": version}

    def _deploy_release(self, release_id: str) -> dict:
        """复制 release 内容（已存在则跳过，不原地覆盖）。"""
        source = self.options.source_dir
        target = self.layout.release_dir(release_id)
        self.rec.copy_tree(source, target)
        return {"action": "release", "release_id": release_id, "path": str(target),
                "exists": target.is_dir()}

    def _prune_releases(self, keep_ids: set[str]) -> list[str]:
        """只保留最近的 release 目录，且 current/previous 永不删。"""
        if self.options.dry_run or not self.layout.releases_dir.is_dir():
            return []
        dirs = [p for p in self.layout.releases_dir.iterdir() if p.is_dir()]
        dirs.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        removed: list[str] = []
        kept = 0
        for path in dirs:
            if path.name in keep_ids or kept < RELEASE_KEEP:
                kept += 1
                continue
            try:
                shutil.rmtree(path)
            except OSError:  # pragma: no cover - 权限/占用
                continue
            removed.append(path.name)
        return removed

    # ------------------------------------------------------------ 服务动作
    def _service_for(self, release_id: str) -> ServiceManager:
        release_dir = self.layout.release_dir(release_id)
        python = str(self.layout.venv_python) if not self.options.no_venv else self.python
        return build_service_manager(self.options, release_dir=release_dir, python=python)

    def _render_and_install_unit(self, *, release_id: str) -> None:
        python = str(self.layout.venv_python) if not self.options.no_venv else self.python
        text = self.render_unit(release_id=release_id, python=python)
        problems = validate_unit(text, required_writable_dirs=[
            self.layout.lib_dir, self.layout.cache_dir, self.layout.run_dir,
        ])
        if problems:
            raise DeployError("unit 校验失败：" + "；".join(problems))
        self.rec.write_text(self.layout.unit_path, text, mode=0o644, owner="root:root",
                            overwrite=True, what="systemd unit")

    def _write_pointer(self, release_id: str) -> None:
        """``current`` 指针由 **root** 拥有：服务用户不应能改写「哪个 release 是活动的」。"""
        self.rec.write_text(self.layout.current_pointer, release_id + "\n", mode=0o644,
                            owner=self.layout.root_owner,
                            overwrite=True, what="release 指针")

    def _health_base(self) -> str | None:
        if self.options.health_url:
            return self.options.health_url
        if not self.layout.config_path.is_file():
            return None
        server = config_mod.server_settings(self._read_config())
        if not server["enabled"]:
            return None
        return health_mod.health_base_url(server["host"], server["port"])

    def _wait_health(self) -> dict:
        if self.options.dry_run:
            return {"ok": None, "skipped": True, "detail": "dry-run：没有启动服务，不做健康检查"}
        base = self._health_base()
        if base is None:
            if self.options.skip_health:
                return {"ok": None, "skipped": True, "detail": "按要求跳过健康检查（--skip-health）"}
            return {
                "ok": False, "skipped": False,
                "detail": "无法做健康检查：生产配置里 [server] enabled = false 且未给 --health-url。"
                          "systemd active 不等于业务健康，本流程要求 /healthz 真的 ok"
                          "（确需跳过请显式 --skip-health）",
            }
        if self.options.skip_health:
            return {"ok": None, "skipped": True, "detail": "按要求跳过健康检查（--skip-health）",
                    "base_url": health_mod.normalize_base_url(base)}
        result = health_mod.wait_until_healthy(
            base, timeout=self.options.health_timeout,
            interval=self.options.health_interval, request_timeout=min(3.0, self.options.health_timeout),
        )
        payload = result.to_dict()
        payload["skipped"] = False
        return payload

    # ------------------------------------------------------------ install
    def install(self) -> dict:
        payload = self._base_payload("install" if not self.options.dry_run else "plan")
        problems = self.preflight(require_config=False, require_installed=False)
        if problems:
            for problem in problems:
                self._step("preflight", "error", detail=problem)
            return self._finish(payload, "PREFLIGHT_FAILED", EXIT_PREFLIGHT)
        self._step("preflight", "ok")

        state = self.load_state()
        release_id = resolve_release_id(self.options.source_dir, self.options.release_id,
                                        now=self.options.now)
        payload["release_id"] = release_id
        payload["previous_release"] = state.get("current")

        self._step_info("create-user", self.ensure_user())
        self._create_layout_dirs()
        self._step("layout", "ok" if not self.options.dry_run else "planned",
                   app_dir=str(self.layout.app_dir), etc_dir=str(self.layout.etc_dir),
                   lib_dir=str(self.layout.lib_dir), cache_dir=str(self.layout.cache_dir),
                   run_dir=str(self.layout.run_dir))
        self._step_info("release", self._deploy_release(release_id))

        venv = self._create_venv()
        self._step_info("venv", venv)
        if venv.get("status") == "error":
            return self._finish(payload, "FAILED", EXIT_ERROR)
        self._step_info("install-project", self._install_project(self.layout.release_dir(release_id)))

        # 配置：**永不覆盖**已存在的生产配置
        config_text = self.render_config()
        problems = self.validate_config_text(config_text)
        if problems:
            for problem in problems:
                self._step("config-validate", "error", detail=problem)
            return self._finish(payload, "FAILED", EXIT_ERROR)
        self._step("config-validate", "ok")
        wrote = self.rec.write_text(
            self.layout.config_path, config_text, mode=0o640,
            owner=self.layout.config_owner, overwrite=False,
            what="生产配置",
        )
        self._step("config", "planned" if self.options.dry_run else ("ok" if wrote else "skipped"),
                   path=str(self.layout.config_path))

        self._step_info("init-db", self._init_database())

        if self.options.service_manager != "none":
            self._render_and_install_unit(release_id=release_id)
        self._write_pointer(release_id)

        state = dict(state)
        state["current"] = release_id
        state["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state.setdefault("prefix", str(self.layout.root))
        state["user"] = self.layout.user
        state["group"] = self.layout.group
        state["method"] = self.options.method
        state = self._record_event(state, "install", release=release_id,
                                   dry_run=self.options.dry_run)
        if not self.options.dry_run:
            self.save_state(state)

        self._apply_modes(release_id=release_id)

        if self.options.service_manager != "none":
            self.service = self._service_for(release_id)
            self._service_action("systemctl:daemon-reload", self.service.daemon_reload)
            self._service_action("systemctl:enable", self.service.enable)
        if self.options.start and not self.options.dry_run:
            self._service_action("systemctl:start", self.service.start)
            health = self._wait_health()
            self._step("health", "ok" if health.get("ok") else "error", **{
                k: v for k, v in health.items() if k != "ok"
            })
            payload["health"] = health
            if not health.get("ok"):
                return self._finish(payload, "HEALTH_FAILED", EXIT_HEALTH)

        self.notes.append(
            "计划模式：以上均为「将要执行」的动作，未写任何文件、未调用 systemd。"
            if self.options.dry_run else
            "安装完成。配置与数据库永不覆盖：重复 install 是幂等的。"
        )
        return self._finish(payload, "PLAN" if self.options.dry_run else "OK", EXIT_OK)

    # ------------------------------------------------------------ upgrade
    def upgrade(self) -> dict:
        payload = self._base_payload("upgrade")
        problems = self.preflight(require_config=True, require_installed=True)
        if problems:
            for problem in problems:
                self._step("preflight", "error", detail=problem)
            return self._finish(payload, "PREFLIGHT_FAILED", EXIT_PREFLIGHT)

        # 升级前体检：跳过端口检查（老服务可能仍在跑并占着端口）
        #
        # QA-007B（service-aware preflight）
        # ---------------------------------
        # systemd 用 ``RuntimeDirectory`` 托管锁目录，服务一停该目录就被删除。
        # 于是旧逻辑形成「不可能同时满足」的两态：服务在跑 ⇒ lock fail；
        # 服务已停 ⇒ dirs fail。这里把服务状态显式告知 doctor：
        #   * ``lock`` 为活实例持锁 ⇒ 只读 preflight 放行（切换前会先 stop）；
        #   * 服务已停 ⇒ 锁目录缺失属正常。
        service_probe = self._service_for(self.active_release())
        # ``query()["active"]`` 可能是 systemctl 的**字符串**（"active" / "inactive" /
        # "failed"，也可能是 "activating" 等过渡态）、布尔或 None。三态归一化由
        # ``_service_active_state`` 统一负责，**只有明确 active 才 True、只有明确
        # inactive/failed 才 False，过渡态与判不出的一律 None**（QA-007B-1）。
        service_active = _service_active_state(service_probe.query().get("active"))
        doctor_result = doctor_mod.collect(
            self.layout.config_path,
            check_port=False,
            service_active=service_active,
            allow_live_lock=True,
        )
        payload["doctor"] = doctor_result
        payload["service_active_at_preflight"] = service_active
        if not doctor_result["ok"]:
            failed = [c["id"] for c in doctor_result["checks"] if c["status"] == "fail"]
            self._step("doctor", "error", failed=failed, service_active=service_active)
            self.notes.append("升级前体检不通过（doctor）。修好后再升级；本次未做任何变更。")
            return self._finish(payload, "PREFLIGHT_FAILED", EXIT_PREFLIGHT)
        self._step("doctor", "ok", service_active=service_active)

        old_release = self.active_release()
        release_id = resolve_release_id(self.options.source_dir, self.options.release_id,
                                        now=self.options.now)
        if release_id == old_release and not self.options.force:
            self._step("resolve-release", "error",
                       detail=f"新 release id 与当前相同（{release_id}）；要重装请加 --force")
            return self._finish(payload, "FAILED", EXIT_ERROR)
        payload["release_id"] = release_id
        payload["previous_release"] = old_release
        self._step("resolve-release", "ok", release_id=release_id, previous=old_release)

        # 1) 备份 SQLite（+ 记录配置元数据）
        if self.options.dry_run:
            backup = {"skipped": True, "reason": "dry-run：不做备份"}
        else:
            backup = backup_mod.backup_sqlite(
                self.layout.db_path, self.layout.backups_dir,
                retention=self.options.retention, now=self.options.now, label=release_id,
            )
        payload["backup"] = backup
        config_meta = None
        if self.layout.config_path.is_file():
            text = _read_text(self.layout.config_path)
            config_meta = {"path": str(self.layout.config_path), "bytes": len(text.encode()),
                           "sha256": _sha256_text(text),
                           "mtime": self.layout.config_path.stat().st_mtime}
        self._step("backup", "planned" if self.options.dry_run else "ok", **(
            {"path": backup.get("path")} if not backup.get("skipped") else {"skipped": True}
        ))

        # 2) 停服务
        self.service = self._service_for(old_release or release_id)
        self._service_action("systemctl:stop", self.service.stop)

        # 2b) 停服务后重建锁目录（QA-007B）
        # systemd ``RuntimeDirectory`` 会在服务停止时删除该目录。升级/回滚期间后续步骤
        # 都要求它存在，因此这里显式重建并回填属主。
        # **只在目录缺失时创建**，绝不修改已存在的目录。
        if not self._ensure_run_dir(payload):
            return self._finish(payload, "FAILED", EXIT_ERROR)

        # 3) 装新 release + 渲染 unit
        self._step_info("release", self._deploy_release(release_id))
        self._render_and_install_unit(release_id=release_id)
        self._write_pointer(release_id)
        self._apply_modes(release_id=release_id)
        state = self.load_state()
        state["previous"] = old_release
        state["current"] = release_id
        state = self._record_event(state, "upgrade", release=release_id, previous=old_release,
                                   config_meta=config_meta)
        if not self.options.dry_run:
            self.save_state(state)

        # 4) 起服务 + 等健康
        # **必须换成「新 release」的服务管理器**：systemd 是从磁盘 unit 读 PYTHONPATH 的，
        # 但 ``process`` 管理器是按 release 目录起进程的 —— 沿用停旧服务时的那个管理器，
        # 会把**旧版本**当成新版本起起来，于是健康闸门验的是旧版本、永远发现不了坏 release
        # （TASK-006 首版缺陷，由 tools/demo_deploy_linux.py 的「注入坏 release」场景暴露）。
        self.service = self._service_for(release_id)
        self._service_action("systemctl:daemon-reload", self.service.daemon_reload)
        self._service_action("systemctl:start", self.service.start)
        health = self._wait_health()
        payload["health"] = health
        if health.get("ok"):
            self._step("health", "ok", **{k: v for k, v in health.items() if k != "ok"})
            self._prune_releases({release_id, old_release} - {None})
            self.notes.append("升级成功；数据库未做任何迁移（schema 仍为 V1）。")
            return self._finish(payload, "OK", EXIT_OK)
        if self.options.dry_run:
            self._step("health", "planned", detail="dry-run：未停/未起任何服务，也未做备份")
            self.notes.append("计划模式：以上只是将要执行的动作，未发生任何变更。")
            return self._finish(payload, "PLAN", EXIT_OK)
        self._step("health", "error", **{k: v for k, v in health.items() if k != "ok"})
        self.notes.append("新 release 健康检查未通过 → 进入应用回滚（数据库不回滚）。")

        # 5) 回滚应用 release
        return self._rollback_locked(payload, target_release=old_release,
                                     reason="health_failed_after_upgrade")

    def _ensure_run_dir(self, payload: dict) -> bool:
        """停服务后确保 ``run_dir`` 存在（QA-007B）。返回是否可继续。

        systemd ``RuntimeDirectory`` 在服务停止时会删除该目录；``upgrade`` /
        ``rollback`` 停服务后仍需要它，因此显式重建。**只在缺失时创建**。
        """
        if self.options.dry_run:
            return True
        run_dir = self.layout.run_dir
        if run_dir.exists():
            return True
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(run_dir, 0o750)
            self.rec.chown(run_dir, self.layout.data_owner, enabled=self.options.chown)
        except OSError as exc:
            self._step("lock-dir", "error", path=str(run_dir), detail=str(exc))
            self.notes.append("锁目录重建失败；已中止（未切换 release）。")
            return False
        self._step("lock-dir", "ok", path=str(run_dir),
                   detail="服务停止后 RuntimeDirectory 被 systemd 删除，已重建（QA-007B）")
        return True

    def _rollback_locked(self, payload: dict, *, target_release: str | None,
                         reason: str) -> dict:
        if not target_release:
            self._step("rollback", "error", detail="没有可回滚的 release")
            return self._finish(payload, "FAILED", EXIT_ERROR)
        target_dir = self.layout.release_dir(target_release)
        if not target_dir.is_dir():
            self._step("rollback", "error",
                       detail=f"上一个 release 目录已不存在：{target_dir}")
            return self._finish(payload, "FAILED", EXIT_ERROR)

        current = self.active_release()
        self._step("rollback", "ok", target=target_release, from_=current)
        if self.service is not None:
            self.service.stop()
        # 停服务后 systemd 会删掉 RuntimeDirectory ⇒ 回填（QA-007B，与 upgrade 同一处理）
        if not self._ensure_run_dir(payload):
            return self._finish(payload, "FAILED", EXIT_ERROR)
        self._write_pointer(target_release)
        self._render_and_install_unit(release_id=target_release)
        self._apply_modes(release_id=target_release)
        state = self.load_state()
        state["previous"] = current
        state["current"] = target_release
        state = self._record_event(state, "rollback", release=target_release,
                                   from_release=current, reason=reason)
        if not self.options.dry_run:
            self.save_state(state)
        self.service = self._service_for(target_release)
        self._service_action("systemctl:daemon-reload", self.service.daemon_reload)
        self._service_action("systemctl:start", self.service.start)
        health = self._wait_health()
        payload["health"] = health
        payload["health_after_rollback"] = health
        self._step("health_after_rollback", "ok" if health.get("ok") else "error",
                   **{k: v for k, v in health.items() if k != "ok"})
        self.notes.append(
            f"已回滚应用到 {target_release}；数据库与 live.m3u 均未改动"
            f"（数据库恢复只能显式执行 restore-db --yes）。"
        )
        if health.get("ok"):
            return self._finish(payload, "ROLLED_BACK", EXIT_HEALTH)
        if self.options.dry_run:
            return self._finish(payload, "PLAN", EXIT_OK)
        return self._finish(payload, "ROLLBACK_FAILED", EXIT_ERROR)

    # ------------------------------------------------------------ rollback
    def rollback(self) -> dict:
        payload = self._base_payload("rollback")
        problems = self.preflight(require_config=True, require_installed=True)
        if problems:
            for problem in problems:
                self._step("preflight", "error", detail=problem)
            return self._finish(payload, "PREFLIGHT_FAILED", EXIT_PREFLIGHT)
        state = self.load_state()
        target = state.get("previous")
        payload["previous_release"] = target
        payload["release_id"] = state.get("current")
        if not target:
            self._step("rollback", "error", detail="deploy-state.json 里没有 previous release")
            return self._finish(payload, "FAILED", EXIT_ERROR)
        self.service = self._service_for(state.get("current") or target)
        return self._rollback_locked(payload, target_release=target, reason="manual_rollback")

    # ------------------------------------------------------------ status
    def status(self, *, probe_health: bool = True) -> dict:
        payload = self._base_payload("status")
        state = self.load_state()
        release = self.active_release()
        payload["release_id"] = release
        payload["previous_release"] = state.get("previous")
        payload["state"] = {
            "installed_at": state.get("installed_at"),
            "updated_at": state.get("updated_at"),
            "prefix": state.get("prefix"),
            "user": state.get("user"),
            "group": state.get("group"),
            "events": list(state.get("events") or [])[-5:],
        }
        artifacts: dict[str, dict] = {}
        for name, path in (
            ("config", self.layout.config_path),
            ("database", self.layout.db_path),
            ("playlist", self.layout.output_path),
            ("status_json", self.layout.status_path),
            ("summary", self.layout.summary_path),
            ("unit", self.layout.unit_path),
            ("venv", self.layout.venv_python),
            ("release_pointer", self.layout.current_pointer),
        ):
            info = {"path": str(path), "exists": path.exists()}
            if path.is_file():
                stat = path.stat()
                info.update(bytes=stat.st_size, mtime=stat.st_mtime)
            artifacts[name] = info
        payload["artifacts"] = artifacts
        payload["backups"] = [str(p) for p in backup_mod.list_backups(self.layout.backups_dir)]
        payload["releases"] = sorted(
            p.name for p in self.layout.releases_dir.iterdir()
        ) if self.layout.releases_dir.is_dir() else []

        for kind in ("systemd", "process"):
            if kind == "systemd":
                prefix = _cmd_for(self.options.systemctl, "systemctl")
                if prefix is None:
                    continue
                manager = SystemdServiceManager(prefix)
                if manager._run("is-active", UNIT_NAME).get("returncode") not in (0, 3):
                    continue  # 没有该 unit，忽略
                payload["service"] = manager.query()
                break
            manager = ProcessServiceManager(
                layout=self.layout, python=self.python,
                release_dir=self.active_release_dir() or self.layout.app_dir,
                config_path=self.layout.config_path,
            )
            info = manager.query()
            if str(info.get("active")) == "active":
                payload["service"] = info
                break

        if probe_health:
            base = self._health_base()
            if base is None:
                payload["health"] = {"ok": None, "detail": "未配置 HTTP 订阅（[server] enabled = false）"}
            elif base.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
                payload["health"] = health_mod.check_once(base, timeout=2.0).to_dict()
            else:
                payload["health"] = {"ok": None, "detail": "非 loopback 地址，status 不主动探测"}

        payload["healthy"] = bool((payload.get("health") or {}).get("ok"))
        self.notes.append("status 只读：不抓取、不发布、不改数据库、不切换 release。")
        return self._finish(payload, "OK", EXIT_OK)

    # ------------------------------------------------------------ 备份/恢复
    def backup(self) -> dict:
        payload = self._base_payload("backup")
        if self.options.dry_run:
            self._step("backup", "planned")
            self.notes.append("dry-run：不会创建任何备份文件。")
            return self._finish(payload, "PLAN", EXIT_OK)
        result = backup_mod.backup_sqlite(
            self.layout.db_path, self.layout.backups_dir,
            retention=self.options.retention, now=self.options.now,
        )
        payload["backup"] = result
        self._step("backup", "ok" if not result.get("skipped") else "skipped",
                   **{k: v for k, v in result.items() if k != "skipped"})
        if result.get("skipped"):
            self._step("backup", "skipped", detail=result.get("reason") or "没有可备份的数据库")
            self.notes.append(result.get("reason") or "没有可备份的数据库")
            return self._finish(payload, "SKIPPED", EXIT_ERROR)
        return self._finish(payload, "OK", EXIT_OK)

    def _restore_service_gate(self, *, force_offline: bool) -> dict:
        """``restore-db`` 的**停机门禁**（QA-006B，fail-closed）。

        语义（冻结）：

        1. 能判断服务状态时：若服务 ``active`` ⇒ **先 stop，再复查** ``inactive``；
           stop 报错或复查仍 ``active`` ⇒ 拒绝（不写库）。
        2. 判断不了状态（``--service-manager none`` / 非托管 / 未安装）⇒ **默认拒绝**，
           除非显式 ``--force-offline-restore``（break-glass，调用方对「服务已停」负责）。
        3. ``--yes`` **只**代表「确认覆盖数据库」，**不代表**服务已停 —— 两者是独立确认。

        返回的 dict 永远带 ``blocked`` / ``reason``；只有 ``blocked`` 为 False 才允许写库。
        """
        manager = self.service
        state = manager.query() if isinstance(manager, ServiceManager) \
            else {"managed": False, "active": None, "enabled": None}

        if not state.get("managed"):
            if force_offline:
                return {"blocked": False, "forced": True, "stopped": False,
                        "reason": "已使用 --force-offline-restore：调用方声明服务未在运行"
                                  "（break-glass，本命令无法替其核实）。"}
            return {"blocked": True, "forced": False, "stopped": False,
                    "reason": "无法判断服务状态（未启用服务托管）：默认拒绝恢复数据库。"
                              "请用 --service-manager systemd/process 让本命令核实并自动停机，"
                              "或在确认服务确已停止后显式加 --force-offline-restore。"}

        if state.get("active") == "active":
            stop = manager.stop()
            after = manager.query()
            if stop.get("status") == "error" or after.get("active") == "active":
                return {"blocked": True, "forced": False, "stopped": False,
                        "reason": "服务仍在运行且停止失败：拒绝恢复（数据库字节未变）。",
                        "before": state, "stop": stop, "after": after}
            return {"blocked": False, "forced": False, "stopped": True,
                    "reason": "服务原本 active：已自动 stop 并复查为 inactive。",
                    "before": state, "stop": stop, "after": after}

        return {"blocked": False, "forced": False, "stopped": True,
                "reason": f"服务当前不是 active（{state.get('active')}）：允许恢复。",
                "before": state}

    def restore_db(self, backup_path, *, yes: bool, force_offline: bool = False) -> dict:
        """把一份备份恢复到生产数据库（**唯一**会写数据库内容的入口，QA-006B）。

        门禁顺序：``--yes`` → 停机门禁 → 一致性安全副本 → 替换 → 恢复后复验。
        任何一步不过 ⇒ 抛错/拒绝，且**数据库字节不变**（安全副本失败也在替换之前）。
        """
        payload = self._base_payload("restore-db")
        if not yes:
            raise DeployError("恢复数据库是破坏性动作，必须显式 --yes")

        # 解析「当前部署实际用的托管方式」，才能核实服务状态（QA-006B 第 1 条）。
        active = self.active_release()
        release_dir = self.layout.release_dir(active) if active else self.layout.app_dir
        python = str(self.layout.venv_python) if not self.options.no_venv else self.python
        self.service = build_service_manager(self.options, release_dir=release_dir, python=python)

        gate = self._restore_service_gate(force_offline=force_offline)
        payload["service_gate"] = gate
        self._step("service-gate", "ok" if not gate["blocked"] else "error",
                   reason=gate["reason"], stopped=gate.get("stopped"),
                   forced=gate.get("forced"))
        if gate["blocked"]:
            self.notes.append(gate["reason"])
            self.notes.append("数据库**字节未变**：门禁未通过时不做任何写库动作。")
            return self._finish(payload, "BLOCKED_SERVICE_RUNNING", EXIT_PREFLIGHT)

        result = backup_mod.restore_sqlite(backup_path, self.layout.db_path,
                                           allow_overwrite=yes, now=self.options.now)
        payload["restore"] = result
        self._step("restore-db", "ok", to=result["to"],
                   safety_copy=result.get("safety_copy"),
                   safety_copy_method=result.get("safety_copy_method"))
        self.notes.append("恢复数据库是**显式**动作：回滚代码永远不会自动覆盖数据库。")
        if gate.get("stopped"):
            self.notes.append(
                "服务已停机（本命令停止，或原先就未运行），**不会自动重启**："
                "请确认数据无误后手工执行 `systemctl start li-iptv`（并复查 /healthz）。"
            )
        elif gate.get("forced"):
            self.notes.append(
                "使用了 --force-offline-restore：调用方声明服务已停；"
                "本命令**无法**替你核实这一点，请自行确认没有进程仍在写库。"
            )
        return self._finish(payload, "OK", EXIT_OK)


# ==================================================================== unit 校验

#: unit 里必须出现的安全检查项（TASK-006 §4 的「hardening 必须有自动验证」）
REQUIRED_UNIT_DIRECTIVES = (
    "User=", "Group=", "WorkingDirectory=", "Environment=PYTHONPATH=", "ExecStart=",
    "Restart=on-failure", "RestartPreventExitStatus=", "KillSignal=SIGTERM",
    "TimeoutStopSec=", "NoNewPrivileges=true", "PrivateTmp=true",
    "ProtectSystem=strict", "ProtectHome=", "ReadWritePaths=", "RuntimeDirectory=",
    "WantedBy=multi-user.target",
)

#: ExecStart 不得出现的 shell 元字符（避免敏感信息被 shell 展开 / 参数注入）
_FORBIDDEN_EXEC_CHARS = (";", "&&", "||", "$(", "`", "|", ">", "<", "*")


def validate_unit(text: str, *, required_writable_dirs: list[pathlib.Path] | None = None) -> list[str]:
    """静态校验渲染后的 unit。返回问题列表（空 = 通过）。

    这是「不抄一堆 hardening 参数然后祈祷能跑」的自动验证：既查必需指令，也查
    ``ProtectSystem=strict`` 与 ``ReadWritePaths`` 是否**覆盖了配置里所有可写目录**。
    """
    problems: list[str] = []
    lines = [ln.strip() for ln in text.splitlines()]
    body = [ln for ln in lines if ln and not ln.startswith("#")]
    if any("@@" in ln for ln in body):
        problems.append("unit 里仍有未替换的占位符")

    for directive in REQUIRED_UNIT_DIRECTIVES:
        if not any(ln.startswith(directive) for ln in body):
            problems.append(f"缺少必需指令：{directive}")

    def value_of(key: str) -> str | None:
        for ln in body:
            if ln.startswith(key):
                return ln.split("=", 1)[1].strip()
        return None

    user = value_of("User=")
    if user is None or user in {"root", "0"}:
        problems.append("User= 缺失或为 root：服务必须非 root 运行")

    exec_start = value_of("ExecStart=")
    if exec_start:
        for bad in _FORBIDDEN_EXEC_CHARS:
            if bad in exec_start:
                problems.append(f"ExecStart 含 shell 元字符 {bad!r}（不允许展开/拼接）")
        if "-m liptv run" not in exec_start or "--serve" not in exec_start:
            problems.append("ExecStart 必须是 `<venv>/python -m liptv run --serve --config …`")
        if "--config" not in exec_start:
            problems.append("ExecStart 缺少 --config")
        if "?" in exec_start or "token" in exec_start.lower():
            problems.append("ExecStart 疑似携带 query/token")
    else:
        problems.append("ExecStart 缺失")

    restart_sec = value_of("RestartSec=")
    if restart_sec is not None and restart_sec.rstrip("s").isdigit() and int(restart_sec.rstrip("s")) < 5:
        problems.append("RestartSec 过小（< 5s）：可能形成重启风暴")

    prevent = value_of("RestartPreventExitStatus=") or ""
    tokens = {t.strip() for t in prevent.replace(",", " ").split()}
    if "3" not in tokens:
        problems.append("RestartPreventExitStatus 未包含 3（EXIT_LOCKED）⇒ 持锁时会高速重启")

    timeout_stop = value_of("TimeoutStopSec=")
    if timeout_stop is not None:
        digits = timeout_stop.rstrip("s")
        if digits.isdigit() and int(digits) < 30:
            problems.append("TimeoutStopSec < 30s：可能截断一轮 ffprobe 清理")

    if (value_of("ProtectSystem=") or "") == "strict":
        rw = value_of("ReadWritePaths=") or ""
        if not rw:
            problems.append("ProtectSystem=strict 但 ReadWritePaths 为空：服务将无法写数据")
        entries = [pathlib.PurePosixPath(item) for item in rw.split() if item]
        # unit 里的路径一律是 POSIX 形式（含 Windows 测试前缀也会被转成正斜杠），
        # 所以两边都规一到 PurePosixPath 再比，保证 Linux / 离线验证口径一致。
        for target in required_writable_dirs or []:
            wanted = pathlib.PurePosixPath(pathlib.Path(target).as_posix())
            if not any(wanted == entry or entry in wanted.parents for entry in entries):
                problems.append(
                    f"可写目录 {target} 未被 ReadWritePaths 覆盖（ProtectSystem=strict 下会写失败）"
                )
        if any(str(item) in {"/", "/var", "/etc", "/usr", "/var/lib"} for item in entries):
            problems.append("ReadWritePaths 过宽（不应开放整个 /var、/etc 或 /）")

    for ln in body:
        if re.search(r"https?://[^\s\"]*\?", ln):
            problems.append(f"unit 里出现带 query 的 URL：{ln[:80]}")
    return problems
