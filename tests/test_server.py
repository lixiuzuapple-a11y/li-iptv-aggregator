"""TASK-004 只读 HTTP 服务测试：路由、缺文件口径、信息边界、并发读一致性。

刻意全部离线：服务只读本机临时目录里的文件，**不访问任何公网地址**，
也不请求任何真实播放地址（夹具里的 URL 全是 ``*.invalid.example`` 占位）。

对应 TASK-004 §8 的验收场景：
  * ``GET /live.m3u`` 正常返回已发布文件；
  * ``HEAD /live.m3u`` 与 GET 报出**一致**的 Content-Length 但不发正文；
  * 文件缺失 / 空文件一律 ``503``（**不**生成空列表冒充成功）；
  * ``GET /healthz`` 只暴露服务 / 文件 / 新鲜度，**不含任何 URL 或签名**；
  * ``/../``、绝对路径、目录外文件、db、``*.previous.m3u``、发布摘要一律 ``404``；
  * 发布侧原子替换与 HTTP 读并发时，客户端**只会**读到完整旧版或完整新版；
  * 只读服务**绝不**发起任何对外连接（含播放地址）。
"""

from __future__ import annotations

import http.client
import json
import os
import pathlib
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from liptv import m3u as m3u_mod
from liptv import publish as publish_mod
from liptv import runtime as runtime_mod
from liptv import server as server_mod

NOW = "2026-10-01T12:00:00+00:00"

PLAYLIST_TEXT = (
    "#EXTM3U\n"
    "#EXTINF:-1 tvg-id=\"cctv1.cn\" group-title=\"新闻\",演示新闻台\n"
    "http://jsnzkpg.invalid.example/live/news/pc.m3u8?txSecret=AAA111&txTime=6A1B2C3D\n"
    "#EXTINF:-1 group-title=\"体育\",演示体育台\n"
    "http://jsnzkpg.invalid.example/live/sports/pc.m3u8?txSecret=BBB222&txTime=6A1B2C3E\n"
)

SENTINELS = (
    "http://",
    "https://",
    "txSecret",
    "AAA111",
    "BBB222",
    "jsnzkpg.invalid.example",
)


# ------------------------------------------------------------------ 夹具

def _write_playlist(path: pathlib.Path, text: str = PLAYLIST_TEXT) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path.read_bytes()


@pytest.fixture()
def out_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    directory = tmp_path / "out"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@contextmanager
def running_server(out_dir: pathlib.Path, **overrides):
    """在本机随机端口起一个只读服务，退出时一定关掉。"""
    kwargs = {
        "host": "127.0.0.1",
        "port": 0,  # 由系统分配空闲端口，避免测试间抢端口
        "playlist_file": out_dir / "live.m3u",
        "status_file": out_dir / "runtime-status.json",
        "stale_after_seconds": 3600,
        "version": "test",
        "started_at": NOW,
        "quiet": True,
    }
    kwargs.update(overrides)
    service = server_mod.SubscriptionServer(**kwargs)
    service.start()
    try:
        yield service
    finally:
        service.stop()


def request(service, method: str, path: str) -> tuple[int, dict, bytes]:
    """发一次裸 HTTP 请求，返回 (状态码, 头字典(小写键), 正文)。"""
    conn = http.client.HTTPConnection("127.0.0.1", service.port, timeout=15)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        body = response.read()
        headers = {k.lower(): v for k, v in response.getheaders()}
        return response.status, headers, body
    finally:
        conn.close()


# ============================================================ 播放列表：正常路径

def test_get_playlist_returns_published_file(out_dir):
    """GET /live.m3u 返回磁盘上的完整文件，并带上可缓存的元信息。"""
    raw = _write_playlist(out_dir / "live.m3u")
    with running_server(out_dir) as service:
        status, headers, body = request(service, "GET", "/live.m3u")

    assert status == 200
    assert body == raw, "必须原样返回文件字节（不做任何重写/过滤）"
    assert headers["content-type"] == server_mod.PLAYLIST_CONTENT_TYPE
    assert int(headers["content-length"]) == len(raw)
    assert headers["cache-control"] == "no-store"
    # 这两个是播放器做条件请求时会看的头；值必须来自本次读到的那个版本
    assert "last-modified" in headers
    assert headers["etag"].startswith('"') and headers["etag"].endswith('"')


def test_head_playlist_matches_get_content_length(out_dir):
    """HEAD 不读正文，但 Content-Length 必须与 GET **完全一致**。"""
    raw = _write_playlist(out_dir / "live.m3u")
    with running_server(out_dir) as service:
        get_status, get_headers, get_body = request(service, "GET", "/live.m3u")
        head_status, head_headers, head_body = request(service, "HEAD", "/live.m3u")

    assert (get_status, head_status) == (200, 200)
    assert head_body == b"", "HEAD 不得发送正文"
    assert head_headers["content-length"] == get_headers["content-length"]
    assert int(head_headers["content-length"]) == len(raw)
    assert head_headers["content-type"] == get_headers["content-type"]
    assert head_headers["etag"] == get_headers["etag"]


def test_content_length_matches_actual_body_for_many_sizes(out_dir):
    """各种长度的文件都不能出现 Content-Length 与实际字节数不符。"""
    with running_server(out_dir) as service:
        for size in (1, 2, 15, 4096, 65536):
            text = "#EXTM3U\n" + ("# comment line padding\n" * (size // 10))
            raw = _write_playlist(out_dir / "live.m3u", text)
            status, headers, body = request(service, "GET", "/live.m3u")
            assert status == 200
            assert int(headers["content-length"]) == len(body) == len(raw)


def test_etag_and_last_modified_change_after_republish(out_dir):
    """重新发布后 ETag 必须变化，否则播放器会一直用缓存的旧列表。"""
    _write_playlist(out_dir / "live.m3u")
    with running_server(out_dir) as service:
        _, first_headers, _ = request(service, "GET", "/live.m3u")
        # 保证 mtime_ns 真的前进（文件系统时间戳精度有限）
        time.sleep(0.01)
        _write_playlist(out_dir / "live.m3u", PLAYLIST_TEXT + "#EXTINF:-1,新增台\nhttp://x.invalid.example/n.m3u8\n")
        _, second_headers, second_body = request(service, "GET", "/live.m3u")

    assert second_headers["etag"] != first_headers["etag"]
    assert "新增台" in second_body.decode("utf-8")


# ============================================================ 播放列表：不可用

def test_missing_playlist_is_503_for_get_and_head(out_dir):
    """文件不存在 ⇒ 503，而不是空 200 / 空列表。"""
    with running_server(out_dir) as service:
        assert not (out_dir / "live.m3u").exists()
        get_status, get_headers, get_body = request(service, "GET", "/live.m3u")
        head_status, head_headers, head_body = request(service, "HEAD", "/live.m3u")

    assert get_status == runtime_mod.HTTP_PLAYLIST_MISSING == 503
    assert get_status == head_status == 503
    assert b"#EXTM3U" not in get_body, "缺文件时绝不能凭空生成一个空播放列表"
    assert get_body.strip(), "应当有可读的说明正文"
    assert head_body == b""
    assert int(head_headers["content-length"]) == len(get_body)


def test_empty_playlist_is_503(out_dir):
    """空文件同样算不可用（不把空文件当成成功订阅）。"""
    (out_dir / "live.m3u").write_bytes(b"")
    with running_server(out_dir) as service:
        status, _, body = request(service, "GET", "/live.m3u")
        head_status, head_headers, _ = request(service, "HEAD", "/live.m3u")

    assert status == 503 and head_status == 503
    assert b"#EXTM3U" not in body
    assert int(head_headers["content-length"]) == len(body)


def test_playlist_becoming_unavailable_flips_back_to_503(out_dir):
    """发布文件被删掉后，服务必须立刻改回 503（不得继续提供旧字节）。"""
    _write_playlist(out_dir / "live.m3u")
    with running_server(out_dir) as service:
        assert request(service, "GET", "/live.m3u")[0] == 200
        (out_dir / "live.m3u").unlink()
        assert request(service, "GET", "/live.m3u")[0] == 503


# ============================================================ /healthz

def test_healthz_reports_ok_and_leaks_no_urls(out_dir):
    """/healthz 只讲「服务在不在 / 文件在不在 / 多久没成功发布」，不含任何 URL。"""
    _write_playlist(out_dir / "live.m3u")
    with running_server(out_dir) as service:
        status, headers, body = request(service, "GET", "/healthz")

    assert status == 200
    assert headers["content-type"] == server_mod.JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload["status"] == runtime_mod.FRESHNESS_OK
    assert payload["service"]["read_only"] is True
    assert payload["playlist"]["exists"] is True
    assert payload["playlist"]["bytes"] == len(PLAYLIST_TEXT.encode("utf-8"))
    for branch in ("service", "playlist", "freshness", "last_run", "note"):
        assert branch in payload

    blob = json.dumps(payload, ensure_ascii=False)
    for sentinel in SENTINELS:
        assert sentinel not in blob, f"/healthz 泄漏了 {sentinel!r}"


def test_healthz_missing_and_stale(out_dir):
    """文件缺失 ⇒ missing；超过 stale_after 没有成功发布 ⇒ stale。"""
    status_path = out_dir / "runtime-status.json"
    with running_server(out_dir, stale_after_seconds=60) as service:
        missing = json.loads(request(service, "GET", "/healthz")[2].decode("utf-8"))
        assert missing["status"] == runtime_mod.FRESHNESS_MISSING
        assert missing["playlist"]["exists"] is False

        _write_playlist(out_dir / "live.m3u")
        status_path.write_text(
            json.dumps({
                "last_success_publish_at": "2020-01-01T00:00:00+00:00",
                "current_round": {
                    "round_id": "r-1", "outcome": "failed", "publish_status": "REJECTED_VALIDATION",
                    "error": {"category": "RuntimeError"},
                },
            }),
            encoding="utf-8",
        )
        stale = json.loads(request(service, "GET", "/healthz")[2].decode("utf-8"))

    assert stale["status"] == runtime_mod.FRESHNESS_STALE
    assert stale["freshness"]["is_stale"] is True
    assert stale["freshness"]["source"] == "runtime_status"
    assert stale["last_run"]["outcome"] == "failed"
    assert stale["last_run"]["error_category"] == "RuntimeError"
    for sentinel in SENTINELS:
        assert sentinel not in json.dumps(stale, ensure_ascii=False)


def test_healthz_head_has_no_body(out_dir):
    _write_playlist(out_dir / "live.m3u")
    with running_server(out_dir) as service:
        get_status, get_headers, get_body = request(service, "GET", "/healthz")
        head_status, head_headers, head_body = request(service, "HEAD", "/healthz")

    assert (get_status, head_status) == (200, 200)
    assert head_body == b""
    assert head_headers["content-length"] == get_headers["content-length"]
    assert int(head_headers["content-length"]) == len(get_body)


# ============================================================ 路由与信息边界

def test_unknown_and_hostile_paths_return_404(out_dir):
    """除两个确切路由外一律 404 —— 包括一切路径穿越尝试。"""
    _write_playlist(out_dir / "live.m3u")
    (out_dir.parent / "outside.txt").write_text("秘密", encoding="utf-8")
    with running_server(out_dir) as service:
        for path in (
            "/",
            "/index.html",
            "/live.m3u/",
            "/live.m3u/extra",
            "/LIVE.M3U",
            "/live.m3u.bak",
            "/live.m3u.previous.m3u",
            "/health",
            "/healthz/",
            "/hello.txt",
            "/..",
            "/../",
            "/../outside.txt",
            "/../%2e%2e%2foutside.txt",
            "/..%2f..%2fetc%2fpasswd",
            "/C:/Windows/win.ini",
            "/%2Fetc%2Fpasswd",
            "/live.m3u%00.txt",
        ):
            status, _, body = request(service, "GET", path)
            assert status == 404, f"{path} 期望 404，实得 {status}"
            assert b"#EXTM3U" not in body
            assert "秘密".encode("utf-8") not in body


def test_sibling_files_are_not_served(out_dir):
    """同一个 out 目录里的其它产物（db / previous / 摘要 / 配置）都不可读。"""
    _write_playlist(out_dir / "live.m3u")
    secrets = {
        out_dir / "liptv.sqlite3": "SQLITE-SECRET",
        out_dir / "live.previous.m3u": "PREVIOUS-SECRET",
        out_dir / "publish-summary.json": "SUMMARY-SECRET",
        out_dir / "runtime-status.json": "STATUS-SECRET",
        out_dir / "config.toml": "CONFIG-SECRET",
    }
    for path, marker in secrets.items():
        path.write_text(marker, encoding="utf-8")

    with running_server(out_dir) as service:
        for path, marker in secrets.items():
            status, _, body = request(service, "GET", f"/{path.name}")
            assert status == 404, f"{path.name} 不应可读"
            assert marker.encode("utf-8") not in body
        # 只有列表本身可读
        assert request(service, "GET", "/live.m3u")[0] == 200


def test_query_string_does_not_change_which_file_is_served(out_dir):
    """查询串不参与路由（仍返回同一个列表），未知路径带查询串仍是 404。"""
    raw = _write_playlist(out_dir / "live.m3u")
    with running_server(out_dir) as service:
        status, _, body = request(service, "GET", "/live.m3u?token=abc&x=1")
        assert (status, body) == (200, raw)
        assert request(service, "GET", "/nope?token=abc")[0] == 404


def test_custom_routes_are_honoured(out_dir):
    """路由可在配置里改名；改名后旧路径必须失效。"""
    _write_playlist(out_dir / "live.m3u")
    with running_server(out_dir, playlist_route="/sub/list.m3u", health_route="/status") as service:
        assert request(service, "GET", "/sub/list.m3u")[0] == 200
        assert request(service, "GET", "/status")[0] == 200
        assert request(service, "GET", "/live.m3u")[0] == 404
        assert request(service, "GET", "/healthz")[0] == 404


def test_bad_route_configuration_is_rejected(out_dir):
    """路由配置本身不合法时要在构造阶段就失败，而不是运行期才出怪事。"""
    _write_playlist(out_dir / "live.m3u")
    with pytest.raises(ValueError):
        server_mod.make_http_server(
            host="127.0.0.1", port=0, playlist_file=out_dir / "live.m3u",
            playlist_route="live.m3u",  # 没有前导 /
        )
    with pytest.raises(ValueError):
        server_mod.make_http_server(
            host="127.0.0.1", port=0, playlist_file=out_dir / "live.m3u",
            playlist_route="/../live.m3u",
        )
    with pytest.raises(ValueError):
        server_mod.make_http_server(
            host="127.0.0.1", port=0, playlist_file=out_dir / "live.m3u",
            playlist_route="/same", health_route="/same",  # 两个路由撞车
        )


def test_server_never_opens_outbound_connection(monkeypatch, out_dir):
    """只读服务绝不外连：即使列表里全是 URL，也不会去请求它们。"""
    _write_playlist(out_dir / "live.m3u")
    real_connect = socket.socket.connect
    blocked: list[tuple] = []

    def guarded(self, address):
        host = address[0] if isinstance(address, (tuple, list)) and address else None
        if host not in ("127.0.0.1", "::1", "localhost"):
            blocked.append(address)
            raise AssertionError(f"只读服务尝试建立对外连接：{address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)

    with running_server(out_dir) as service:
        assert request(service, "GET", "/live.m3u")[0] == 200
        assert request(service, "GET", "/healthz")[0] == 200

    assert blocked == []


def test_server_writes_nothing(out_dir):
    """所有请求都不得在磁盘上留下任何新文件（服务是纯只读的）。"""
    _write_playlist(out_dir / "live.m3u")
    before = sorted(p.name for p in out_dir.iterdir())
    with running_server(out_dir) as service:
        request(service, "GET", "/live.m3u")
        request(service, "HEAD", "/live.m3u")
        request(service, "GET", "/healthz")
        request(service, "GET", "/nope")
    after = sorted(p.name for p in out_dir.iterdir())
    assert before == after == ["live.m3u"]


# ============================================================ 并发读一致性

def test_concurrent_get_never_returns_half_written_file(out_dir):
    """一边原子替换、一边 HTTP 拉取：客户端只应拿到完整旧版或完整新版。

    这条用例锁住 TASK-004 §4 的 Windows 陷阱：
    读取侧若用普通 ``open()`` 持有目标文件，发布侧的 ``os.replace`` 会抛
    ``PermissionError [WinError 5]``。此处要求「写入全部成功」且「读到的每个字节
    都恰好等于某个完整版本」。
    """
    target = out_dir / "live.m3u"
    versions: list[bytes] = []
    for index in range(20):
        text = (
            "#EXTM3U\n"
            + "".join(
                f'#EXTINF:-1 group-title="G{index}",频道-{index}-{n}\n'
                f"http://t4.invalid.example/v{index}/{n}.m3u8\n"
                for n in range(40)
            )
        )
        versions.append(text.encode("utf-8"))

    _write_playlist(out_dir / "live.m3u", versions[0].decode("utf-8"))

    write_errors: list[BaseException] = []
    reads = 0
    bad_reads: list[str] = []
    stop = threading.Event()

    def writer() -> None:
        try:
            for index, raw in enumerate(versions):
                tmp = target.with_name(f"{target.stem}.w{index}.tmp{target.suffix}")
                tmp.write_bytes(raw)
                # 原子替换：读取侧用共享读句柄，因此替换不该被"正在被读"顶失败
                m3u_mod._replace_with_retry(tmp, target)
                time.sleep(0.005)  # 让读者有机会在两次替换之间插进来
        except BaseException as exc:  # noqa: BLE001 - 任何写失败都要带回主线程
            write_errors.append(exc)
        finally:
            stop.set()

    with running_server(out_dir) as service:
        thread = threading.Thread(target=writer, name="t4-writer", daemon=True)
        thread.start()
        deadline = time.perf_counter() + 60
        while not stop.is_set() and time.perf_counter() < deadline:
            status, headers, body = request(service, "GET", "/live.m3u")
            reads += 1
            if status != 200:
                bad_reads.append(f"status={status}")
                break
            if int(headers["content-length"]) != len(body):
                bad_reads.append("content-length 与正文不符")
            if body not in versions:
                bad_reads.append(f"{len(body)} 字节的内容不属于任何完整版本")
                break
        thread.join(timeout=30)
        stop.set()

    assert not thread.is_alive(), "写入线程没有正常结束"
    assert write_errors == [], f"原子替换不该失败：{write_errors!r}"
    assert bad_reads == [], f"读到了半文件：{bad_reads[:3]}"
    assert reads >= 5, f"并发窗口太短，只读到 {reads} 次，用例失去意义"
    assert target.read_bytes() == versions[-1], "最终文件应为最后一个完整版本"


# ==================================================== 状态文件读 / 写并发（QA-004C）

#: 大G第二轮实测反例的规模：500 次状态原子写 + 2000 次 /healthz GET。
#: 本用例刻意按「数百次写 + 2 个并发客户端」的同一形状做（不放大到 500 次是为了
#: 控制 Windows 上的用时时长；判别力来自「0 次冲突」而不是写次数）。
STATUS_WRITE_COUNT = 300
HEALTH_READER_THREADS = 2
MIN_HEALTH_READS = 200


def test_concurrent_healthz_reads_never_fail_status_writes(out_dir):
    """QA-004C 永久回归：``/healthz`` 高频读取期间，状态原子写必须**零**共享冲突失败。

    复现大G第二轮的实测反例（Windows 10 / Python 3.13）：

    ```text
    STATUS_RACE_WRITES 209 READS 2000 ERRORS 291
    ERR_SAMPLE: PermissionError [WinError 5] runtime-status.json.tmp... -> runtime-status.json
    ```

    即 500 次 ``StatusStore.write()`` 里 291 次被 ``/healthz`` 的读取句柄顶成
    ``WinError 5``（读侧用 ``Path.read_text()``，默认句柄不允许别人 ``os.replace``）。
    后果不是「轻微观测误差」：scheduler + HTTP 同时跑时大部分状态更新被丢弃，
    ``/healthz`` 会长期展示旧的 ``last_success_publish_at``。

    本用例要求：

    * 状态写 **0 次**因读写竞争失败（0 次 winerror 5/32），其它异常同样不许出现；
    * 每一次 ``/healthz`` 都是 200 且正文是可解析 JSON（读侧自己也不能被替换顶失败）；
    * **每一次** ``/healthz`` 都真的读到了状态文件（``freshness.source == "runtime_status"``）
      —— 即读者从没撞上「替换的那一瞬间文件不存在」；
    * 最终文件可解析，且**最后一次写入的值**确实落地（不靠「丢写」蒙混过关）；
    * 不留下 ``*.tmp*`` 残骸。
    """
    _write_playlist(out_dir / "live.m3u")
    status_path = out_dir / "runtime-status.json"
    store = runtime_mod.StatusStore(status_path, version="race")
    # 先播一个 baseline：这样「读到状态文件」与「读不到」在 /healthz 上有可区分的外观
    store.write({"marker": -1, "last_success_publish_at": "2026-10-01T11:00:00+00:00"})

    payloads = [
        {
            "marker": index,
            "last_success_publish_at": f"2026-10-01T12:{index // 60:02d}:{index % 60:02d}+00:00",
        }
        for index in range(STATUS_WRITE_COUNT)
    ]

    write_errors: list[BaseException] = []
    read_errors: list[str] = []
    degraded = 0
    reads = 0
    reads_lock = threading.Lock()
    stop = threading.Event()

    def writer() -> None:
        try:
            for payload in payloads:
                store.write(payload)
        except BaseException as exc:  # noqa: BLE001 - 任何写失败都要带回主线程
            write_errors.append(exc)
        finally:
            stop.set()

    with running_server(out_dir) as service:
        def reader() -> None:
            nonlocal reads, degraded
            conn = http.client.HTTPConnection("127.0.0.1", service.port, timeout=15)
            try:
                while not stop.is_set():
                    try:
                        conn.request("GET", "/healthz")
                        response = conn.getresponse()
                        body = response.read()
                    except Exception as exc:  # noqa: BLE001 - 连接层失败也要带回去
                        read_errors.append(f"{type(exc).__name__}: {exc}")
                        return
                    if response.status != 200:
                        read_errors.append(f"/healthz 返回 {response.status}")
                        return
                    try:
                        payload = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        read_errors.append(f"/healthz 正文不是合法 JSON：{exc}")
                        return
                    with reads_lock:
                        reads += 1
                        if payload["freshness"]["source"] != "runtime_status":
                            degraded += 1
            finally:
                conn.close()

        readers = [
            threading.Thread(target=reader, name=f"healthz-{i}", daemon=True)
            for i in range(HEALTH_READER_THREADS)
        ]
        for thread in readers:
            thread.start()
        writer_thread = threading.Thread(target=writer, name="status-writer")
        writer_thread.start()

        writer_thread.join(timeout=180)
        stop.set()
        for thread in readers:
            thread.join(timeout=30)

    assert not writer_thread.is_alive(), "状态写入线程没有正常结束"
    # 先单独把「读写竞争」这一类挑出来，失败信息能直接指出病根
    conflicts = [
        exc for exc in write_errors
        if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32)
    ]
    assert conflicts == [], (
        f"{STATUS_WRITE_COUNT} 次状态原子写中有 {len(conflicts)} 次被 /healthz 读取顶成共享冲突："
        f"{conflicts[:3]!r}"
    )
    assert write_errors == [], f"状态写入不该有任何失败：{write_errors[:3]!r}"
    assert read_errors == [], f"/healthz 在并发写入期间必须始终可用：{read_errors[:3]!r}"
    assert degraded == 0, (
        f"{reads} 次 /healthz 里有 {degraded} 次没读到状态文件"
        "（说明读者撞上了替换的空窗，last_success_publish_at 会短暂显示成旧值）"
    )
    assert reads >= MIN_HEALTH_READS, f"并发窗口太短，只读到 {reads} 次 /healthz"

    final = json.loads(status_path.read_text(encoding="utf-8"))
    assert final["marker"] == STATUS_WRITE_COUNT - 1, "最后一次写入没落地"
    assert final["last_success_publish_at"] == payloads[-1]["last_success_publish_at"]

    leftovers = sorted(p.name for p in out_dir.iterdir() if ".tmp" in p.name)
    assert leftovers == [], f"不该留下临时文件：{leftovers}"


def test_status_read_does_not_block_atomic_replace(out_dir):
    """QA-004C 最小判别：**读句柄持有期间**，状态文件的原子替换仍必须成功。

    上一条用例是压力版；这一条把病因钉到单次操作上 —— 若 ``StatusStore.read()``
    又退回普通 ``open()``（默认共享方式不允许 rename），``os.replace`` 会立刻抛
    ``PermissionError [WinError 5]``。用例在 Windows 上必然判别得出，其它平台天然通过。
    """
    status_path = out_dir / "runtime-status.json"
    store = runtime_mod.StatusStore(status_path, version="race")
    store.write({"marker": "before", "last_success_publish_at": "2026-10-01T12:00:00+00:00"})

    # 手动持有一个「共享读」句柄，模拟 /healthz 正在读的那一刻
    handle = server_mod._open_shared_read(status_path)  # noqa: SLF001 - 直接验证共享读口径
    try:
        assert store.read()["marker"] == "before"
        # 读句柄还开着，替换仍必须成功（这正是 QA-004C 的判别点）
        store.write({"marker": "after", "last_success_publish_at": "2026-10-01T13:00:00+00:00"})
    finally:
        os.close(handle)

    assert store.read()["marker"] == "after"


def test_read_playlist_returns_self_consistent_bytes_and_stat(out_dir):
    """read_playlist 的正文与 stat 取自同一句柄，ETag 才能和字节数自洽。"""
    raw = _write_playlist(out_dir / "live.m3u")
    data, stat = server_mod.read_playlist(out_dir / "live.m3u")
    assert data == raw
    assert stat.st_size == len(raw)
    assert os.path.getsize(out_dir / "live.m3u") == len(raw)


def test_read_playlist_missing_file_raises(out_dir):
    with pytest.raises(FileNotFoundError):
        server_mod.read_playlist(out_dir / "does-not-exist.m3u")


# ============================================================ 生命周期

def test_stop_is_idempotent_and_running_flag(out_dir):
    """stop() 可重复调用；running 标志与实际监听状态一致。"""
    _write_playlist(out_dir / "live.m3u")
    service = server_mod.SubscriptionServer(
        host="127.0.0.1", port=0, playlist_file=out_dir / "live.m3u", quiet=True
    )
    service.start()
    try:
        assert service.running is True
        assert service.port > 0
        assert service.url == f"http://127.0.0.1:{service.port}"
        assert request(service, "GET", "/live.m3u")[0] == 200
    finally:
        service.stop()
        service.stop()  # 第二次必须安全

    assert service.running is False
    with pytest.raises(OSError):
        request(service, "GET", "/live.m3u")


def test_context_manager_stops_service(out_dir):
    _write_playlist(out_dir / "live.m3u")
    with server_mod.SubscriptionServer(
        host="127.0.0.1", port=0, playlist_file=out_dir / "live.m3u", quiet=True
    ) as service:
        assert request(service, "GET", "/live.m3u")[0] == 200
    assert service.running is False


def test_bound_port_is_loopback_by_default(out_dir):
    """默认只绑 127.0.0.1；非 loopback 绑定必须给出告警串。"""
    _write_playlist(out_dir / "live.m3u")
    with running_server(out_dir) as service:
        assert service.host == "127.0.0.1"

    assert runtime_mod.validate_server_binding("127.0.0.1") is None
    assert "127.0.0.1" in runtime_mod.validate_server_binding("0.0.0.0")
    assert runtime_mod.validate_server_binding("192.168.1.10") is not None
