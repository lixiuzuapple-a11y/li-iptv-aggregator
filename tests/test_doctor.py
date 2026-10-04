"""TASK-006 ``liptv doctor`` 的离线回归测试。

doctor 的定位是「生产 preflight 体检」：**只诊断**——不抓取、不发布、不请求媒体流、
不修改数据库业务状态。本文件逐条固定它的判定分支，并证明它真的没副作用。
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liptv import db as db_mod  # noqa: E402
from liptv import deploy as deploy_mod  # noqa: E402
from liptv import doctor as doctor_mod  # noqa: E402
from liptv import runtime as runtime_mod  # noqa: E402

FAKE_FFPROBE = REPO_ROOT / "tools" / "fake_ffprobe.py"


# ==================================================================== 工具

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_prefix(tmp_path: pathlib.Path, *, port: int | None = None,
                 server_enabled: bool = True, probe_enabled: bool = False,
                 ffprobe_path: str = '"/usr/bin/ffprobe"',
                 db_path: pathlib.Path | None = None,
                 output_path: pathlib.Path | None = None,
                 dynamic_tmp: pathlib.Path | None = None,
                 create_dirs: bool = True) -> dict:
    """搭一个最小的「类生产」前缀，并写一份 doctor 用的 config.toml。"""
    layout = deploy_mod.build_layout(tmp_path / "root", user="liptv", group="liptv")
    if create_dirs:
        for directory in (layout.lib_dir, layout.cache_dir, layout.run_dir,
                          layout.dynamic_tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)

    db = db_path or layout.db_path
    out = output_path or layout.output_path
    tmp = dynamic_tmp or layout.dynamic_tmp_dir

    layout.config_path.parent.mkdir(parents=True, exist_ok=True)
    layout.config_path.write_text(
        "[database]\n"
        f'path = "{pathlib.Path(db).as_posix()}"\n'
        "\n[output]\n"
        f'm3u_path = "{pathlib.Path(out).as_posix()}"\n'
        "keep_previous = true\n"
        "\n[fetch]\n"
        "timeout_seconds = 5.0\n"
        f'dynamic_tmp_dir = "{pathlib.Path(tmp).as_posix()}"\n'
        "\n[publish]\n"
        f'summary_path = "{layout.summary_path.as_posix()}"\n'
        "\n[runtime]\n"
        "interval_seconds = 10800\n"
        "run_on_start = true\n"
        f'lock_path = "{layout.lock_path.as_posix()}"\n'
        f'status_path = "{layout.status_path.as_posix()}"\n'
        "stale_after_seconds = 21600\n"
        "include_dynamic = false\n"
        "\n[server]\n"
        f"enabled = {'true' if server_enabled else 'false'}\n"
        'host = "127.0.0.1"\n'
        f"port = {port if port is not None else free_port()}\n"
        "\n[probe]\n"
        f"enabled = {'true' if probe_enabled else 'false'}\n"
        f"ffprobe_path = {ffprobe_path}\n",
        encoding="utf-8", newline="\n",
    )
    return {"layout": layout, "config": layout.config_path, "db": pathlib.Path(db),
            "output": pathlib.Path(out)}


def make_db(path: pathlib.Path, *, version: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(path)
    try:
        db_mod.init_db(conn)
        if version is not None and version != db_mod.SCHEMA_VERSION:
            conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                         (int(version), "2026-01-01T00:00:00+00:00"))
            conn.commit()
    finally:
        conn.close()


def check_of(payload: dict, check_id: str) -> dict:
    matches = [c for c in payload["checks"] if c["id"] == check_id]
    assert matches, f"没有 {check_id} 检查项：{[c['id'] for c in payload['checks']]}"
    return matches[0]


# ================================================================== 基本分支

def test_missing_config_is_a_failure(tmp_path: pathlib.Path) -> None:
    payload = doctor_mod.collect(tmp_path / "nope.toml")
    assert payload["ok"] is False
    assert check_of(payload, "config")["status"] == doctor_mod.CHECK_FAIL


def test_unparsable_config_is_a_failure(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "broken.toml"
    bad.write_text("[database\npath = 1\n", encoding="utf-8", newline="\n")
    payload = doctor_mod.collect(bad)
    assert payload["ok"] is False
    assert check_of(payload, "config")["status"] == doctor_mod.CHECK_FAIL


def test_never_uses_builtin_defaults_for_production(tmp_path: pathlib.Path) -> None:
    """生产 doctor 必须要求显式配置文件，不用内置默认值蒙混过关。"""
    payload = doctor_mod.collect("")
    assert payload["ok"] is False
    assert check_of(payload, "config")["status"] == doctor_mod.CHECK_FAIL


def test_healthy_prefix_passes(tmp_path: pathlib.Path) -> None:
    env = build_prefix(tmp_path)
    make_db(env["db"])
    env["output"].write_text("#EXTM3U\n", encoding="utf-8", newline="\n")

    payload = doctor_mod.collect(env["config"])

    assert payload["ok"] is True, payload["checks"]
    assert payload["summary"][doctor_mod.CHECK_FAIL] == 0
    assert check_of(payload, "config")["status"] == doctor_mod.CHECK_OK
    assert check_of(payload, "guard")["status"] == doctor_mod.CHECK_OK
    assert check_of(payload, "dirs")["status"] == doctor_mod.CHECK_OK
    assert check_of(payload, "db")["status"] == doctor_mod.CHECK_OK
    assert check_of(payload, "schema")["status"] == doctor_mod.CHECK_OK
    assert check_of(payload, "port")["status"] == doctor_mod.CHECK_OK
    assert check_of(payload, "lock")["status"] == doctor_mod.CHECK_OK
    assert "只诊断" in payload["note"]


# ================================================================== db/schema

def test_missing_database_only_warns(tmp_path: pathlib.Path) -> None:
    """首次安装时没有库是正常的：warn，不是 fail。"""
    env = build_prefix(tmp_path)
    payload = doctor_mod.collect(env["config"])
    assert check_of(payload, "db")["status"] == doctor_mod.CHECK_WARN
    assert check_of(payload, "schema")["status"] == doctor_mod.CHECK_SKIP
    assert payload["ok"] is True


def test_schema_mismatch_is_a_failure(tmp_path: pathlib.Path) -> None:
    env = build_prefix(tmp_path)
    make_db(env["db"], version=99)

    payload = doctor_mod.collect(env["config"])

    schema = check_of(payload, "schema")
    assert schema["status"] == doctor_mod.CHECK_FAIL
    assert "99" in schema["message"]
    assert payload["ok"] is False


def test_corrupt_database_is_a_failure(tmp_path: pathlib.Path) -> None:
    env = build_prefix(tmp_path)
    env["db"].parent.mkdir(parents=True, exist_ok=True)
    env["db"].write_bytes(b"not a sqlite database at all")

    payload = doctor_mod.collect(env["config"])

    assert check_of(payload, "db")["status"] == doctor_mod.CHECK_FAIL
    assert payload["ok"] is False


# =================================================================== 目录

def test_missing_runtime_directory_is_a_failure(tmp_path: pathlib.Path) -> None:
    env = build_prefix(tmp_path, create_dirs=False)
    payload = doctor_mod.collect(env["config"])

    dirs = check_of(payload, "dirs")
    assert dirs["status"] == doctor_mod.CHECK_FAIL
    assert payload["ok"] is False
    assert dirs["detail"]["problems"]


def test_dirs_check_writes_and_removes_a_probe_only(tmp_path: pathlib.Path) -> None:
    env = build_prefix(tmp_path, create_dirs=False)
    doctor_mod.collect(env["config"], write_probe=False)
    # 只记录意图，不落任何文件
    assert list(env["layout"].lib_dir.glob(".liptv-doctor-probe")) == []


# ============================================================= 端口 / ffprobe

def test_occupied_port_is_a_failure(tmp_path: pathlib.Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        busy = int(listener.getsockname()[1])
        env = build_prefix(tmp_path, port=busy)

        payload = doctor_mod.collect(env["config"])

    assert check_of(payload, "port")["status"] == doctor_mod.CHECK_FAIL
    assert payload["ok"] is False


def test_port_check_can_be_skipped(tmp_path: pathlib.Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        env = build_prefix(tmp_path, port=int(listener.getsockname()[1]))

        payload = doctor_mod.collect(env["config"], check_port=False)

    assert check_of(payload, "port")["status"] == doctor_mod.CHECK_SKIP
    assert payload["ok"] is True


def test_port_skipped_when_server_disabled(tmp_path: pathlib.Path) -> None:
    env = build_prefix(tmp_path, server_enabled=False)
    payload = doctor_mod.collect(env["config"])
    assert check_of(payload, "port")["status"] == doctor_mod.CHECK_SKIP


def test_ffprobe_skipped_when_probe_disabled(tmp_path: pathlib.Path) -> None:
    env = build_prefix(tmp_path, probe_enabled=False,
                       ffprobe_path='"definitely-not-ffprobe-xyz"')
    payload = doctor_mod.collect(env["config"])
    assert check_of(payload, "ffprobe")["status"] == doctor_mod.CHECK_SKIP
    assert payload["ok"] is True, "测活关闭时不该因为缺 ffprobe 而 fail"


def test_ffprobe_required_when_probe_enabled(tmp_path: pathlib.Path) -> None:
    env = build_prefix(tmp_path, probe_enabled=True,
                       ffprobe_path='"definitely-not-ffprobe-xyz"')
    payload = doctor_mod.collect(env["config"])
    ffprobe = check_of(payload, "ffprobe")
    assert ffprobe["status"] == doctor_mod.CHECK_FAIL
    # 回归：这条分支曾因 ``**capability.to_dict()`` 与 _check(message=...) 撞名而 TypeError
    assert "probe_message" in ffprobe["detail"]
    assert payload["ok"] is False


def test_ffprobe_passes_with_offline_double(tmp_path: pathlib.Path) -> None:
    """离线替身：ffprobe_path 写成 argv 数组（仓库既有接缝），不走 PATH。"""
    env = build_prefix(
        tmp_path, probe_enabled=True,
        ffprobe_path=json.dumps([sys.executable, str(FAKE_FFPROBE)]),
    )
    payload = doctor_mod.collect(env["config"])
    ffprobe = check_of(payload, "ffprobe")
    assert ffprobe["status"] == doctor_mod.CHECK_OK, ffprobe
    assert "ffprobe" in str(ffprobe["detail"].get("version", "")).lower()
    # 回归：capability 自己的 ``message`` 字段不能与 _check(message=...) 撞名
    assert "probe_message" in ffprobe["detail"]


# ===================================================================== 锁

def test_no_lock_is_ok(tmp_path: pathlib.Path) -> None:
    env = build_prefix(tmp_path)
    payload = doctor_mod.collect(env["config"])
    assert check_of(payload, "lock")["status"] == doctor_mod.CHECK_OK


def test_live_lock_is_a_failure(tmp_path: pathlib.Path) -> None:
    """同一套 data/output 目录不允许两个写入者：活实例持锁 ⇒ fail。"""
    env = build_prefix(tmp_path)
    lock = runtime_mod.SingleInstanceLock(
        env["layout"].lock_path, stale_after_seconds=21600, interval_seconds=10800,
    )
    lock.acquire()
    try:
        payload = doctor_mod.collect(env["config"])
    finally:
        lock.release()

    item = check_of(payload, "lock")
    assert item["status"] == doctor_mod.CHECK_FAIL
    assert item["detail"]["holder"]["pid"] == os.getpid()
    assert payload["ok"] is False


def test_stale_lock_only_warns(tmp_path: pathlib.Path) -> None:
    """本机死 PID 的锁可以安全接管：warn，不是 fail。"""
    env = build_prefix(tmp_path)
    layout = env["layout"]
    layout.run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": 999_999_999,
        "hostname": socket.gethostname(),
        "created_at": "2026-01-01T00:00:00+00:00",
        "heartbeat_at": "2026-01-01T00:00:00+00:00",
    }
    layout.lock_path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")

    result = doctor_mod.collect(env["config"])

    item = check_of(result, "lock")
    assert item["status"] == doctor_mod.CHECK_WARN
    assert result["ok"] is True


def test_unreadable_lock_metadata_is_a_failure_when_fresh(tmp_path: pathlib.Path) -> None:
    env = build_prefix(tmp_path)
    env["layout"].run_dir.mkdir(parents=True, exist_ok=True)
    env["layout"].lock_path.write_text("{ not json", encoding="utf-8", newline="\n")

    result = doctor_mod.collect(env["config"])

    assert check_of(result, "lock")["status"] == doctor_mod.CHECK_FAIL
    assert result["ok"] is False


# ================================================================== guard

def test_guard_conflict_is_a_failure(tmp_path: pathlib.Path) -> None:
    """运行期路径落到「Git 工作树内且被跟踪」的位置 ⇒ fail（护栏不能被绕过）。"""
    env = build_prefix(tmp_path, output_path=REPO_ROOT / "README.md")
    payload = doctor_mod.collect(env["config"])
    item = check_of(payload, "guard")
    assert item["status"] == doctor_mod.CHECK_FAIL
    assert payload["ok"] is False
    # 护栏刚拒绝过这个位置，doctor 自己就更不该往仓库里写探针文件
    # （既自洽，也避免「误配到仓库内目录时 doctor 在仓库里建/删临时文件」）。
    dirs = check_of(payload, "dirs")
    assert "未写探针" in dirs["detail"]["dirs"]["output"]["note"]
    assert not (REPO_ROOT / ".liptv-doctor-probe").exists()
    assert not (REPO_ROOT / "README.md").is_dir()


# ================================================================ 只读性

def test_doctor_is_read_only(tmp_path: pathlib.Path) -> None:
    env = build_prefix(tmp_path)
    make_db(env["db"])
    env["output"].write_text("#EXTM3U\n", encoding="utf-8", newline="\n")

    db_before = env["db"].read_bytes()
    out_before = env["output"].read_bytes()
    db_stat_before = env["db"].stat().st_mtime_ns

    payload = doctor_mod.collect(env["config"])
    assert payload["ok"] is True

    assert env["db"].read_bytes() == db_before, "doctor 不能改数据库内容"
    assert env["output"].read_bytes() == out_before, "doctor 不能碰 live.m3u"
    assert env["db"].stat().st_mtime_ns == db_stat_before
    assert list(env["layout"].lib_dir.glob(".liptv-doctor-probe")) == []
    assert list(env["layout"].cache_dir.glob(".liptv-doctor-probe")) == []
    assert list(env["layout"].run_dir.glob(".liptv-doctor-probe")) == []


def test_doctor_never_calls_ffprobe_when_disabled(tmp_path: pathlib.Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """测活关闭时连能力检查都不该跑（否则会在无 ffprobe 的机器上误报）。"""
    from liptv import probe as probe_mod

    called: list[int] = []
    monkeypatch.setattr(probe_mod, "check_ffprobe",
                        lambda *a, **k: called.append(1))
    env = build_prefix(tmp_path, probe_enabled=False)
    doctor_mod.collect(env["config"])
    assert called == []


def test_runtime_paths_are_extracted_from_config(tmp_path: pathlib.Path) -> None:
    env = build_prefix(tmp_path)
    from liptv import config as config_mod

    cfg = config_mod.load_config(env["config"])
    paths = doctor_mod.runtime_paths(cfg)
    assert paths["db"] == env["db"]
    assert paths["output"] == env["output"]
    assert paths["lock"] == env["layout"].lock_path
    assert isinstance(paths["server_port"], int)
    assert paths["server_enabled"] is True


def test_doctor_reports_backups_count(tmp_path: pathlib.Path) -> None:
    env = build_prefix(tmp_path)
    payload = doctor_mod.collect(env["config"])
    assert check_of(payload, "backups")["status"] == doctor_mod.CHECK_WARN
    (env["layout"].backups_dir).mkdir(parents=True, exist_ok=True)
    (env["layout"].backups_dir / "liptv-20260101T000000Z.sqlite3").write_bytes(b"x")
    payload = doctor_mod.collect(env["config"])
    assert check_of(payload, "backups")["status"] == doctor_mod.CHECK_OK


def test_doctor_does_not_migrate_schema(tmp_path: pathlib.Path) -> None:
    """即使库是 V99，doctor 也只报错、绝不迁移（schema 仍冻结 V1）。"""
    env = build_prefix(tmp_path)
    make_db(env["db"], version=99)
    before = env["db"].read_bytes()

    doctor_mod.collect(env["config"])

    assert env["db"].read_bytes() == before
    conn = sqlite3.connect(str(env["db"]))
    try:
        conn.row_factory = sqlite3.Row
        assert db_mod.read_schema_version(conn) == 99
    finally:
        conn.close()


# =============================================== QA-007B：RuntimeDirectory 条件语义
#
# systemd unit 用 ``RuntimeDirectory=li-iptv-aggregator`` 托管锁目录，**服务停止时
# 该目录会被 systemd 删除**。修复前形成「不可能同时满足」的两态：
#
#   服务运行中 ⇒ doctor ``lock`` 判 fail（活实例持锁）
#   服务停止后 ⇒ doctor ``dirs`` 判 fail（/run 目录被 systemd 回收）
#
# ⇒ 生产 ``/`` 上 ``upgrade`` / ``rollback`` 永远走不到切换 release 那一步。
#
# 修复：doctor 接收 ``service_active`` 与 ``allow_live_lock`` 两个显式参数。
# 下列用例固定这条语义，并证明**普通体检的严格行为未被放宽**。


def test_live_lock_still_fails_without_allow_live_lock(tmp_path: pathlib.Path) -> None:
    """默认（普通体检）语义不变：活实例持锁仍然 fail。"""
    env = build_prefix(tmp_path)
    lock = runtime_mod.SingleInstanceLock(
        env["layout"].lock_path, stale_after_seconds=21600, interval_seconds=10800,
    )
    lock.acquire()
    try:
        payload = doctor_mod.collect(env["config"], allow_live_lock=False)
    finally:
        lock.release()

    assert check_of(payload, "lock")["status"] == doctor_mod.CHECK_FAIL
    assert payload["ok"] is False


def test_allow_live_lock_passes_for_readonly_preflight(tmp_path: pathlib.Path) -> None:
    """QA-007B 语义 1：service-aware 只读 preflight 下，活实例持锁放行。"""
    env = build_prefix(tmp_path)
    lock = runtime_mod.SingleInstanceLock(
        env["layout"].lock_path, stale_after_seconds=21600, interval_seconds=10800,
    )
    lock.acquire()
    try:
        payload = doctor_mod.collect(env["config"], allow_live_lock=True)
    finally:
        lock.release()

    item = check_of(payload, "lock")
    assert item["status"] == doctor_mod.CHECK_OK
    assert item["detail"]["preflight_only"] is True


def test_missing_run_dir_is_ok_when_service_inactive(tmp_path: pathlib.Path) -> None:
    """QA-007B 语义 2：服务已停 ⇒ RuntimeDirectory 缺失属正常，dirs 不再 fail。"""
    # 真实场景：只有 RuntimeDirectory 被 systemd 回收，lib/cache/db 目录都还在。
    env = build_prefix(tmp_path)
    run_dir = env["layout"].run_dir
    for child in run_dir.iterdir():
        if child.is_file():
            child.unlink()
    run_dir.rmdir()
    assert not run_dir.exists(), "夹具前提：run 目录此时应不存在（模拟 systemd 已回收）"

    payload = doctor_mod.collect(env["config"], service_active=False)

    dirs = check_of(payload, "dirs")
    assert dirs["status"] == doctor_mod.CHECK_OK
    assert dirs["detail"]["dirs"]["lock"]["writable"] is True
    assert dirs["detail"]["dirs"]["lock"]["service_managed"] is True
    assert payload["ok"] is True, payload["checks"]


def test_missing_run_dir_still_fails_when_service_active(tmp_path: pathlib.Path) -> None:
    """QA-007B 语义 3：服务 active 时锁目录**应当存在**，缺失仍判 fail。"""
    env = build_prefix(tmp_path)
    run_dir = env["layout"].run_dir
    for child in run_dir.iterdir():
        if child.is_file():
            child.unlink()
    run_dir.rmdir()
    assert not env["layout"].run_dir.exists()

    payload = doctor_mod.collect(env["config"], service_active=True)

    assert check_of(payload, "dirs")["status"] == doctor_mod.CHECK_FAIL
    assert payload["ok"] is False


def test_missing_run_dir_still_fails_when_state_unknown(tmp_path: pathlib.Path) -> None:
    """QA-007B 语义 4：状态未知（``None``）时保持严格判定，不放宽。"""
    env = build_prefix(tmp_path)
    run_dir = env["layout"].run_dir
    for child in run_dir.iterdir():
        if child.is_file():
            child.unlink()
    run_dir.rmdir()
    assert not env["layout"].run_dir.exists()

    payload = doctor_mod.collect(env["config"], service_active=None)

    assert check_of(payload, "dirs")["status"] == doctor_mod.CHECK_FAIL


def test_other_dirs_stay_strict_when_service_inactive(tmp_path: pathlib.Path) -> None:
    """QA-007B 语义 5：只有锁目录走条件语义，``lib`` 缺失照样 fail。"""
    # 真实场景：run 目录被 systemd 回收（合法），但 lib 目录也缺失（不合法）。
    env = build_prefix(tmp_path)
    run_dir = env["layout"].run_dir
    for child in run_dir.iterdir():
        if child.is_file():
            child.unlink()
    run_dir.rmdir()
    lib_dir = env["layout"].lib_dir
    for child in lib_dir.iterdir():
        if child.is_file():
            child.unlink()
    lib_dir.rmdir()

    payload = doctor_mod.collect(env["config"], service_active=False)

    dirs = check_of(payload, "dirs")
    assert dirs["status"] == doctor_mod.CHECK_FAIL
    problems = dirs["detail"]["problems"]
    assert any("db" in p for p in problems)
    # 锁目录本身不应出现在问题清单里（它缺失是合法的）
    assert not any(p.startswith("lock(") for p in problems)
