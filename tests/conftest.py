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
