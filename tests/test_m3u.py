"""M3U parser / generator 测试。"""

from __future__ import annotations

import pytest

from liptv import m3u
from tests.conftest import SOURCE_A_M3U, SOURCE_B_M3U


# ----------------------------------------------------------------- parser

def test_parse_reads_header_and_entries():
    result = m3u.parse_text(SOURCE_A_M3U)
    assert result.has_header is True
    assert result.entry_count == 3
    assert result.skipped_count == 0


def test_parse_extinf_attributes():
    entry = m3u.parse_text(SOURCE_A_M3U).entries[0]
    assert entry.name == "CCTV-1 综合"
    assert entry.url == "http://src-a.example/cctv1/index.m3u8"
    assert entry.tvg_id == "cctv1.cn"
    assert entry.tvg_name == "CCTV1"
    assert entry.tvg_logo == "http://logo.example/cctv1.png"
    assert entry.group_title == "新闻"
    assert entry.duration == "-1"


def test_parse_keeps_comma_inside_display_name_and_quoted_attrs():
    """引号内的逗号不能把属性切断，显示名里的逗号必须保留。"""
    entry = m3u.parse_text(SOURCE_A_M3U).entries[2]
    assert entry.tvg_logo == "http://logo.example/movie,a.png"
    assert entry.name == "CCTV 电视剧, 高清"
    assert entry.group_title == "影视"


def test_parse_extgrp_as_group_fallback():
    result = m3u.parse_text(SOURCE_B_M3U)
    assert result.entry_count == 3
    assert result.entries[1].name == "五星体育高清"
    assert result.entries[1].group_title == "体育"


def test_parse_skips_url_without_extinf():
    result = m3u.parse_text(SOURCE_B_M3U)
    assert result.skipped == {"url_without_extinf": 1}
    assert result.skipped_count == 1


def test_parse_ignores_unknown_directives():
    text = "#EXTM3U\n#EXTVLCOPT:http-user-agent=x\n#EXTINF:-1,A\nhttp://a/1.m3u8\n"
    result = m3u.parse_text(text)
    assert result.entry_count == 1
    assert result.skipped == {"ignored_directive": 1}


def test_parse_flags_extinf_without_url():
    text = "#EXTM3U\n#EXTINF:-1,只有标题\n"
    result = m3u.parse_text(text)
    assert result.entry_count == 0
    assert result.skipped == {"extinf_without_url": 1}


def test_parse_tolerates_missing_header():
    result = m3u.parse_text("#EXTINF:-1,A\nhttp://a/1.m3u8\n")
    assert result.has_header is False
    assert result.entry_count == 1


def test_parse_file_handles_bom(tmp_path):
    path = tmp_path / "bom.m3u"
    path.write_text("\ufeff#EXTM3U\n#EXTINF:-1,A\nhttp://a/1.m3u8\n", encoding="utf-8")
    result = m3u.parse_file(path)
    assert result.has_header is True
    assert result.entry_count == 1


# -------------------------------------------------------------- generator

def test_generate_standard_output():
    text = m3u.generate_text([
        m3u.M3UChannel(key=1, name="CCTV-1 综合", url="http://a/1.m3u8", tvg_id="cctv1.cn",
                       tvg_name="CCTV-1 综合", tvg_logo="http://l/1.png", group_title="新闻"),
    ])
    lines = text.strip().split("\n")
    assert lines[0] == "#EXTM3U"
    assert lines[1] == (
        '#EXTINF:-1 tvg-id="cctv1.cn" tvg-name="CCTV-1 综合" '
        'tvg-logo="http://l/1.png" group-title="新闻",CCTV-1 综合'
    )
    assert lines[2] == "http://a/1.m3u8"
    assert text.endswith("\n")


def test_generate_omits_missing_attributes():
    text = m3u.generate_text([m3u.M3UChannel(key=1, name="裸频道", url="http://a/1.m3u8")])
    assert text.strip().split("\n")[1] == "#EXTINF:-1,裸频道"


def test_generate_escapes_quotes():
    text = m3u.generate_text([
        m3u.M3UChannel(key=1, name="A", url="http://a/1.m3u8", tvg_name='含"引号"')
    ])
    assert 'tvg-name="含\'引号\'"' in text


def test_generate_rejects_empty_url():
    with pytest.raises(m3u.M3UError):
        m3u.generate_text([m3u.M3UChannel(key=1, name="A", url="   ")])


def test_generate_rejects_duplicate_key():
    """验收 7 的一部分：同一 canonical channel 不得出现两次。"""
    channels = [
        m3u.M3UChannel(key=7, name="A", url="http://a/1.m3u8"),
        m3u.M3UChannel(key=7, name="A 备用", url="http://a/2.m3u8"),
    ]
    with pytest.raises(m3u.M3UError):
        m3u.generate_text(channels)


def test_write_m3u_creates_file(tmp_path):
    target = tmp_path / "out" / "live.m3u"
    stats = m3u.write_m3u([m3u.M3UChannel(key=1, name="A", url="http://a/1.m3u8")], target)
    assert target.exists()
    assert stats["channel_count"] == 1
    assert stats["previous"] is None
    assert stats["checksum"]


def test_write_m3u_keeps_previous_version(tmp_path):
    target = tmp_path / "live.m3u"
    m3u.write_m3u([m3u.M3UChannel(key=1, name="A", url="http://a/1.m3u8")], target)
    m3u.write_m3u([m3u.M3UChannel(key=1, name="A", url="http://a/2.m3u8")], target)

    previous = tmp_path / "live.previous.m3u"
    assert previous.exists()
    assert "http://a/1.m3u8" in previous.read_text(encoding="utf-8")
    assert "http://a/2.m3u8" in target.read_text(encoding="utf-8")


def test_write_m3u_does_not_clobber_on_validation_failure(tmp_path):
    """生成失败必须保留 last-known-good。"""
    target = tmp_path / "live.m3u"
    m3u.write_m3u([m3u.M3UChannel(key=1, name="A", url="http://a/1.m3u8")], target)
    original = target.read_text(encoding="utf-8")

    with pytest.raises(m3u.M3UError):
        m3u.write_m3u([
            m3u.M3UChannel(key=2, name="B", url="http://b/1.m3u8"),
            m3u.M3UChannel(key=2, name="B2", url="http://b/2.m3u8"),
        ], target)

    assert target.read_text(encoding="utf-8") == original
    assert not (tmp_path / "live.tmp.m3u").exists()


def test_roundtrip_parse_generate_parse(tmp_path):
    entries = m3u.parse_text(SOURCE_A_M3U).entries
    channels = [
        m3u.M3UChannel(key=i, name=e.name, url=e.url, tvg_id=e.tvg_id,
                       tvg_name=e.tvg_name, tvg_logo=e.tvg_logo, group_title=e.group_title)
        for i, e in enumerate(entries)
    ]
    path = tmp_path / "roundtrip.m3u"
    m3u.write_m3u(channels, path)
    reparsed = m3u.parse_file(path)
    assert reparsed.entry_count == len(entries)
    assert [e.url for e in reparsed.entries] == [e.url for e in entries]
    assert [e.name for e in reparsed.entries] == [e.name for e in entries]
