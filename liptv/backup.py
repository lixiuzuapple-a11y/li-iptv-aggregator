"""SQLite 一致性备份 / 校验 / 显式恢复（TASK-006 §6）。

口径（冻结）：

* 备份走 **SQLite 在线备份 API**（``sqlite3.Connection.backup``）：即使调度器正在写库，
  也能拿到一个事务一致的快照，**不**用「直接复制 .sqlite3 文件」那种可能撕裂的做法。
* 备份文件名带 UTC 时间戳，落在 ``<lib>/backups/``，默认保留最近 **5** 份。
* 每份新备份立刻**重开验证**：``PRAGMA integrity_check`` + 业务表数量 + schema 版本。
  验证不通过 ⇒ 删除该份备份并报错（绝不留一份读不开的「备份」）。
* 恢复**必须显式触发**（``restore_db``），并且覆盖前先给现有库留一份 ``pre-restore`` 快照。
  升级 / 回滚流程**永不**自动恢复数据库。
* 备份目录不在 Git 工作树内（生产为 ``/var/lib/...``）；即便在，本模块也不写任何
  ``.gitignore`` 之外的路径 —— 由部署器统一走运行期产物护栏。
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib
import sqlite3
import time

from . import db as db_mod

#: 默认保留份数（TASK-006 §6：配置化或简单固定，如 5）
DEFAULT_RETENTION = 5

#: 备份文件名前缀
BACKUP_PREFIX = "liptv"

#: 备份时最多等待多少秒拿写锁（源库可能正被调度器写）
BUSY_TIMEOUT_SECONDS = 10.0


class BackupError(RuntimeError):
    """备份 / 恢复无法完成。"""


@dataclasses.dataclass(frozen=True)
class BackupVerification:
    """一份备份的可重开性验证结果。"""

    ok: bool
    integrity: str | None = None
    table_count: int | None = None
    schema_version: int | None = None
    bytes: int | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _timestamp(now: float | None = None) -> str:
    import datetime

    moment = datetime.datetime.fromtimestamp(now if now is not None else time.time(),
                                            tz=datetime.timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup(path) -> BackupVerification:
    """尝试把一份备份当只读库打开并做完整性检查（**不修改**该文件）。"""
    target = pathlib.Path(path)
    if not target.is_file():
        return BackupVerification(ok=False, detail="备份文件不存在")
    size = target.stat().st_size
    if size == 0:
        return BackupVerification(ok=False, bytes=0, detail="备份文件为空")
    try:
        conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row  # db.table_names / read_schema_version 按列名取值
    except sqlite3.Error as exc:  # pragma: no cover - 权限/路径异常
        return BackupVerification(ok=False, bytes=size, detail=f"无法打开：{exc}")
    try:
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity = str(row[0]) if row else None
        except sqlite3.DatabaseError as exc:
            return BackupVerification(ok=False, bytes=size, detail=f"完整性检查失败：{exc}")
        try:
            tables = db_mod.table_names(conn)
        except sqlite3.DatabaseError as exc:
            return BackupVerification(ok=False, bytes=size, detail=f"读表失败：{exc}")
        version = db_mod.read_schema_version(conn)
    finally:
        conn.close()

    ok = integrity == "ok" and len(tables) > 0
    detail = "" if ok else f"integrity={integrity!r} tables={len(tables)}"
    return BackupVerification(
        ok=ok, integrity=integrity, table_count=len(tables),
        schema_version=version, bytes=size, detail=detail,
    )


def list_backups(backup_dir) -> list[pathlib.Path]:
    """按修改时间从新到旧列出备份文件。"""
    directory = pathlib.Path(backup_dir)
    if not directory.is_dir():
        return []
    files = [p for p in directory.glob(f"{BACKUP_PREFIX}-*.sqlite3") if p.is_file()]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def prune_backups(backup_dir, *, keep: int = DEFAULT_RETENTION) -> list[pathlib.Path]:
    """只保留最近 ``keep`` 份备份，返回被删除的路径列表。"""
    keep = max(0, int(keep))
    removed: list[pathlib.Path] = []
    for stale in list_backups(backup_dir)[keep:]:
        try:
            stale.unlink()
        except OSError:  # pragma: no cover - 竞态/权限
            continue
        removed.append(stale)
    return removed


def backup_sqlite(
    source,
    backup_dir,
    *,
    retention: int = DEFAULT_RETENTION,
    now: float | None = None,
    label: str | None = None,
) -> dict:
    """对 ``source`` 做一次一致性备份到 ``backup_dir``，验证后按保留策略清理。

    返回结构化结果（**不含任何业务内容**）：路径、字节数、sha256、验证详情、
    被清理的旧备份。源库不存在时返回 ``{"skipped": True, ...}``（升级流程据此跳过，
    绝不为「没有库」凭空造一个空库）。
    """
    origin = pathlib.Path(source)
    directory = pathlib.Path(backup_dir)
    if not origin.is_file():
        return {"skipped": True, "reason": "数据库不存在，无需备份", "source": str(origin)}

    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{BACKUP_PREFIX}-{_timestamp(now)}"
    if label:
        safe = "".join(ch for ch in str(label) if ch.isalnum() or ch in "-_.")[:40]
        if safe:
            stem = f"{stem}-{safe}"
    target = directory / f"{stem}.sqlite3"
    suffix = 0
    while target.exists():  # 同一秒内两次备份也不互相覆盖
        suffix += 1
        target = directory / f"{stem}-{suffix}.sqlite3"

    src_conn = sqlite3.connect(str(origin), timeout=BUSY_TIMEOUT_SECONDS)
    dst_conn = sqlite3.connect(str(target), timeout=BUSY_TIMEOUT_SECONDS)
    try:
        src_conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
        src_conn.backup(dst_conn)  # 在线备份 API：事务一致，允许并发写
        dst_conn.commit()
    except sqlite3.Error as exc:
        dst_conn.close()
        src_conn.close()
        if target.exists():
            try:
                target.unlink()
            except OSError:  # pragma: no cover
                pass
        raise BackupError(f"SQLite 备份失败：{exc}") from exc
    finally:
        try:
            dst_conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass
        try:
            src_conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    verification = verify_backup(target)
    if not verification.ok:
        try:
            target.unlink()
        except OSError:  # pragma: no cover
            pass
        raise BackupError(f"备份验证失败，已删除该份备份：{verification.detail}")

    pruned = [str(p) for p in prune_backups(directory, keep=retention)]
    return {
        "skipped": False,
        "path": str(target),
        "bytes": verification.bytes,
        "sha256": _sha256(target),
        "retention": int(retention),
        "pruned": pruned,
        "verification": verification.to_dict(),
        "kept": len(list_backups(directory)),
    }


def restore_sqlite(
    backup_path,
    db_path,
    *,
    allow_overwrite: bool = False,
    now: float | None = None,
) -> dict:
    """显式把一份备份恢复到 ``db_path``（**唯一**会写数据库内容的入口）。

    * 必须先 ``allow_overwrite=True``（CLI 上是显式 ``--yes``）—— 回滚代码**不会**碰数据库；
    * 覆盖前把现有库另存为 ``<db>.pre-restore-<ts>``，且这份安全副本**必须**用
      SQLite 在线备份 API 生成（QA-006B）：裸 ``read_bytes()`` 在服务仍写库时会得到
      撕裂副本，事后无法作为回滚依据；
    * 恢复后再验证一次，验证不过即报错（不静默留下半截数据）。

    ⚠️ 「服务是否已停」**不由本函数负责**：调用方（``Deployer.restore_db``）必须先过
    停机门禁；本函数只保证「给定一份好备份时，替换过程本身是原子的、可复验的」。
    """
    source = pathlib.Path(backup_path)
    target = pathlib.Path(db_path)
    if not source.is_file():
        raise BackupError(f"备份文件不存在：{source}")
    if not allow_overwrite:
        raise BackupError("恢复数据库是破坏性动作，必须显式确认（--yes）")

    verification = verify_backup(source)
    if not verification.ok:
        raise BackupError(f"备份文件未通过验证，拒绝恢复：{verification.detail}")

    safety_copy = None
    safety_copy_method = None
    if target.is_file():
        safety_copy = target.with_name(f"{target.name}.pre-restore-{_timestamp(now)}")
        # 一致性安全副本：走 SQLite 在线备份 API，而不是裸文件复制。
        src_conn = sqlite3.connect(str(target), timeout=BUSY_TIMEOUT_SECONDS)
        dst_conn = sqlite3.connect(str(safety_copy), timeout=BUSY_TIMEOUT_SECONDS)
        try:
            src_conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
            src_conn.backup(dst_conn)
            dst_conn.commit()
        except sqlite3.Error as exc:
            for conn in (dst_conn, src_conn):
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover
                    pass
            try:
                safety_copy.unlink()
            except OSError:  # pragma: no cover
                pass
            # 拿不到一致快照就**拒绝继续**：目标库此时尚未被触碰，字节不变。
            raise BackupError(f"pre-restore 安全副本生成失败（拒绝继续）：{exc}") from exc
        finally:
            for conn in (dst_conn, src_conn):
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover
                    pass
        safety_verification = verify_backup(safety_copy)
        if not safety_verification.ok:
            try:
                safety_copy.unlink()
            except OSError:  # pragma: no cover
                pass
            raise BackupError(
                f"pre-restore 安全副本未通过验证（拒绝继续）：{safety_verification.detail}"
            )
        safety_copy_method = "sqlite-backup-api"

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.restore-tmp")
    tmp.write_bytes(source.read_bytes())
    tmp.replace(target)

    after = verify_backup(target)
    if not after.ok:
        raise BackupError(f"恢复后校验失败：{after.detail}")
    return {
        "restored": True,
        "from": str(source),
        "to": str(target),
        "safety_copy": str(safety_copy) if safety_copy else None,
        "safety_copy_method": safety_copy_method,
        "bytes": after.bytes,
        "sha256": _sha256(target),
        "verification": after.to_dict(),
    }
