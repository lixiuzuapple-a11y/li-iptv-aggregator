"""TASK-012 §31 自动化测试（第 2 部分：preflight / reliability / no-change / 安全）。

本文件覆盖 §31 清单里的 23–26（preflight 三态与 policy）、29–34（dynamic 冻结、
脱敏）、36–45（reliability JSON / no-change / LKG）、53–58（安全与边界）。

preflight 测试打**真实本地 HTTP 服务**，不复用 conftest 的 mock_source_server
（那个返回 M3U，不适合构造 HTML / 分片 404 这类响应）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from liptv import db as db_mod
from liptv import errors as errors_mod
from liptv import preflight as preflight_mod
from liptv import publish as publish_mod
from liptv import reliability as reliability_mod
from liptv import repo
from liptv import stability as stability_mod

NOW = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------- 真实 HTTP 服务

class _Handler(BaseHTTPRequestHandler):
    """按路径返回可控响应。``server.state`` 可在测试中切换。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", ctype="", extra=None):
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        state = self.server.state
        if path == "/good.m3u8":
            body = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:4.0,\nseg1.ts\n"
            self._send(200, body, "application/vnd.apple.mpegurl")
        elif path == "/empty.m3u8":
            self._send(200, b"#EXTM3U\n#EXT-X-VERSION:3\n",
                       "application/vnd.apple.mpegurl")
        elif path == "/html.m3u8":
            self._send(200, b"<!DOCTYPE html><html>Forbidden</html>", "text/html")
        elif path == "/html200.m3u8":
            # 假 200：状态正常但内容是 HTML 且 content-type 说 m3u
            self._send(200, b"<!DOCTYPE html><html>nope</html>",
                       "application/vnd.apple.mpegurl")
        elif path == "/seg1.ts":
            mode = state.get("seg", "ok")
            if mode == "ok":
                self._send(200, b"\x47" + b"\x00" * 600, "video/mp2t")
            elif mode == "404":
                self._send(404, b"", "")
            elif mode == "html":
                self._send(200, b"<!DOCTYPE html><html>403</html>", "text/html")
            elif mode == "empty":
                self._send(200, b"", "video/mp2t")
            elif mode == "500":
                self._send(500, b"boom", "text/plain")
        elif path == "/raw.ts":
            self._send(200, b"\x47" + b"\x11" * 400, "video/mp2t")
        elif path == "/gone":
            self._send(410, b"", "")
        elif path == "/geoblock":
            self._send(403, b"", "")
        elif path == "/boom":
            self._send(502, b"", "")
        elif path.startswith("/redirect-chain"):
            self._send(302, b"", "", {"Location": "/redirect-chain2"})
        else:
            self._send(404, b"", "")


@pytest.fixture(scope="module")
def precheck_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = {"seg": "ok"}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


# ============================================ §17 preflight 三态（场景 23-25）

class TestPreflightOutcomes:
    def test_r2_pass_on_working_hls(self, precheck_server):
        server, base = precheck_server
        server.state["seg"] = "ok"
        result = preflight_mod.precheck_entry(f"{base}/good.m3u8")
        assert result.result == preflight_mod.PRECHECK_PASS
        assert result.segment_probed is True
        assert result.segment_ok is True

    def test_r2_fail_on_html_fake(self, precheck_server):
        server, base = precheck_server
        result = preflight_mod.precheck_entry(f"{base}/html.m3u8")
        assert result.result == preflight_mod.PRECHECK_FAIL
        assert result.error_category == errors_mod.HTML_FAKE

    def test_r2_http_200_with_html_body_is_not_pass(self, precheck_server):
        """content-type 撒谎时看 body —— 「200 就当成功」是最贵的错。"""
        server, base = precheck_server
        result = preflight_mod.precheck_entry(f"{base}/html200.m3u8")
        assert result.result == preflight_mod.PRECHECK_FAIL
        assert result.error_category == errors_mod.HTML_FAKE

    def test_r2_fail_on_empty_hls(self, precheck_server):
        server, base = precheck_server
        result = preflight_mod.precheck_entry(f"{base}/empty.m3u8")
        assert result.result == preflight_mod.PRECHECK_FAIL
        assert result.error_category == errors_mod.EMPTY_MEDIA

    def test_r2_segment_404_is_segment_unreachable(self, precheck_server):
        """TASK-011 §13：playlist 正常但分片全 404。"""
        server, base = precheck_server
        server.state["seg"] = "404"
        try:
            result = preflight_mod.precheck_entry(f"{base}/good.m3u8")
        finally:
            server.state["seg"] = "ok"
        assert result.result == preflight_mod.PRECHECK_FAIL
        assert result.error_category == errors_mod.SEGMENT_UNREACHABLE

    def test_r2_segment_html_is_html_fake(self, precheck_server):
        server, base = precheck_server
        server.state["seg"] = "html"
        try:
            result = preflight_mod.precheck_entry(f"{base}/good.m3u8")
        finally:
            server.state["seg"] = "ok"
        assert result.result == preflight_mod.PRECHECK_FAIL
        assert result.error_category == errors_mod.HTML_FAKE

    def test_r2_segment_empty_is_empty_media(self, precheck_server):
        server, base = precheck_server
        server.state["seg"] = "empty"
        try:
            result = preflight_mod.precheck_entry(f"{base}/good.m3u8")
        finally:
            server.state["seg"] = "ok"
        assert result.result == preflight_mod.PRECHECK_FAIL
        assert result.error_category == errors_mod.EMPTY_MEDIA

    def test_r2_410_is_http_4xx(self, precheck_server):
        server, base = precheck_server
        result = preflight_mod.precheck_entry(f"{base}/gone")
        assert result.result == preflight_mod.PRECHECK_FAIL
        assert result.error_category == errors_mod.HTTP_4XX

    def test_r2_403_is_geo_suspected(self, precheck_server):
        server, base = precheck_server
        result = preflight_mod.precheck_entry(f"{base}/geoblock")
        assert result.error_category == errors_mod.GEO_BLOCK_SUSPECTED

    def test_r2_gateway_status_is_unknown_not_fail(self, precheck_server):
        """502/503/504 **不作为源站失败依据**。

        🚨 本机实测（本文件写作时）：开着 FlClash 时，一个根本不存在的端口
        会得到 ``HTTP 502 Bad Gateway`` 而不是 ConnectionRefused。若照字面判 FAIL，
        authoritative 源会被**整批删掉**赛事 —— 这是 §17 最贵的误杀。
        """
        server, base = precheck_server
        result = preflight_mod.precheck_entry(f"{base}/boom")
        assert result.result == preflight_mod.PRECHECK_UNKNOWN
        assert "网关" in (result.detail or "")

    def test_r2_gateway_never_excluded(self, precheck_server):
        server, base = precheck_server
        results = preflight_mod.precheck_entries([f"{base}/boom", f"{base}/boom"])
        decision = preflight_mod.apply_policy(results, True)
        assert decision["excluded_indexes"] == []
        assert decision["advisory_fail"] == 0

    def test_r2_404_is_still_authoritative_fail(self, precheck_server):
        """对照组：404 是源站自己的明确答复，**不受**代理降级影响。"""
        server, base = precheck_server
        result = preflight_mod.precheck_entry(f"{base}/missing-path")
        assert result.result == preflight_mod.PRECHECK_FAIL
        assert result.error_category == errors_mod.HTTP_4XX

    # ---------------- negative verification（§「只写全绿的测试没意义」）

    def test_r2_negative_gateway_set_is_exactly_502_503_504(self):
        """把防护集合改宽/改窄都必须被这条抓住。

        负向验证：若有人把 ``_PROXY_INDUCIBLE_STATUS`` 改成空集合（即恢复
        「所有 5xx 都算源站失败」），本测试会 failed ⇒ 防护被移除时必然报警。
        """
        assert preflight_mod._PROXY_INDUCIBLE_STATUS == {502, 503, 504}

    def test_r2_negative_gateway_set_excludes_4xx(self):
        """4xx 绝不能进降级集合 —— 那是源站的明确答复，代理不会伪造它。"""
        for code in (400, 401, 403, 404, 410, 451):
            assert code not in preflight_mod._PROXY_INDUCIBLE_STATUS

    def test_r2_unknown_on_unreachable(self):
        """连不上 ⇒ UNKNOWN（不猜 FAIL）。"""
        result = preflight_mod.precheck_entry("http://127.0.0.1:9/x.m3u8")
        assert result.result == preflight_mod.PRECHECK_UNKNOWN

    def test_r2_unknown_never_excluded(self):
        results = [preflight_mod.PrecheckResult(
            entry_index=0, result=preflight_mod.PRECHECK_UNKNOWN, host="h",
            scheme="http", error_category=errors_mod.TIMEOUT)]
        decision = preflight_mod.apply_policy(results, True)
        assert decision["excluded_indexes"] == []
        assert 0 in decision["kept_indexes"]

    def test_r2_fail_on_unsupported_scheme(self):
        """结构性结论（非抖动）⇒ FAIL。"""
        result = preflight_mod.precheck_entry("gopher://example/x")
        assert result.result == preflight_mod.PRECHECK_FAIL

    def test_r2_non_hls_direct_stream_passes_without_deep_probe(self, precheck_server):
        """直连 TS：只确认「不是 HTML」，不假装做了深验证。"""
        server, base = precheck_server
        result = preflight_mod.precheck_entry(f"{base}/raw.ts")
        assert result.result == preflight_mod.PRECHECK_PASS
        assert result.segment_probed is False
        assert "未做深验证" in (result.detail or "")


# =============================== §18 policy：authoritative vs advisory（26-28/33）

class TestPreflightPolicy:
    def _results(self):
        return [
            preflight_mod.PrecheckResult(
                entry_index=0, result=preflight_mod.PRECHECK_FAIL, host="a",
                scheme="http", error_category=errors_mod.HTML_FAKE),
            preflight_mod.PrecheckResult(
                entry_index=1, result=preflight_mod.PRECHECK_PASS, host="b",
                scheme="http", error_category=errors_mod.UNKNOWN),
            preflight_mod.PrecheckResult(
                entry_index=2, result=preflight_mod.PRECHECK_UNKNOWN, host="c",
                scheme="http", error_category=errors_mod.TIMEOUT),
        ]

    def test_r2_authoritative_excludes_fail(self):
        decision = preflight_mod.apply_policy(self._results(), True)
        assert decision["excluded_indexes"] == [0]
        assert decision["advisory_fail"] == 0

    def test_r2_advisory_never_excludes(self):
        """KORICE 冻结：上海 FAIL 只能 advisory。"""
        decision = preflight_mod.apply_policy(self._results(), False)
        assert decision["excluded_indexes"] == []
        assert decision["advisory_fail"] == 1
        assert sorted(decision["kept_indexes"]) == [0, 1, 2]

    def test_r2_korice_cloud_fail_still_keeps_all(self):
        decision = preflight_mod.apply_policy(self._results(), False)
        assert len(decision["kept_indexes"]) == 3

    def test_r2_per_result_authoritative_false_also_protects(self):
        """条目级 authoritative=False 同样受保护（来源级已关时不会漏删）。"""
        results = [preflight_mod.PrecheckResult(
            entry_index=0, result=preflight_mod.PRECHECK_FAIL, host="a",
            scheme="http", error_category=errors_mod.HTML_FAKE,
            authoritative=False)]
        assert preflight_mod.apply_policy(results, True)["excluded_indexes"] == []
        assert preflight_mod.apply_policy(results, False)["excluded_indexes"] == []

    def test_r2_summary_counts(self, precheck_server):
        server, base = precheck_server
        results = preflight_mod.precheck_entries(
            [f"{base}/good.m3u8", f"{base}/html.m3u8", "http://127.0.0.1:9/x.m3u8"])
        decision = preflight_mod.apply_policy(results, True)
        summary = preflight_mod.summarize(results, decision)
        assert summary["checked"] == 3
        assert summary["pass"] + summary["fail"] + summary["unknown"] == 3
        assert summary["excluded"] == 1
        assert summary["authoritative"] is True


# ================================== §17 冻结：不写表 / 不保存 URL（29/34/53/54）

class TestPreflightFreeze:
    def test_r2_precheck_writes_nothing_to_db(self, precheck_server, tmp_path):
        server, base = precheck_server
        conn = db_mod.connect(tmp_path / "freeze.sqlite3")
        db_mod.init_db(conn)
        before = repo.counts(conn)
        preflight_mod.precheck_entries([
            f"{base}/good.m3u8", f"{base}/html.m3u8",
            f"{base}/gone", "http://127.0.0.1:9/x.m3u8",
        ])
        assert repo.counts(conn) == before
        conn.close()

    def test_r2_result_object_holds_no_url(self, precheck_server):
        server, base = precheck_server
        result = preflight_mod.precheck_entry(f"{base}/good.m3u8?token=SECRET123")
        assert not hasattr(result, "url")
        blob = json.dumps(result.to_dict())
        assert "SECRET123" not in blob
        assert "/good.m3u8" not in blob

    def test_r2_redact_url_drops_query(self):
        redacted = preflight_mod.redact_url(
            "https://host.example/path/x.m3u8?playtoken=SEKRIT&sign=ABC")
        assert redacted == "https://host.example/..."
        assert "SEKRIT" not in redacted and "sign=" not in redacted

    def test_r2_redact_url_handles_junk(self):
        assert preflight_mod.redact_url("") == "<unparsable-url>"
        assert preflight_mod.redact_url(None) == "<unparsable-url>"

    def test_r2_budget_caps_work(self, precheck_server):
        """预算耗尽 ⇒ 剩余条目 UNKNOWN，绝不突破预算。"""
        server, base = precheck_server
        settings = preflight_mod.PrecheckSettings(
            budget_seconds=0.0, max_concurrency=1)
        results = preflight_mod.precheck_entries(
            [f"{base}/good.m3u8"] * 4, settings=settings)
        assert len(results) == 4
        assert all(r.result == preflight_mod.PRECHECK_UNKNOWN for r in results)
        assert all("预算" in (r.detail or "") for r in results)

    def test_r2_empty_input(self):
        assert preflight_mod.precheck_entries([]) == []

    def test_r2_result_labels_cover_all(self):
        for key in preflight_mod.ALL_RESULTS:
            assert preflight_mod.RESULT_LABELS[key]


# ============================== §19 reliability summary（33-38/43）

def _seeded_db():
    conn = db_mod.connect(":memory:")
    db_mod.init_db(conn)
    for name, ok in (("CH-A", True), ("CH-B", True), ("CH-C", False)):
        cid = repo.add_canonical_channel(conn, name, category="新闻")
        sid = int(conn.execute(
            "INSERT INTO stream (canonical_channel_id, url, url_hash, first_seen_at,"
            " last_seen_at, enabled, status) VALUES (?, ?, ?, '2026-10-01T00:00:00+00:00',"
            " '2026-10-01T00:00:00+00:00', 1, 'active')",
            (cid, f"http://s/{name}.m3u8", f"h{name}")).lastrowid)
        pattern = [True, True, True] if ok else [False, False, False]
        for i, good in enumerate(pattern):
            conn.execute(
                "INSERT INTO probe_result (stream_id, probe_id, checked_at, success,"
                " error_type, startup_ms) VALUES (?, 1, ?, ?, ?, ?)",
                (sid, (NOW - dt.timedelta(hours=i + 1)).isoformat(),
                 1 if good else 0, None if good else "TIMEOUT", 100 + i * 10))
    conn.commit()
    return conn


class TestReliabilitySummary:
    def test_r2_summary_has_four_required_sections(self):
        summary = reliability_mod.build_summary(_seeded_db(), now=NOW)
        for key in ("fixed", "dynamic", "epg", "runtime"):
            assert key in summary, key
        assert summary["schema"] == "li-iptv-aggregator/reliability-summary"
        assert summary["version"] == reliability_mod.SUMMARY_VERSION

    def test_r2_fixed_section_has_required_counters(self):
        fixed = reliability_mod.build_summary(_seeded_db(), now=NOW)["fixed"]
        for key in ("canonical_inventory", "published", "stable", "degraded",
                    "failed", "unknown", "stream_total", "stream_healthy",
                    "stream_failed", "stable_rate"):
            assert key in fixed, key

    def test_r2_fixed_counts_are_real(self):
        conn = _seeded_db()
        fixed = reliability_mod.build_summary(conn, now=NOW)["fixed"]
        assert fixed["canonical_inventory"] == 3
        assert fixed["stable"] == 2
        assert fixed["failed"] == 1
        assert fixed["stable_rate"] == pytest.approx(2 / 3, abs=1e-4)

    def test_r2_probe_section_percentiles(self):
        probe = reliability_mod.build_summary(_seeded_db(), now=NOW)["probe"]
        assert probe["requested"] == 9
        assert probe["success"] == 6
        assert probe["failure"] == 3
        assert probe["median_startup_ms"] is not None

    def test_r2_p95_null_when_sample_insufficient(self):
        """§37：样本不够不算分位。"""
        conn = db_mod.connect(":memory:")
        db_mod.init_db(conn)
        cid = repo.add_canonical_channel(conn, "X", category="新闻")
        sid = int(conn.execute(
            "INSERT INTO stream (canonical_channel_id,url,url_hash,first_seen_at,"
            "last_seen_at,enabled,status) VALUES (?,?,?,'2026-10-01T00:00:00+00:00',"
            "'2026-10-01T00:00:00+00:00',1,'active')",
            (cid, "http://a/1.m3u8", "hx")).lastrowid)
        conn.execute(
            "INSERT INTO probe_result (stream_id,probe_id,checked_at,success,startup_ms)"
            " VALUES (?,1,?,1,50)", (sid, (NOW - dt.timedelta(hours=1)).isoformat()))
        conn.commit()
        probe = reliability_mod.build_summary(conn, now=NOW)["probe"]
        assert probe["p95_startup_ms"] is None

    def test_r2_dynamic_absent_when_no_publish_summary(self):
        dyn = reliability_mod.build_summary(_seeded_db(), now=NOW)["dynamic"]
        assert dyn["available"] is False
        assert dyn["sources"] == []

    def test_r2_dynamic_per_source_not_merged(self):
        """§36：动态按 source 分列，不给「总数」。"""
        summary = reliability_mod.build_summary(
            _seeded_db(), now=NOW,
            publish_summary={
                "fixed_count": 3,
                "dynamic_sources": [
                    {"name": "jsnzkpg-sports", "ok": True, "status": "ok"},
                    {"name": "korice-ppv", "ok": True, "status": "ok"},
                ],
                "dynamic_summary": {
                    "published_counted": 7, "selected_sources": 2,
                    "failure_policy": "isolate",
                    "sources": [
                        {"name": "jsnzkpg-sports", "fetched": 12, "published": 7},
                        {"name": "korice-ppv", "fetched": 3, "published": 0,
                         "precheck_fail": 2, "authoritative": False},
                    ],
                },
            })
        names = [s["name"] for s in summary["dynamic"]["sources"]]
        assert names == ["jsnzkpg-sports", "korice-ppv"]
        korice = summary["dynamic"]["sources"][1]
        assert korice["authoritative"] is False
        assert korice["precheck_fail"] == 2

    def test_r2_json_contains_no_stream_url(self):
        """§19：禁完整 stream URL / signed query / Cookie / Authorization / VPN。

        检查的是「**值**里没有敏感串」，不是「键名里没有 authorization」——
        ``redaction`` 块本身就列出这些键名（它们是「已确认没有」的声明，
        值全为 ``false``），按键名搜会自证式误报。
        """
        conn = _seeded_db()
        summary = reliability_mod.build_summary(conn, now=NOW)
        blob = json.dumps(summary, ensure_ascii=False)
        assert "http://s/CH-A.m3u8" not in blob
        assert "playtoken" not in blob.lower()
        # 敏感项必须是「键存在但值为 false」，而不是「键不存在」
        assert all(value is False for value in summary["redaction"].values())
        for key, value in summary["redaction"].items():
            assert value is False, f"{key} 不该为真"

    def test_r2_redaction_flags_all_false(self):
        summary = reliability_mod.build_summary(_seeded_db(), now=NOW)
        assert all(value is False for value in summary["redaction"].values())

    def test_r2_runtime_section_from_status(self):
        summary = reliability_mod.build_summary(
            _seeded_db(), now=NOW,
            runtime_status={
                "last_success_publish_at": "2026-10-07T11:00:00+00:00",
                "current_round": {"round_id": "r-9", "outcome": "ok",
                                  "publish_status": "OK",
                                  "error": {"category": "TIMEOUT"}},
            })
        runtime = summary["runtime"]
        assert runtime["available"] is True
        assert runtime["scheduler_round_id"] == "r-9"
        assert runtime["last_error_category"] == errors_mod.TIMEOUT
        assert runtime["seconds_since_last_publish"] == pytest.approx(3600, abs=5)

    def test_r2_epg_section(self):
        summary = reliability_mod.build_summary(
            _seeded_db(), now=NOW,
            epg_status={"last_success_epoch": 1000, "feeds": [{"ok": True}]},
            epg_live={"channel_count": 42, "programme_count": 11059,
                      "channels_with_future_programme": 40})
        assert summary["epg"]["available"] is True
        assert summary["epg"]["channels"] == 42
        assert summary["epg"]["programmes"] == 11059
        assert summary["epg"]["feeds_ok"] == 1

    def test_r2_epg_absent_ok(self):
        assert reliability_mod.build_summary(
            _seeded_db(), now=NOW)["epg"]["available"] is False

    def test_r2_human_output_is_short(self):
        """§19：不要打印成几百行日志。"""
        text = reliability_mod.render_human(
            reliability_mod.build_summary(_seeded_db(), now=NOW))
        assert 0 < len(text.splitlines()) <= 25

    def test_r2_human_output_mentions_all_sections(self):
        text = reliability_mod.render_human(
            reliability_mod.build_summary(_seeded_db(), now=NOW))
        for token in ("[固定]", "[测活]", "[动态]", "[节目单]", "[运行]"):
            assert token in text

    def test_r2_human_shows_unstable_channels(self):
        conn = _seeded_db()
        text = reliability_mod.render_human(
            reliability_mod.build_summary(conn, now=NOW))
        assert "CH-C" in text

    def test_r2_write_summary_roundtrip(self, tmp_path):
        target = tmp_path / "reliability-summary.json"
        summary = reliability_mod.build_summary(_seeded_db(), now=NOW)
        result = reliability_mod.write_summary(summary, target)
        assert result["written"] is True
        assert json.loads(target.read_text(encoding="utf-8"))["version"] == \
            reliability_mod.SUMMARY_VERSION

    def test_r2_write_failure_is_soft(self, tmp_path):
        """F8：摘要写失败**不得**抛异常打断调用方。"""
        blocked = tmp_path / "as-a-file"
        blocked.write_text("x", encoding="utf-8")
        result = reliability_mod.write_summary(
            {"a": 1}, blocked / "sub" / "out.json")
        assert result["written"] is False
        assert result["error"]

    def test_r2_write_is_atomic_no_tmp_left(self, tmp_path):
        target = tmp_path / "rel.json"
        reliability_mod.write_summary({"a": 1}, target)
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []

    def test_r2_stable_rate_uses_published_basis(self):
        fixed = reliability_mod.build_summary(
            _seeded_db(), now=NOW,
            publish_summary={"fixed_count": 2})["fixed"]
        assert fixed["published"] == 2
        assert fixed["published_basis"] == "publish_summary"

    def test_r2_published_falls_back_when_no_summary(self):
        fixed = reliability_mod.build_summary(_seeded_db(), now=NOW)["fixed"]
        assert fixed["published_basis"] == "eligible_derived"
        assert fixed["published"] == 2      # CH-A/CH-B 有可用线，CH-C 没有


# =========================== §21 publish no-change / churn（37/43）

def _publish_fixture(tmp_path, conn):
    cid = repo.add_canonical_channel(conn, "CCTV-1", category="新闻")
    sid = int(conn.execute(
        "INSERT INTO stream (canonical_channel_id,url,url_hash,first_seen_at,"
        "last_seen_at,enabled,status) VALUES (?,?,?,'2026-10-01T00:00:00+00:00',"
        "'2026-10-01T00:00:00+00:00',1,'active')",
        (cid, "http://a/1.m3u8", "ha")).lastrowid)
    for i in range(3):
        conn.execute(
            "INSERT INTO probe_result (stream_id,probe_id,checked_at,success,startup_ms)"
            " VALUES (?,1,?,1,100)", (sid, (NOW - dt.timedelta(hours=i + 1)).isoformat()))
    conn.commit()
    return cid, sid, tmp_path / "out" / "live.m3u"


class TestPublishNoChange:
    def test_r2_first_publish_writes(self, tmp_path):
        conn = db_mod.connect(":memory:")
        db_mod.init_db(conn)
        _, _, out = _publish_fixture(tmp_path, conn)
        result = publish_mod.publish(
            conn, output_path=out, group_order=["新闻"],
            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        assert result["published"] is True
        assert result["rewritten"] is True
        assert result["no_change"] is False
        assert out.is_file()

    def test_r2_identical_republish_skips_rewrite(self, tmp_path):
        conn = db_mod.connect(":memory:")
        db_mod.init_db(conn)
        _, _, out = _publish_fixture(tmp_path, conn)
        first = publish_mod.publish(
            conn, output_path=out, group_order=["新闻"],
            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        assert first["rewritten"] is True
        before_mtime = out.stat().st_mtime_ns

        second = publish_mod.publish(
            conn, output_path=out, group_order=["新闻"],
            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        assert second["published"] is True
        assert second["rewritten"] is False
        assert second["no_change"] is True
        # mtime 不变 ⇒ 订阅端不会看到无意义抖动
        assert out.stat().st_mtime_ns == before_mtime

    def test_r2_no_change_preserves_previous_as_real_last_version(self, tmp_path):
        """§21 + TASK-003 LKG：no-change 不能把 previous 刷成同一份内容。"""
        conn = db_mod.connect(":memory:")
        db_mod.init_db(conn)
        _, sid, out = _publish_fixture(tmp_path, conn)
        publish_mod.publish(conn, output_path=out, group_order=["新闻"],
                            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        # 让内容真的变一次（加 probe 让分辨率变化 → M3U 不含分辨率，改用换线路）
        second_url = "http://a/2.m3u8"
        conn.execute(
            "UPDATE stream SET status='stale' WHERE id = ?", (sid,))
        conn.execute(
            "INSERT INTO stream (canonical_channel_id,url,url_hash,first_seen_at,"
            "last_seen_at,enabled,status) VALUES (?,?,?,'2026-10-01T00:00:00+00:00',"
            "'2026-10-01T00:00:00+00:00',1,'active')",
            (conn.execute("SELECT canonical_channel_id FROM stream WHERE id=?",
                          (sid,)).fetchone()[0], second_url, "hb"))
        for i in range(3):
            conn.execute(
                "INSERT INTO probe_result (stream_id,probe_id,checked_at,success,startup_ms)"
                " VALUES ((SELECT id FROM stream WHERE url_hash='hb'),1,?,1,100)",
                ((NOW - dt.timedelta(hours=i + 1)).isoformat(),))
        conn.commit()

        changed = publish_mod.publish(
            conn, output_path=out, group_order=["新闻"],
            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        assert changed["rewritten"] is True
        previous = pathlib.Path(str(changed["previous"]))
        assert previous.is_file()
        saved = previous.read_text(encoding="utf-8")
        assert "a/1.m3u8" in saved and "a/2.m3u8" not in saved

        # 再发一次完全相同的 ⇒ 不重写，previous 仍指向真正的上一版
        third = publish_mod.publish(
            conn, output_path=out, group_order=["新闻"],
            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        assert third["rewritten"] is False
        assert pathlib.Path(third["previous"]).read_text(
            encoding="utf-8") == saved

    def test_r2_dry_run_never_writes(self, tmp_path):
        conn = db_mod.connect(":memory:")
        db_mod.init_db(conn)
        _, _, out = _publish_fixture(tmp_path, conn)
        result = publish_mod.publish(
            conn, output_path=out, group_order=["新闻"],
            selection_kwargs={"now": NOW}, stamp=NOW.isoformat(), dry_run=True)
        assert result["published"] is False
        assert not out.exists()

    def test_r2_no_change_reports_checksum(self, tmp_path):
        conn = db_mod.connect(":memory:")
        db_mod.init_db(conn)
        _, _, out = _publish_fixture(tmp_path, conn)
        first = publish_mod.publish(
            conn, output_path=out, group_order=["新闻"],
            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        second = publish_mod.publish(
            conn, output_path=out, group_order=["新闻"],
            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        assert second["checksum"] == first["checksum"] == second["expected_checksum"]
        assert second["no_change_reason"]

    def test_r2_selector_change_forces_rewrite(self, tmp_path):
        """内容真变了就必须写 —— no-change 不能挡住 failover 生效。

        ⚠️ B 必须有**足够的成功样本**，否则它自己也进不了候选集，
        组合会为空并被 REJECTED_VALIDATION（那是正确行为，不是本测试要测的）。
        """
        conn = db_mod.connect(":memory:")
        db_mod.init_db(conn)
        cid, sid, out = _publish_fixture(tmp_path, conn)
        publish_mod.publish(conn, output_path=out, group_order=["新闻"],
                            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        # 备用线路 B：3 次成功（>= min_samples），启动更快
        conn.execute(
            "INSERT INTO stream (canonical_channel_id,url,url_hash,first_seen_at,"
            "last_seen_at,enabled,status) VALUES (?,?,?,'2026-10-01T00:00:00+00:00',"
            "'2026-10-01T00:00:00+00:00',1,'active')", (cid, "http://b/1.m3u8", "hb"))
        for i in range(3):
            conn.execute(
                "INSERT INTO probe_result (stream_id,probe_id,checked_at,success,startup_ms)"
                " VALUES ((SELECT id FROM stream WHERE url_hash='hb'),1,?,1,80)",
                ((NOW - dt.timedelta(hours=i + 1)).isoformat(),))
        conn.commit()
        # A 现在连续失败 3 次 ⇒ 达退出门槛 ⇒ B 上场 ⇒ URL 变了 ⇒ 必须重写
        for hours in (0.5, 0.4, 0.3):
            conn.execute(
                "INSERT INTO probe_result (stream_id,probe_id,checked_at,success,error_type)"
                " VALUES (?,1,?,0,'TIMEOUT')",
                (sid, (NOW - dt.timedelta(hours=hours)).isoformat()))
        conn.commit()
        result = publish_mod.publish(
            conn, output_path=out, group_order=["新闻"],
            selection_kwargs={"now": NOW}, stamp=NOW.isoformat())
        assert result["status"] == publish_mod.STATUS_OK
        assert result["rewritten"] is True
        assert "b/1.m3u8" in out.read_text(encoding="utf-8")


# ==================================== 边界与不变量（§30/§33/§39）

class TestBoundaries:
    def test_r2_derive_layer_does_not_write(self, tmp_path):
        """§4 派生不改行为：调用前后 DB 内容完全一致。"""
        conn = _seeded_db()
        before = repo.counts(conn)
        stability_mod.derive_all(conn, now=NOW)
        reliability_mod.build_summary(conn, now=NOW)
        assert repo.counts(conn) == before

    def test_r2_unknown_not_counted_as_stable_anywhere(self):
        conn = db_mod.connect(":memory:")
        db_mod.init_db(conn)
        add = repo.add_canonical_channel(conn, "NO-DATA", category="新闻")
        assert add
        summary = reliability_mod.build_summary(conn, now=NOW)
        assert summary["fixed"]["stable"] == 0
        assert summary["fixed"]["unknown"] == 1
        assert summary["fixed"]["stable_rate"] == 0.0

    def test_r2_shanghai_context_stated(self):
        """§7：上海 probe 只代表 shanghai-cloud。"""
        text = reliability_mod.render_human(
            reliability_mod.build_summary(_seeded_db(), now=NOW))
        assert "shanghai" in text.lower()

    def test_r2_summary_never_invents_dynamic_data(self):
        summary = reliability_mod.build_summary(_seeded_db(), now=NOW)
        assert summary["dynamic"]["available"] is False
        assert "published_counted" not in summary["dynamic"]
