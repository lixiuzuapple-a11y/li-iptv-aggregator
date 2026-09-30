"""HTTP 拉取层测试（TASK-002 验收门槛 2）。

全部打本机 mock 服务，**不依赖公网**。
"""

from __future__ import annotations

import urllib.request

import pytest

from liptv import fetch as fetch_mod
from tools.mock_source_server import _free_port

LIMITS = fetch_mod.FetchLimits(
    timeout_seconds=5.0, max_bytes=2_000_000, max_redirects=3
)


# ------------------------------------------------------------------ 成功路径

def test_fetch_returns_text_and_metadata(mock_server):
    _server, base = mock_server
    got = fetch_mod.fetch_text(f"{base}/ok.m3u", limits=LIMITS)
    assert got.status == 200
    assert got.byte_count > 0
    assert got.text.startswith("#EXTM3U")
    assert got.text.count("#EXTINF") == 3
    assert got.charset == "utf-8"


def test_fetch_follows_bounded_redirect(mock_server):
    _server, base = mock_server
    got = fetch_mod.fetch_text(f"{base}/hop1.m3u", limits=LIMITS)
    assert got.status == 200
    assert "演示新闻台" in got.text


def test_fetch_limits_from_mapping_matches_defaults():
    assert fetch_mod.FetchLimits.from_mapping(None) == fetch_mod.FetchLimits()
    custom = fetch_mod.FetchLimits.from_mapping(
        {"timeout_seconds": 1.5, "max_bytes": 123, "max_redirects": 1, "user_agent": "ua"}
    )
    assert custom.timeout_seconds == 1.5
    assert custom.max_bytes == 123
    assert custom.max_redirects == 1
    assert custom.user_agent == "ua"


# ------------------------------------------------------------------ 失败分类

@pytest.mark.parametrize("path,expected_status", [("/error.m3u", 500), ("/forbidden.m3u", 403)])
def test_fetch_http_error_is_classified(mock_server, path, expected_status):
    _server, base = mock_server
    with pytest.raises(fetch_mod.FetchError) as info:
        fetch_mod.fetch_text(f"{base}{path}", limits=LIMITS)
    assert info.value.category == fetch_mod.ERROR_HTTP_STATUS
    assert info.value.http_status == expected_status


def test_fetch_missing_path_is_http_status(mock_server):
    _server, base = mock_server
    with pytest.raises(fetch_mod.FetchError) as info:
        fetch_mod.fetch_text(f"{base}/nope.m3u", limits=LIMITS)
    assert info.value.category == fetch_mod.ERROR_HTTP_STATUS
    assert info.value.http_status == 404


def test_fetch_timeout_is_classified(mock_server):
    _server, base = mock_server
    limits = fetch_mod.FetchLimits(timeout_seconds=0.5, max_bytes=1_000_000, max_redirects=3)
    with pytest.raises(fetch_mod.FetchError) as info:
        fetch_mod.fetch_text(f"{base}/slow.m3u?seconds=5", limits=limits)
    assert info.value.category == fetch_mod.ERROR_TIMEOUT


def test_fetch_response_too_large_is_classified(mock_server):
    _server, base = mock_server
    limits = fetch_mod.FetchLimits(timeout_seconds=10.0, max_bytes=4096, max_redirects=3)
    with pytest.raises(fetch_mod.FetchError) as info:
        fetch_mod.fetch_text(f"{base}/big.m3u", limits=limits)
    assert info.value.category == fetch_mod.ERROR_TOO_LARGE


def test_fetch_decode_error_is_classified(mock_server):
    _server, base = mock_server
    with pytest.raises(fetch_mod.FetchError) as info:
        fetch_mod.fetch_text(f"{base}/badutf8.m3u", limits=LIMITS)
    assert info.value.category == fetch_mod.ERROR_DECODE


def test_fetch_redirect_loop_is_classified(mock_server):
    _server, base = mock_server
    with pytest.raises(fetch_mod.FetchError) as info:
        fetch_mod.fetch_text(f"{base}/loop.m3u", limits=LIMITS)
    assert info.value.category == fetch_mod.ERROR_TOO_MANY_REDIRECTS


def test_fetch_redirect_over_limit_is_classified(mock_server):
    _server, base = mock_server
    limits = fetch_mod.FetchLimits(timeout_seconds=5.0, max_bytes=1_000_000, max_redirects=1)
    with pytest.raises(fetch_mod.FetchError) as info:
        fetch_mod.fetch_text(f"{base}/hop1.m3u", limits=limits)
    assert info.value.category == fetch_mod.ERROR_TOO_MANY_REDIRECTS


def test_fetch_unresolvable_host_is_network_error():
    """DNS 解析不了的主机名 → NETWORK_ERROR（显式绕开环境代理，保证确定性）。"""
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(fetch_mod.FetchError) as info:
        fetch_mod.fetch_text(
            "http://liptv-no-such-host.invalid/ok.m3u",
            limits=fetch_mod.FetchLimits(timeout_seconds=5.0),
            opener=direct,
        )
    assert info.value.category == fetch_mod.ERROR_NETWORK


def test_fetch_closed_loopback_port_is_network_layer_error():
    """连接没人监听的端口 → 必须是网络层失败（拒绝或超时）。

    本机开着代理 TUN / 防火墙时，Windows 对未监听端口可能直接丢包（→ TIMEOUT）
    而不是回 RST（→ NETWORK_ERROR），所以这里两种都接受；
    但**绝不允许**被误判成 HTTP 状态、解码或 M3U 之类的业务错误。
    """
    port = _free_port()
    with pytest.raises(fetch_mod.FetchError) as info:
        fetch_mod.fetch_text(
            f"http://127.0.0.1:{port}/ok.m3u",
            limits=fetch_mod.FetchLimits(timeout_seconds=2.0),
        )
    assert info.value.category in (fetch_mod.ERROR_NETWORK, fetch_mod.ERROR_TIMEOUT)


@pytest.mark.parametrize("url", ["file:///etc/hosts", "ftp://example.com/x.m3u", "not-a-url", ""])
def test_fetch_rejects_non_http_scheme(url):
    with pytest.raises(fetch_mod.FetchError) as info:
        fetch_mod.fetch_text(url, limits=LIMITS)
    assert info.value.category == fetch_mod.ERROR_NETWORK


def test_fetch_only_requests_the_given_url(mock_server):
    """关键约束：整次拉取只发起 1 次 HTTP 请求，且只针对传入的 M3U 地址。

    条目里的播放地址（http://stream.invalid.example/...）绝不被顺带请求 ——
    否则会变成对视频流的试探，违反 TASK-002 §交付 2。
    """
    _server, base = mock_server
    target = f"{base}/ok.m3u"
    requested: list[str] = []
    real = fetch_mod._build_opener(LIMITS, target)

    class CountingOpener:
        def open(self, request, timeout=None):  # noqa: A002
            requested.append(request.full_url)
            return real.open(request, timeout=timeout)

    got = fetch_mod.fetch_text(target, limits=LIMITS, opener=CountingOpener())
    assert requested == [target]
    assert "#EXTINF" in got.text
    # 响应体里确实含播放地址，但它们没有产生任何请求
    assert "http://stream.invalid.example/" in got.text


def test_fetch_loopback_bypasses_environment_proxy(mock_server, monkeypatch):
    """本机 mock 必须绕过环境里的 HTTP_PROXY，否则测试会被代理环境搞挂。"""
    _server, base = mock_server
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # 一个必然连不上的代理
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    got = fetch_mod.fetch_text(f"{base}/ok.m3u", limits=LIMITS)
    assert got.status == 200
