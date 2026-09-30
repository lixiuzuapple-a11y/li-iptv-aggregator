"""本地 mock HTTP 服务（TASK-002）。

两个用途：
  1. 自动化测试：提供**完全不依赖公网**的固定响应（import 本模块起线程内服务）；
  2. 手工演示：直接运行，用浏览器/curl 查看各类成功与失败场景。

独立运行：
    python tools/mock_source_server.py --port 8800

所有端点都是只读 GET，只返回文本；它**不是**视频服务，也不模拟任何播放行为。
"""

from __future__ import annotations

import argparse
import pathlib
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ------------------------------------------------------------------ 样本内容

OK_M3U = """#EXTM3U
#EXTINF:-1 tvg-id="mock-news.cn" tvg-name="Mock News" group-title="新闻",演示新闻台
http://stream.invalid.example/news/index.m3u8
#EXTINF:-1 tvg-id="mock-sports.cn" tvg-name="Mock Sports" group-title="体育",演示体育台
http://stream.invalid.example/sports/index.m3u8
#EXTINF:-1 tvg-id="mock-doc.cn" tvg-name="Mock Doc" group-title="纪录片",演示纪录台
http://stream.invalid.example/doc/index.m3u8
"""

# 与 OK 相比：news/sports 原样保留，doc 消失，music 新增
CHANGED_M3U = """#EXTM3U
#EXTINF:-1 tvg-id="mock-news.cn" tvg-name="Mock News" group-title="新闻",演示新闻台
http://stream.invalid.example/news/index.m3u8
#EXTINF:-1 tvg-id="mock-sports.cn" tvg-name="Mock Sports" group-title="体育",演示体育台
http://stream.invalid.example/sports/index.m3u8
#EXTINF:-1 tvg-id="mock-music.cn" tvg-name="Mock Music" group-title="音乐",演示音乐台
http://stream.invalid.example/music/index.m3u8
"""

# 有 M3U 结构，但没有任何带播放地址的有效条目
BROKEN_M3U = """#EXTM3U
#EXTINF:-1 tvg-id="no-url.cn",这个条目没有播放地址
"""

# QA-002A 反例：前一条完整，末尾的 #EXTINF 声明后直接结束（缺配套播放地址）。
# 解析器会把它统计为 extinf_without_url —— 属明显截断的「部分成功前缀」，
# 绝不能当作完整快照去驱动「未出现即下线」。
TRUNCATED_M3U = """#EXTM3U
#EXTINF:-1 tvg-id="mock-news.cn" tvg-name="Mock News" group-title="新闻",演示新闻台
http://stream.invalid.example/news/index.m3u8
#EXTINF:-1 tvg-id="mock-sports.cn" group-title="体育",演示体育台
"""

NOT_M3U = """<!DOCTYPE html>
<html><head><title>502 Bad Gateway</title></head>
<body><h1>502 Bad Gateway</h1><p>upstream unavailable</p></body></html>
"""

# 动态赛事列表：刻意带 txSecret / txTime 这类短时签名参数
DYNAMIC_M3U = """#EXTM3U
#EXTINF:-1 tvg-id="live-mci-ars" group-title="正在直播",[解说] 曼城 vs 阿森纳
http://jsnzkpg.invalid.example/live/mci-ars/pc.m3u8?txSecret=DEADBEEF1234567890&txTime=6A1B2C3D
#EXTINF:-1 tvg-id="live-mci-ars" group-title="正在直播",[原声] 曼城 vs 阿森纳
http://jsnzkpg.invalid.example/live/mci-ars/raw.flv?txSecret=CAFEBABE9876543210&txTime=6A1B2C3E
#EXTINF:-1 group-title="即将开始",[解说] 皇马 vs 巴萨
http://jsnzkpg.invalid.example/live/rma-bar/pc.m3u8?txSecret=0F0F0F0F0F0F0F0F&txTime=6A1B2C3F
#EXTINF:-1 group-title="宣传",官方 App 下载：https://example.invalid/app
http://jsnzkpg.invalid.example/promo/loop.m3u8?txSecret=1111222233334444&txTime=6A1B2C40
"""

# 发布用动态样本（TASK-003）：刻意包含「同源字节重复」「赛事回放」「宣传」「✈️TG频道 推广」，
# 用来验证纳入规则与同源去重的过滤计数。**不要动 /dynamic.m3u**（test_dynamic.py 断言其 4 条）。
DYNAMIC_PUBLISH_M3U = """#EXTM3U
#EXTINF:-1 tvg-id="live-mci-ars" group-title="正在直播",[解说] 曼城 vs 阿森纳
http://jsnzkpg.invalid.example/live/mci-ars/pc.m3u8?txSecret=AAA111&txTime=6A1B2C3D
#EXTINF:-1 tvg-id="live-mci-ars" group-title="正在直播",[原声] 曼城 vs 阿森纳
http://jsnzkpg.invalid.example/live/mci-ars/raw.flv?txSecret=BBB222&txTime=6A1B2C3E
#EXTINF:-1 tvg-id="live-mci-ars" group-title="正在直播",[解说] 曼城 vs 阿森纳
http://jsnzkpg.invalid.example/live/mci-ars/pc.m3u8?txSecret=AAA111&txTime=6A1B2C3D
#EXTINF:-1 group-title="即将开始",[解说] 皇马 vs 巴萨
http://jsnzkpg.invalid.example/live/rma-bar/pc.m3u8?txSecret=CCC333&txTime=6A1B2C3F
#EXTINF:-1 group-title="赛事回放",[回放] 曼联 vs 利物浦
http://jsnzkpg.invalid.example/replay/mun-liv/pc.m3u8?txSecret=DDD444&txTime=6A1B2C40
#EXTINF:-1 group-title="宣传",官方 App 下载入口
http://jsnzkpg.invalid.example/promo/loop.m3u8?txSecret=EEE555&txTime=6A1B2C41
#EXTINF:-1 group-title="✈️TG频道",赛事推送群
http://jsnzkpg.invalid.example/tg/join.m3u8?txSecret=FFF666&txTime=6A1B2C42
"""

# 第二个动态来源：用于验证「不跨来源去重」（内容与上面完全无关）。
DYNAMIC_ALT_M3U = """#EXTM3U
#EXTINF:-1 group-title="正在直播",[解说] 拜仁 vs 多特
http://alt-dynamic.invalid.example/live/bay-dor/pc.m3u8?txSecret=GGG777&txTime=6A1B2C43
"""

# 非法 UTF-8 字节（0xFF 不是任何 UTF-8 序列的开头）
BAD_UTF8_BYTES = b"#EXTM3U\n#EXTINF:-1,\xff\xfe\xfd broken\nhttp://stream.invalid.example/x.m3u8\n"

BIG_M3U = "#EXTM3U\n" + "".join(
    f'#EXTINF:-1 tvg-id="big-{i:05d}" group-title="测试",大列表条目 {i:05d}\n'
    f"http://stream.invalid.example/big/{i:05d}.m3u8\n"
    for i in range(4000)
)

ENDPOINTS: list[tuple[str, str]] = [
    ("/ok.m3u", "200 正常 M3U（3 条：新闻 / 体育 / 纪录片）"),
    ("/changed.m3u", "200 变更后的 M3U（保留 2 条、消失 1 条、新增 1 条）"),
    ("/seq.m3u", "200 可编程内容（测试里用 server.state.content 控制）"),
    ("/empty.m3u", "200 空响应体 → EMPTY_LIST"),
    ("/broken.m3u", "200 有 #EXTM3U 但无有效条目 → EMPTY_LIST"),
    ("/truncated.m3u", "200 截断的 M3U（前段完整、末尾 #EXTINF 缺 URL）→ INVALID_M3U"),
    ("/notm3u.m3u", "200 HTML 错误页 → INVALID_M3U"),
    ("/badutf8.m3u", "200 声明 charset=utf-8 但字节非法 → DECODE_ERROR"),
    ("/big.m3u", "200 约 1MB 响应 → 测 RESPONSE_TOO_LARGE"),
    ("/slow.m3u?seconds=3", "200 延迟返回 → 测 TIMEOUT"),
    ("/error.m3u", "500 服务器错误 → HTTP_STATUS"),
    ("/forbidden.m3u", "403 禁止访问 → HTTP_STATUS"),
    ("/hop1.m3u", "302 → hop2 → ok（两跳，测 max_redirects）"),
    ("/loop.m3u", "302 指向自身 → TOO_MANY_REDIRECTS"),
    ("/dynamic.m3u", "200 动态赛事列表（含 txSecret / txTime 短时参数）"),
    ("/dynamic-publish.m3u", "200 发布用动态列表（含重复/回放/宣传/✈️TG频道）"),
    ("/dynamic-alt.m3u", "200 第二个动态来源（验证不跨来源去重）"),
]


class MockState:
    """可编程响应内容（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._content = OK_M3U

    @property
    def content(self) -> str:
        with self._lock:
            return self._content

    @content.setter
    def content(self, value: str) -> None:
        with self._lock:
            self._content = value

    def set_ok(self) -> None:
        self.content = OK_M3U

    def set_changed(self) -> None:
        self.content = CHANGED_M3U


class MockHandler(BaseHTTPRequestHandler):
    server_version = "liptv-mock/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------- 响应工具
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_text(self, status: int, text: str, charset: str = "utf-8") -> None:
        self._send(
            status,
            text.encode(charset, errors="replace"),
            f"text/plain; charset={charset}",
        )

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        self._route()

    def do_HEAD(self) -> None:  # noqa: N802
        self._route()

    def _route(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        state: MockState = self.server.state  # type: ignore[attr-defined]
        quiet: bool = getattr(self.server, "quiet", True)
        if not quiet:
            print(f"[mock] GET {self.path}", flush=True)

        if path in ("/", "/index.html", "/endpoints"):
            listing = "liptv mock source server\n\n" + "\n".join(
                f"  {p:<22} {desc}" for p, desc in ENDPOINTS
            )
            self._send_text(200, listing + "\n")
            return

        if path == "/ok.m3u":
            self._send_text(200, OK_M3U)
            return

        if path == "/changed.m3u":
            self._send_text(200, CHANGED_M3U)
            return

        if path == "/seq.m3u":
            self._send_text(200, state.content)
            return

        if path == "/empty.m3u":
            self._send_text(200, "")
            return

        if path == "/broken.m3u":
            self._send_text(200, BROKEN_M3U)
            return

        if path == "/truncated.m3u":
            self._send_text(200, TRUNCATED_M3U)
            return

        if path == "/notm3u.m3u":
            self._send(200, NOT_M3U.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/badutf8.m3u":
            self._send(200, BAD_UTF8_BYTES, "text/plain; charset=utf-8")
            return

        if path == "/big.m3u":
            self._send_text(200, BIG_M3U)
            return

        if path == "/slow.m3u":
            seconds = float((parse_qs(parsed.query).get("seconds") or ["3"])[0])
            time.sleep(max(0.0, seconds))
            self._send_text(200, OK_M3U)
            return

        if path == "/error.m3u":
            self._send_text(500, "internal error\n")
            return

        if path == "/forbidden.m3u":
            self._send_text(403, "forbidden\n")
            return

        if path == "/hop1.m3u":
            self._redirect("/hop2.m3u")
            return

        if path == "/hop2.m3u":
            self._redirect("/ok.m3u")
            return

        if path == "/loop.m3u":
            self._redirect("/loop.m3u")
            return

        if path == "/dynamic.m3u":
            self._send_text(200, DYNAMIC_M3U)
            return

        if path == "/dynamic-publish.m3u":
            self._send_text(200, DYNAMIC_PUBLISH_M3U)
            return

        if path == "/dynamic-alt.m3u":
            self._send_text(200, DYNAMIC_ALT_M3U)
            return

        self._send_text(404, "not found\n")

    def log_message(self, fmt: str, *args) -> None:  # pragma: no cover
        if getattr(self.server, "quiet", True):
            return
        print("[mock] " + (fmt % args), flush=True)


def make_server(host: str = "127.0.0.1", port: int = 0, *, quiet: bool = True) -> ThreadingHTTPServer:
    """创建（未启动的）mock server；port=0 时由系统分配空闲端口。"""
    server = ThreadingHTTPServer((host, port), MockHandler)
    server.daemon_threads = True
    server.state = MockState()          # type: ignore[attr-defined]
    server.quiet = quiet                # type: ignore[attr-defined]
    return server


def base_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}"


class RunningMock:
    """上下文管理器：起线程内服务，退出时干净关闭（供测试使用）。"""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0, quiet: bool = True):
        self.server = make_server(host, port, quiet=quiet)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> tuple[ThreadingHTTPServer, str]:
        self._thread.start()
        return self.server, base_url(self.server)

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="liptv 本地 mock HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--quiet", action="store_true", help="不打印每个请求")
    args = parser.parse_args(argv)

    server = make_server(args.host, args.port, quiet=args.quiet)
    url = base_url(server)
    print(f"mock source server listening on {url}")
    print("可用端点：")
    for path, desc in ENDPOINTS:
        print(f"  {url}{path:<22} {desc}")
    print("\n示例：python -m liptv fetch --source demo-fixed --config config/config.mock.toml")
    print("Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
