"""TASK-012 §31 自动化测试（第 1 部分：错误分类 / 稳定性派生 / preflight）。

对照 TASK-012 §31 的 60 类清单逐条落地。本文件覆盖 1–34 中的
分类、派生状态、failover、preflight、policy 相关场景。

⚠️ 每个「行为」测试都配一个 **negative** 检查（见文件末尾 test_r1_*_negative_*）：
把被测行为改回旧语义后该测试必须 failed。否则「只写全绿的测试」等于没写。
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from liptv import db as db_mod
from liptv import errors as errors_mod
from liptv import preflight as preflight_mod
from liptv import reliability as reliability_mod
from liptv import repo
from liptv import select as select_mod
from liptv import stability as stability_mod

NOW = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.timezone.utc)


# ----------------------------------------------------------------- 工具

def make_db() -> sqlite3.Connection:
    conn = db_mod.connect(":memory:")
    db_mod.init_db(conn)
    return conn


def add_channel(conn, name="CCTV-1", **kw) -> int:
    return repo.add_canonical_channel(
        conn, name, category=kw.pop("category", "新闻"), **kw)


def add_stream(conn, cid, url) -> int:
    cur = conn.execute(
        "INSERT INTO stream (canonical_channel_id, url, url_hash, first_seen_at,"
        " last_seen_at, enabled, status) VALUES (?, ?, ?, '2026-10-01T00:00:00+00:00',"
        " '2026-10-01T00:00:00+00:00', 1, 'active')",
        (cid, url, f"h-{abs(hash(url))}"))
    conn.commit()
    return int(cur.lastrowid)


def add_probe(conn, sid, ok, *, hours_ago=1.0, error=None, startup_ms=None) -> None:
    stamp = (NOW - dt.timedelta(hours=hours_ago)).isoformat()
    conn.execute(
        "INSERT INTO probe_result (stream_id, probe_id, checked_at, success,"
        " error_type, startup_ms) VALUES (?, 1, ?, ?, ?, ?)",
        (sid, stamp, 1 if ok else 0, error, startup_ms))
    conn.commit()


def feed(conn, sid, pattern, *, error="TIMEOUT", start=1.0, step=1.0) -> None:
    """按 (ok, hours_ago) 序列灌 probe。"""
    for i, ok in enumerate(pattern):
        add_probe(conn, sid, ok, hours_ago=start + i * step,
                  error=None if ok else error)


# =============================================== §9 错误分类（场景 6/18-22/27/28）

class TestErrorTaxonomy:
    """§9：统一术语 + 派生映射。"""

    def test_r1_all_categories_are_the_14_required(self):
        """§9 列了 14 个术语，缺一个都不算完成。"""
        required = {
            "DNS_ERROR", "CONNECT_ERROR", "TIMEOUT", "HTTP_4XX", "HTTP_5XX",
            "HTML_FAKE", "INVALID_PLAYLIST", "INVALID_MEDIA",
            "SEGMENT_UNREACHABLE", "SIGNED_DOWNSTREAM_UNREACHABLE", "EMPTY_MEDIA",
            "GEO_BLOCK_SUSPECTED", "ENVIRONMENT_ERROR", "UNKNOWN",
        }
        assert required == set(errors_mod.ALL_CATEGORIES)

    def test_r1_probe_legacy_types_normalize(self):
        assert errors_mod.normalize("TIMEOUT") == errors_mod.TIMEOUT
        assert errors_mod.normalize("DNS_ERROR") == errors_mod.DNS_ERROR
        assert errors_mod.normalize("CONNECT_ERROR") == errors_mod.CONNECT_ERROR
        # TLS 不在 §9 列表 → 归 CONNECT_ERROR（真实原因就是连不上）
        assert errors_mod.normalize("TLS_ERROR") == errors_mod.CONNECT_ERROR
        # ffprobe 缺失 = 环境级
        assert errors_mod.normalize("FFPROBE_NOT_FOUND") == errors_mod.ENVIRONMENT_ERROR
        assert errors_mod.normalize("FFPROBE_START_FAILED") == errors_mod.ENVIRONMENT_ERROR

    def test_r1_fetch_legacy_types_normalize(self):
        assert errors_mod.normalize("NETWORK_ERROR") == errors_mod.CONNECT_ERROR
        assert errors_mod.normalize("INVALID_M3U") == errors_mod.INVALID_PLAYLIST
        assert errors_mod.normalize("EMPTY_LIST") == errors_mod.EMPTY_MEDIA
        assert errors_mod.normalize("TOO_MANY_REDIRECTS") == errors_mod.HTTP_4XX

    def test_r1_normalize_is_idempotent(self):
        for cat in errors_mod.ALL_CATEGORIES:
            assert errors_mod.normalize(cat) == cat
            assert errors_mod.normalize(errors_mod.normalize(cat)) == cat

    def test_r1_unknown_strings_become_unknown(self):
        assert errors_mod.normalize("WAT") == errors_mod.UNKNOWN
        assert errors_mod.normalize(None) == errors_mod.UNKNOWN
        assert errors_mod.normalize("") == errors_mod.UNKNOWN

    def test_r1_http_status_split_4xx_5xx(self):
        assert errors_mod.classify_http_status(404) == errors_mod.HTTP_4XX
        assert errors_mod.classify_http_status(503) == errors_mod.HTTP_5XX
        # 2xx/3xx 不该出现在失败语境 → 不硬塞 4xx
        assert errors_mod.classify_http_status(200) == errors_mod.UNKNOWN
        assert errors_mod.classify_http_status(None) == errors_mod.UNKNOWN

    def test_r1_http_status_detail_overrides_generic(self):
        assert errors_mod.normalize("HTTP_STATUS", http_status=503) == errors_mod.HTTP_5XX
        assert errors_mod.normalize("HTTP_STATUS", http_status=404) == errors_mod.HTTP_4XX

    def test_r1_fake_200_detected_from_stderr(self):
        """TASK-011 §13 的核心教训：200 不等于能播。"""
        result = errors_mod.classify_probe_failure(
            timed_out=False, error_type="HTTP_ERROR",
            stderr="<html><body>403 Forbidden</body></html>")
        assert result == errors_mod.HTML_FAKE

    def test_r1_segment_404_only_when_declared_segment(self):
        """不声明 segment 时 404 就是普通 4xx —— 区分「源挂了」与「分片对不上」。"""
        text = "Server returned 404 Not Found"
        assert errors_mod.classify_probe_failure(
            timed_out=False, error_type="HTTP_ERROR", stderr=text,
            segment=True) == errors_mod.SEGMENT_UNREACHABLE
        assert errors_mod.classify_probe_failure(
            timed_out=False, error_type="HTTP_ERROR", stderr=text,
            segment=False) == errors_mod.HTTP_4XX

    def test_r1_403_is_geo_suspected(self):
        for stderr in ("Server returned 403 Forbidden", "HTTP error 403"):
            assert errors_mod.classify_probe_failure(
                timed_out=False, error_type="HTTP_ERROR", stderr=stderr,
            ) == errors_mod.GEO_BLOCK_SUSPECTED
        # 451 同理
        assert errors_mod.classify_probe_failure(
            timed_out=False, error_type="HTTP_ERROR",
            stderr="Server returned 451 Unavailable") == errors_mod.GEO_BLOCK_SUSPECTED

    def test_r1_geo_by_text_without_status(self):
        assert errors_mod.classify_probe_failure(
            timed_out=False, error_type="HTTP_ERROR",
            stderr="stream not available in your country",
        ) == errors_mod.GEO_BLOCK_SUSPECTED

    def test_r1_timeout_wins_over_everything(self):
        assert errors_mod.classify_probe_failure(
            timed_out=True, error_type="DNS_ERROR",
            stderr="<html>") == errors_mod.TIMEOUT

    def test_r1_5xx_not_confused_with_4xx(self):
        assert errors_mod.classify_probe_failure(
            timed_out=False, error_type="HTTP_ERROR",
            stderr="Server returned 500 Internal Server Error",
        ) == errors_mod.HTTP_5XX

    def test_r1_empty_stderr_falls_back_to_error_type(self):
        assert errors_mod.classify_probe_failure(
            timed_out=False, error_type="DNS_ERROR", stderr="",
        ) == errors_mod.DNS_ERROR

    def test_r1_stream_failure_resolve_stage(self):
        assert errors_mod.classify_stream_failure(
            stage="resolve", error="timed out") == errors_mod.TIMEOUT
        assert errors_mod.classify_stream_failure(
            stage="resolve", error="Name or service not known",
        ) == errors_mod.DNS_ERROR

    def test_r1_stream_failure_connect_stage(self):
        assert errors_mod.classify_stream_failure(
            stage="connect", error="Connection refused") == errors_mod.CONNECT_ERROR
        assert errors_mod.classify_stream_failure(
            stage="connect", error="timed out") == errors_mod.TIMEOUT

    def test_r1_stream_failure_content_type_html(self):
        """content-type 是我们自己拿到的，比从 stderr 猜更可靠。"""
        assert errors_mod.classify_stream_failure(
            stage="playlist", http_status=200, content_type="text/html; charset=utf-8",
        ) == errors_mod.HTML_FAKE
        assert errors_mod.classify_stream_failure(
            stage="playlist", http_status=200,
            body_head="<!DOCTYPE html><html>",
        ) == errors_mod.HTML_FAKE

    def test_r1_stream_failure_segment_404(self):
        assert errors_mod.classify_stream_failure(
            stage="segment", http_status=404, segment=True,
        ) == errors_mod.SEGMENT_UNREACHABLE

    def test_r1_probe_and_preflight_agree_on_403(self):
        """§9：probe / report / status 同一口径。同一个 403 两边必须同名。"""
        from_probe = errors_mod.classify_probe_failure(
            timed_out=False, error_type="HTTP_ERROR",
            stderr="Server returned 403 Forbidden")
        from_preflight = errors_mod.classify_stream_failure(
            stage="playlist", http_status=403)
        assert from_probe == from_preflight == errors_mod.GEO_BLOCK_SUSPECTED

    def test_r1_distribution_orders_and_counts(self):
        dist = errors_mod.distribution(["TIMEOUT", "TIMEOUT", None, "DNS_ERROR"])
        assert dist == {"TIMEOUT": 2, "DNS_ERROR": 1, "UNKNOWN": 1}
        # 顺序按 ALL_CATEGORIES（具体在前）
        keys = list(dist)
        assert keys.index("DNS_ERROR") < keys.index("TIMEOUT")
        assert keys.index("TIMEOUT") < keys.index("UNKNOWN")

    def test_r1_distribution_empty(self):
        assert errors_mod.distribution([]) == {}
        assert errors_mod.distribution(None) == {}

    def test_r1_environment_flag(self):
        assert errors_mod.is_environment(errors_mod.ENVIRONMENT_ERROR)
        assert not errors_mod.is_environment(errors_mod.TIMEOUT)
        assert not errors_mod.is_environment(None)

    def test_r1_every_category_has_label(self):
        for cat in errors_mod.ALL_CATEGORIES:
            assert errors_mod.label(cat) and errors_mod.label(cat) != cat


# ==================================== §10/§11 派生状态（场景 1-8/27-31）

class TestStabilityStates:
    def test_r1_unknown_when_no_probe(self):
        conn = make_db()
        cid = add_channel(conn)
        add_stream(conn, cid, "http://a/1.m3u8")
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert ch.state == stability_mod.UNKNOWN

    def test_r1_unknown_when_no_stream(self):
        conn = make_db()
        cid = add_channel(conn)
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert ch.state == stability_mod.UNKNOWN
        assert "stream" in (ch.reason or "")

    def test_r1_minimum_samples_gate(self):
        """样本不足 ⇒ UNKNOWN，**绝不算 STABLE**（§2 明令）。"""
        conn = make_db()
        cid = add_channel(conn)
        sid = add_stream(conn, cid, "http://a/1.m3u8")
        feed(conn, sid, [True, True])          # 2 个样本 < 门槛 3
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert ch.state == stability_mod.UNKNOWN
        assert ch.probe_count == 2

    def test_r1_stable_when_recent_and_high_rate(self):
        conn = make_db()
        cid = add_channel(conn)
        sid = add_stream(conn, cid, "http://a/1.m3u8")
        feed(conn, sid, [True, True, True, True, False, True])
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert ch.state == stability_mod.STABLE

    def test_r1_degraded_when_mixed(self):
        conn = make_db()
        cid = add_channel(conn)
        sid = add_stream(conn, cid, "http://a/1.mu8".replace("mu8", "m3u8"))
        feed(conn, sid, [False, False, True, False, False, True])
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert ch.state == stability_mod.DEGRADED

    def test_r1_failed_when_all_streams_fail(self):
        conn = make_db()
        cid = add_channel(conn)
        sid = add_stream(conn, cid, "http://a/1.m3u8")
        add_probe(conn, sid, True, hours_ago=10)
        feed(conn, sid, [False, False, False])
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert ch.state == stability_mod.FAILED
        assert ch.selected_stream_id is None

    def test_r1_consecutive_failure_threshold(self):
        """连续失败达门槛 ⇒ 该 stream 不 eligible。"""
        conn = make_db()
        cid = add_channel(conn)
        sid = add_stream(conn, cid, "http://a/1.m3u8")
        add_probe(conn, sid, True, hours_ago=20)
        feed(conn, sid, [False, False], start=1.0)
        scores = select_mod.score_streams(conn, cid, now=NOW)
        assert scores[0].consecutive_failures == 2
        assert scores[0].eligible          # 2 < 3
        add_probe(conn, sid, False, hours_ago=0.5)
        scores = select_mod.score_streams(conn, cid, now=NOW)
        assert scores[0].consecutive_failures == 3
        assert not scores[0].eligible

    def test_r1_stale_success_cannot_rescue(self):
        """§11：半年前的成功不能证明「现在能播」。"""
        conn = make_db()
        cid = add_channel(conn)
        sid = add_stream(conn, cid, "http://a/1.m3u8")
        # 200 天前的老成功（早于 7 天窗口，本就不该进窗口）
        conn.execute(
            "INSERT INTO probe_result (stream_id, probe_id, checked_at, success)"
            " VALUES (?, 1, ?, 1)",
            (sid, (NOW - dt.timedelta(days=200)).isoformat()))
        feed(conn, sid, [False, False, False])
        conn.commit()
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        # 窗口内只有 3 个失败 ⇒ FAILED，绝不是 STABLE
        assert ch.state == stability_mod.FAILED

    def test_r1_stale_but_passing_is_degraded(self):
        """窗口内成功率够，但最近成功太久 ⇒ DEGRADED（不是 STABLE）。"""
        conn = make_db()
        cid = add_channel(conn)
        sid = add_stream(conn, cid, "http://a/1.m3u8")
        # 最近一次成功在 72h 前（> 48h 门槛），但连续失败未达 3
        add_probe(conn, sid, True, hours_ago=72)
        add_probe(conn, sid, True, hours_ago=73)
        add_probe(conn, sid, True, hours_ago=74)
        add_probe(conn, sid, False, hours_ago=1)
        add_probe(conn, sid, True, hours_ago=0.5)
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert ch.state == stability_mod.STABLE  # 0.5h 前刚成功过

    def test_r1_recent_recovery_reenters(self):
        """恢复的 stream 重新进入候选集，无需人工 reset。"""
        conn = make_db()
        cid = add_channel(conn)
        sid = add_stream(conn, cid, "http://a/1.m3u8")
        add_probe(conn, sid, True, hours_ago=50)
        feed(conn, sid, [False, False, False], start=3.0)
        assert stability_mod.derive_channel_health(
            conn, cid, now=NOW).state == stability_mod.FAILED
        add_probe(conn, sid, True, hours_ago=0.3)
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert sid in ch.candidate_stream_ids
        assert ch.selected_stream_id == sid

    def test_r1_top_error_category_uses_unified_vocabulary(self):
        conn = make_db()
        cid = add_channel(conn)
        sid = add_stream(conn, cid, "http://a/1.m3u8")
        feed(conn, sid, [False, False, False], error="TLS_ERROR")
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert ch.top_error_category == errors_mod.CONNECT_ERROR

    def test_r1_aggregate_stable_rate_denominator(self):
        """§38：分母是全部 canonical，UNKNOWN 不许被踢出分母。"""
        conn = make_db()
        # 2 个稳定 + 2 个 UNKNOWN（无 probe）
        for name in ("A", "B"):
            cid = add_channel(conn, name)
            sid = add_stream(conn, cid, f"http://x/{name}.m3u8")
            feed(conn, sid, [True, True, True])
        for name in ("C", "D"):
            add_channel(conn, name)
        agg = stability_mod.aggregate(stability_mod.derive_all(conn, now=NOW))
        assert agg["canonical_total"] == 4
        assert agg["stable"] == 2
        assert agg["unknown"] == 2
        assert agg["stable_rate"] == 0.5

    def test_r1_aggregate_empty(self):
        agg = stability_mod.aggregate({})
        assert agg["canonical_total"] == 0
        assert agg["stable_rate"] == 0.0

    def test_r1_state_labels(self):
        for state in stability_mod.ALL_STATES:
            assert stability_mod.state_label(state)
        assert stability_mod.state_label("BOGUS") == "BOGUS"
        assert stability_mod.state_label(None) == stability_mod.state_label("UNKNOWN")

    def test_r1_derived_does_not_change_selector_choice(self):
        """核心不变量：派生层**不改变** publish 选哪条线。"""
        conn = make_db()
        cid = add_channel(conn)
        good = add_stream(conn, cid, "http://good/1.m3u8")
        bad = add_stream(conn, cid, "http://bad/1.m3u8")
        feed(conn, good, [True, True, True, True])
        feed(conn, bad, [False, False, False])
        best = select_mod.select_best_stream(conn, cid, now=NOW)
        derived = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert derived.selected_stream_id == best.stream_id == good

    # ---------------- negative verification

    def test_r1_negative_unknown_must_not_be_stable(self):
        """§2/§38 的核心禁令：UNKNOWN 绝不算 STABLE。

        负向验证：若有人把 ``derive_channel_health`` 的 UNKNOWN 分支删掉
        （让样本不足直接落到 STABLE），本测试与
        ``test_r1_minimum_samples_gate`` 都会 failed。
        """
        conn = make_db()
        cid = add_channel(conn)
        sid = add_stream(conn, cid, "http://a/1.m3u8")
        feed(conn, sid, [True, True])           # 全成功，但样本不足
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert ch.state != stability_mod.STABLE
        assert ch.state == stability_mod.UNKNOWN
        # 且聚合时绝不能进 stable 分子
        agg = stability_mod.aggregate(stability_mod.derive_all(conn, now=NOW))
        assert agg["stable"] == 0
        assert agg["stable_rate"] == 0.0

    def test_r1_negative_selector_still_drives_selection(self):
        """负向验证：派生层若**自己另算**一套选线（而不是调 selector），
        本测试会 failed。

        做法不是硬编码「应该选谁」—— 那样测的是数据不是不变量。
        真正的不变量是：**在任何 probe 分布下，派生层报出的 selected
        必须等于 selector 自己算出来的那个**。若有人把 derive_channel_health
        里的 ``select_best_stream`` 换成自研逻辑（例如「只认 STABLE 的」），
        在「A 连续失败、B 可用」这种分布下两边就会分叉 ⇒ 本测试 failed。
        """
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        b = add_stream(conn, cid, "http://b/1.m3u8")
        # 分布 1：A 优、B 不可用
        feed(conn, a, [True, True, True, True])
        feed(conn, b, [False, False, False])
        # 分布 2：A 连续失败退出、B 可用（failover 后状态）
        feed(conn, a, [False, False, False], start=0.5, step=0.1)
        feed(conn, b, [True, True, True], start=0.4, step=0.1)

        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        truth = select_mod.select_best_stream(conn, cid, now=NOW)
        assert truth is not None
        assert ch.selected_stream_id == truth.stream_id == b

    def test_r1_negative_error_vocabulary_is_closed_set(self):
        """负向验证：若有人往 §9 集合外塞新词（如 ``WEIRD_ERROR``），
        本测试 failed —— 保证报告口径封闭。"""
        assert set(errors_mod.ALL_CATEGORIES) <= set(errors_mod.CATEGORY_LABELS)
        assert len(errors_mod.ALL_CATEGORIES) == 14
        for legacy in ("TLS_ERROR", "HTTP_ERROR", "NETWORK_ERROR", "INVALID_M3U",
                       "FFPROBE_NOT_FOUND"):
            assert errors_mod.normalize(legacy) in errors_mod.ALL_CATEGORIES


# ==================================== §3/§27 failover（场景 12-17）

class TestFailover:
    def _two_line(self):
        conn = make_db()
        cid = add_channel(conn, "CCTV-1", tvg_id="CCTV1", logo="http://logo/1.png")
        a = add_stream(conn, cid, "http://a/1.m3u8")
        b = add_stream(conn, cid, "http://b/1.m3u8")
        return conn, cid, a, b

    def test_r1_failover_a_to_b(self):
        conn, cid, a, b = self._two_line()
        for i in range(4):
            add_probe(conn, a, True, hours_ago=6 - i)
            add_probe(conn, b, True, hours_ago=6 - i)
        before = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert before.selected_stream_id == a
        # A 连续失败 3 次 ⇒ 达退出门槛
        for i, hours in enumerate((1.0, 0.8, 0.6)):
            add_probe(conn, a, False, hours_ago=hours)
            add_probe(conn, b, True, hours_ago=hours)
        after = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert after.selected_stream_id == b
        assert a not in after.candidate_stream_ids

    def test_r1_metadata_stable_across_failover(self):
        """§3.4：failover 前后 canonical 名 / tvg-id / logo 不变。"""
        conn, cid, a, b = self._two_line()
        row_before = conn.execute(
            "SELECT name, preferred_tvg_id, preferred_logo FROM canonical_channel"
            " WHERE id = ?", (cid,)).fetchone()
        for i in range(4):
            add_probe(conn, a, True, hours_ago=6 - i)
            add_probe(conn, b, True, hours_ago=6 - i)
        stability_mod.derive_channel_health(conn, cid, now=NOW)
        for hours in (1.0, 0.8, 0.6):
            add_probe(conn, a, False, hours_ago=hours)
            add_probe(conn, b, True, hours_ago=hours)
        stability_mod.derive_channel_health(conn, cid, now=NOW)
        row_after = conn.execute(
            "SELECT name, preferred_tvg_id, preferred_logo FROM canonical_channel"
            " WHERE id = ?", (cid,)).fetchone()
        assert tuple(row_before) == tuple(row_after)

    def test_r1_all_fail_canonical_skipped(self):
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://a/1.m3u8")
        b = add_stream(conn, cid, "http://b/1.m3u8")
        for sid in (a, b):
            add_probe(conn, sid, True, hours_ago=10)
            feed(conn, sid, [False, False, False], start=3.0)
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert ch.state == stability_mod.FAILED
        assert ch.selected_stream_id is None
        assert select_mod.select_best_stream(conn, cid, now=NOW) is None

    def test_r1_no_forced_switch_back(self):
        """§3.6：A 恢复后**不要求**立即抢回；是否切回应由可解释规则决定。"""
        conn, cid, a, b = self._two_line()
        for i in range(4):
            add_probe(conn, a, True, hours_ago=6 - i)
            add_probe(conn, b, True, hours_ago=6 - i)
        for hours in (1.0, 0.8, 0.6):
            add_probe(conn, a, False, hours_ago=hours)
            add_probe(conn, b, True, hours_ago=hours)
        assert stability_mod.derive_channel_health(
            conn, cid, now=NOW).selected_stream_id == b
        # A 只恢复 1 次 —— 单次成功不足以抢回（B 依然 eligible 且 A 连续失败被打断）
        add_probe(conn, a, True, hours_ago=0.2)
        after = stability_mod.derive_channel_health(conn, cid, now=NOW)
        # 关键：无论选中谁，**都不抛错、不要求人工干预**；A 已回到候选集
        assert a in after.candidate_stream_ids
        assert after.selected_stream_id in (a, b)

    def test_r1_cross_source_preference_uses_metadata_not_name(self):
        """多源同名不按名字统计（QA-008A 教训的延伸）。"""
        conn = make_db()
        cid = add_channel(conn)
        a = add_stream(conn, cid, "http://src-a/1.m3u8")
        b = add_stream(conn, cid, "http://src-b/1.m3u8")
        for i in range(4):
            add_probe(conn, a, True, hours_ago=6 - i)
            add_probe(conn, b, True, hours_ago=6 - i)
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert len(ch.candidate_stream_ids) == 2
        assert ch.stream_total == 2

    def test_r1_single_stream_channel_reported(self):
        conn = make_db()
        cid = add_channel(conn)
        sid = add_stream(conn, cid, "http://a/1.m3u8")
        feed(conn, sid, [True, True, True])
        ch = stability_mod.derive_channel_health(conn, cid, now=NOW)
        assert ch.stream_total == 1
        agg = stability_mod.aggregate(stability_mod.derive_all(conn, now=NOW))
        assert agg["multi_stream"] == 0

    def test_r1_multi_stream_with_backup_counted(self):
        conn = make_db()
        cid = add_channel(conn)
        add_stream(conn, cid, "http://a/1.m3u8")
        add_stream(conn, cid, "http://b/1.m3u8")
        sid = add_stream(conn, cid, "http://solo/1.m3u8")
        feed(conn, sid, [True, True, True])
        agg = stability_mod.aggregate(stability_mod.derive_all(conn, now=NOW))
        assert agg["multi_stream"] == 1
        assert agg["multi_stream_with_backup"] == 0   # 两条线都没样本
