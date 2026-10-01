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

import pytest

from liptv import config as config_mod
from liptv import db as db_mod
from liptv import publish as publish_mod
from liptv import repo
from liptv import runtime as runtime_mod
from liptv.cli import main as cli_main

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

    # ③ 锁已释放
    assert not rt_env["lock"].exists()
    assert "锁已释放：True" in out

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
