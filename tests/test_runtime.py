"""TASK-004 运行期测试：单实例锁 / 调度循环 / 状态与新鲜度 / run --once 离线 E2E。

全部离线：固定源与动态源都打本机 mock HTTP 服务（``tools/mock_source_server.py``），
**不访问任何公网地址**，也不请求任何真实播放地址。

对应 TASK-004 §8 的验收场景：
  * 单实例：第二实例被明确拒绝、正常退出释放、stale 锁有**永久回归**（死 PID / 过期心跳 /
    远端新鲜心跳 / 本机活 PID 但心跳过期 / 元数据损坏 五种分支）；
  * loop：注入 clock/sleep 连跑 3 轮不真实等待；某轮抛异常后下一轮仍继续；停止请求在休眠中也能生效；
  * run --once 离线 E2E：mock fixed fetch → stream-sync → 既有 mock probe 历史 → publish；
  * 新鲜度：missing → ok → stale → 新成功发布恢复 ok；**失败轮次不得伪造最后成功发布时间**；
  * 信息边界：运行期状态 JSON 里不含任何 URL / 签名。
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
import tomllib
from datetime import timedelta

import pytest

from liptv import config as config_mod
from liptv import db as db_mod
from liptv import publish as publish_mod
from liptv import repo
from liptv import runtime as runtime_mod
from liptv.cli import main as cli_main
from liptv.util import dt_to_iso, iso_to_dt

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

NOW = "2026-10-01T12:00:00+00:00"
LATER = "2026-10-01T18:00:00+00:00"


# ------------------------------------------------------------------ 通用工具

def build(capsys, *argv) -> tuple[int, str]:
    code = cli_main([str(a) for a in argv])
    return code, capsys.readouterr().out


def ok_entry(*, status: str = publish_mod.STATUS_OK, published: bool = True) -> dict:
    """一个形状正确的「成功轮次」结果，供不依赖数据库的 loop 测试使用。"""
    return {
        "started_at": NOW,
        "finished_at": NOW,
        "duration_ms": 3,
        "fetch": {"requested": 1, "ok": 1, "failed": 0, "sources": []},
        "stream_sync": {"streams_created": 1, "streams_updated": 0},
        "publish": {
            "status": status,
            "exit_code": 0 if published else 2,
            "published": published,
            "fixed_count": 3,
            "dynamic_count": 0,
            "channel_count": 3,
        },
        "published": published,
        "publish_status": status,
        "dynamic_fail_closed": False,
        "errors": [],
        "outcome": "ok" if published else "failed",
    }


class SleepRecorder:
    """假的 ``sleep``：只记录调用，不真的等待。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.calls)


class FakeWallClock:
    """假的墙钟：**只有显式推进才前进**，用来观察锁心跳是否真的在刷新。

    配合 ``SingleInstanceLock(..., now=clock.now_iso)`` 使用（``now`` 支持可调用对象），
    这样「调度器休眠了两小时」在测试里是瞬时完成的，而锁文件里的 ``heartbeat_at``
    仍严格跟着假时间走 —— 于是「心跳有没有推进」可以被断言，而不是靠猜。
    """

    def __init__(self, start: str = NOW) -> None:
        self._dt = iso_to_dt(start)

    def advance(self, seconds: float) -> None:
        self._dt = self._dt + timedelta(seconds=float(seconds))

    def now_iso(self) -> str:
        """给 ``SingleInstanceLock`` 当 ``now`` 用（可调用对象形式）。"""
        return dt_to_iso(self._dt)

    def age_of(self, iso_text: str) -> float:
        return (self._dt - iso_to_dt(iso_text)).total_seconds()

    @property
    def elapsed_seconds(self) -> float:
        return (self._dt - iso_to_dt(NOW)).total_seconds()


def read_lock_file(path):
    """读锁文件；返回 ``None`` 表示解析不出来（与 ``_read_metadata`` 同口径）。"""
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_raw(path, text: str) -> None:
    """直接（非原子）写锁文件，用于模拟「外部进程写了坏内容」。"""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def make_settings(tmp_path, **overrides) -> runtime_mod.RuntimeSettings:
    base = {
        "interval_seconds": 1,
        "stale_after_seconds": 3600,
        "lock_path": str(tmp_path / "out" / "liptv.lock"),
        "status_path": str(tmp_path / "out" / "runtime-status.json"),
        "status_history": 3,
    }
    base.update(overrides)
    return runtime_mod.RuntimeSettings.from_mapping(base)


# ============================================================ 配置默认值一致

def test_runtime_settings_defaults_match_config_defaults():
    """[runtime] 的代码内默认值必须与配置模板一致，防止两处漂移。"""
    cfg_defaults = config_mod.DEFAULT_CONFIG["runtime"]
    settings = runtime_mod.RuntimeSettings.from_mapping({})
    assert settings.interval_seconds == cfg_defaults["interval_seconds"]
    assert settings.run_on_start == cfg_defaults["run_on_start"]
    assert settings.lock_path == cfg_defaults["lock_path"]
    assert settings.status_path == cfg_defaults["status_path"]
    assert settings.stale_after_seconds == cfg_defaults["stale_after_seconds"]
    assert settings.status_history == cfg_defaults["status_history"]
    assert settings.include_dynamic == cfg_defaults["include_dynamic"]
    assert list(settings.dynamic_sources) == list(cfg_defaults["dynamic_sources"])
    assert settings.require_dynamic == cfg_defaults["require_dynamic"]
    # 默认必须是「不主动访问公网动态源」
    assert settings.include_dynamic is False


def test_server_settings_defaults_match_config_defaults():
    cfg_defaults = config_mod.DEFAULT_CONFIG["server"]
    settings = runtime_mod.ServerSettings.from_mapping({})
    assert settings.enabled == cfg_defaults["enabled"]
    assert settings.host == cfg_defaults["host"] == "127.0.0.1"
    assert settings.port == cfg_defaults["port"]
    assert settings.playlist_path == cfg_defaults["playlist_path"]
    assert settings.health_path == cfg_defaults["health_path"]
    assert settings.enabled is False, "HTTP 服务默认必须是关闭的"


def test_server_settings_reads_configured_section():
    cfg = {"server": {"enabled": True, "host": "0.0.0.0", "port": 9000,
                      "playlist_path": "live/lang.m3u"}}
    merged = config_mod.server_settings(cfg)
    settings = runtime_mod.ServerSettings.from_mapping(merged)
    assert settings.enabled is True
    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.playlist_path == "/live/lang.m3u"     # 自动补前导斜杠


def test_config_example_runtime_and_server_match_code_defaults():
    """`config/config.example.toml` 的 [runtime] / [server] 必须与代码内默认值一致。

    示例文件是用户照抄的模板：一旦它与代码默认值漂移，用户抄到的就是
    「文档说的」与「程序实际做的」不一致的配置。这里把它锁死。
    """
    example = REPO_ROOT / "config" / "config.example.toml"
    if not example.exists():  # pragma: no cover - 仓库里必然存在
        pytest.skip("config.example.toml 不存在")
    parsed = tomllib.loads(example.read_text(encoding="utf-8"))
    assert parsed["runtime"] == config_mod.DEFAULT_CONFIG["runtime"]
    assert parsed["server"] == config_mod.DEFAULT_CONFIG["server"]
    # 示例里的动态来源必须默认关闭，且不写进 [runtime] dynamic_sources
    assert parsed["runtime"]["include_dynamic"] is False
    assert parsed["runtime"]["dynamic_sources"] == []
    assert parsed["server"]["enabled"] is False
    assert parsed["server"]["host"] == "127.0.0.1"
    for entry in parsed.get("sources") or []:
        assert entry.get("enabled") in (False, None), (
            f"config.example.toml 里的来源 {entry.get('name')!r} 不得默认为启用"
        )


# ================================================================== 单实例锁

def test_lock_second_instance_is_rejected(tmp_path):
    """第一实例持锁时，第二实例必须明确拒绝，且**不执行**任何 fetch/publish。"""
    path = tmp_path / "out" / "liptv.lock"
    first = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    first.acquire()
    assert first.acquired is True

    ran = {"round": 0}

    def round_fn(*, round_id):
        ran["round"] += 1
        return ok_entry()

    scheduler = runtime_mod.Scheduler(
        settings=make_settings(tmp_path), round_fn=round_fn,
        status_store=runtime_mod.StatusStore(tmp_path / "out" / "s.json"),
    )
    with pytest.raises(runtime_mod.LockError) as excinfo:
        runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600).acquire()
    assert excinfo.value.reason == runtime_mod.LOCK_REASON_HELD_LIVE_PID
    assert excinfo.value.holder["pid"] == os.getpid()
    assert ran["round"] == 0
    assert scheduler.rounds_run == 0
    first.release()


def test_lock_released_on_normal_exit(tmp_path):
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    lock.acquire()
    assert path.exists()
    assert lock.release() is True
    assert not path.exists()
    # 释放后新实例可以正常拿到
    second = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    second.acquire()
    assert second.acquired is True
    second.release()


def test_lock_context_manager_releases_even_on_exception(tmp_path):
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    with pytest.raises(ValueError):
        with lock:
            raise ValueError("boom")
    assert not path.exists(), "异常路径也必须释放锁"


def test_lock_release_does_not_delete_someone_elses_lock(tmp_path):
    """锁被别人接管后，本实例的 release 不得删掉别人的锁。"""
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    lock.acquire()
    # 模拟「另一端接管」：token 被换掉
    stolen = json.loads(path.read_text(encoding="utf-8"))
    stolen["token"] = "someone-else"
    path.write_text(json.dumps(stolen), encoding="utf-8")
    assert lock.release() is False
    assert path.exists()


def test_heartbeat_stops_when_token_changes(tmp_path):
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    lock.acquire()
    assert lock.heartbeat() is True
    data = json.loads(path.read_text(encoding="utf-8"))
    data["token"] = "someone-else"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert lock.heartbeat() is False
    assert lock.acquired is False, "发现锁被接管后必须放弃所有权"


# ------------------------------------------------ 保守 release / 心跳（QA-004B）

def test_release_refuses_corrupt_lock_file(tmp_path):
    """QA-004B 永久回归：锁文件被换成损坏 JSON ⇒ **不得删除**。"""
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    lock.acquire()
    write_raw(path, "not-json{{{")           # 另一个持有者/外部进程写坏了它

    assert lock.release() is False
    assert path.exists(), "无法证明锁是自己的时候，宁可留着也不能删"
    assert path.read_text(encoding="utf-8") == "not-json{{{", "也不得覆盖未知锁"


def test_release_refuses_empty_lock_file(tmp_path):
    """QA-004B 永久回归：空锁文件 ⇒ **不得删除**（空文件同样无法证明归属）。"""
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    lock.acquire()
    write_raw(path, "")

    assert lock.release() is False
    assert path.exists()


def test_release_refuses_metadata_without_pid(tmp_path):
    """QA-004B 永久回归：能解析但缺少 pid（_read_metadata 判为不可读）⇒ 不得删除。"""
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    lock.acquire()
    write_raw(path, json.dumps({"token": "someone-else"}))

    assert lock.release() is False
    assert path.exists()


def test_release_refuses_when_lock_file_already_gone(tmp_path):
    """锁文件已经不在了 ⇒ 返回 False 且不抛异常（缺文件不算「释放成功」）。"""
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    lock.acquire()
    path.unlink()

    assert lock.release() is False
    assert not path.exists()


def test_release_deletes_only_its_own_lock(tmp_path):
    """正常路径不能被「保守」误伤：自己的合法 token 必须照常删除。"""
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    lock.acquire()
    token = lock.token
    assert read_lock_file(path)["token"] == token

    assert lock.release() is True
    assert not path.exists()
    # 幂等：再释放一次不报错、也不会删掉别人的东西
    assert lock.release() is False


def test_heartbeat_refuses_and_does_not_overwrite_unreadable_lock(tmp_path):
    """QA-004B 永久回归：心跳同样 fail-closed —— 读不出归属就放弃，**不覆盖未知锁**。"""
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    lock.acquire()
    write_raw(path, "not-json{{{")

    assert lock.heartbeat() is False
    assert lock.acquired is False
    assert lock.ownership_lost is True
    assert path.read_text(encoding="utf-8") == "not-json{{{", "绝不能把未知锁覆盖成自己的"
    # 放弃所有权后也不能顺手删掉（release 的保守语义）
    assert lock.release() is False
    assert path.exists()


def test_heartbeat_refuses_when_lock_file_missing(tmp_path):
    """锁文件被删掉 ⇒ 心跳必须返回 False（已不再是「我在持有」）。"""
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    lock.acquire()
    path.unlink()

    assert lock.heartbeat() is False
    assert lock.acquired is False
    assert not path.exists(), "不得凭想象重建锁文件"


# ------------------------------------------------------ 心跳周期（QA-004A）

def test_heartbeat_interval_is_far_below_stale_threshold():
    """心跳周期必须显著小于 stale 阈值，且不会因为 interval 很小而疯狂刷盘。"""
    # 真实默认：6h stale / 3h interval → 300s（= stale 的 1/72）
    assert runtime_mod.resolve_heartbeat_interval(21600, 10800) == 300
    # 小阈值场景：stale 10s → 2.5s（= stale 的 1/4）
    assert runtime_mod.resolve_heartbeat_interval(10, 60) == pytest.approx(2.5)
    # interval 非常小 → 取 interval/4，且不低于下限
    assert runtime_mod.resolve_heartbeat_interval(3600, 1) == runtime_mod.HEARTBEAT_MIN_INTERVAL_SECONDS
    # 极端小 stale：仍不低于下限（不会退化成 0 造成死循环刷盘）
    assert runtime_mod.resolve_heartbeat_interval(1, 1) == runtime_mod.HEARTBEAT_MIN_INTERVAL_SECONDS
    # 一定不会超过上限
    assert runtime_mod.resolve_heartbeat_interval(10 ** 9, 10 ** 9) == runtime_mod.HEARTBEAT_MAX_INTERVAL_SECONDS
    for stale, interval in ((10, 60), (300, 600), (21600, 10800), (1, 1), (86400, 100)):
        assert runtime_mod.resolve_heartbeat_interval(stale, interval) <= max(1, stale) * 0.25 + 1e-9


def _write_lock(path: pathlib.Path, *, hostname: str, pid: int, heartbeat: str,
                token: str = "stale-token") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "pid": pid, "hostname": hostname, "token": token,
            "created_at": heartbeat, "heartbeat_at": heartbeat,
            "interval_seconds": 1, "version": "0.1.0",
        }),
        encoding="utf-8",
    )


def test_stale_lock_dead_pid_is_taken_over(tmp_path):
    """本机 + 进程已退出 ⇒ 判定 stale 并安全接管（不能只因文件存在就永久锁死）。"""
    path = tmp_path / "out" / "liptv.lock"
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _write_lock(path, hostname=socket.gethostname(), pid=dead.pid, heartbeat=NOW)

    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=1, now=LATER)
    lock.acquire()
    assert lock.acquired is True
    assert lock.taken_over_from["reason"] == runtime_mod.LOCK_REASON_STALE_DEAD_PID
    assert lock.taken_over_from["previous"]["pid"] == dead.pid
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == lock.token
    lock.release()


def test_stale_lock_remote_host_old_heartbeat_is_taken_over(tmp_path):
    """他机持有且心跳过期 ⇒ 按心跳判定 stale 接管（远端存活本来就无法验证）。"""
    path = tmp_path / "out" / "liptv.lock"
    _write_lock(path, hostname="another-host.invalid", pid=4242, heartbeat=NOW)
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=600, now=LATER)
    lock.acquire()
    assert lock.acquired is True
    assert lock.taken_over_from["reason"] == runtime_mod.LOCK_REASON_STALE_HEARTBEAT
    lock.release()


def test_fresh_lock_from_remote_host_is_not_stolen(tmp_path):
    """他机持有且心跳新鲜 ⇒ **不抢**（无法判断远端进程是否还活着时不得随意抢锁）。"""
    path = tmp_path / "out" / "liptv.lock"
    _write_lock(path, hostname="another-host.invalid", pid=4242, heartbeat=LATER)
    with pytest.raises(runtime_mod.LockError) as excinfo:
        runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600, now=LATER).acquire()
    assert excinfo.value.reason == runtime_mod.LOCK_REASON_HELD_REMOTE
    assert path.exists()


def test_live_pid_with_stale_heartbeat_is_not_stolen(tmp_path):
    """本机活进程但心跳过期 ⇒ 拒绝抢锁并给出 PID 与清理办法（而不是盲目接管）。"""
    path = tmp_path / "out" / "liptv.lock"
    _write_lock(path, hostname=socket.gethostname(), pid=os.getpid(), heartbeat=NOW)
    with pytest.raises(runtime_mod.LockError) as excinfo:
        runtime_mod.SingleInstanceLock(path, stale_after_seconds=60, now=LATER).acquire()
    assert excinfo.value.reason == runtime_mod.LOCK_REASON_HELD_SUSPECT
    assert str(os.getpid()) in str(excinfo.value)


def test_unreadable_lock_metadata_fresh_is_refused_and_old_is_taken_over(tmp_path):
    """元数据损坏：新鲜 ⇒ 不敢抢；过期 ⇒ 按 mtime 判 stale 接管。"""
    path = tmp_path / "out" / "liptv.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json{{{", encoding="utf-8")

    with pytest.raises(runtime_mod.LockError) as excinfo:
        runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600).acquire()
    assert excinfo.value.reason == runtime_mod.LOCK_REASON_HELD_UNREADABLE

    old = time.time() - 7200
    os.utime(path, (old, old))
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=60)
    lock.acquire()
    assert lock.acquired is True
    assert lock.taken_over_from["previous"]["metadata"] == "unreadable"
    lock.release()


def test_lock_path_inside_untracked_git_worktree_is_refused(tmp_path):
    """锁文件也是运行期产物：落在未被忽略的 Git 工作树内必须被护栏拒绝。"""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text("out/\n", encoding="utf-8")
    bad = root / "SOURCES" / "liptv.lock"
    bad.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError):
        runtime_mod.SingleInstanceLock(bad, stale_after_seconds=60).acquire()
    assert not bad.exists()


def test_pid_alive_reports_self_and_dead_process():
    assert runtime_mod._pid_alive(os.getpid()) is True
    assert runtime_mod._pid_alive(0) is False
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert runtime_mod._pid_alive(dead.pid) is False


# ================================================================== 调度循环

def test_loop_runs_three_rounds_without_real_waiting(tmp_path):
    """注入 fake sleep：连跑 3 轮，且**不真的等待** interval。"""
    calls = {"n": 0}

    def round_fn(*, round_id):
        calls["n"] += 1
        return ok_entry()

    sleep = SleepRecorder()
    settings = make_settings(tmp_path, interval_seconds=3600)
    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=round_fn, sleep=sleep,
        status_store=runtime_mod.StatusStore(settings.status_path),
    )
    started = time.perf_counter()
    result = scheduler.run(max_rounds=3)
    elapsed = time.perf_counter() - started

    assert result["rounds"] == 3
    assert result["failed_rounds"] == 0
    assert calls["n"] == 3
    assert elapsed < 5, f"循环不应真实等待：实测 {elapsed:.2f}s"
    # 3 轮之间只休眠 2 次；每次被切成 0.25s 的小片便于响应停止
    assert len(sleep.calls) == 2 * int(3600 / runtime_mod.SLEEP_POLL_SECONDS)
    assert all(c == runtime_mod.SLEEP_POLL_SECONDS for c in sleep.calls)


def test_loop_survives_round_exception(tmp_path):
    """某轮抛异常后 loop 不能死：第 3 轮仍要执行，且异常被记录。"""
    seen = []

    def round_fn(*, round_id):
        seen.append(round_id)
        if len(seen) == 2:
            raise RuntimeError("模拟第二轮 fetch 炸掉 https://secret.invalid/x?txSecret=ZZZ")
        return ok_entry()

    settings = make_settings(tmp_path)
    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=round_fn, sleep=SleepRecorder(),
        status_store=runtime_mod.StatusStore(settings.status_path),
    )
    result = scheduler.run(max_rounds=3)

    assert result["rounds"] == 3
    assert result["failed_rounds"] == 1
    assert len(seen) == 3

    doc = json.loads(pathlib.Path(settings.status_path).read_text(encoding="utf-8"))
    rounds = doc["rounds"]
    assert len(rounds) == 3
    failed = rounds[1]
    assert failed["outcome"] == "failed"
    assert failed["error"]["type"] == "RuntimeError"
    # 异常文本里的 URL 也必须被脱敏
    assert "secret.invalid" not in json.dumps(failed, ensure_ascii=False)


def test_stop_request_takes_effect_during_sleep(tmp_path):
    """停止请求在「休眠中」也要迅速生效，不再启动新一轮。"""
    calls = {"n": 0}
    holder: dict = {}

    def round_fn(*, round_id):
        calls["n"] += 1
        return ok_entry()

    def fake_sleep(seconds):
        holder["slept"] = holder.get("slept", 0) + 1
        holder["scheduler"].request_stop("test-stop")

    settings = make_settings(tmp_path, interval_seconds=3600)
    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=round_fn, sleep=fake_sleep,
        status_store=runtime_mod.StatusStore(settings.status_path),
    )
    holder["scheduler"] = scheduler
    result = scheduler.run()

    assert calls["n"] == 1
    assert result["rounds"] == 1
    assert result["stopped"] is True
    assert result["stop_reason"] == "test-stop"


def test_run_on_start_false_sleeps_before_first_round(tmp_path):
    calls = {"n": 0}

    def round_fn(*, round_id):
        calls["n"] += 1
        return ok_entry()

    settings = make_settings(tmp_path, interval_seconds=1, run_on_start=False)
    sleep = SleepRecorder()
    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=round_fn, sleep=sleep,
        status_store=runtime_mod.StatusStore(settings.status_path),
    )
    scheduler.run(max_rounds=1)
    assert calls["n"] == 1
    assert sleep.calls, "run_on_start=false 时应先睡一个周期"


def test_status_history_is_capped(tmp_path):
    settings = make_settings(tmp_path, status_history=2)
    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=lambda *, round_id: ok_entry(),
        sleep=SleepRecorder(),
        status_store=runtime_mod.StatusStore(settings.status_path, max_rounds=2),
    )
    scheduler.run(max_rounds=5)
    doc = json.loads(pathlib.Path(settings.status_path).read_text(encoding="utf-8"))
    assert scheduler.rounds_run == 5
    assert len(doc["rounds"]) == 2, "状态历史必须封顶，不做无限增长"
    assert doc["current_round"]["round_number"] == 5


# ============================================ 锁心跳接入调度生命周期（QA-004A）

def test_long_run_refreshes_heartbeat_and_remote_observer_cannot_take_over(tmp_path, monkeypatch):
    """QA-004A 永久回归：长跑远超 stale 阈值时心跳持续推进，他机观察仍判 HELD_REMOTE。

    旧实现里 ``Scheduler`` 从不调用 ``heartbeat()``，``heartbeat_at`` 会永远停在 acquire
    时刻；于是一把 stale=10s 的锁在「跑了 240s」之后会被另一个主机当成 stale_heartbeat
    接管 —— 共享目录上出现两个写入者，正是 TASK-004 禁止的情形。
    """
    path = tmp_path / "out" / "liptv.lock"
    stale = 10
    interval = 60
    clock = FakeWallClock(NOW)

    lock = runtime_mod.SingleInstanceLock(
        path, stale_after_seconds=stale, interval_seconds=interval, now=clock.now_iso,
    )
    lock.acquire()
    acquired_at = read_lock_file(path)["heartbeat_at"]

    ages: list[float] = []

    def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)
        # 每个休眠切片都量一次「当前心跳有多旧」，谁都别想蒙混过关
        ages.append(clock.age_of(read_lock_file(path)["heartbeat_at"]))

    settings = runtime_mod.RuntimeSettings.from_mapping({
        "interval_seconds": interval,
        "stale_after_seconds": stale,
        "lock_path": str(path),
        "status_path": str(tmp_path / "out" / "runtime-status.json"),
    })
    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=lambda *, round_id: ok_entry(), sleep=fake_sleep,
        status_store=runtime_mod.StatusStore(settings.status_path),
        lock=lock,
        heartbeat_thread=False,   # 用「轮次边界 + 休眠切片」这条确定性路径驱动
    )
    result = scheduler.run(max_rounds=5)

    assert result["rounds"] == 5
    assert result["lock_lost"] is False
    assert result["stop_reason"] is None
    # 本用例必须真的越过 stale 阈值才有判别力
    assert clock.elapsed_seconds == interval * 4
    assert clock.elapsed_seconds > stale * 10, "模拟时长必须远超 stale 阈值"

    assert ages, "fake sleep 一次都没被调用，用例本身失效"
    assert max(ages) < stale, (
        f"心跳年龄不得逼近 stale 阈值：最大 {max(ages):.3f}s（阈值 {stale}s）"
    )
    # 时间戳是**秒级精度**（util.utcnow_iso 统一丢掉微秒），所以这里留 1s 余量
    assert max(ages) <= scheduler.heartbeat_interval + 1.1, (
        f"实际最大心跳年龄 {max(ages):.3f}s 应贴近配置周期 {scheduler.heartbeat_interval}s"
    )

    hb_after = read_lock_file(path)["heartbeat_at"]
    assert hb_after != acquired_at, "长跑之后 heartbeat_at 必须已经推进"
    assert hb_after == clock.now_iso(), "轮次结束应把心跳对齐到当前时刻"

    # 另一个「主机」在同一时刻观察同一把锁：心跳新鲜 ⇒ 必须拒绝接管
    with monkeypatch.context() as patch:
        patch.setattr(runtime_mod.socket, "gethostname", lambda: "another-host.invalid")
        contender = runtime_mod.SingleInstanceLock(
            path, stale_after_seconds=stale, interval_seconds=interval, now=clock.now_iso,
        )
        with pytest.raises(runtime_mod.LockError) as excinfo:
            contender.acquire()
    assert excinfo.value.reason == runtime_mod.LOCK_REASON_HELD_REMOTE
    assert path.exists()
    assert read_lock_file(path)["token"] == lock.token

    # 反证：把心跳冻结在 acquire 时刻（= 旧实现的行为），同一路径立刻能被接管。
    # 这一步证明上面的 HELD_REMOTE 是「心跳真的在跑」挣来的，而不是场景不成立。
    stale_path = tmp_path / "out" / "frozen.lock"
    _write_lock(stale_path, hostname="another-host.invalid", pid=4242, heartbeat=acquired_at)
    rival = runtime_mod.SingleInstanceLock(
        stale_path, stale_after_seconds=stale, interval_seconds=interval, now=clock.now_iso,
    )
    rival.acquire()
    assert rival.taken_over_from["reason"] == runtime_mod.LOCK_REASON_STALE_HEARTBEAT
    rival.release()


def test_heartbeat_continues_during_long_sleep_without_rounds(tmp_path):
    """``run_on_start=false`` 且 interval ≫ stale：首轮之前的长休眠里心跳也必须刷新。"""
    path = tmp_path / "out" / "liptv.lock"
    clock = FakeWallClock(NOW)
    lock = runtime_mod.SingleInstanceLock(
        path, stale_after_seconds=10, interval_seconds=3600, now=clock.now_iso,
    )
    lock.acquire()

    ages: list[float] = []

    def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)
        ages.append(clock.age_of(read_lock_file(path)["heartbeat_at"]))

    settings = runtime_mod.RuntimeSettings.from_mapping({
        "interval_seconds": 3600,
        "stale_after_seconds": 10,
        "run_on_start": False,
        "lock_path": str(path),
        "status_path": str(tmp_path / "out" / "runtime-status.json"),
    })
    calls = {"n": 0}

    def round_fn(*, round_id):
        calls["n"] += 1
        return ok_entry()

    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=round_fn, sleep=fake_sleep,
        status_store=runtime_mod.StatusStore(settings.status_path),
        lock=lock, heartbeat_thread=False,
    )
    scheduler.run(max_rounds=1)

    assert calls["n"] == 1
    assert clock.elapsed_seconds == 3600, "首轮前应先睡满一个周期"
    assert ages and max(ages) < 10, f"长休眠期间心跳年龄最大 {max(ages):.3f}s，超过 stale 阈值"


def test_scheduler_stops_after_lock_is_stolen_and_never_touches_it(tmp_path):
    """QA-004A 永久回归：心跳发现 token 被替换 ⇒ 立刻停止，不再跑后续轮次，也不动别人的锁。"""
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600, interval_seconds=2)
    lock.acquire()

    calls = {"n": 0}

    def round_fn(*, round_id):
        calls["n"] += 1
        return ok_entry()

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:      # 第一轮结束、正在休眠时，锁被另一个实例接管
            data = read_lock_file(path)
            data["token"] = "intruder-token"
            path.write_text(json.dumps(data), encoding="utf-8")

    settings = make_settings(tmp_path, interval_seconds=2, stale_after_seconds=3600)
    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=round_fn, sleep=fake_sleep,
        status_store=runtime_mod.StatusStore(settings.status_path),
        lock=lock, heartbeat_thread=False,
    )
    result = scheduler.run()

    assert calls["n"] == 1, "失去锁之后绝不允许再跑任何一轮"
    assert result["rounds"] == 1
    assert result["lock_lost"] is True
    assert result["stop_reason"] == runtime_mod.LOCK_REASON_LOST
    assert len(sleeps) < int(2 / runtime_mod.SLEEP_POLL_SECONDS), "应在休眠中立刻停，而不是睡满整个周期"
    # 保守语义：绝不覆盖、也绝不删除别人的锁
    assert read_lock_file(path)["token"] == "intruder-token"
    assert lock.release() is False
    assert path.exists()


def test_run_once_after_lock_lost_skips_without_calling_round_fn(tmp_path):
    """QA-004A 永久回归：轮次边界守卫 —— 丢锁后 ``run_once`` 不得调用 ``round_fn``。"""
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    lock.acquire()

    calls = {"n": 0}

    def round_fn(*, round_id):
        calls["n"] += 1
        return ok_entry()

    settings = make_settings(tmp_path)
    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=round_fn,
        status_store=runtime_mod.StatusStore(settings.status_path),
        lock=lock, heartbeat_thread=False,
    )

    first = scheduler.run_once()
    assert calls["n"] == 1 and first["outcome"] == "ok"

    data = read_lock_file(path)
    data["token"] = "intruder-token"
    path.write_text(json.dumps(data), encoding="utf-8")

    second = scheduler.run_once()
    assert calls["n"] == 1, "丢锁后不得再调用 round_fn（即不得 fetch/publish）"
    assert second["skipped"] is True
    assert second["skip_reason"] == runtime_mod.LOCK_REASON_LOST
    assert second["outcome"] == "lock_lost"
    assert second["fetch"]["requested"] == 0
    assert second["publish"] == {}
    assert second["published"] is False
    assert second["exit_code"] == runtime_mod.EXIT_ROUND_FAILED
    assert scheduler.lock_lost is True
    assert scheduler.stop_reason == runtime_mod.LOCK_REASON_LOST

    doc = json.loads(pathlib.Path(settings.status_path).read_text(encoding="utf-8"))
    assert doc["last_run_outcome"] == "lock_lost"
    assert doc["last_success_publish_at"]
    assert doc["rounds"][-1]["skip_reason"] == runtime_mod.LOCK_REASON_LOST


def test_no_lock_means_scheduler_untouched_by_heartbeat(tmp_path):
    """不传 ``lock`` 时行为与首版完全一致：不刷新、不报 lost、不请求停止。"""
    settings = make_settings(tmp_path)
    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=lambda *, round_id: ok_entry(),
        sleep=SleepRecorder(),
        status_store=runtime_mod.StatusStore(settings.status_path),
    )
    result = scheduler.run(max_rounds=2)
    assert result["rounds"] == 2
    assert result["lock_lost"] is False
    assert result["heartbeat_count"] == 0
    assert result["heartbeat_beats"] == 0
    assert scheduler.heartbeat_interval is None
    assert scheduler.heartbeat_now() is True, "没有锁时心跳视为「无需维护」"


def test_heartbeat_thread_refreshes_during_a_long_round(tmp_path):
    """QA-004A：单轮本身很久时由后台线程兜底刷新（不依赖轮次边界）。

    手法：锁用一个**假墙钟**（只有轮次自己推进它），而线程按**真实** 0.05s 周期跑。
    于是「轮次等待期间读到的 heartbeat_at 已经跟着假钟前进了」只可能来自后台线程 ——
    轮次还没结束，轮次边界心跳根本还没轮到。
    """
    path = tmp_path / "out" / "liptv.lock"
    clock = FakeWallClock(NOW)
    lock = runtime_mod.SingleInstanceLock(
        path, stale_after_seconds=3600, interval_seconds=10800, now=clock.now_iso,
    )
    lock.acquire()
    acquired_at = read_lock_file(path)["heartbeat_at"]
    assert acquired_at == NOW

    during: dict = {}

    def slow_round(*, round_id):
        clock.advance(2)          # 模拟一次很慢的抓取：假钟前进 2s
        time.sleep(0.6)           # 真实等待，让后台线程有机会在「轮次进行中」刷新
        during["heartbeat_at"] = read_lock_file(path)["heartbeat_at"]
        return ok_entry()

    settings = make_settings(tmp_path, interval_seconds=10800, stale_after_seconds=3600)
    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=slow_round, sleep=SleepRecorder(),
        status_store=runtime_mod.StatusStore(settings.status_path),
        lock=lock, heartbeat_interval_seconds=0.05,
    )
    assert scheduler.heartbeat_interval == 0.05
    result = scheduler.run(max_rounds=1)

    assert result["heartbeat_beats"] >= 1, f"后台线程一次都没刷到：{result}"
    assert during["heartbeat_at"] != acquired_at, "长轮次期间后台线程必须刷新心跳"
    assert during["heartbeat_at"] == clock.now_iso(), (
        "轮次进行中读到的心跳必须已经跟着假钟前进（只有后台线程能做到这一步）"
    )
    assert result["lock_lost"] is False
    assert lock.acquired is True
    assert not list(path.parent.glob(f"{path.name}.tmp*")), "临时文件必须被清理干净"


def test_heartbeat_thread_and_round_heartbeats_do_not_self_conflict(tmp_path):
    """QA-004A：心跳线程与轮次边界心跳**并发**刷新同一把锁，不得互相误伤。

    这条是从实测里挖出来的：只要读侧用默认 ``open()``（不允许别人替换），
    或两个线程算出同一个临时文件名，主线程就会在一瞬间「读不出来」，
    进而把一把健康的锁误判成「已失去归属」而整条长跑停摆 —— 比原 bug 还隐蔽。
    """
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600, interval_seconds=1)
    lock.acquire()

    def round_fn(*, round_id):
        time.sleep(0.01)          # 让线程与主线程真的有交错机会
        return ok_entry()

    def tiny_sleep(seconds: float) -> None:
        time.sleep(0.005)

    settings = make_settings(tmp_path, interval_seconds=1, stale_after_seconds=3600)
    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=round_fn, sleep=tiny_sleep,
        status_store=runtime_mod.StatusStore(settings.status_path),
        lock=lock, heartbeat_interval_seconds=0.01,
    )
    result = scheduler.run(max_rounds=25)

    assert result["rounds"] == 25
    assert result["lock_lost"] is False, (
        f"并发刷新不得被误判为失去锁：{lock.last_error!r}"
    )
    assert result["heartbeat_beats"] >= 5, f"后台线程应当刷到很多次：{result}"
    assert result["heartbeat_count"] >= 25, "轮次边界心跳也必须照常"
    assert lock.acquired is True
    assert read_lock_file(path)["token"] == lock.token
    assert not list(path.parent.glob(f"{path.name}.tmp*")), "并发下临时文件必须被清理干净"


def test_heartbeat_thread_does_not_start_when_lock_already_lost(tmp_path):
    """已经不再持有锁时不得启动心跳线程（否则等于假装还在持锁）。"""
    path = tmp_path / "out" / "liptv.lock"
    lock = runtime_mod.SingleInstanceLock(path, stale_after_seconds=3600)
    lock.acquire()
    write_raw(path, "not-json{{{")
    assert lock.heartbeat() is False      # 主动放弃所有权

    settings = make_settings(tmp_path)
    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=lambda *, round_id: ok_entry(),
        status_store=runtime_mod.StatusStore(settings.status_path), lock=lock,
    )
    assert scheduler.start_heartbeat() is None
    assert scheduler.lock_lost is True
    assert scheduler.stopped is True
    scheduler.stop_heartbeat()


# ================================================================== 新鲜度

def test_health_missing_when_no_playlist(tmp_path):
    health = runtime_mod.compute_health(
        playlist_path=tmp_path / "live.m3u", now=NOW, stale_after_seconds=3600
    )
    assert health["status"] == runtime_mod.FRESHNESS_MISSING
    assert health["playlist"]["exists"] is False
    assert health["freshness"]["last_success_publish_at"] is None


def test_health_ok_then_stale_then_ok_again(tmp_path):
    playlist = tmp_path / "live.m3u"
    playlist.write_text("#EXTM3U\n", encoding="utf-8")
    status_path = tmp_path / "runtime-status.json"
    store = runtime_mod.StatusStore(status_path)
    store.record_round({
        "round_id": "r1", "started_at": NOW, "finished_at": NOW, "outcome": "ok",
        "published": True, "publish_status": publish_mod.STATUS_OK,
    })

    fresh = runtime_mod.compute_health(
        playlist_path=playlist, status_path=status_path, now=NOW, stale_after_seconds=3600
    )
    assert fresh["status"] == runtime_mod.FRESHNESS_OK
    assert fresh["freshness"]["source"] == "runtime_status"
    assert fresh["freshness"]["seconds_since_last_success"] == 0

    stale = runtime_mod.compute_health(
        playlist_path=playlist, status_path=status_path, now=LATER, stale_after_seconds=3600
    )
    assert stale["status"] == runtime_mod.FRESHNESS_STALE
    assert stale["freshness"]["is_stale"] is True

    # 新的成功发布 ⇒ 立刻恢复 ok
    store.record_round({
        "round_id": "r2", "started_at": LATER, "finished_at": LATER, "outcome": "ok",
        "published": True, "publish_status": publish_mod.STATUS_DEGRADED_FIXED_ONLY,
    })
    recovered = runtime_mod.compute_health(
        playlist_path=playlist, status_path=status_path, now=LATER, stale_after_seconds=3600
    )
    assert recovered["status"] == runtime_mod.FRESHNESS_OK
    assert recovered["freshness"]["last_success_publish_at"] == LATER


def test_failed_round_does_not_fake_last_success(tmp_path):
    """失败轮次不能推进「最后成功发布」时间（否则 /healthz 会撒谎）。"""
    status_path = tmp_path / "runtime-status.json"
    store = runtime_mod.StatusStore(status_path)
    store.record_round({
        "round_id": "ok-1", "started_at": NOW, "finished_at": NOW, "outcome": "ok",
        "published": True, "publish_status": publish_mod.STATUS_OK,
    })
    store.record_round({
        "round_id": "bad-2", "started_at": LATER, "finished_at": LATER, "outcome": "failed",
        "published": False, "publish_status": publish_mod.STATUS_REJECTED_VALIDATION,
    })
    doc = store.read()
    assert doc["last_success_publish_at"] == NOW
    assert doc["last_run_outcome"] == "failed"

    playlist = tmp_path / "live.m3u"
    playlist.write_text("#EXTM3U\n", encoding="utf-8")
    health = runtime_mod.compute_health(
        playlist_path=playlist, status_path=status_path, now=LATER, stale_after_seconds=60
    )
    assert health["status"] == runtime_mod.FRESHNESS_STALE
    assert health["freshness"]["last_success_publish_at"] == NOW
    assert health["last_run"]["outcome"] == "failed"


def test_health_payload_has_no_url_like_fields(tmp_path):
    playlist = tmp_path / "live.m3u"
    playlist.write_text("#EXTM3U\n", encoding="utf-8")
    health = runtime_mod.compute_health(playlist_path=playlist, now=NOW)
    text = json.dumps(health, ensure_ascii=False)
    for branch in ("playlist", "freshness", "service", "last_run"):
        assert branch in health
    assert "http://" not in text and "https://" not in text
    assert "txSecret" not in text


# ============================================================== 配置校验辅助

def test_validate_server_binding():
    assert runtime_mod.validate_server_binding("127.0.0.1") is None
    assert runtime_mod.validate_server_binding("localhost") is None
    assert runtime_mod.validate_server_binding("0.0.0.0") is not None
    assert runtime_mod.validate_server_binding("192.168.1.10") is not None


# ==================================================== run --once 离线 E2E

@pytest.fixture()
def rt_env(tmp_path, mock_server):
    """离线运行期环境：mock 固定源 + mock 动态源 + out 目录。"""
    server, base = mock_server
    server.state.set_ok()

    out_dir = tmp_path / "out"
    (out_dir / "tmp").mkdir(parents=True, exist_ok=True)
    db = tmp_path / "liptv.sqlite3"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[database]\n"
        f'path = "{db.as_posix()}"\n'
        "\n[output]\n"
        f'm3u_path = "{(out_dir / "live.m3u").as_posix()}"\n'
        "keep_previous = true\n"
        "\n[fetch]\n"
        "timeout_seconds = 5.0\n"
        "max_bytes = 2000000\n"
        "max_redirects = 3\n"
        f'dynamic_tmp_dir = "{(out_dir / "tmp").as_posix()}"\n'
        "\n[publish]\n"
        f'summary_path = "{(out_dir / "publish-summary.json").as_posix()}"\n'
        "\n[runtime]\n"
        "interval_seconds = 1\n"
        "run_on_start = true\n"
        f'lock_path = "{(out_dir / "liptv.lock").as_posix()}"\n'
        f'status_path = "{(out_dir / "runtime-status.json").as_posix()}"\n'
        "stale_after_seconds = 3600\n"
        "status_history = 3\n"
        "include_dynamic = false\n"
        "\n[server]\n"
        "enabled = false\n"
        'host = "127.0.0.1"\n'
        "port = 0\n"
        'playlist_path = "/live.m3u"\n'
        'health_path = "/healthz"\n'
        "\n[[sources]]\n"
        'name = "mock-fixed"\n'
        'kind = "fixed_m3u"\n'
        f'url = "{base}/seq.m3u"\n'
        "enabled = true\n"
        "\n[[sources]]\n"
        'name = "mock-dynamic"\n'
        'kind = "dynamic_event_m3u"\n'
        f'url = "{base}/dynamic-publish.m3u"\n'
        "enabled = true\n",
        encoding="utf-8",
    )
    return {
        "cfg": cfg,
        "db": db,
        "out_dir": out_dir,
        "live": out_dir / "live.m3u",
        "status": out_dir / "runtime-status.json",
        "lock": out_dir / "liptv.lock",
        "summary": out_dir / "publish-summary.json",
        "base": base,
        "server": server,
        "tmp_path": tmp_path,
    }


def seed(capsys, env, *, fetch: bool = True) -> None:
    """建库 + 注册来源（+ 抓一次固定源），全部走 CLI，保持端到端。"""
    code, out = build(capsys, "init-db", "--config", env["cfg"], "--db", env["db"], "--json")
    assert code == 0, out
    code, out = build(capsys, "source-register", "--from-config",
                      "--config", env["cfg"], "--db", env["db"], "--json")
    assert code == 0, out
    if fetch:
        code, out = build(capsys, "fetch", "--all",
                          "--config", env["cfg"], "--db", env["db"], "--json")
        assert code == 0, out


def bind_and_probe(env, *, now: str = NOW) -> None:
    """建 canonical + 绑定 + 归集 stream + 写**既有**的 mock 测活历史。"""
    conn = db_mod.connect(env["db"])
    for sc in repo.list_source_channels(conn):
        if not int(sc["active"]):
            continue
        cid = repo.add_canonical_channel(
            conn, sc["raw_name"], category=(sc["raw_group"] or "其他"), now=now
        )
        repo.bind_source_channel(conn, int(sc["id"]), cid, now=now)
    repo.sync_streams(conn, now=now)
    probe_id = repo.ensure_probe(conn, "test-probe", now=now)
    for stream in repo.list_streams(conn):
        repo.add_probe_result(
            conn, stream_id=int(stream["id"]), probe_id=probe_id,
            success=True, checked_at=now, startup_ms=1200,
        )
    conn.commit()
    conn.close()


def run_cli(capsys, env, *extra):
    return build(capsys, "run", "--config", env["cfg"], "--db", env["db"], "--json", *extra)


def test_run_once_offline_e2e(capsys, rt_env):
    """mock fixed fetch → stream-sync → 既有 mock probe 历史 → publish，全程离线。"""
    seed(capsys, rt_env)
    bind_and_probe(rt_env)

    code, out = run_cli(capsys, rt_env, "--once", "--now", NOW)
    assert code == 0, out
    payload = json.loads(out)

    assert payload["mode"] == "once"
    round_ = payload["round"]
    assert round_["fetch"]["requested"] == 1
    assert round_["fetch"]["ok"] == 1
    assert round_["fetch"]["failed"] == 0
    # sync_streams 的真实返回键是 streams_created/streams_updated（不是笼统的 created）；
    # 夹具已 sync 过一次，所以本轮应当是「更新」，只断言总量 ≥ 1 即可。
    sync = round_["stream_sync"]
    assert sync.get("streams_created", 0) + sync.get("streams_updated", 0) >= 1
    assert sync.get("streams_created", 0) + sync.get("links_created", 0) >= 0
    assert round_["publish"]["status"] == publish_mod.STATUS_OK
    assert round_["publish"]["published"] is True
    assert round_["outcome"] == "ok"

    text = rt_env["live"].read_text(encoding="utf-8")
    assert text.startswith("#EXTM3U")
    assert "演示新闻台" in text

    # 状态文件写出来了，且**不含任何 URL**
    doc = json.loads(rt_env["status"].read_text(encoding="utf-8"))
    blob = json.dumps(doc, ensure_ascii=False)
    assert doc["last_success_publish_at"] == NOW
    assert doc["last_run_outcome"] == "ok"
    assert "http://" not in blob and "https://" not in blob
    assert "txSecret" not in blob
    assert len(blob) < 20000

    # 锁已释放
    assert payload["lock_released"] is True
    assert not rt_env["lock"].exists()


def test_run_once_does_not_create_canonical_or_binding(capsys, rt_env):
    """运行链不得自动创造 canonical / binding：只做 stream-sync 归集。"""
    seed(capsys, rt_env)          # 抓取 + 入库 source_channel，但**不建** canonical/binding
    code, out = run_cli(capsys, rt_env, "--once", "--now", NOW)
    assert code == 1, out          # 没有可发布频道 → 拒绝发布
    payload = json.loads(out)
    # TASK-003 冻结口径：空组合走 validate_composition 的 composition_errors，
    # 因此状态是 REJECTED_VALIDATION（REJECTED_EMPTY 只是常量，未在实现中产生）。
    publish = payload["round"]["publish"]
    assert publish["status"] == publish_mod.STATUS_REJECTED_VALIDATION
    assert publish["published"] is False
    assert "组合结果为空" in (publish.get("reason") or "")

    conn = db_mod.connect(rt_env["db"])
    assert repo.list_canonical_channels(conn) == []
    assert repo.list_bindings(conn) == []
    conn.close()


def test_run_second_instance_exits_locked(capsys, rt_env):
    """已有实例持锁时 run 必须以 3 退出，且不执行任何 fetch/publish。"""
    seed(capsys, rt_env)
    bind_and_probe(rt_env)

    holder = runtime_mod.SingleInstanceLock(rt_env["lock"], stale_after_seconds=3600)
    holder.acquire()
    try:
        code, out = run_cli(capsys, rt_env, "--once", "--now", NOW)
    finally:
        holder.release()

    assert code == runtime_mod.EXIT_LOCKED, out
    payload = json.loads(out)
    assert payload["status"] == "LOCKED"
    assert payload["reason"] == runtime_mod.LOCK_REASON_HELD_LIVE_PID
    assert not rt_env["live"].exists(), "被锁定的一轮不得写任何文件"


def test_run_once_survives_fixed_source_failure(capsys, rt_env):
    """单个 fixed 来源失败不能阻塞其它来源，也不能把整轮判成异常。"""
    seed(capsys, rt_env)
    bind_and_probe(rt_env)
    code, out = build(capsys, "source-add", "--name", "mock-bad", "--kind", "fixed_m3u",
                      "--url", f"{rt_env['base']}/error.m3u",
                      "--config", rt_env["cfg"], "--db", rt_env["db"], "--json")
    assert code == 0, out

    code, out = run_cli(capsys, rt_env, "--once", "--now", NOW)
    assert code == 0, out
    payload = json.loads(out)
    fetch = payload["round"]["fetch"]
    assert fetch["requested"] == 2
    assert fetch["ok"] == 1 and fetch["failed"] == 1
    assert fetch["sources"][1]["error_category"] == "HTTP_STATUS"
    # 好来源仍然发布了
    assert payload["round"]["publish"]["published"] is True


def test_run_once_with_dynamic_enabled_merges_and_fails_closed(capsys, rt_env):
    """动态显式启用：成功时合并；把动态端点打挂后必须 fail-closed 只发固定。"""
    seed(capsys, rt_env)
    bind_and_probe(rt_env)

    code, out = run_cli(capsys, rt_env, "--once", "--now", NOW, "--dynamic")
    assert code == 0, out
    ok_payload = json.loads(out)
    assert ok_payload["round"]["publish"]["dynamic_count"] == 3
    assert ok_payload["round"]["publish"]["fixed_count"] == 3

    # 让动态源返回 500：mock-dyn-bad 指向 /error.m3u
    code, out = build(capsys, "source-add", "--name", "mock-dyn-bad",
                      "--kind", "dynamic_event_m3u", "--url", f"{rt_env['base']}/error.m3u",
                      "--disable", "--config", rt_env["cfg"], "--db", rt_env["db"], "--json")
    assert code == 0, out
    code, out = run_cli(capsys, rt_env, "--once", "--now", LATER,
                        "--dynamic-source", "mock-dynamic", "--dynamic-source", "mock-dyn-bad")
    assert code == 0, out
    payload = json.loads(out)
    publish = payload["round"]["publish"]
    assert publish["status"] == publish_mod.STATUS_DEGRADED_FIXED_ONLY
    assert publish["dynamic_fail_closed"] is True
    assert publish["dynamic_discarded"] == 3
    assert publish["dynamic_count"] == 0
    text = rt_env["live"].read_text(encoding="utf-8")
    assert "体育赛事（实时）" not in text
    assert "txSecret" not in text


def test_run_loop_with_max_rounds_via_cli(capsys, rt_env):
    """CLI 层的 loop：--max-rounds 3 且 interval=1s，不真实长等。"""
    seed(capsys, rt_env)
    bind_and_probe(rt_env)
    started = time.perf_counter()
    code, out = run_cli(capsys, rt_env, "--max-rounds", "3", "--interval", "1",
                        "--now", NOW)
    elapsed = time.perf_counter() - started
    assert code == 0, out
    payload = json.loads(out)
    assert payload["mode"] == "loop"
    assert payload["loop"]["rounds"] == 3
    assert payload["loop"]["failed_rounds"] == 0
    assert elapsed < 20, f"3 轮 × 1s 周期不该跑这么久：{elapsed:.1f}s"

    doc = json.loads(rt_env["status"].read_text(encoding="utf-8"))
    assert doc["current_round"]["round_number"] == 3
    assert len(doc["rounds"]) == 3
    assert not rt_env["lock"].exists()


def test_run_once_reports_uninitialized_db(capsys, rt_env):
    """数据库没初始化时，轮次要作为失败被记录（而不是把进程炸掉）。"""
    code, out = run_cli(capsys, rt_env, "--once", "--now", NOW)
    assert code == 1, out
    payload = json.loads(out)
    assert payload["round"]["outcome"] == "failed"
    doc = json.loads(rt_env["status"].read_text(encoding="utf-8"))
    assert doc["current_round"]["error"]["type"] == "RuntimeError"
    assert not rt_env["lock"].exists()


def test_run_serve_flag_starts_readonly_http(capsys, rt_env):
    """`run --serve` 必须真的把只读 HTTP 服务拉起来（用临时端口验证接线）。"""
    seed(capsys, rt_env)
    bind_and_probe(rt_env)
    code, out = run_cli(capsys, rt_env, "--once", "--now", NOW,
                        "--serve", "--port", "0")
    assert code == 0, out
    payload = json.loads(out)
    assert payload["serve"] is True
    assert payload["service_url"] and payload["service_url"].endswith("/live.m3u")
    assert not rt_env["lock"].exists()


def test_cli_run_wires_lock_heartbeat_into_scheduler(capsys, rt_env, monkeypatch):
    """QA-004A 接线永久回归：命令行 ``run`` 必须**真的**驱动锁心跳。

    首版把 ``heartbeat()`` 实现了却从没被调用过 —— 这个用例直接把「接线」本身钉死：
    用 spy 包住 ``SingleInstanceLock.heartbeat``，任何一次真实调用都会被数到。
    """
    seed(capsys, rt_env)
    bind_and_probe(rt_env)

    seen = {"n": 0}
    original = runtime_mod.SingleInstanceLock.heartbeat

    def spy(self, *args, **kwargs):
        seen["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(runtime_mod.SingleInstanceLock, "heartbeat", spy)
    code, out = build(capsys, "run", "--config", rt_env["cfg"], "--db", rt_env["db"],
                      "--json", "--once", "--now", NOW)
    assert code == 0, out
    payload = json.loads(out)

    assert seen["n"] >= 2, f"至少应有「轮次开始 + 轮次结束」两次心跳，实测 {seen['n']}"
    assert payload["heartbeat_count"] >= 2
    assert payload["lock_lost"] is False
    assert payload["lock_released"] is True
    assert payload["heartbeat_interval_seconds"] == runtime_mod.resolve_heartbeat_interval(
        payload["stale_after_seconds"], payload["interval_seconds"]
    )
    assert not rt_env["lock"].exists(), "正常退出后锁必须被删掉"


def test_cli_run_loop_stops_when_lock_is_stolen_and_keeps_foreign_lock(capsys, rt_env, monkeypatch):
    """QA-004A 端到端：循环运行中丢锁 ⇒ 停止、退出码 1，并且**不删**别人的锁。"""
    seed(capsys, rt_env)
    bind_and_probe(rt_env)

    real_round = runtime_mod.Scheduler.run_once

    def round_then_steal(self, **kwargs):
        entry = real_round(self, **kwargs)
        if self.lock_lost is False and self.rounds_run == 1:
            # 第一轮刚跑完就把锁「交给」另一个实例
            data = read_lock_file(rt_env["lock"])
            data["token"] = "intruder-token"
            rt_env["lock"].write_text(json.dumps(data), encoding="utf-8")
        return entry

    monkeypatch.setattr(runtime_mod.Scheduler, "run_once", round_then_steal)
    code, out = build(capsys, "run", "--config", rt_env["cfg"], "--db", rt_env["db"],
                      "--json", "--max-rounds", "3")
    assert code == runtime_mod.EXIT_ROUND_FAILED, out
    payload = json.loads(out)

    assert payload["lock_lost"] is True
    assert payload["lock_released"] is False, "无法证明归属时不得删除锁文件"
    assert payload["loop"]["rounds"] == 1, "丢锁之后不得再跑第二轮"
    assert payload["loop"]["stop_reason"] == runtime_mod.LOCK_REASON_LOST
    assert read_lock_file(rt_env["lock"])["token"] == "intruder-token"


# ============================================== 真实进程停止（Ctrl+C / SIGTERM）

def _wait_for(predicate, *, timeout: float, interval: float = 0.1) -> bool:
    """轮询等待，超时返回 False（不抛异常，交给调用方断言）。"""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _stop_signal_for_subprocess() -> int:
    """挑一个能**定向**送给子进程组、又不会连累测试进程自己的停止信号。

    Windows 上 ``CTRL_C_EVENT`` 会发给共享同一控制台的所有进程（包括 pytest 自己），
    因此只能用 ``CTRL_BREAK_EVENT`` + ``CREATE_NEW_PROCESS_GROUP``；
    POSIX 上直接用 ``SIGINT``。
    """
    if os.name == "nt":
        return signal.CTRL_BREAK_EVENT  # type: ignore[attr-defined]
    return signal.SIGINT


def test_real_process_stops_cleanly_on_signal_and_releases_lock(rt_env, capsys):
    """真起一个 loop 子进程，发停止信号：不启动下一轮、锁释放、HTTP 服务关闭。

    覆盖 TASK-004 §8-9：停止后不再开新一轮、锁不残留、HTTP server 正常关闭。
    """
    seed(capsys, rt_env)
    bind_and_probe(rt_env)

    stop_signal = _stop_signal_for_subprocess()
    creationflags = 0
    if os.name == "nt":
        if not hasattr(signal, "CTRL_BREAK_EVENT") or not hasattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP"
        ):  # pragma: no cover - 平台能力缺失
            pytest.skip("当前 Windows 环境不支持定向发送 CTRL_BREAK_EVENT")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"

    argv = [
        sys.executable, "-m", "liptv", "run",
        "--config", str(rt_env["cfg"]), "--db", str(rt_env["db"]),
        # 周期故意放大到 600s：能停下来就证明没有傻等整个周期
        "--interval", "600", "--serve", "--port", "0",
    ]
    proc = subprocess.Popen(
        argv, cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=creationflags,
    )
    try:
        # ① 等第一轮真的跑完（状态文件里出现 current_round）
        def first_round_done() -> bool:
            if proc.poll() is not None:
                return False
            try:
                doc = json.loads(rt_env["status"].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            return bool((doc.get("current_round") or {}).get("finished_at"))

        assert _wait_for(first_round_done, timeout=90), "第一轮没有在 90s 内完成"
        assert rt_env["lock"].exists(), "持锁期间应当能看到锁文件"

        started = time.perf_counter()
        proc.send_signal(stop_signal)
        out, _ = proc.communicate(timeout=60)
        elapsed = time.perf_counter() - started
    finally:
        if proc.poll() is None:  # pragma: no cover - 只有断言失败时才会走到
            proc.kill()
            proc.communicate(timeout=30)

    assert proc.returncode == 0, f"停止后应当正常退出（0），实得 {proc.returncode}\n{out}"
    assert elapsed < 30, f"停止请求没有被及时响应，耗时 {elapsed:.1f}s（周期是 600s）"

    # ② 没有再开新一轮
    doc = json.loads(rt_env["status"].read_text(encoding="utf-8"))
    assert doc["current_round"]["round_number"] == 1
    assert len(doc["rounds"]) == 1

    # ③ 锁已释放（日志改为「已释放：<lock 路径>」，见 QA-004B 返工）
    assert not rt_env["lock"].exists()
    assert f"[runtime] 锁已释放：{rt_env['lock']}" in out
    assert "保守处理：未删除锁文件" not in out

    # ④ HTTP 服务确实起过、并且已经关掉（地址不可再连接）
    assert "只读订阅服务" in out and "只读订阅服务已关闭" in out
    marker = out.split("只读订阅服务：", 1)[1].split("/live.m3u", 1)[0]
    port = int(marker.rsplit(":", 1)[1])
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


def test_keyboard_interrupt_is_caught_and_releases_lock(rt_env, capsys, monkeypatch):
    """Ctrl+C 打在「轮次执行中」或「循环中」都必须收敛成正常退出，且不留脏锁。"""
    seed(capsys, rt_env)
    bind_and_probe(rt_env)

    # ① 单轮执行途中被 Ctrl+C
    def interrupt_run_once(self, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime_mod.Scheduler, "run_once", interrupt_run_once)
    code, out = run_cli(capsys, rt_env, "--once", "--now", NOW)
    assert code == 0, f"Ctrl+C 不是失败，退出码应为 0：{out}"
    payload = json.loads(out)
    assert payload["interrupted"] is True
    assert payload["lock_released"] is True
    assert not rt_env["lock"].exists()

    # ② 长期循环中被 Ctrl+C
    def interrupt_run(self, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime_mod.Scheduler, "run", interrupt_run)
    code, out = run_cli(capsys, rt_env, "--interval", "1")
    assert code == 0, out
    payload = json.loads(out)
    assert payload["mode"] == "loop" and payload["interrupted"] is True
    assert payload["lock_released"] is True
    assert not rt_env["lock"].exists()
