"""QA-007A 永久回归：`health.check_once()` 在非 ok 业务状态下**不得抛异常**。

背景（TASK-007 Review 01 / QA-007A）
------------------------------------
`HealthResult` 是 `@dataclasses.dataclass(frozen=True)`，而 `check_once()` 曾经在
`if not result.detail:` 分支里就地执行 `result.detail = ...`
⇒ 只要 ``/healthz`` HTTP 200 且业务 ``status != ok``（真实生产就是 ``status=missing``），
就抛 `dataclasses.FrozenInstanceError`。

这会让：
* ``python -m liptv deploy status`` 在 missing/stale 场景崩掉（运维不可用）；
* ``Deployer._wait_health()`` → ``upgrade()`` 崩掉（健康闸门失去"失败可回滚"语义）。

因此本文件按 Review 01 的要求覆盖四类场景，逐一断言：
**返回 ``HealthResult(ok=False)``，而不是抛异常。**
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading

import pytest

from liptv.health import HealthResult, check_once


# --------------------------------------------------------------------------- 夹具
class _Handler(http.server.BaseHTTPRequestHandler):
    """按 ``SCENARIO`` 里配置的两个响应回话；测试期间可随时改写。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102 - 静音
        pass

    def do_GET(self):  # noqa: N802,D102 - BaseHTTPRequestHandler 约定
        if self.path.startswith("/healthz"):
            code, body = self.server.scenario["healthz"]  # type: ignore[attr-defined]
        elif self.path.startswith("/live.m3u"):
            code, body = self.server.scenario["live"]  # type: ignore[attr-defined]
        else:
            code, body = 404, b"nope"
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    srv.scenario = {  # type: ignore[attr-defined]
        "healthz": (200, b'{"status":"ok"}'),
        "live": (200, b"#EXTM3U\n"),
    }
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


@pytest.fixture
def set_scenario(server):
    def _apply(healthz, live):
        server.scenario["healthz"] = healthz  # type: ignore[attr-defined]
        server.scenario["live"] = live  # type: ignore[attr-defined]

    return _apply


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


# --------------------------------------------------------------------- QA-007A 回归
def test_status_missing_returns_unhealthy_without_raising(server, set_scenario):
    """Review 01 的最小反例：``/healthz`` 200 + ``{"status":"missing"}``。

    修复前抛 ``FrozenInstanceError``；修复后必须正常返回 ``ok=False``。
    """
    set_scenario((200, json.dumps({"status": "missing"}).encode()), (200, b"#EXTM3U\n"))

    result = check_once(_url(server))

    assert isinstance(result, HealthResult)
    assert result.ok is False
    assert result.health_status == "missing"
    assert "missing" in result.detail


def test_status_stale_returns_unhealthy_without_raising(server, set_scenario):
    """``stale`` 与 ``missing`` 走同一条分支，同样不得抛异常。"""
    set_scenario((200, json.dumps({"status": "stale"}).encode()), (200, b"#EXTM3U\n"))

    result = check_once(_url(server))

    assert result.ok is False
    assert result.health_status == "stale"
    assert "stale" in result.detail


def test_unparsable_health_body_returns_unhealthy_without_raising(server, set_scenario):
    """``/healthz`` 200 但正文不是合法 JSON —— 也不得抛异常。"""
    set_scenario((200, b"<html>not json at all</html>"), (200, b"#EXTM3U\n"))

    result = check_once(_url(server))

    assert result.ok is False
    assert result.health_status is None
    assert "不可解析" in result.detail


def test_playlist_503_returns_unhealthy_without_raising(server, set_scenario):
    """``/healthz`` 业务 ok 但 ``/live.m3u`` 503（缺文件/空文件）⇒ 不健康，不抛异常。"""
    set_scenario((200, json.dumps({"status": "ok"}).encode()), (503, b"playlist missing"))

    result = check_once(_url(server))

    assert result.ok is False
    assert result.playlist_http_status == 503
    assert result.detail


def test_empty_playlist_bytes_is_unhealthy(server, set_scenario):
    """200 但 0 字节播放列表同样判不健康（"不生成空列表冒充成功"）。"""
    set_scenario((200, json.dumps({"status": "ok"}).encode()), (200, b""))

    result = check_once(_url(server))

    assert result.ok is False
    assert result.playlist_bytes == 0


def test_healthy_control_case_still_ok(server, set_scenario):
    """对照组：一切正常时仍然必须返回 ``ok=True``（修 bug 不能把健康判成不健康）。"""
    set_scenario(
        (200, json.dumps({"status": "ok"}).encode()),
        (200, b"#EXTM3U\n#EXTINF:-1 tvg-id=\"x\",A\nhttp://example.invalid/a.ts\n"),
    )

    result = check_once(_url(server))

    assert result.ok is True
    assert result.health_status == "ok"
    assert result.playlist_bytes > 0


def test_connection_refused_returns_unhealthy_without_raising():
    """连不上时（``code is None`` 分支）也不得抛异常。"""
    # 保留端口后立刻关闭 ⇒ 连接必然被拒
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    srv.server_close()

    result = check_once(f"http://127.0.0.1:{port}", timeout=0.5)

    assert result.ok is False
    assert result.detail


# ------------------------------------------------------------------ 冻结语义守护
def test_health_result_is_frozen():
    """守护测试：``HealthResult`` 必须保持 frozen（否则 QA-007A 会静默回归）。"""
    result = HealthResult(ok=True, base_url="http://x")
    with pytest.raises(Exception):
        result.detail = "试图就地赋值"  # type: ignore[misc]
