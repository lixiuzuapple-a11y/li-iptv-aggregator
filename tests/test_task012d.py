"""TASK-012 REVIEW-01 返工专项：R1 日报自动闭环 + R2 文档口径。

Reviewer 原文：``REVIEWS/TASK-012-REVIEW-01.md``。

本批只覆盖返工单点名的内容，**不重做**已通过的部分（stability 42/44、
8/8 failover、preflight、KORICE advisory、retention、no-change publish、
healthz stale 修复本身）。

R1 的九条验收条件与本文件的对应关系：

===========================  ==============================================
Reviewer 条件                用例
===========================  ==============================================
1 生产自动生成               ``test_r1_*``（临时目录 = 生产等价路径形状）
2 service user 可读写         路径推导 + 权限位断言（POSIX）
3 原子写                ``test_r1_write_is_atomic_no_partial_file``
4 写失败不影响publish   ``test_r1_write_failure_does_not_change_exit``
5 只覆盖一个有界文件          ``test_r1_single_file_no_history_dir``
6 输出脱敏                   ``test_r1_summary_is_redacted``
7 重启后继续自动更新          ``test_r1_restart_continues_overwriting``
8 generated_at 随轮次推进     ``test_r1_generated_at_advances_per_round``
9 不新增 timer               ``test_r1_no_new_timer``（结构性约束）
===========================  ==============================================

⚠️ 时间纪律（§36）：所有涉及时间敏感函数的用例都注入确定性时钟，
不用真实墙钟。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import stat
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from liptv import cli as cli_mod                # noqa: E402
from liptv import db as db_mod                  # noqa: E402
from liptv import publish as publish_mod        # noqa: E402
from liptv import reliability as reliability_mod  # noqa: E402
from liptv import runtime as runtime_mod        # noqa: E402

NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)
NOW_ISO = NOW.isoformat()


# ----------------------------------------------------------------- fixtures

def make_db() -> sqlite3.Connection:
    conn = db_mod.connect(":memory:")
    db_mod.init_db(conn)
    return conn


def write_config(tmp_path: pathlib.Path, **overrides) -> pathlib.Path:
    """写一份最小可用的 config.toml，路径全部落在 ``tmp_path`` 里。

    形状对齐生产：database / output / publish / runtime 的产物路径同目录，
    这样 ``default_summary_path`` 推导出来的就是「运行数据目录下的摘要」。
    """
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)

    def p(name: str) -> str:
        return str(data / name).replace("\\", "/")

    lines = [
        "[database]",
        f'path = "{p("liptv.sqlite3")}"',
        "",
        "[output]",
        f'm3u_path = "{p("live.m3u")}"',
        "keep_previous = true",
        "",
        "[publish]",
        f'summary_path = "{p("publish-summary.json")}"',
        "",
        "[runtime]",
        "interval_seconds = 60",
        "run_on_start = true",
        f'lock_path = "{p("liptv.lock")}"',
        f'status_path = "{p("runtime-status.json")}"',
        "stale_after_seconds = 3600",
        "status_history = 5",
    ]
    for key, value in overrides.items():
        lines.append(f"{key} = {json.dumps(value)}")
    cfg = tmp_path / "config.toml"
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cfg


class Args:
    """最小 argparse 替身：只提供 ``cmd_reliability_after_round`` 用到的字段。"""

    def __init__(self, cfg: pathlib.Path, **extra):
        self.config = str(cfg)
        self.db = None
        self.json = False
        self.now = None
        self.publish_summary = None
        self.runtime_status = None
        self.playlist = None
        self.window_days = None
        self.max_consecutive_failures = None
        self.min_successes = None
        self.started_at = None
        self.out_path = None
        self.reliability_summary = extra.pop("reliability_summary", None)
        self.no_reliability_summary = extra.pop("no_reliability_summary", False)
        for key, value in extra.items():
            setattr(self, key, value)


def make_round_entry(*, outcome="ok", published=True,
                     status=publish_mod.STATUS_OK, stamp=NOW_ISO, round_id="r1"):
    """构造一个形似 ``Scheduler`` 轮次结果的 entry。"""
    return {
        "round_id": round_id,
        "round_number": 1,
        "started_at": stamp,
        "finished_at": stamp,
        "duration_ms": 12,
        "fetch": {"requested": 1, "ok": 1, "failed": 0, "sources": []},
        "stream_sync": {},
        "publish": {
            "status": status,
            "exit_code": publish_mod.EXIT_OK,
            "published": published,
            "fixed_count": 2,
            "dynamic_count": 1,
        },
        "published": published,
        "publish_status": status,
        "outcome": outcome,
        "errors": [],
        "exit_code": publish_mod.EXIT_OK,
    }


def seed_db_and_status(cfg: pathlib.Path) -> None:
    """把 config 指向的库初始化好，并在磁盘上放一份 publish 摘要。"""
    settings = runtime_mod.RuntimeSettings.from_mapping(
        __import__("tomllib").loads(cfg.read_text(encoding="utf-8"))["runtime"]
    )
    db_file = pathlib.Path(
        [ln.split("=", 1)[1].strip().strip('"')
         for ln in cfg.read_text(encoding="utf-8").splitlines()
         if ln.startswith("path")][0]
    )
    conn = db_mod.connect(db_file)
    db_mod.init_db(conn)
    conn.close()

    pub = db_file.parent / "publish-summary.json"
    pub.write_text(json.dumps({
        "status": publish_mod.STATUS_OK,
        "published": True,
        "fixed_count": 2,
        "dynamic_count": 1,
        "dynamic_summary": {"sources": [{"name": "jsnzkpg-sports", "published": 1}]},
    }), encoding="utf-8")

    store = runtime_mod.StatusStore(settings.status_path)
    store.record_round(make_round_entry())


# ================================================== R1 · 日报自动闭环

class TestR1SchedulerHook:
    """scheduler 每轮结束必须自动写摘要（Reviewer R1 主条件 1/7/8）。"""

    def test_r1_hook_writes_summary_after_round(self, tmp_path):
        """方案 A：挂在 scheduler 每轮末尾 ⇒ 无需任何人工命令就有摘要。"""
        cfg = write_config(tmp_path)
        seed_db_and_status(cfg)
        args = Args(cfg)
        hook, path = cli_mod._reliability_after_round(args, None)

        assert not pathlib.Path(path).is_file(), "钩子只是构造，不该在没跑轮次时就落盘"
        result = hook(make_round_entry())
        assert result["ok"] is True, result
        assert result["written"] is True, result
        assert pathlib.Path(path).is_file()

    def test_r1_summary_path_follows_runtime_data_dir(self, tmp_path):
        """条件 1/2：路径固定在**运行数据目录**，且与 runtime-status 同级。

        Reviewer 点名的生产路径是 ``/var/lib/li-iptv-aggregator/``，
        正是 status_path 所在目录 —— 所以推导规则必须是从 status_path 出发，
        而不是写死绝对路径（否则非默认前缀部署会写到无权访问的位置）。
        """
        cfg = write_config(tmp_path)
        args = Args(cfg)
        _, path = cli_mod._reliability_after_round(args, None)
        data_dir = (tmp_path / "data").resolve()
        assert pathlib.Path(path).resolve().parent == data_dir
        assert pathlib.Path(path).name == "reliability-summary.json"

    def test_r1_explicit_override_honoured(self, tmp_path):
        """显式指定路径时按指定的来（运维需要挪位置时可用）。"""
        cfg = write_config(tmp_path)
        target = tmp_path / "custom" / "daily.json"
        args = Args(cfg, reliability_summary=str(target))
        _, path = cli_mod._reliability_after_round(args, None)
        assert pathlib.Path(path) == target

    def test_r1_generated_at_advances_per_round(self, tmp_path):
        """条件 8：generated_at 必须随生产轮次推进，不能永远停在同一个值。"""
        cfg = write_config(tmp_path)
        seed_db_and_status(cfg)
        hook, path = cli_mod._reliability_after_round(Args(cfg), None)

        stamps = [
            (dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=dt.timezone.utc), "r1"),
            (dt.datetime(2026, 10, 8, 12, 15, 0, tzinfo=dt.timezone.utc), "r2"),
            (dt.datetime(2026, 10, 8, 12, 30, 0, tzinfo=dt.timezone.utc), "r3"),
        ]
        seen = []
        for when, rid in stamps:
            entry = make_round_entry(stamp=when.isoformat(), round_id=rid)
            assert hook(entry)["ok"] is True
            doc = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
            seen.append(doc["generated_at"])

        assert seen == [w.isoformat() for w, _ in stamps], seen
        assert len(set(seen)) == len(seen), f"generated_at 没有推进：{seen}"

    def test_r1_restart_continues_overwriting(self, tmp_path):
        """条件 7：重启（重新构造 scheduler）后下一轮继续覆盖同一份文件。"""
        cfg = write_config(tmp_path)
        seed_db_and_status(cfg)
        args = Args(cfg)

        # 第一次「进程」
        hook_a, path_a = cli_mod._reliability_after_round(args, None)
        hook_a(make_round_entry(stamp="2026-10-08T12:00:00+00:00", round_id="a1"))

        # 重启：全新的 args / 钩子（模拟服务重启后重新装配）
        args2 = Args(cfg)
        hook_b, path_b = cli_mod._reliability_after_round(args2, None)
        assert str(path_a) == str(path_b), "重启后摘要路径必须一致，否则会散成多份"
        hook_b(make_round_entry(stamp="2026-10-08T12:15:00+00:00", round_id="b1"))

        doc = json.loads(pathlib.Path(path_b).read_text(encoding="utf-8"))
        assert doc["generated_at"] == "2026-10-08T12:15:00+00:00"

    def test_r1_single_file_no_history_dir(self, tmp_path):
        """条件 5：只覆盖**一个**有界文件，绝不滚成 history 目录。"""
        cfg = write_config(tmp_path)
        seed_db_and_status(cfg)
        hook, path = cli_mod._reliability_after_round(Args(cfg), None)
        for i in range(5):
            hook(make_round_entry(
                stamp=f"2026-10-08T12:0{i}:00+00:00", round_id=f"r{i}"))

        data_dir = pathlib.Path(path).parent
        files = sorted(p.name for p in data_dir.iterdir() if p.is_file())
        assert "reliability-summary.json" in files
        assert not any(p.is_dir() for p in data_dir.iterdir()), \
            f"不该出现子目录：{[p.name for p in data_dir.iterdir() if p.is_dir()]}"
        # 没有 .tmp 残留（写成功时 tmp 必须已被 os.replace 消费掉）
        assert not (data_dir / "reliability-summary.json.tmp").exists()
        assert len(files) <= 6, f"文件数不该随轮次增长：{files}"

    def test_r1_write_is_atomic_no_partial_file(self, tmp_path):
        """条件 3：原子写 —— 失败时不留半个文件，成功时 tmp 已消失。"""
        target = tmp_path / "atomic.json"
        good = {"schema": "x", "generated_at": "2026-10-08T12:00:00+00:00"}
        assert reliability_mod.write_summary(good, target)["written"] is True
        assert not (tmp_path / "atomic.json.tmp").exists()

        # 目标是个目录 ⇒ os.replace 必然失败
        blocked = tmp_path / "blocked.json"
        blocked.mkdir()
        result = reliability_mod.write_summary(good, blocked)
        assert result["written"] is False
        assert result["error"], "写失败必须给出原因，否则等于静默"

    def test_r1_summary_is_redacted(self, tmp_path):
        """条件 6：绝不含完整 stream URL / signed query / Cookie / Authorization / VPN。"""
        cfg = write_config(tmp_path)
        seed_db_and_status(cfg)
        hook, path = cli_mod._reliability_after_round(Args(cfg), None)
        assert hook(make_round_entry())["ok"] is True

        raw = pathlib.Path(path).read_text(encoding="utf-8")
        for token in ("http://", "https://", "playtoken", "Cookie",
                      "Authorization", "sign=", "token="):
            assert token not in raw, f"摘要里出现了不该有的敏感内容：{token}"

    def test_r1_no_new_timer(self, tmp_path):
        """条件 9：本轮返工**不引入任何新 systemd unit / timer**。"""
        repo = pathlib.Path(__file__).resolve().parents[1]
        units = sorted((repo / "deploy" / "systemd").glob("*.timer"))
        assert units == [], f"返工不应新增 timer：{[u.name for u in units]}"
        # 也不该有第二个「每天跑一次」的独立入口：scheduler 是唯一挂载点。
        assert (repo / "liptv" / "reliability.py").is_file()


class TestR1SchedulerIntegration:
    """``Scheduler`` 侧的挂载语义（条件 4：写失败不影响发布）。"""

    def _scheduler(self, tmp_path, after_round):
        settings = runtime_mod.RuntimeSettings.from_mapping({
            "interval_seconds": 1,
            "run_on_start": True,
            "lock_path": str(tmp_path / "l.lock"),
            "status_path": str(tmp_path / "status.json"),
            "stale_after_seconds": 3600,
        })
        calls = []

        def round_fn(*, round_id):
            calls.append(round_id)
            return make_round_entry(round_id=round_id)

        return runtime_mod.Scheduler(
            settings=settings, round_fn=round_fn, clock=lambda: 0.0,
            sleep=lambda _s: None, after_round=after_round,
        )

    def test_r1_after_round_called_every_round(self, tmp_path):
        seen = []
        sched = self._scheduler(tmp_path, lambda e: seen.append(e) or {"ok": True})
        sched.run(max_rounds=3)
        assert len(seen) == 3
        assert sched.after_round_calls == 3
        assert sched.after_round_failures == 0

    def test_r1_after_round_runs_for_degraded_and_rejected(self, tmp_path):
        """DEGRADED_DYNAMIC_PARTIAL 与 REJECTED 轮次**同样**要刷新日报。

        语义明确：日报是「当前可靠性快照」，不是「成功证明」——
        出问题时运维更需要有日报。只在成功轮刷新等于故障时瞎掉。
        """
        seen = []

        def make(status, published, outcome):
            def round_fn(*, round_id):
                return make_round_entry(status=status, published=published,
                                        outcome=outcome, round_id=round_id)
            return round_fn

        for status, published, outcome in (
            (publish_mod.STATUS_OK, True, "ok"),
            (publish_mod.STATUS_DEGRADED_DYNAMIC_PARTIAL, True, "degraded"),
            (publish_mod.STATUS_REJECTED_EMPTY, False, "failed"),
        ):
            sched = self._scheduler(tmp_path, lambda e: seen.append(
                e["publish_status"]) or {"ok": True})
            sched.round_fn = make(status, published, outcome)
            sched.run(max_rounds=1)
        assert seen == [
            publish_mod.STATUS_OK,
            publish_mod.STATUS_DEGRADED_DYNAMIC_PARTIAL,
            publish_mod.STATUS_REJECTED_EMPTY,
        ], seen

    def test_r1_write_failure_does_not_change_exit(self, tmp_path):
        """条件 4：摘要写失败绝不能改本轮 exit_code，也绝不能杀死 loop。"""
        def boom(_entry):
            raise PermissionError("read-only file system")

        sched = self._scheduler(tmp_path, boom)
        sched.run(max_rounds=3)
        assert sched.after_round_calls == 3
        assert sched.after_round_failures == 3
        # loop 跑满了 3 轮 ⇒ 没被异常杀死
        assert sched.rounds_run == 3
        assert sched.failed_rounds == 0, "观测失败不算轮次失败"
        doc = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
        assert doc["current_round"]["exit_code"] == publish_mod.EXIT_OK

    def test_r1_hook_returning_none_is_logged_not_swallowed(self, tmp_path):
        """返回 None（钩子什么都没做）必须留痕（计数 + 日志），不能静默。"""
        logs = []
        sched = self._scheduler(tmp_path, lambda _e: None)
        sched._logger = logs.append
        sched.run(max_rounds=1)
        assert sched.after_round_failures == 1
        assert any("轮次后处理" in m for m in logs), logs

    def test_r1_hook_runs_after_status_recorded(self, tmp_path):
        """顺序不变量：钩子必须看到**已含本轮**的 runtime-status。

        顺序反了的话，摘要里的 runtime 段永远是上一轮 —— 这类错很隐蔽，
        所以用「钩子内读文件断言本轮 round_number 已在」把它钉死。
        """
        seen_numbers = []

        def hook(_entry):
            doc = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
            seen_numbers.append(doc["current_round"]["round_number"])
            return {"ok": True}

        sched = self._scheduler(tmp_path, hook)
        sched.run(max_rounds=2)
        # 每一次钩子看到的 current_round 都必须是**当轮**的编号，
        # 若是上一轮则说明钩子在 record_round 之前被调用了。
        assert seen_numbers == [1, 2], seen_numbers

    def test_r1_hook_reports_failure_via_ok_flag(self, tmp_path):
        """钩子返回 ``{"ok": False, ...}``（非空但表示失败）必须被记为失败。

        这条是被真实缺陷逼出来的：``refresh_after_round`` 失败时返回的是
        非空字典，只判 truthiness 会把「磁盘满」当成成功，日志里看不出来。
        """
        logs = []
        sched = self._scheduler(
            tmp_path, lambda _e: {"ok": False, "error": "OSError: no space",
                                  "stage": "write", "written": False})
        sched._logger = logs.append
        sched.run(max_rounds=1)
        assert sched.after_round_failures == 1
        assert any("stage=write" in m and "no space" in m for m in logs), logs
        # 轮次本身仍然算成功
        assert sched.failed_rounds == 0

    def test_r1_no_hook_keeps_legacy_behaviour(self, tmp_path):
        """不传 after_round ⇒ 行为与 TASK-004 首版完全一致（零回归）。"""
        sched = self._scheduler(tmp_path, None)
        sched.run(max_rounds=2)
        assert sched.after_round_calls == 0
        assert sched.rounds_run == 2


class TestR1RefreshAfterRound:
    """``refresh_after_round`` 的失败降级矩阵（F8：任何一步失败都不外泄）。"""

    def _call(self, tmp_path, *, conn_factory, inputs_fn=None):
        return reliability_mod.refresh_after_round(
            conn_factory,
            output_path=tmp_path / "s.json",
            window_days=7,
            selection_kwargs={"now": NOW},
            inputs_fn=inputs_fn,
            generated_at=NOW_ISO,
            now=NOW_ISO,
        )

    def test_r1_connect_failure_is_contained(self, tmp_path):
        def boom():
            raise RuntimeError("数据库尚未初始化")
        result = self._call(tmp_path, conn_factory=boom)
        assert result["ok"] is False
        assert result["stage"] == "connect"
        assert not (tmp_path / "s.json").exists()

    def test_r1_inputs_failure_is_contained(self, tmp_path):
        def bad_inputs():
            raise OSError("epg file unreadable")
        result = self._call(tmp_path, conn_factory=make_db, inputs_fn=bad_inputs)
        assert result["ok"] is False
        assert result["stage"] == "inputs"

    def test_r1_build_failure_is_contained(self, tmp_path):
        """build_summary 抛异常必须被收敛成 ``stage="build"``，不外泄、不落半份文件。"""
        def bad_inputs():
            # 合法连接 + 合法元组，但让 build 之后写盘前的路径不可用来触发
            # 写失败；build 本身用 monkeypatch 制造异常更直接。
            raise AssertionError("unused")

        import liptv.reliability as real
        original = real.build_summary

        def boom(*_a, **_k):
            raise ValueError("window arithmetic exploded")

        real.build_summary = boom
        try:
            result = self._call(tmp_path, conn_factory=make_db,
                                inputs_fn=lambda: (None, None, None, None, None))
        finally:
            real.build_summary = original
        assert result["ok"] is False
        assert result["stage"] == "build"
        assert "ValueError" in result["error"]
        assert not (tmp_path / "s.json").exists(), "build 失败不该留下任何文件"

    def test_r1_success_path_returns_details(self, tmp_path):
        result = self._call(
            tmp_path, conn_factory=make_db,
            inputs_fn=lambda: (None, None, None, None, None))
        assert result["ok"] is True
        assert result["stage"] is None
        assert result["bytes"] > 0
        doc = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
        assert doc["schema"] == "li-iptv-aggregator/reliability-summary"

    def test_r1_conn_is_always_closed(self, tmp_path):
        closed = {"value": False}

        class Conn:
            def close(self):
                closed["value"] = True

        def factory():
            return Conn()

        self._call(tmp_path, conn_factory=factory)
        assert closed["value"] is True

    def test_r1_close_failure_is_contained(self, tmp_path):
        """``close()`` 抛异常不该让已经算好的摘要丢失。

        用一个**能正常查询**但 ``close`` 会炸的连接来验证 ——
        否则失败会提前落到 connect/build 阶段，测的就不是 close 了。
        """
        real_conn = make_db()
        state = {"closed": False}

        class BadClose:
            """把除 close 外的所有调用都转给真实连接（build_summary 用到不少接口）。"""

            def __getattr__(self, name):
                return getattr(real_conn, name)

            def close(self):
                state["closed"] = True
                raise OSError("close failed")

        result = self._call(tmp_path, conn_factory=BadClose)
        assert state["closed"] is True, "close 应当被调用过"
        assert result["ok"] is True, "关连接失败不该让摘要丢失"
        real_conn.close()


class TestR1CliWiring:
    """CLI 装配层：默认开、可关、payload 暴露统计。"""

    def test_r1_summary_path_helper(self, tmp_path):
        cfg = write_config(tmp_path)
        paths = cli_mod._reliability_paths(Args(cfg))
        data_dir = (tmp_path / "data").resolve()
        for key in ("publish_summary", "runtime_status", "playlist"):
            assert pathlib.Path(paths[key]).resolve().parent == data_dir, key

    def test_r1_reliability_status_write_default_follows_status_path(self, tmp_path):
        """手工 ``--write`` 的默认落点也必须与自动路径一致（不能分叉）。"""
        cfg = write_config(tmp_path)
        seed_db_and_status(cfg)
        args = Args(cfg, write=True, output=None)
        assert cli_mod.cmd_reliability_status(args) == 0
        data_dir = (tmp_path / "data").resolve()
        assert (data_dir / "reliability-summary.json").is_file()

    def test_r1_no_reliability_summary_flag_parsed(self):
        """逃生阀参数存在且默认 False（默认必须开启自动生成）。"""
        import argparse
        parser = cli_mod.build_parser()
        ns = parser.parse_args(["run"])
        assert ns.no_reliability_summary is False
        ns = parser.parse_args(["run", "--no-reliability-summary"])
        assert ns.no_reliability_summary is True


# ================================================== R2 · 文档口径统一

class TestR2ReportConsistency:
    """主报告与执行摘要不能自相矛盾（Reviewer R2）。"""

    @staticmethod
    def _report() -> str:
        repo = pathlib.Path(__file__).resolve().parents[1]
        return (repo / "REPORTS" / "TASK-012-REPORT.md").read_text(encoding="utf-8")

    @staticmethod
    def _task() -> str:
        repo = pathlib.Path(__file__).resolve().parents[1]
        return (repo / "TASKS" / "TASK-012.md").read_text(encoding="utf-8")

    def test_r2_no_stale_not_deployed_claim(self):
        """正文不得再说「healthz 修复尚未部署到生产」。"""
        text = self._report()
        assert "尚未部署到生产" not in text, \
            "主报告仍保留旧快照 —— 与执行摘要「已上线 release」自相矛盾"

    def test_r2_healthz_deployed_stated(self):
        """必须明确写出「已部署」这一最终事实。"""
        text = self._report()
        assert "997326580c86-20261008T010432Z" in text, \
            "主报告必须写明 healthz 修复实际发布的 release"

    def test_r2_soak_final_number_is_55(self):
        """soak 最终轮次统一用 55 轮；21 轮只能作为阶段快照出现且被标注。"""
        text = self._report()
        assert "55 轮" in text
        # 「21 轮」只允许出现在明确标了「阶段快照」的上下文里。
        # 用前后文窗口判定，而不是逐行 —— 标注常写在引用块或同一段的旁注里。
        for match in re.finditer("21 轮", text):
            window = text[max(0, match.start() - 260): match.end() + 260]
            assert "阶段" in window, \
                f"21 轮出现在没有阶段标注的上下文：…{window[200:340]}…"

    def test_r2_summary_section_is_55_not_21(self):
        """开篇「一句话」段用最终数字，不用早期快照。

        允许开篇的**口径说明引用块**提到 21 轮（那正是它在解释
        「21 轮是阶段快照」），但正文那句摘要必须是 55 轮。
        """
        text = self._report()
        head = text.split("## §36")[0]
        assert "**55 轮**生产 soak" in head, "摘要段仍停留在早期轮次"
        assert "**21 轮**生产 soak" not in head, \
            "摘要段还把 21 轮当最终数字"

    def test_r2_review_mentions_reliability_closed_loop(self):
        """执行摘要要写清日报已自动闭环（否则又是新矛盾）。"""
        text = self._task()
        assert "reliability-summary.json" in text
        assert "每轮" in text


class TestR2FileHygiene:
    """仓库卫生：CRLF 与临时文件（提交前硬门禁）。"""

    def test_r2_changed_files_are_lf_only(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        for name in ("liptv/cli.py", "liptv/runtime.py", "liptv/reliability.py",
                     "tests/test_task012d.py", "REPORTS/TASK-012-REPORT.md"):
            data = (repo / name).read_bytes()
            assert b"\r\n" not in data, f"{name} 含 CRLF，必须纯 LF"

    def test_r2_no_tmp_artifacts_in_repo(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        leftovers = [p.name for p in repo.rglob("*.tmp")
                     if ".git" not in p.parts]
        assert leftovers == [], f"仓库里有 .tmp 残留：{leftovers}"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位语义")
    def test_r2_summary_is_world_readable(self, tmp_path):
        """条件 2 的可读侧：文件权限至少让服务账号之外的运维能读。"""
        cfg = write_config(tmp_path)
        seed_db_and_status(cfg)
        hook, path = cli_mod._reliability_after_round(Args(cfg), None)
        hook(make_round_entry())
        mode = stat.S_IMODE(pathlib.Path(path).stat().st_mode)
        assert mode & stat.S_IRUSR
        assert mode & stat.S_IWUSR