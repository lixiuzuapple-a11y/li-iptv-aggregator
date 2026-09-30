"""pytest 公共 fixture。"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liptv import db as db_mod  # noqa: E402

# 固定的参考时刻，保证选线测试可复现（不依赖真实时钟）
NOW = "2026-09-30T12:00:00+00:00"

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
