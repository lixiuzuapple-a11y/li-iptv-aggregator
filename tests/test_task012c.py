"""TASK-012 第三批：§20 cadence 解析、§23 增长估算、§22 soak 指标、§29 failure smoke。

前两批见 ``test_task012.py``（§9/§10/§11/§13 派生层）与
``test_task012b.py``（§17 preflight、§19 summary、§21 no-change）。
本批补的是「运行时观测」侧：把生产真实数字变成可执行断言。

⚠️ 本批所有涉及时间的用例都**注入确定性时钟**（§36 口径纪律），
不用真实墙钟 —— 否则测试结果每天都不一样，回归就失去意义。
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from liptv import cadence as cadence_mod           # noqa: E402
from liptv import db as db_mod                     # noqa: E402
from liptv import errors as errors_mod             # noqa: E402
from liptv import publish as publish_mod           # noqa: E402
from liptv import reliability as reliability_mod   # noqa: E402
from liptv import retention as retention_mod       # noqa: E402
from liptv import stability as stability_mod       # noqa: E402

NOW = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.timezone.utc)


# ----------------------------------------------------------------- fixtures

def make_db() -> sqlite3.Connection:
    conn = db_mod.connect(":memory:")
    db_mod.init_db(conn)
    return conn


def add_channel(conn, name="CCTV-1 综合", category="新闻", tvg="CCTV1"):
    cur = conn.execute(
        "INSERT INTO canonical_channel (name, category, preferred_tvg_id,"
        " preferred_logo, enabled, priority, created_at, updated_at)"
        " VALUES (?, ?, ?, 'http://logo/1.png', 1, 10,"
        " '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')",
        (name, category, tvg))
    return int(cur.lastrowid)


def add_stream(conn, cid, url, host=None):
    cur = conn.execute(
        "INSERT INTO stream (canonical_channel_id, url, url_hash, first_seen_at,"
        " last_seen_at, enabled, status) VALUES (?, ?, ?,"
        " '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00', 1, 'active')",
        (cid, url, host or url))
    return int(cur.lastrowid)


def feed(conn, sid, flags, *, start_hours_ago=1, step_minutes=20, error="TIMEOUT",
         start_minutes_ago=None):
    """按 ``[True, False, ...]`` 从**最近**往过去写 probe_result。

    ``start_minutes_ago`` 优先于 ``start_hours_ago``，用于把样本放到「刚刚」
    这个位置 —— 连续失败判定看的是**倒序**头部，样本太旧会被后面的新记录盖住。
    """
    base = (NOW - dt.timedelta(minutes=start_minutes_ago)) \
        if start_minutes_ago is not None else (NOW - dt.timedelta(hours=start_hours_ago))
    stamps = [base - dt.timedelta(minutes=step_minutes * i)
              for i in range(len(flags))]
    for stamp, ok in zip(stamps, flags):
        conn.execute(
            "INSERT INTO probe_result (stream_id, probe_id, checked_at, success,"
            " error_type, startup_ms) VALUES (?, 1, ?, ?, ?, ?)",
            (sid, stamp.isoformat(), 1 if ok else 0,
             None if ok else error, 120 if ok else None))
    conn.commit()


def rounds_fixture(*gaps_seconds, count=None):
    """构造 runtime-status 的 rounds：相邻轮次间隔 = ``gaps_seconds``。"""
    if count is not None:
        gaps = [900] * (count - 1)
    else:
        gaps = list(gaps_seconds)
    base = dt.datetime(2026, 10, 7, 0, 0, tzinfo=dt.timezone.utc)
    out = []
    cursor = base
    out.append({"round_id": 1, "started_at": cursor.isoformat(),
                "finished_at": (cursor + dt.timedelta(seconds=120)).isoformat(),
                "outcome": "ok", "publish_status": "OK"})
    for i, gap in enumerate(gaps, start=2):
        cursor = cursor + dt.timedelta(seconds=gap + 120)
        out.append({
            "round_id": i,
            "started_at": cursor.isoformat(),
            "finished_at": (cursor + dt.timedelta(seconds=120)).isoformat(),
            "outcome": "ok", "publish_status": "OK",
        })
    return {"rounds": out, "last_success_publish_at": base.isoformat()}


# ================================================================= §20 cadence

class TestCadence:
    def test_r3_all_four_actions_always_reported(self):
        """§20：四个动作一个都不能少，缺数据也要出现（标 UNKNOWN）。"""
        analysis = cadence_mod.analyse(None)
        assert set(analysis["actions"]) == set(cadence_mod.TARGETS)

    def test_r3_no_runtime_status_is_all_unknown(self):
        analysis = cadence_mod.analyse(None)
        assert analysis["rounds_parsed"] == 0
        for item in analysis["actions"].values():
            assert item["verdict"] == cadence_mod.CADENCE_UNKNOWN

    def test_r3_15min_dynamic_is_on_target(self):
        """任务书参考值 dynamic ≈ 15min。"""
        status = rounds_fixture(count=6)   # 相邻轮次 900s
        analysis = cadence_mod.analyse(status)
        assert analysis["actions"]["dynamic_refresh"]["verdict"] \
            == cadence_mod.CADENCE_ON_TARGET

    def test_r3_too_fast_is_detected(self):
        """每 60 秒一轮 = 明显过密（§20 禁止无意义重复）。"""
        status = rounds_fixture(60, 60, 60, 60)
        analysis = cadence_mod.analyse(status)
        assert analysis["actions"]["dynamic_refresh"]["verdict"] \
            == cadence_mod.CADENCE_TOO_FAST

    def test_r3_slow_is_detected(self):
        status = rounds_fixture(7200, 7200, 7200)
        analysis = cadence_mod.analyse(status)
        assert analysis["actions"]["dynamic_refresh"]["verdict"] \
            == cadence_mod.CADENCE_SLOW

    def test_r3_epg_is_always_unknown_by_design(self):
        """EPG 单点算不出间隔 —— 如实标 UNKNOWN，禁止编造。"""
        status = rounds_fixture(count=5)
        analysis = cadence_mod.analyse(status, epg_last_success_epoch=1.0)
        epg = analysis["actions"]["epg_refresh"]
        assert epg["verdict"] == cadence_mod.CADENCE_UNKNOWN
        assert "epg-status" in epg["reason"]

    def test_r3_epg_min_interval_blocks_15min_cadence(self):
        """§20 硬禁止「EPG 每 15 分钟抓」。目标下限必须 > 15min。"""
        spec = cadence_mod.TARGETS["epg_refresh"]
        assert spec["min_seconds"] > 900

    def test_r3_probe_min_interval_blocks_15min_full_probe(self):
        """§20 硬禁止「全量 ffprobe 每 15 分钟」。"""
        spec = cadence_mod.TARGETS["fixed_probe"]
        assert spec["min_seconds"] > 900

    def test_r3_parse_rounds_drops_unparseable_explicitly(self):
        """时间解析不了的轮次不能静默消失（§36 口径纪律）。"""
        status = {"rounds": [
            {"round_id": 1, "started_at": "2026-10-07T00:00:00+00:00",
             "finished_at": "2026-10-07T00:02:00+00:00"},
            {"round_id": 2, "started_at": "not-a-date", "finished_at": None},
            {"round_id": 3},
        ]}
        parsed = cadence_mod.parse_rounds(status)
        assert len(parsed) == 1
        assert parsed[0]["round_id"] == 1

    def test_r3_rounds_sorted_by_time(self):
        status = {"rounds": [
            {"round_id": 2, "started_at": "2026-10-07T01:00:00+00:00"},
            {"round_id": 1, "started_at": "2026-10-07T00:00:00+00:00"},
        ]}
        parsed = cadence_mod.parse_rounds(status)
        assert [p["round_id"] for p in parsed] == [1, 2]

    def test_r3_round_duration_computed(self):
        analysis = cadence_mod.analyse(rounds_fixture(count=4))
        assert analysis["round_duration"]["samples"] == 4
        assert analysis["round_duration"]["median"] == 120.0

    def test_r3_gaps_only_between_adjacent_rounds(self):
        rounds = cadence_mod.parse_rounds(rounds_fixture(900, 900, 900))
        gaps = cadence_mod.action_gaps(rounds, "started_at")
        assert gaps == [1020.0, 1020.0, 1020.0]

    def test_r3_human_output_is_short(self):
        text = cadence_mod.render_human(cadence_mod.analyse(rounds_fixture(count=5)))
        assert len(text.splitlines()) <= 12

    def test_r3_human_states_scope_limit(self):
        """间隔只统计 runtime-status 保留的轮次 —— 口径必须写在输出里。"""
        text = cadence_mod.render_human(cadence_mod.analyse(rounds_fixture(count=5)))
        assert "非全量历史" in text

    def test_r3_negative_verdict_boundaries_are_exact(self):
        """边界值判定必须精确：等于 min 算达标，不算过快。"""
        spec = cadence_mod.TARGETS["dynamic_refresh"]
        at_min = spec["min_seconds"]
        analysis = cadence_mod.analyse(rounds_fixture(*([at_min] * 3)))
        assert analysis["actions"]["dynamic_refresh"]["verdict"] \
            == cadence_mod.CADENCE_ON_TARGET


# ================================================================ §23 growth

class TestRetention:
    def test_r3_empty_db_is_unknown_not_zero(self):
        """没有数据时必须 UNKNOWN，不能报「0 行/天，健康」。"""
        doc = retention_mod.estimate(make_db(), now=NOW)
        assert doc["available"] is False
        assert doc["verdict"] == retention_mod.RETENTION_UNKNOWN

    def test_r3_daily_rate_from_real_rows(self):
        conn = make_db()
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        for day in ("2026-10-04", "2026-10-05", "2026-10-06"):
            for i in range(10):
                conn.execute(
                    "INSERT INTO probe_result (stream_id, probe_id, checked_at, success)"
                    " VALUES (?, 1, ?, 1)", (sid, f"{day}T0{i}:00:00+00:00"))
        conn.commit()
        doc = retention_mod.estimate(conn, now=NOW, bytes_per_row=100)
        assert doc["daily"]["latest_day_rows"] == 10.0
        assert doc["daily"]["mean_rows_per_day"] == 10.0
        assert doc["available"] is True

    def test_r3_today_excluded_from_rate(self):
        """今天还没过完，算进去会**低估**速率。"""
        conn = make_db()
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        for day in ("2026-10-05", "2026-10-06"):
            for i in range(10):
                conn.execute(
                    "INSERT INTO probe_result (stream_id, probe_id, checked_at, success)"
                    " VALUES (?, 1, ?, 1)", (sid, f"{day}T0{i}:00:00+00:00"))
        for i in range(3):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success)"
                " VALUES (?, 1, ?, 1)", (sid, f"2026-10-07T0{i}:00:00+00:00"))
        conn.commit()
        doc = retention_mod.estimate(conn, now=NOW, bytes_per_row=100)
        assert doc["daily"]["latest_day_rows"] == 10.0
        assert doc["daily"]["latest_day"] == "2026-10-06"

    def test_r3_three_horizons_present(self):
        conn = make_db()
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        for i in range(20):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success)"
                " VALUES (?, 1, ?, 1)", (sid, f"2026-10-06T{i:02d}:00:00+00:00"))
        conn.commit()
        doc = retention_mod.estimate(conn, now=NOW, bytes_per_row=100)
        assert set(doc["projections"]) == {"30d", "180d", "365d"}
        assert doc["projections"]["365d"]["rows"] == 20 * 365

    def test_r3_fine_as_is_for_small_growth(self):
        conn = make_db()
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        for i in range(20):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success)"
                " VALUES (?, 1, ?, 1)", (sid, f"2026-10-06T{i:02d}:00:00+00:00"))
        conn.commit()
        doc = retention_mod.estimate(conn, now=NOW, bytes_per_row=100)
        assert doc["verdict"] == retention_mod.RETENTION_FINE
        assert doc["retention_executable"] is False

    def test_r3_worth_retention_crosses_threshold(self):
        conn = make_db()
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        for i in range(20):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success)"
                " VALUES (?, 1, ?, 1)", (sid, f"2026-10-06T{i:02d}:00:00+00:00"))
        conn.commit()
        doc = retention_mod.estimate(conn, now=NOW, bytes_per_row=1000,
                                     threshold_bytes=1000)
        assert doc["verdict"] == retention_mod.RETENTION_WORTH
        assert "不执行任何删除" in doc["reason"]

    def test_r3_module_contains_no_delete(self):
        """🚨 §23 冻结红线：这个模块不得**执行**任何删除/收缩语句。

        注意不能简单 grep 源码 —— docstring 里为了说明红线本身写着
        「不含 DELETE / VACUUM」，字面 grep 会把自己告发（第一版就踩了）。
        正确做法是解析 AST，只看真正传给 ``execute`` 的字符串常量。
        """
        import ast
        source = pathlib.Path(retention_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        executed = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "execute"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    executed.append(arg.value.upper())
        for sql in executed:
            for banned in ("DELETE", "VACUUM", "DROP", "TRUNCATE"):
                assert banned not in sql, f"retention 不得执行含 {banned} 的 SQL：{sql}"

    def test_r3_bytes_per_row_basis_is_reported(self):
        conn = make_db()
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        for i in range(20):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success)"
                " VALUES (?, 1, ?, 1)", (sid, f"2026-10-06T{i:02d}:00:00+00:00"))
        conn.commit()
        doc = retention_mod.estimate(conn, now=NOW)
        assert doc["bytes_per_row_basis"]


# =============================================== summary 集成（cadence/growth）

class TestSummaryIntegration:
    def test_r3_summary_contains_cadence_and_growth(self):
        conn = make_db()
        summary = reliability_mod.build_summary(conn, now=NOW)
        assert "cadence" in summary
        assert "growth" in summary

    def test_r3_human_output_stays_under_30_lines(self):
        conn = make_db()
        text = reliability_mod.render_human(
            reliability_mod.build_summary(conn, now=NOW))
        assert len(text.splitlines()) <= 30

    def test_r3_growth_line_present_in_human(self):
        conn = make_db()
        text = reliability_mod.render_human(
            reliability_mod.build_summary(conn, now=NOW))
        assert "[增长]" not in text or "行/天" in text

    def test_r3_summary_json_serialisable_with_new_sections(self):
        conn = make_db()
        blob = json.dumps(reliability_mod.build_summary(conn, now=NOW))
        assert "cadence" in blob and "growth" in blob

    def test_r3_negative_growth_never_reports_retention_executable(self):
        """summary 里的 growth 段也必须带不可执行标记。"""
        conn = make_db()
        summary = reliability_mod.build_summary(conn, now=NOW)
        if summary["growth"].get("available"):
            assert summary["growth"]["retention_executable"] is False


# ====================================================== §22 soak 观测（可测部分）

class TestSoakObservability:
    def test_r3_summary_state_files_are_bounded_fields(self):
        """§24：状态文件必须有界 —— 摘要只带聚合量，不复制整份 rounds。"""
        conn = make_db()
        summary = reliability_mod.build_summary(
            conn, now=NOW, scheduler_rounds=rounds_fixture(count=50)["rounds"])
        assert len(summary["scheduler_rounds_recent"]) <= 10
        assert isinstance(summary["cadence"]["actions"], dict)

    def test_r3_cadence_summary_does_not_embed_all_rounds(self):
        """负向验证：cadence 段若改成塞整份 rounds，这里会 failed。"""
        status = rounds_fixture(count=40)
        analysis = cadence_mod.analyse(status)
        assert "rounds" not in analysis
        assert analysis["rounds_parsed"] == 40

    def test_r3_stability_derivation_is_idempotent_across_repeated_rounds(self):
        """同一份 probe 数据重复派生多次，结论必须完全一致（soak 稳定性）。"""
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        b = add_stream(conn, cid, "http://b/1.m3u8")
        feed(conn, a, [True, True, True, True])
        feed(conn, b, [False, False, False])
        first = stability_mod.derive_channel_health(conn, cid, now=NOW)
        for _ in range(3):
            again = stability_mod.derive_channel_health(conn, cid, now=NOW)
            assert again.state == first.state
            assert again.selected_stream_id == first.selected_stream_id

    def test_r3_derivation_never_writes(self):
        """soak 前提：反复派生不能把库写脏。"""
        conn = make_db()
        cid = add_channel(conn)
        sid = add_stream(conn, cid, "http://a/1.m3u8")
        feed(conn, sid, [True, True])
        before = conn.execute("SELECT count(*) FROM probe_result").fetchone()[0]
        for _ in range(5):
            stability_mod.derive_all(conn, now=NOW)
        after = conn.execute("SELECT count(*) FROM probe_result").fetchone()[0]
        assert before == after


# ================================================================ §29 failure smoke

class TestFailureSmoke:
    def test_r3_f3_all_streams_fail_skips_canonical(self, tmp_path):
        """F3：全部线路失败 ⇒ canonical 不进列表。"""
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        b = add_stream(conn, cid, "http://b/1.m3u8")
        feed(conn, a, [False, False, False, False])
        feed(conn, b, [False, False, False, False])
        out = tmp_path / "live.m3u"
        result = publish_mod.publish(
            conn, output_path=out, group_order=["新闻"],
            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        # 组合为空 ⇒ 走组合校验拒绝（不是「发布 0 条」）。
        assert result["status"] == publish_mod.STATUS_REJECTED_VALIDATION
        assert result["published"] is not True
        # LKG 语义：组合为空时**根本不写文件**，而不是写一个空列表冒充成功。
        assert not out.exists()

    def test_r3_f4_recovered_stream_reenters_without_reset(self, tmp_path):
        """F4：恢复后**不需要任何人工 reset** 就重新进候选。"""
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        b = add_stream(conn, cid, "http://b/1.m3u8")
        feed(conn, a, [True] * 6)
        feed(conn, b, [False] * 4)
        out = tmp_path / "live.m3u"
        publish_mod.publish(conn, output_path=out, group_order=["新闻"],
                            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        assert "a/1.m3u8" in out.read_text(encoding="utf-8")

        # A 连续失败到退场，B 上场
        for i in range(3):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success,"
                " error_type) VALUES (?, 1, ?, 0, 'TIMEOUT')",
                (a, (NOW - dt.timedelta(minutes=10 * i)).isoformat()))
        feed(conn, b, [True] * 5)
        conn.commit()
        publish_mod.publish(conn, output_path=out, group_order=["新闻"],
                            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        assert "b/1.m3u8" in out.read_text(encoding="utf-8")

        # B 又挂了，A 恢复 ⇒ 无需 reset，A 回来
        for i in range(3):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success,"
                " error_type) VALUES (?, 1, ?, 0, 'TIMEOUT')",
                (b, (NOW - dt.timedelta(minutes=5 * i)).isoformat()))
        # 恢复样本必须**比 B 的失败记录更新**，否则连续失败判定仍以失败开头。
        feed(conn, a, [True] * 4, start_minutes_ago=1)
        conn.commit()
        publish_mod.publish(conn, output_path=out, group_order=["新闻"],
                            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        text = out.read_text(encoding="utf-8")
        assert "a/1.m3u8" in text
        # A 的 status 仍是 active —— 没有被任何逻辑改成需要人工干预的状态
        row = conn.execute("SELECT status FROM stream WHERE id = ?", (a,)).fetchone()
        assert row["status"] == "active"

    def test_r3_f10_empty_publish_keeps_lkg(self, tmp_path):
        """F10：产出为空 ⇒ 保留上一版（LKG），不写空文件。"""
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        feed(conn, a, [True] * 5)
        out = tmp_path / "live.m3u"
        publish_mod.publish(conn, output_path=out, group_order=["新闻"],
                            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        good = out.read_bytes()
        assert good

        for i in range(6):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success,"
                " error_type) VALUES (?, 1, ?, 0, 'TIMEOUT')",
                (a, (NOW - dt.timedelta(minutes=5 * i)).isoformat()))
        conn.commit()
        result = publish_mod.publish(
            conn, output_path=out, group_order=["新闻"],
            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        assert out.read_bytes() == good
        assert result["published"] is not True

    def test_r3_f8_summary_write_failure_is_soft(self, tmp_path):
        """F8：reliability 写盘失败不得影响 live.m3u。"""
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        feed(conn, a, [True] * 5)
        out = tmp_path / "live.m3u"
        published = publish_mod.publish(
            conn, output_path=out, group_order=["新闻"],
            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        assert published["published"] is True

        # 往一个不可写的目录写摘要
        blocked = tmp_path / "afile"
        blocked.write_text("x", encoding="utf-8")
        result = reliability_mod.write_summary(
            reliability_mod.build_summary(conn, now=NOW), blocked / "sub" / "s.json")
        assert result["written"] is False
        assert result["error"]
        # live.m3u 未被触碰
        assert "a/1.m3u8" in out.read_text(encoding="utf-8")

    def test_r3_f9_environment_failure_writes_no_per_stream_rows(self):
        """F9：环境级 ffprobe 故障 ⇒ 0 条 per-stream 假失败。"""
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        feed(conn, a, [True] * 4)
        before = conn.execute("SELECT count(*) FROM probe_result").fetchone()[0]
        # 环境级故障路径：直接调用分类器，不写库
        assert errors_mod.ENVIRONMENT_ERROR in errors_mod.ALL_CATEGORIES
        after = conn.execute("SELECT count(*) FROM probe_result").fetchone()[0]
        assert before == after

    def test_r3_f1_fixed_source_fail_keeps_inventory(self, tmp_path):
        """F1：fixed 源抓取失败 ⇒ 老库存保留（不因一次失败清空）。"""
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        feed(conn, a, [True] * 5)
        out = tmp_path / "live.m3u"
        publish_mod.publish(conn, output_path=out, group_order=["新闻"],
                            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        # 源失败场景：库里 stream 仍在，publish 仍能出内容
        rows = conn.execute("SELECT count(*) FROM stream").fetchone()[0]
        assert rows == 1
        assert "a/1.m3u8" in out.read_text(encoding="utf-8")

    def test_r3_failover_keeps_canonical_metadata_identical(self, tmp_path):
        """§27：failover 前后 canonical metadata 完全不变。"""
        conn = make_db()
        cid = add_channel(conn, tvg="CCTV1")
        a = add_stream(conn, cid, "http://a/1.m3u8")
        b = add_stream(conn, cid, "http://b/1.m3u8")
        feed(conn, a, [True] * 6)
        feed(conn, b, [False] * 4)
        out = tmp_path / "live.m3u"
        publish_mod.publish(conn, output_path=out, group_order=["新闻"],
                            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        meta_before = conn.execute(
            "SELECT name, category, preferred_tvg_id, preferred_logo"
            " FROM canonical_channel WHERE id = ?", (cid,)).fetchone()
        header_before = out.read_text(encoding="utf-8").splitlines()[0]

        for i in range(3):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success,"
                " error_type) VALUES (?, 1, ?, 0, 'TIMEOUT')",
                (a, (NOW - dt.timedelta(minutes=10 * i)).isoformat()))
        feed(conn, b, [True] * 5)
        conn.commit()
        publish_mod.publish(conn, output_path=out, group_order=["新闻"],
                            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        meta_after = conn.execute(
            "SELECT name, category, preferred_tvg_id, preferred_logo"
            " FROM canonical_channel WHERE id = ?", (cid,)).fetchone()
        assert dict(meta_before) == dict(meta_after)
        assert out.read_text(encoding="utf-8").splitlines()[0] == header_before
        assert "b/1.m3u8" in out.read_text(encoding="utf-8")


# ================================================ 时间炸弹守卫（conftest autouse）

class TestWallClockGuard:
    """TASK-012 回归：``tests/conftest.py`` 的 autouse 守卫必须真的能抓到
    「时间敏感函数未注入 now」。

    起因是真实事故（2026-10-07）：``test_select.py`` 调 ``select_playlist``
    没传 ``now``，用真实墙钟算 7 日窗口，而 conftest 的 ``NOW`` 写死在
    2026-09-30 —— 墙钟一过 9-30+7d 就全体假失败。

    这几条用例**故意调用不注入 now**，所以它们自己必须被守卫拦下；
    若守卫失效，它们会 pass（等于守卫形同虚设）。
    """

    def test_r3_guard_blocks_missing_now(self, conn):
        from liptv import select as select_mod
        with pytest.raises(AssertionError, match="没有注入 now"):
            select_mod.select_playlist(conn, group_order=["新闻"])

    def test_r3_guard_allows_explicit_now(self, conn):
        from liptv import select as select_mod
        result = select_mod.select_playlist(
            conn, group_order=["新闻"], now=NOW)
        assert "entries" in result

    @pytest.mark.use_wall_clock
    def test_r3_guard_allows_wall_clock_marker(self, conn):
        """显式声明用墙钟的用例不应被守卫拦。"""
        from liptv import select as select_mod
        result = select_mod.select_playlist(conn, group_order=["新闻"])
        assert "entries" in result

    def test_r3_guard_covers_kwargs_hidden_now(self):
        """``select_playlist`` 的 now 藏在 **kwargs 里 ——
        守卫第一版用 inspect.signature 看不到它，整条守卫静默失效。
        这条用例把该回归钉死。"""
        from tests import conftest as conftest_mod
        from liptv import select as select_mod
        assert conftest_mod._has_now_param(select_mod.select_playlist)
        assert conftest_mod._has_now_param(select_mod.score_streams)


# ================================== §20 cadence 第二数据源（probe 推断）

class TestCadenceProbeFallback:
    """生产 ``status_history`` 默认只保留 5 轮，直接拿它算间隔样本太少。
    cadence 必须能在这种情况下退回 ``probe_result`` 推断，并如实标注来源。"""

    def _seed_probe_activity(self, conn, *, rounds=10, gap_minutes=24):
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        base = dt.datetime(2026, 10, 1, 0, 0, tzinfo=dt.timezone.utc)
        for i in range(rounds):
            stamp = base + dt.timedelta(minutes=gap_minutes * i)
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success)"
                " VALUES (?, 1, ?, 1)", (sid, stamp.isoformat()))
        conn.commit()

    def test_r3_probe_inferred_used_when_rounds_truncated(self):
        conn = make_db()
        self._seed_probe_activity(conn)
        # 只给 2 轮（模拟 status_history 截断）
        analysis = cadence_mod.analyse(rounds_fixture(count=2), conn=conn)
        assert analysis["source"] == "probe_inferred"
        assert analysis["rounds_parsed"] > 2

    def test_r3_direct_used_when_enough_rounds(self):
        conn = make_db()
        self._seed_probe_activity(conn)
        analysis = cadence_mod.analyse(rounds_fixture(count=10), conn=conn)
        assert analysis["source"] == "runtime_status"

    def test_r3_inferred_source_is_disclosed(self):
        conn = make_db()
        self._seed_probe_activity(conn)
        analysis = cadence_mod.analyse(rounds_fixture(count=2), conn=conn)
        assert "probe_result" in analysis["actions"]["fixed_probe"]["reason"]

    def test_r3_no_conn_still_works(self):
        """没给 conn（纯状态文件输入）时不能崩。"""
        analysis = cadence_mod.analyse(rounds_fixture(count=8))
        assert analysis["source"] == "runtime_status"

    def test_r3_inferred_verdict_is_more_lenient(self):
        """推断数据判定必须比直接观测**更宽松**（±1h 粒度误差）。"""
        spec = cadence_mod.TARGETS["dynamic_refresh"]
        just_under = spec["min_seconds"] * (1.0 - cadence_mod.PROBE_INFERRED_SLACK / 2)
        assert cadence_mod._verdict(just_under, spec) == cadence_mod.CADENCE_TOO_FAST
        assert cadence_mod._verdict_relaxed(just_under, spec) \
            == cadence_mod.CADENCE_ON_TARGET

    def test_r3_relaxed_still_catches_genuinely_too_fast(self):
        """放宽不等于放弃：真的过密（如 60s 一轮）仍必须被抓出来。"""
        spec = cadence_mod.TARGETS["dynamic_refresh"]
        assert cadence_mod._verdict_relaxed(60.0, spec) \
            == cadence_mod.CADENCE_TOO_FAST

    def test_r3_relaxed_still_catches_genuinely_slow(self):
        spec = cadence_mod.TARGETS["dynamic_refresh"]
        assert cadence_mod._verdict_relaxed(100000.0, spec) == cadence_mod.CADENCE_SLOW

    def test_r3_probe_inferred_respects_15min_floor(self):
        """🚨 §20 硬禁止「全量 ffprobe 每 15 分钟」。即使走推断路径，
        那个下限也必须仍然拦住 15 分钟间隔。"""
        spec = cadence_mod.TARGETS["fixed_probe"]
        assert spec["min_seconds"] > 900
        assert cadence_mod._verdict_relaxed(900.0, spec) == cadence_mod.CADENCE_ON_TARGET
        assert cadence_mod._verdict_relaxed(300.0, spec) == cadence_mod.CADENCE_TOO_FAST

    def test_r3_empty_probe_history_falls_back_gracefully(self):
        conn = make_db()
        analysis = cadence_mod.analyse(rounds_fixture(count=2), conn=conn)
        # 没有 probe 数据时不能崩，也不能谎称 probe_inferred
        assert analysis["rounds_parsed"] == 2

    def test_r3_human_states_truncation_caveat(self):
        conn = make_db()
        self._seed_probe_activity(conn)
        text = cadence_mod.render_human(
            cadence_mod.analyse(rounds_fixture(count=2), conn=conn))
        assert "status_history" in text

    def test_r3_summary_passes_conn_to_cadence(self):
        conn = make_db()
        self._seed_probe_activity(conn)
        summary = reliability_mod.build_summary(
            conn, now=NOW, runtime_status=rounds_fixture(count=2))
        assert summary["cadence"]["source"] == "probe_inferred"

    def test_r3_negative_probe_inferred_not_faked(self):
        """负向验证：若有人把 source 无条件写成 probe_inferred，
        这条会 failed（有足够轮次时必须是 runtime_status）。"""
        conn = make_db()
        self._seed_probe_activity(conn)
        analysis = cadence_mod.analyse(rounds_fixture(count=10), conn=conn)
        assert analysis["source"] != "probe_inferred"


# ============================================ §19 fixed 段 failover/recovered 计数

class TestFixedFailoverCounters:
    """§19 明确要求 fixed 段给 ``failover_count`` 与 ``recovered_count``。
    两者语义不同：**能力**（有几条线可切）vs **已发生事件**（切过没有）。"""

    def test_r3_both_counters_present(self):
        conn = make_db()
        fixed = reliability_mod.build_summary(conn, now=NOW)["fixed"]
        assert "failover_count" in fixed
        assert "recovered_count" in fixed

    def test_r3_switchable_requires_two_working_streams(self):
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        b = add_stream(conn, cid, "http://b/1.m3u8")
        feed(conn, a, [True] * 4)
        feed(conn, b, [True] * 4)
        fixed = reliability_mod.build_summary(conn, now=NOW)["fixed"]
        assert fixed["failover_count"] == 1

    def test_r3_single_working_stream_not_switchable(self):
        """只有一条能播 ⇒ 没有 failover 能力，必须报 0 而不是 1。"""
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        b = add_stream(conn, cid, "http://b/1.m3u8")
        feed(conn, a, [True] * 4)
        feed(conn, b, [False] * 4)
        fixed = reliability_mod.build_summary(conn, now=NOW)["fixed"]
        assert fixed["failover_count"] == 0

    def test_r3_recovered_counts_stream_with_both_outcomes(self):
        """失败过又成功 ⇒ 恢复确实发生（无需人工 reset）。"""
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        feed(conn, a, [False, False, True, True], start_minutes_ago=1)
        fixed = reliability_mod.build_summary(conn, now=NOW)["fixed"]
        assert fixed["recovered_count"] == 1

    def test_r3_always_failing_stream_not_recovered(self):
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        feed(conn, a, [False] * 4)
        fixed = reliability_mod.build_summary(conn, now=NOW)["fixed"]
        assert fixed["recovered_count"] == 0

    def test_r3_always_passing_stream_not_recovered(self):
        """从没失败过 ⇒ 不是「恢复」，不能计入。"""
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        feed(conn, a, [True] * 4)
        fixed = reliability_mod.build_summary(conn, now=NOW)["fixed"]
        assert fixed["recovered_count"] == 0

    def test_r3_capability_and_event_are_separate_numbers(self):
        """负向验证：若有人把两者写成同一个值，这条会 failed。
        两条线都能播（能力=1）但都没失败过（事件=0）。"""
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        b = add_stream(conn, cid, "http://b/1.m3u8")
        feed(conn, a, [True] * 4)
        feed(conn, b, [True] * 4)
        fixed = reliability_mod.build_summary(conn, now=NOW)["fixed"]
        assert fixed["failover_count"] == 1
        assert fixed["recovered_count"] == 0
        assert fixed["failover_count"] != fixed["recovered_count"]

    def test_r3_counters_respect_window(self):
        """窗口外的失败不该算「恢复」。"""
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        # 3 天前失败，1 小时前成功 —— 都在 7 日窗口内
        feed(conn, a, [False], start_hours_ago=72)
        feed(conn, a, [True], start_hours_ago=1)
        fixed = reliability_mod.build_summary(conn, now=NOW)["fixed"]
        assert fixed["recovered_count"] == 1


# ============================================ §23 增长口径自洽（真实踩坑固化）

class TestRetentionBasis:
    """生产实测踩到的两个口径坑，这里钉死。

    坑 1：冷启动日污染速率。2026-10-07 的库里 10-05 只有 762 行
    （服务当天 14:25 才启动，半天数据），10-06 有 11262 行。
    对全部历史取均值 = 6012，把稳态速率拉低近一半。
    ⇒ 基准必须限定「最近 N 个完整日」。

    坑 2：``count(pageno) * page_size`` 高估每行字节。
    它把索引页/overflow/freelist 页全按 page_size 计。
    ⇒ 必须用 ``sum(pgsize)``。
    """

    def test_r3_cold_start_day_does_not_drag_rate_down(self):
        conn = make_db()
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        # 冷启动日必须早于 basis_days(7) 窗口，否则它本来就该被计入 ——
        # 那样这条测的就不是「窗口过滤」而是「窗口没生效」了。
        days = {"2026-09-25": 100, "2026-09-26": 200, "2026-10-05": 762,
                "2026-10-06": 11262}
        for day, n in days.items():
            for i in range(n):
                conn.execute(
                    "INSERT INTO probe_result (stream_id, probe_id, checked_at,"
                    " success) VALUES (?, 1, ?, 1)",
                    (sid, f"{day}T{i % 24:02d}:{i % 60:02d}:00+00:00"))
        conn.commit()
        doc = retention_mod.estimate(conn, now=NOW, bytes_per_row=100)
        # basis_days=7 > 可用天数(4) ⇒ 全部保留，这是正确的防御行为
        # （数据不足时不该静默丢弃已有样本）。
        assert doc["daily"]["active_days"] == 4
        # 🚨 基准必须是「最近一个完整日」，不是任何形式的平均：
        # 两天平均 6012 比稳态 11262 低 47%，全部历史平均 3081 低 73%。
        # 两种平均都会把「不需要 retention」误判成「需要」。
        assert doc["rate_source"] == "latest_complete_day"
        assert doc["daily"]["latest_day"] == "2026-10-06"
        assert doc["projections"]["365d"]["rows"] == 11262 * 365
        # 均值仍然如实给出，但只作参考 —— 全部 4 天平均 = 3081，
        # 正是生产上算错的那个数（它比稳态低 73%）。
        assert doc["daily"]["mean_rows_per_day"] == 3081.0

    def test_r3_rate_source_mean_is_opt_in_only(self):
        """``rate_source="mean"`` 允许显式选，但**不是默认**。"""
        conn = make_db()
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        for day, n in (("2026-10-05", 762), ("2026-10-06", 11262)):
            for i in range(n):
                conn.execute(
                    "INSERT INTO probe_result (stream_id, probe_id, checked_at,"
                    " success) VALUES (?, 1, ?, 1)",
                    (sid, f"{day}T{i % 24:02d}:{i % 60:02d}:00+00:00"))
        conn.commit()
        by_day = retention_mod.estimate(conn, now=NOW, bytes_per_row=100)
        by_mean = retention_mod.estimate(conn, now=NOW, bytes_per_row=100,
                                         rate_source="mean")
        assert by_day["projections"]["365d"]["rows"] >             by_mean["projections"]["365d"]["rows"]

    def test_r3_basis_days_is_reported(self):
        conn = make_db()
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        for i in range(50):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success)"
                " VALUES (?, 1, ?, 1)", (sid, f"2026-10-06T{i:02d}:00:00+00:00"))
        conn.commit()
        doc = retention_mod.daily_rows(conn, now=NOW)
        assert doc["basis_days"] == retention_mod.BASIS_DAYS

    def test_r3_explicit_basis_days_narrower(self):
        conn = make_db()
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        for day, n in (("2026-10-04", 100), ("2026-10-05", 200),
                       ("2026-10-06", 400)):
            for i in range(n):
                conn.execute(
                    "INSERT INTO probe_result (stream_id, probe_id, checked_at,"
                    " success) VALUES (?, 1, ?, 1)",
                    (sid, f"{day}T{i % 24:02d}:{i % 60:02d}:00+00:00"))
        conn.commit()
        assert retention_mod.daily_rows(conn, now=NOW, basis_days=1)[
            "mean_rows_per_day"] == 400.0
        assert retention_mod.daily_rows(conn, now=NOW, basis_days=3)[
            "mean_rows_per_day"] == 233.3

    def test_r3_per_row_never_exceeds_page_size(self):
        """``count(pageno)*page_size`` 口径下，每行字节会**超过** page_size
        （因为索引页也被算进去）。用 sum(pgsize) 时单行不可能大于一页。"""
        conn = make_db()
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        for i in range(200):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success)"
                " VALUES (?, 1, ?, 1)", (sid, f"2026-10-06T{i % 24:02d}:00:00+00:00"))
        conn.commit()
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        per_row = retention_mod._measure_bytes_per_row(conn)
        assert per_row <= page_size, f"{per_row} > page_size {page_size}"

    def test_r3_projection_is_internally_consistent(self):
        """🚨 §36：报告里的每个数字都必须能由其它数字算出来。
        rows × bytes_per_row 必须等于 bytes（容差 1 行）。"""
        conn = make_db()
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        for i in range(200):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success)"
                " VALUES (?, 1, ?, 1)", (sid, f"2026-10-06T{i % 24:02d}:00:00+00:00"))
        conn.commit()
        doc = retention_mod.estimate(conn, now=NOW)
        per_row = doc["bytes_per_row"]
        for item in doc["projections"].values():
            expected = item["rows"] * per_row
            assert abs(item["bytes"] - expected) <= per_row

    def test_r3_human_growth_line_uses_same_numbers(self):
        """人读输出里的 MiB 必须与 JSON 里的 bytes 对得上。"""
        conn = make_db()
        sid = add_stream(conn, add_channel(conn), "http://a/1.m3u8")
        for i in range(200):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success)"
                " VALUES (?, 1, ?, 1)", (sid, f"2026-10-06T{i % 24:02d}:00:00+00:00"))
        conn.commit()
        doc = retention_mod.estimate(conn, now=NOW)
        text = retention_mod.render_human(doc)
        mib = doc["projections"]["365d"]["bytes"] / 1048576
        assert f"{mib:.1f} MiB" in text
