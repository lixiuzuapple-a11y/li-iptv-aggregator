"""生产 preflight 体检（TASK-006 §5）。

``python -m liptv doctor --config /etc/li-iptv-aggregator/config.toml``

只做诊断，**不**改数据库业务状态、**不**抓取、**不**发布、**不**请求任何媒体流：

| 检查 | 判据 | 失败级别 |
|---|---|---|
| `config` | 配置文件存在且可解析 | fail |
| `guard` | 所有运行期路径都不落在「Git 工作树内且未被忽略/已被跟踪」的位置 | fail |
| `dirs` | DB / 输出 / 状态 / 锁 / 摘要 / 动态快照目录存在且可写 | fail |
| `db` | DB 文件状态（不存在 ⇒ warn：首次安装时正常） | warn |
| `schema` | schema 版本 == ``db.SCHEMA_VERSION``（不升级、不迁移） | fail |
| `ffprobe` | **仅当** ``probe.enabled = true`` 时要求 ``ffprobe -version`` 可用 | fail |
| `port` | **仅当** ``server.enabled = true`` 时要求 ``host:port`` 可绑定 | fail |
| `lock` | 单实例锁只读检查：无锁/过期锁 pass，活实例持有 ⇒ fail | fail |

关于「唯一会碰盘的动作」：``dirs`` 检查用一个临时探针文件真实写入并**立即删除**，
才能诚实地判断可写（``os.access`` 在 root 下会骗人）。探针文件不碰数据库、不碰
``live.m3u``，可用 ``write_probe=False`` 关掉。

一个例外：**已经被 ``guard`` 判为不合法的路径，``dirs`` 不会再往里写探针**
（只退化成 ``os.access``）。理由是自洽 —— 护栏刚说「这里不许写运行期产物」，
体检工具自己就更不该往那里写文件；顺带也避免了「误配到仓库内目录时 doctor 反而在
仓库里建/删临时文件」这种副作用。
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import socket
import sqlite3

from . import backup as backup_mod
from . import config as config_mod
from . import db as db_mod
from . import probe as probe_mod
from . import publish as publish_mod
from . import runtime as runtime_mod

CHECK_OK = "ok"
CHECK_WARN = "warn"
CHECK_FAIL = "fail"
CHECK_SKIP = "skip"

#: 探针文件名（写完立即删除）
_PROBE_NAME = ".liptv-doctor-probe"


@dataclasses.dataclass(frozen=True)
class Check:
    id: str
    status: str
    message: str
    detail: dict

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _check(id_: str, status: str, message: str, **detail) -> Check:
    return Check(id=id_, status=status, message=message, detail=detail)


def _dir_writable(directory: pathlib.Path, *, write_probe: bool) -> tuple[bool, str]:
    """真实判断目录是否可写（不创建目录）。"""
    if not directory.exists():
        return False, "目录不存在"
    if not directory.is_dir():
        return False, "路径不是目录"
    if not write_probe:
        return bool(os.access(directory, os.W_OK)), "仅用 os.access 判断（未写探针）"
    probe = directory / _PROBE_NAME
    try:
        probe.write_text("probe\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        return False, f"写入探针失败：{type(exc).__name__}"
    finally:
        try:
            if probe.exists():
                probe.unlink()
        except OSError:  # pragma: no cover - 只读挂载的极端情况
            return False, "探针文件删不掉（目录可能只读）"
    return True, "写入并删除探针成功"


def runtime_paths(cfg: dict, *, db_override: str | None = None) -> dict[str, pathlib.Path]:
    """从配置里取出所有**运行期可写路径**（绝对化，不创建）。"""
    view = config_mod.runtime_settings(cfg)
    server = config_mod.server_settings(cfg)
    publish_cfg = config_mod.publish_settings(cfg)
    fetch_cfg = config_mod.fetch_settings(cfg)
    db_path = db_override or cfg.get("database", {}).get("path") or "data/liptv.sqlite3"
    return {
        "db": pathlib.Path(str(db_path)),
        "output": pathlib.Path(str(cfg.get("output", {}).get("m3u_path") or "out/live.m3u")),
        "status": pathlib.Path(str(view["status_path"])),
        "lock": pathlib.Path(str(view["lock_path"])),
        "summary": pathlib.Path(str(publish_cfg["summary_path"])),
        "dynamic_tmp": pathlib.Path(str(fetch_cfg["dynamic_tmp_dir"])),
        "server_host": server["host"],
        "server_port": int(server["port"]),
        "server_enabled": bool(server["enabled"]),
        "interval_seconds": int(view["interval_seconds"]),
        "stale_after_seconds": int(view["stale_after_seconds"]),
    }


def collect(
    config_path,
    *,
    db_override: str | None = None,
    check_port: bool = True,
    write_probe: bool = True,
    now: str | None = None,
) -> dict:
    """跑一遍体检，返回结构化结果（``ok`` = 没有 ``fail``）。"""
    cfg_path = pathlib.Path(config_path) if config_path else None
    checks: list[Check] = []

    # ---------------------------------------------------------------- config
    if cfg_path is None or not cfg_path.exists():
        checks.append(_check(
            "config", CHECK_FAIL,
            f"配置文件不存在：{cfg_path}（生产 doctor 要求显式配置文件，不使用内置默认值）",
            path=str(cfg_path),
        ))
        return _finish(cfg_path, checks)
    try:
        cfg = config_mod.load_config(cfg_path)
    except Exception as exc:  # tomllib.TOMLDecodeError / OSError
        checks.append(_check("config", CHECK_FAIL, f"配置无法解析：{exc}", path=str(cfg_path)))
        return _finish(cfg_path, checks)
    checks.append(_check("config", CHECK_OK, "配置存在且可解析", path=str(cfg_path)))

    paths = runtime_paths(cfg, db_override=db_override)

    # ------------------------------------------------------------------ guard
    guard_problems: list[str] = []
    guard_rejected: set[str] = set()
    for name, target in paths.items():
        if isinstance(target, (str, int, bool)):
            continue
        try:
            publish_mod.guard_runtime_output_path(target)
        except ValueError as exc:
            guard_problems.append(f"{name}: {exc}")
            guard_rejected.add(name)
    if guard_problems:
        checks.append(_check(
            "guard", CHECK_FAIL, "运行期路径与 Git 输出护栏冲突", problems=guard_problems
        ))
    else:
        checks.append(_check("guard", CHECK_OK, "运行期路径均不在受保护的 Git 工作树内"))

    # ------------------------------------------------------------------- dirs
    dir_targets = {
        "db": paths["db"].parent,
        "output": paths["output"].parent,
        "status": paths["status"].parent,
        "lock": paths["lock"].parent,
        "summary": paths["summary"].parent,
        "dynamic_tmp": paths["dynamic_tmp"],
    }
    dir_detail: dict[str, dict] = {}
    bad_dirs: list[str] = []
    for name, directory in dir_targets.items():
        if name in guard_rejected:
            # 护栏刚拒绝过这个位置，体检工具自己不再往那里写探针（只做 os.access）。
            ok = bool(os.access(directory, os.W_OK))
            note = "未写探针：该路径已被 Git 输出护栏拒绝，doctor 不往那里写文件"
        else:
            ok, note = _dir_writable(directory, write_probe=write_probe)
        dir_detail[name] = {"path": str(directory), "writable": ok, "note": note}
        if not ok:
            bad_dirs.append(f"{name}({directory})：{note}")
    if bad_dirs:
        checks.append(_check("dirs", CHECK_FAIL, "存在不可写的运行期目录", dirs=dir_detail,
                             problems=bad_dirs))
    else:
        checks.append(_check("dirs", CHECK_OK, "运行期目录均可写", dirs=dir_detail))

    # --------------------------------------------------------------------- db
    db_path = paths["db"]
    db_state = {"path": str(db_path), "exists": db_path.is_file()}
    if not db_path.is_file():
        db_state["bytes"] = None
        checks.append(_check(
            "db", CHECK_WARN,
            "数据库不存在（首次安装时正常；install 会显式 init，绝不会重建已有库）",
            **db_state,
        ))
        checks.append(_check("schema", CHECK_SKIP, "数据库不存在，跳过 schema 检查", **db_state))
    else:
        db_state["bytes"] = db_path.stat().st_size
        try:
            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row  # db.table_names / read_schema_version 按列名取值
        except sqlite3.Error as exc:
            checks.append(_check("db", CHECK_FAIL, f"数据库无法打开：{exc}", **db_state))
            checks.append(_check("schema", CHECK_SKIP, "数据库打不开，跳过 schema 检查", **db_state))
            conn = None
        if conn is not None:
            try:
                tables = db_mod.table_names(conn)
                version = db_mod.read_schema_version(conn)
                integrity_row = conn.execute("PRAGMA quick_check").fetchone()
                integrity = str(integrity_row[0]) if integrity_row else None
            except sqlite3.DatabaseError as exc:
                checks.append(_check("db", CHECK_FAIL, f"数据库读取失败：{exc}", **db_state))
                conn.close()
                conn = None
                version = None
                tables = []
                integrity = None
            if conn is not None:
                conn.close()
                db_state.update(tables=len(tables), integrity=integrity,
                                schema_version=version)
                if integrity == "ok" and tables:
                    checks.append(_check("db", CHECK_OK, "数据库可读且完整", **db_state))
                else:
                    checks.append(_check(
                        "db", CHECK_FAIL, f"数据库完整性异常（quick_check={integrity}）", **db_state
                    ))
                if version is None:
                    checks.append(_check(
                        "schema", CHECK_FAIL, "数据库没有 schema_version 记录（未初始化？）", **db_state
                    ))
                elif int(version) != db_mod.SCHEMA_VERSION:
                    checks.append(_check(
                        "schema", CHECK_FAIL,
                        f"schema 版本不匹配：库内 {version}，本项目要求 {db_mod.SCHEMA_VERSION}",
                        **db_state,
                    ))
                else:
                    checks.append(_check(
                        "schema", CHECK_OK, f"schema V{version} 与代码一致", **db_state
                    ))

    # ---------------------------------------------------------------- ffprobe
    probe_cfg = config_mod.probe_settings(cfg)
    probe_settings = probe_mod.ProbeSettings.from_mapping(probe_cfg)
    if not probe_settings.enabled:
        checks.append(_check(
            "ffprobe", CHECK_SKIP,
            "[probe] enabled = false：不要求 ffprobe（也不会有任何流测活）",
            ffprobe_path=probe_settings.ffprobe_path,
        ))
    else:
        capability = probe_mod.check_ffprobe(probe_settings)
        # FfprobeCapability 自身有一个 ``message`` 字段，直接 ``**to_dict()`` 会与
        # ``_check(..., message=...)`` 的形参撞名 ⇒ TypeError。改名为 ``probe_message``
        # 后再展开（只在「测活已启用」的分支才会走到，默认关闭时不会暴露）。
        info = capability.to_dict()
        info["probe_message"] = info.pop("message")
        if capability.ok:
            checks.append(_check(
                "ffprobe", CHECK_OK, f"ffprobe 可用：{capability.version_line}",
                **info,
            ))
        else:
            checks.append(_check(
                "ffprobe", CHECK_FAIL,
                f"ffprobe 不可用（{capability.error_type}）：{capability.message}",
                **info,
            ))

    # ------------------------------------------------------------------- port
    if not check_port:
        checks.append(_check("port", CHECK_SKIP, "按要求跳过端口检查（升级前服务可能仍占用端口）"))
    elif not paths["server_enabled"]:
        checks.append(_check("port", CHECK_SKIP, "[server] enabled = false：不做端口检查"))
    else:
        host, port = str(paths["server_host"]), int(paths["server_port"])
        bind_host = "" if host in {"0.0.0.0", "::", ""} else host
        sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((bind_host, port))
            checks.append(_check("port", CHECK_OK, f"{host}:{port} 可绑定", host=host, port=port))
        except OSError as exc:
            checks.append(_check(
                "port", CHECK_FAIL,
                f"{host}:{port} 无法绑定（{type(exc).__name__}）：可能已有实例在跑",
                host=host, port=port, error=str(exc),
            ))
        finally:
            sock.close()

    # ------------------------------------------------------------------- lock
    inspection = runtime_mod.inspect_lock(
        paths["lock"],
        stale_after_seconds=int(paths["stale_after_seconds"]),
        interval_seconds=int(paths["interval_seconds"]),
        now=now,
    )
    state = inspection["state"]
    if state == "free":
        checks.append(_check("lock", CHECK_OK, "没有实例持锁", **inspection))
    elif state == "stale":
        checks.append(_check(
            "lock", CHECK_WARN,
            f"检测到过期锁（{inspection.get('reason')}）：启动时可安全接管", **inspection
        ))
    elif state == "held":
        checks.append(_check(
            "lock", CHECK_FAIL,
            f"已有实例持锁（{inspection.get('reason')}）：不要启动第二个写入者",
            **inspection,
        ))
    else:
        checks.append(_check(
            "lock", CHECK_FAIL, f"锁文件状态无法判断（{inspection.get('reason')}）", **inspection
        ))

    # ---------------------------------------------------------- 备份目录（warn）
    backup_dir = backup_mod.list_backups(paths["db"].parent / "backups")
    checks.append(_check(
        "backups", CHECK_OK if backup_dir else CHECK_WARN,
        f"已有 {len(backup_dir)} 份 SQLite 备份" if backup_dir else "尚无 SQLite 备份（install/upgrade 会自动备份）",
        count=len(backup_dir),
    ))

    return _finish(cfg_path, checks)


def _finish(cfg_path, checks: list[Check]) -> dict:
    counts = {CHECK_OK: 0, CHECK_WARN: 0, CHECK_FAIL: 0, CHECK_SKIP: 0}
    for item in checks:
        counts[item.status] = counts.get(item.status, 0) + 1
    return {
        "ok": counts[CHECK_FAIL] == 0,
        "config_path": str(cfg_path) if cfg_path else None,
        "checks": [c.to_dict() for c in checks],
        "summary": counts,
        "note": "doctor 只诊断：不抓取、不发布、不请求媒体流、不修改数据库业务状态。",
    }
