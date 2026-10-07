"""pytest 公共 fixture。"""

from __future__ import annotations

import datetime as _dt
import inspect
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liptv import db as db_mod  # noqa: E402

# 固定的参考时刻，保证选线测试可复现（不依赖真实时钟）
NOW = "2026-09-30T12:00:00+00:00"

# 🚨 选线/派生层的 7 日窗口（``select.DEFAULT_WINDOW_DAYS``）。
#: ``NOW`` 是**写死**的，一旦真实墙钟越过 ``NOW + 窗口``，那些「没注入 now」
#: 的用例会突然全体失败 —— 2026-10-07 就发生过一次（test_select 的
#: ``select_playlist`` 没传 now，entries 全空）。这类时间炸弹编译器不报错、
#: 测试不提醒，只会在某一天集体爆炸。
#:
#: 下面这个 autouse fixture 把炸弹在**萌芽期**就拦下来：只要有测试调用了
#: 「时间敏感 + 支持 now 参数」的函数却没注入 now，直接 fail 并指出位置。
#: 需要真的用墙钟的用例（server 的 /healthz）显式标 ``@pytest.mark.use_wall_clock``。
_TIME_SENSITIVE = {
    "liptv.select": ("score_streams", "select_best_stream", "select_playlist"),
    "liptv.stability": ("derive_stream_health", "derive_channel_health", "derive_all"),
    "liptv.reliability": ("build_summary",),
    "liptv.retention": ("estimate", "daily_rows"),
    # ⚠️ ``liptv.cadence.analyse`` **故意不在名单里**：它的 ``now`` 参数在
    # 函数体里压根没被使用（只算 runtime-status 相邻轮次的间隔），
    # 加进来只会误伤所有间接调用 build_summary 的用例。
}


def _called_from_cli(frame) -> bool:
    """判断这次调用是不是**从 CLI 里发出来的**。

    CLI（``liptv.cli``）走的是生产路径，那里就该用真实墙钟 ——
    ``select --all`` / ``generate-m3u`` 这类命令本来就不接受 ``--now``。
    守卫只该拦「测试自己直接调库函数却忘了注入时钟」的情况，
    否则会把所有走 CLI 的端到端用例全部误伤（第一版就误伤了
    ``test_dynamic.py::test_cli_dynamic_source_never_enters_live_m3u``）。
    """
    # 最多往上找 12 层，足够穿过 cli_main → cmd_xxx → publish/select
    depth = 0
    node = frame
    while node is not None and depth < 12:
        module = node.f_globals.get("__name__", "")
        if module == "liptv.cli" or module.endswith(".cli"):
            return True
        node = node.f_back
        depth += 1
    return False


def _has_now_param(func) -> bool:
    """函数是否**接受** ``now``（显式参数或 ``**kwargs`` 透传都算）。

    ⚠️ 不能只用 ``inspect.signature``：``select_playlist(conn, *, group_order,
    **kwargs)`` 把 ``now`` 藏在 ``**kwargs`` 里，signature 返回的参数名里根本
    没有 ``now`` —— 守卫第一版就是这么漏的（写了探测用例才发现抓不到）。
    所以额外扫一下源码里的 ``**kwargs``。
    """
    try:
        if "now" in inspect.signature(func).parameters:
            return True
    except (TypeError, ValueError):
        return False
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):
        return False
    return "**kwargs" in source


@pytest.fixture(autouse=True)
def _no_implicit_wall_clock(request):
    """禁止时间敏感函数在测试里静默回落到真实墙钟。"""
    if request.node.get_closest_marker("use_wall_clock"):
        yield
        return

    import importlib

    patched: list = []
    for module_name, func_names in _TIME_SENSITIVE.items():
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for func_name in func_names:
            original = getattr(module, func_name, None)
            if original is None or not callable(original) or not _has_now_param(original):
                continue

            def make_wrapper(func, mod_name, fname):
                def wrapper(*args, **kwargs):
                    if kwargs.get("now") is None and not _called_from_cli(
                            inspect.currentframe()):
                        raise AssertionError(
                            f"{mod_name}.{fname}() 在测试里没有注入 now=，"
                            f"会回落到真实墙钟。写死时间戳 + 真实墙钟 = 时间炸弹"
                            f"（7 日窗口一过就假失败）。请显式传入 now=，"
                            f"或用 @pytest.mark.use_wall_clock 明确声明用墙钟。"
                        )
                    return func(*args, **kwargs)
                return wrapper

            setattr(module, func_name, make_wrapper(original, module_name, func_name))
            patched.append((module, func_name, original))
    try:
        yield
    finally:
        for module, func_name, original in patched:
            setattr(module, func_name, original)

SOURCE_A_M3U = """#EXTM3U
#EXTINF:-1 tvg-id="cctv1.cn" tvg-name="CCTV1" tvg-logo="http://logo.example/cctv1.png" group-title="新闻",CCTV-1 综合
http://src-a.example/cctv1/index.m3u8
#EXTINF:-1 tvg-id="sports5.cn" group-title="体育",五星体育
http://src-a.example/sports5/index.m3u8
#EXTINF:-1 tvg-logo="http://logo.example/movie,a.png" group-title="影视",CCTV 电视剧, 高清
http://src-a.example/tv-drama/index.m3u8
"""

SOURCE_B_M3U = """#EXTM3U
#EXTINF:-1 tvg-id="cctv1.cn" tvg-name="CCTV1 HD" group-title="新闻",CCTV1 高清
http://src-b.example/cctv1/hd.m3u8
#EXTGRP:体育
#EXTINF:-1,五星体育高清
http://src-a.example/sports5/index.m3u8
#EXTINF:-1 tvg-id="orphan.cn",无地址条目
http://src-b.example/orphan/index.m3u8
http://src-b.example/nowhere/index.m3u8
"""


@pytest.fixture()
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "liptv.sqlite3"


@pytest.fixture()
def conn(db_path: pathlib.Path):
    connection = db_mod.connect(db_path)
    db_mod.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def source_a(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "source_a.m3u"
    path.write_text(SOURCE_A_M3U, encoding="utf-8")
    return path


@pytest.fixture()
def source_b(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "source_b.m3u"
    path.write_text(SOURCE_B_M3U, encoding="utf-8")
    return path


# --------------------------------------------------- TASK-002 远程来源夹具

@pytest.fixture(scope="module")
def mock_server():
    """线程内启动的本地 mock HTTP 服务（TASK-002）。

    全部远程来源测试都打这个服务，**不依赖任何公网**。
    返回 (server, base_url)；改内容用 server.state.set_ok()/set_changed()。
    """
    from tools.mock_source_server import RunningMock

    with RunningMock() as (server, url):
        yield server, url


@pytest.fixture()
def remote_config(tmp_path: pathlib.Path, mock_server):
    """生成一份指向 mock server 的临时 config.toml。

    返回 (config_path, db_path, base_url, server)：
      * mock-fixed     fixed_m3u，enabled=true，指向 /seq.m3u（内容可编程）
      * mock-disabled  fixed_m3u，enabled=false（不得被 fetch --all 请求）
      * mock-dynamic   dynamic_event_m3u，enabled=false，指向 /dynamic.m3u
    """
    server, base = mock_server
    server.state.set_ok()

    # TOML 基本字符串里反斜杠是转义符，Windows 路径统一写成正斜杠
    db_file = str(tmp_path / "liptv.sqlite3").replace("\\", "/")
    tmp_dir = str(tmp_path / "out" / "tmp").replace("\\", "/")

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[database]\n"
        f'path = "{db_file}"\n'
        "\n[fetch]\n"
        "timeout_seconds = 5.0\n"
        "max_bytes = 2000000\n"
        "max_redirects = 3\n"
        f'dynamic_tmp_dir = "{tmp_dir}"\n'
        "\n[[sources]]\n"
        'name = "mock-fixed"\n'
        'kind = "fixed_m3u"\n'
        f'url = "{base}/seq.m3u"\n'
        "enabled = true\n"
        "\n[[sources]]\n"
        'name = "mock-disabled"\n'
        'kind = "fixed_m3u"\n'
        f'url = "{base}/ok.m3u"\n'
        "enabled = false\n"
        "\n[[sources]]\n"
        'name = "mock-dynamic"\n'
        'kind = "dynamic_event_m3u"\n'
        f'url = "{base}/dynamic.m3u"\n'
        "enabled = false\n",
        encoding="utf-8",
    )
    return cfg, tmp_path / "liptv.sqlite3", base, server


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "use_wall_clock: 允许该用例让时间敏感函数回落到真实墙钟"
        "（用于 server /healthz 这类由模块内部取 now 的路径）",
    )
