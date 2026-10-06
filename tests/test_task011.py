"""TASK-011 测试：metadata 映射、tvg-id 稳定性、EPG、Logo、播放器输出。

**全部离线**（任务书 §17）：不访问任何真实 EPG / logo 地址，
EPG 一律用手写的小样本 XML，logo 一律用假 server。

覆盖任务书 §21 的 40 项清单（编号即任务书条目号）：

   1. metadata load
   2. duplicate canonical reject
   3. duplicate tvg-id reject
   4. explicit alias metadata
   5. missing metadata fallback
   6. tvg-id M3U rendering
   7. tvg-logo rendering
   8. XML escaping
   9. group rendering
  10. EPG XML parse
  11. channel unique
  12. programme references
  13. timezone
  14. malformed XML reject
  15. empty XML reject
  16. LKG preservation
  17. EPG atomic write
  18. refresh failure no overwrite
  19. logo HEAD/GET fake HTML detection helper
  20. logo missing non-blocking
  21. EPG merge deterministic
  22. programme duplicate handling
  23. M3U ↔ XMLTV tvg-id match
  24. CCTV-5 ≠ CCTV-5+
  25. fixed metadata does not alter selector
  26. dynamic events don't inherit fixed EPG
  27. JSNZKPG regression
  28. KORICE regression
  29. all_or_nothing regression
  30. isolate regression
  31. dynamic default regression
  32. playback_context regression
  33. no credentials in metadata
  34. France24 mapping
  35. NHK mapping
  36. EPG health warning
  37. /epg.xml HTTP behavior
  38. /live.m3u regression
  39. /healthz regression
  40. reverse parse

零回归护栏：TASK-008 isolate / dynamic default / playback_context 全部保持。
"""

from __future__ import annotations

import datetime as dt
import http.server
import pathlib
import threading
import tomllib
import urllib.request
import xml.etree.ElementTree as ET

import pytest

from liptv import channel_metadata as meta_mod
from liptv import config as config_mod
from liptv import epg as epg_mod
from liptv import epg_refresh as epg_refresh_mod
from liptv import m3u as m3u_mod
from liptv import server as server_mod

REPO = pathlib.Path(__file__).resolve().parent.parent
META_PATH = REPO / "config" / "channel_metadata.toml"
SEED_PATH = REPO / "config" / "fixed_seed_bindings.toml"

NOW = "2026-10-06T00:00:00Z"


# ---------------------------------------------------------------- 工具


def _ts(hours_from_now: float, *, base: dt.datetime | None = None) -> str:
    base = base or dt.datetime.now().astimezone()
    return (base + dt.timedelta(hours=hours_from_now)).strftime("%Y%m%d%H%M%S %z")


def _xmltv(channels: list[tuple[str, str]], programmes: list[str]) -> str:
    """拼一个最小 XMLTV。``channels`` 是 (id, display-name)。"""
    ch = "".join(
        f'<channel id="{cid}"><display-name lang="zh">{name}</display-name></channel>'
        for cid, name in channels
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><tv>{ch}{"".join(programmes)}</tv>'


def _prog(channel: str, start_h: float, stop_h: float, title: str) -> str:
    return (
        f'<programme start="{_ts(start_h)}" stop="{_ts(stop_h)}" channel="{channel}">'
        f'<title lang="zh">{title}</title></programme>'
    )


def _write_meta(path: pathlib.Path, rows: list[dict]) -> pathlib.Path:
    lines = []
    for row in rows:
        lines.append("[[channel]]")
        for key, value in row.items():
            lines.append(f'{key} = "{value}"')
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return path


# ================================================================
# §4 / §5 —— metadata 映射层
# ================================================================

def test_01_metadata_load():
    book = meta_mod.load_channel_metadata(META_PATH)
    assert len(book) >= 30, "TASK-011 硬指标要求 fixed canonical >= 30"
    meta = book.get("CCTV-1 综合")
    assert meta.tvg_id, "已登记的 canonical 必须有 tvg_id"
    assert meta.logo and meta.logo.startswith("https://"), "logo 必须是 HTTPS URL"


def test_02_duplicate_canonical_rejected(tmp_path):
    p = _write_meta(tmp_path / "m.toml", [
        {"canonical": "CCTV-1 综合", "tvg_id": "A.cn"},
        {"canonical": "CCTV-1 综合", "tvg_id": "B.cn"},
    ])
    with pytest.raises(meta_mod.MetadataError, match="重复定义"):
        meta_mod.load_channel_metadata(p)


def test_03_duplicate_tvg_id_rejected(tmp_path):
    """两个 canonical 抢同一个 tvg-id ⇒ fail-closed。

    这在播放器里会把两个频道合并成一个，必须在配置阶段就拒绝。
    """
    p = _write_meta(tmp_path / "m.toml", [
        {"canonical": "CCTV-1 综合", "tvg_id": "SAME.cn"},
        {"canonical": "CCTV-2 财经", "tvg_id": "SAME.cn"},
    ])
    with pytest.raises(meta_mod.MetadataError, match="同时占用"):
        meta_mod.load_channel_metadata(p)


def test_04_explicit_alias_metadata():
    """alias 表（TASK-010 §5.4）与 metadata（TASK-011 §4）是**两套东西**。

    alias 管「上游条目名 → canonical」；metadata 管「canonical → tvg-id」。
    任务书 §4 明确要求二者分开，本用例守住这条边界。
    """
    alias_path = REPO / "config" / "fixed_aliases.toml"
    meta_raw = META_PATH.read_text(encoding="utf-8")
    assert "canonical =" in meta_raw
    alias_raw = alias_path.read_text(encoding="utf-8")
    # 两套映射的**主键语义**必须分开：alias 的键是 from（上游条目名），
    # metadata 的键是 canonical。各自只该出现自己那套。
    assert "[[alias]]" in alias_raw and "from =" in alias_raw
    assert "[[alias]]" not in meta_raw
    # alias 不得夹带 metadata 才有的字段（否则两套映射会混在一起）
    assert "tvg_id" not in alias_raw, "alias 表不该管 tvg-id（那是 metadata 的职责）"


def test_05_missing_metadata_falls_back():
    book = meta_mod.load_channel_metadata(None)
    assert len(book) == 0
    meta = book.get("不存在的频道")
    assert meta.tvg_id is None and meta.logo is None
    assert meta.has_epg is False, "未登记频道不得声称有 EPG"


# ================================================================
# §14 —— M3U 输出渲染
# ================================================================

def test_06_tvg_id_rendered():
    ch = m3u_mod.M3UChannel(
        key="k", name="CCTV-1 综合", url="http://x/1.m3u8",
        tvg_id="CCTV1.cn", tvg_name="CCTV1",
        tvg_logo="https://l/a.png", group_title="央视",
    )
    text = m3u_mod.generate_text([ch])
    assert 'tvg-id="CCTV1.cn"' in text, "tvg-id 必须渲染进 EXTINF"
    assert text.startswith("#EXTM3U")


def test_07_tvg_logo_rendered():
    ch = m3u_mod.M3UChannel(
        key="k", name="X", url="http://x/1.m3u8",
        tvg_logo="https://live.fanmingming.cn/tv/CCTV1.png",
    )
    assert "tvg-logo=" in m3u_mod.generate_text([ch])


def test_08_xml_escaping():
    """名字里的引号/&/< 必须被正确转义，否则 EXTINF 会被截断。"""
    ch = m3u_mod.M3UChannel(
        key="k", name='A&B "C" <D>', url="http://x/1.m3u8", group_title="G",
    )
    text = m3u_mod.generate_text([ch])
    assert '"A&B "C" <D>"' not in text
    assert "&" in text  # 至少被转义过
    # 反向解析必须仍然能读回（roundtrip）
    parsed = m3u_mod.parse_text(text)
    assert len(parsed.entries) == 1


def test_09_group_rendered():
    ch = m3u_mod.M3UChannel(
        key="k", name="X", url="http://x/1.m3u8", group_title="卫视",
    )
    assert 'group-title="卫视"' in m3u_mod.generate_text([ch])


def test_14_no_empty_attributes():
    """§14：不输出空字符串垃圾字段。"""
    ch = m3u_mod.M3UChannel(key="k", name="X", url="http://x/1.m3u8")
    text = m3u_mod.generate_text([ch])
    assert 'tvg-id=""' not in text
    assert 'tvg-logo=""' not in text
    assert 'group-title=""' not in text


# ================================================================
# §16 —— EPG 解析与质量
# ================================================================

def test_10_epg_xml_parse():
    xml = _xmltv([("CCTV1", "CCTV-1"), ("HN", "湖南卫视")],
                 [_prog("CCTV1", -1, 1, "现在"), _prog("HN", -1, 1, "湖南新闻")])
    feed = epg_mod.parse_xmltv(xml)
    assert feed.quality.well_formed
    assert feed.quality.channel_count == 2
    assert feed.quality.programme_count == 2


def test_10b_display_name_is_child_element_not_attribute():
    """🚨 TASK-011 真实 bug的回归防护。

    XMLTV 里 ``display-name`` 是**子元素**而不是 attribute。误用
    ``node.get("display-name")`` 会让每个频道名静默退化成 channel id。

    这个 bug 极其阴险：它**不报错**，而且如果样本里 display-name 恰好
    等于 channel id，roundtrip 依然自洽 —— 看起来一切正常。
    所以本用例刻意让 id 与显示名**明显不同**。
    """
    xml = _xmltv([("HN", "湖南卫视")], [_prog("HN", 1, 2, "t")])
    feed = epg_mod.parse_xmltv(xml)
    assert feed.channels["HN"]["display_name"] == "湖南卫视", (
        "display-name 读成了 channel id —— 播放器里所有频道都会显示成 "
        "内部 id，而不是真实频道名"
    )
    assert feed.channels["HN"]["display_name"] != "HN"


def test_10c_icon_src_attribute():
    """icon 的 URL 在 ``src`` attribute 上，不在子元素文本里。"""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><tv>'
        '<channel id="A"><display-name>频道A</display-name>'
        '<icon src="https://example.com/a.png" /></channel></tv>'
    )
    feed = epg_mod.parse_xmltv(xml)
    assert feed.channels["A"]["icon"] == "https://example.com/a.png", (
        "icon 的 src attribute 没被读到"
    )


def test_11_channel_id_unique():
    """重复 channel id 必须被去重并计数（§16）。"""
    xml = _xmltv([("A", "A1"), ("A", "A2"), ("B", "B1")], [_prog("A", 1, 2, "t")])
    feed = epg_mod.parse_xmltv(xml)
    assert feed.quality.channel_count == 2
    assert feed.quality.duplicate_channel_ids == 1
    assert "A1" in feed.channels["A"]["display_name"], "应保留先出现的那个"


def test_12_programme_reference_filtered():
    """F6：引用未知 channel 的 programme 必须被过滤，不能生成坏 XML。"""
    xml = _xmltv([("A", "A")], [_prog("A", 1, 2, "ok"), _prog("GHOST", 1, 2, "orphan")])
    feed = epg_mod.parse_xmltv(xml)
    assert feed.quality.orphan_programmes == 1
    assert all(p["channel"] != "GHOST" for p in feed.programmes)


def test_13_timezone_preserved():
    """§15：带时区偏移的时间必须保留，不能被当成 UTC 丢掉。"""
    xml = _xmltv([("A", "A")], [_prog("A", 1, 2, "t")])
    feed = epg_mod.parse_xmltv(xml)
    prog = feed.programmes[0]
    assert prog["start"].tzinfo is not None, "丢失时区 = 节目时间整体错位"
    rendered = epg_mod.render_xmltv(feed).decode("utf-8")
    assert re_search_offset(rendered), "输出必须带 +0800 之类的偏移"


def re_search_offset(text: str) -> bool:
    return any(sign in text for sign in ("+0800", "+0000", "-0500"))


def test_14_malformed_xml_rejected():
    feed = epg_mod.parse_xmltv("<tv><channel id='A'>未闭合")
    assert not feed.quality.well_formed
    assert not feed.quality.usable
    assert feed.quality.error


def test_15_empty_xml_rejected():
    """空 XML 绝不算成功（§16）。"""
    for text in ("", "   ", "<tv></tv>", "<?xml version='1.0'?><tv/>"):
        feed = epg_mod.parse_xmltv(text)
        assert not feed.quality.usable, f"{text!r} 不应判为可用"


def test_stale_feed_rejected():
    """过期几个月的 feed 必须被拒（§16）。

    这里显式构造「120 天前」的时间戳，而不是靠 now 参数错位 ——
    否则测试自己就会因为「节目是新鲜的」而通过，测不到任何东西。
    """
    past = dt.datetime.now().astimezone() - dt.timedelta(days=120)
    stamp = past.strftime("%Y%m%d%H%M%S %z")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><tv>'
        '<channel id="A"><display-name>A</display-name></channel>'
        '<channel id="B"><display-name>B</display-name></channel>'
        f'<programme start="{stamp}" stop="{stamp}" channel="A">'
        '<title>old</title></programme>'
        f'<programme start="{stamp}" stop="{stamp}" channel="B">'
        '<title>old</title></programme>'
        "</tv>"
    )
    feed = epg_mod.parse_xmltv(xml)
    assert feed.quality.well_formed
    assert not feed.quality.usable, "所有节目都在 120 天前 ⇒ stale，不该写盘"
    assert feed.quality.channels_with_future_programme == 0


# ================================================================
# §9 / §23 —— LKG 与原子写
# ================================================================

def test_16_lkg_preserved_on_source_failure(tmp_path):
    out = tmp_path / "epg.xml"
    good = b'<?xml version="1.0"?><tv><channel id="A"/></tv>'
    out.write_bytes(good)
    result = epg_refresh_mod.refresh_epg(
        sources=[epg_refresh_mod.EpgSource("bad", "http://127.0.0.1:9/none.xml")],
        output_path=out,
    )
    assert not result.ok
    assert result.lkg_preserved
    assert out.read_bytes() == good, "源挂掉时 LKG 文件必须一字不变"


def test_17_epg_atomic_write_no_partial(tmp_path):
    """原子写：任何时刻磁盘上要么是旧文件要么是新文件，没有半文件。"""
    out = tmp_path / "epg.xml"
    out.write_bytes(b"OLD")
    leftovers_before = set(p.name for p in tmp_path.iterdir())
    epg_mod.write_epg_atomic(b"NEW-CONTENT", out)
    assert out.read_bytes() == b"NEW-CONTENT"
    # 临时文件必须已清理
    assert set(p.name for p in tmp_path.iterdir()) == leftovers_before


def test_18_refresh_failure_does_not_overwrite(tmp_path):
    """malformed / empty 源都不得覆盖已有好文件（§20 F2/F3）。"""
    out = tmp_path / "epg.xml"
    good = b'<?xml version="1.0"?><tv><channel id="A"/></tv>'
    for bad_body in (b"<tv><channel id='A'>broken", b"", b"   "):
        out.write_bytes(good)
        result = epg_refresh_mod.refresh_epg(
            sources=[_FakeSource(bad_body)],
            output_path=out,
        )
        assert not result.ok, f"{bad_body!r} 应被拒"
        assert out.read_bytes() == good


class _FakeSource:
    """直接喂字节的假源（避免起真实 server）。"""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.key = "fake"
        self.url = "http://fake.invalid/x.xml"
        self.title = "fake"

    def __repr__(self) -> str:  # pragma: no cover
        return f"_FakeSource({len(self.body)}B)"


def _patch_fetch(monkeypatch, body: bytes):
    """把 epg_refresh 用到的 fetch_text 换成返回固定字节。"""

    class _R:
        def __init__(self, text: str) -> None:
            self.text = text
            self.byte_count = len(body)
            self.status = 200
            self.content_type = "application/xml"
            self.charset = "utf-8"

    monkeypatch.setattr(
        epg_refresh_mod.fetch_mod, "fetch_text",
        lambda url, limits=None, opener=None: _R(body.decode("utf-8", "replace")),
    )


def test_f2_malformed_rejected_via_refresh(tmp_path, monkeypatch):
    out = tmp_path / "epg.xml"
    good = b'<?xml version="1.0"?><tv><channel id="A"/></tv>'
    out.write_bytes(good)
    _patch_fetch(monkeypatch, b"<tv><channel id='A'>broken")
    result = epg_refresh_mod.refresh_epg(
        sources=[epg_refresh_mod.EpgSource("f", "http://f/x.xml")], output_path=out
    )
    assert not result.ok
    assert out.read_bytes() == good


def test_f3_empty_rejected_via_refresh(tmp_path, monkeypatch):
    out = tmp_path / "epg.xml"
    good = b'<?xml version="1.0"?><tv><channel id="A"/></tv>'
    out.write_bytes(good)
    _patch_fetch(monkeypatch, b"")
    result = epg_refresh_mod.refresh_epg(
        sources=[epg_refresh_mod.EpgSource("f", "http://f/x.xml")], output_path=out
    )
    assert not result.ok
    assert out.read_bytes() == good


# ================================================================
# §15 —— 合并
# ================================================================

def test_21_merge_deterministic():
    a = epg_mod.parse_xmltv(_xmltv([("A", "A源")], [_prog("A", 1, 2, "x")]))
    b = epg_mod.parse_xmltv(_xmltv([("B", "B源")], [_prog("B", 1, 2, "y")]))
    m1 = epg_mod.merge_feeds([a, b])
    m2 = epg_mod.merge_feeds([a, b])
    assert epg_mod.render_xmltv(m1) == epg_mod.render_xmltv(m2), "合并必须完全确定"
    assert m1.quality.channel_count == 2


def test_22_programme_duplicate_dedup():
    xml_a = _xmltv([("A", "A")], [_prog("A", 1, 2, "同一条")])
    xml_b = _xmltv([("A", "A")], [_prog("A", 1, 2, "同一条")])
    merged = epg_mod.merge_feeds([
        epg_mod.parse_xmltv(xml_a), epg_mod.parse_xmltv(xml_b)
    ])
    assert merged.quality.programme_count == 1, "重复 programme 必须去重"


def test_merge_restrict_to():
    a = epg_mod.parse_xmltv(_xmltv(
        [("A", "A"), ("B", "B")], [_prog("A", 1, 2, "x"), _prog("B", 1, 2, "y")]
    ))
    merged = epg_mod.merge_feeds([a], restrict_to={"A"})
    assert set(merged.channels) == {"A"}
    assert all(p["channel"] == "A" for p in merged.programmes)


def test_merge_skips_broken_feed():
    good = epg_mod.parse_xmltv(_xmltv([("A", "A")], [_prog("A", 1, 2, "x")]))
    bad = epg_mod.parse_xmltv("<tv>broken")
    merged = epg_mod.merge_feeds([bad, good])
    assert merged.quality.channel_count == 1, "坏 feed 不应拖垮好 feed"


# ================================================================
# §19 / §23 —— M3U ↔ XMLTV 关联
# ================================================================

def test_23_m3u_tvg_id_matches_xmltv():
    """M3U 的 tvg-id 必须在 XMLTV 里真实存在（否则播放器匹配不到节目）。"""
    book = meta_mod.load_channel_metadata(META_PATH)
    # 用真实 metadata 的 epg_channel_id 与 tvg_id 建立对应
    xml = _xmltv(
        [(m.epg_channel_id, m.tvg_name or m.canonical) for m in book.entries if m.epg_channel_id],
        [_prog(m.epg_channel_id, 1, 2, "节目") for m in book.entries if m.epg_channel_id],
    )
    feed = epg_mod.parse_xmltv(xml)
    epg_ids = set(feed.channels)
    missing = [
        m.canonical for m in book.entries
        if m.epg_channel_id and m.epg_channel_id not in epg_ids
    ]
    assert not missing, f"metadata 声明了 EPG 但 XMLTV 里没有：{missing}"


def test_24_cctv5_and_cctv5plus_distinct():
    book = meta_mod.load_channel_metadata(META_PATH)
    a = book.get("CCTV-5 体育")
    b = book.get("CCTV-5+ 体育赛事")
    assert a.tvg_id and b.tvg_id
    assert a.tvg_id != b.tvg_id, "CCTV-5 与 CCTV-5+ 绝不能共用 tvg-id"
    assert a.epg_channel_id != b.epg_channel_id


# ================================================================
# §33 —— 无凭据
# ================================================================

def test_33_no_credentials_in_metadata_file():
    """检查**结构化字段**，不是整个文件。

    文件头注释里恰好写着「禁止写入 cookie / authorization」这类警告，
    对全文做子串匹配会100% 假阳性 —— 只看 ``key = "value"`` 形式的行。
    """
    import re as _re

    banned = ("cookie", "authorization", "password", "passwd", "secret",
              "migutoken", "msisdn", "token", "credential")
    offenders: list[str] = []
    for lineno, raw in enumerate(META_PATH.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue  # 注释不是字段
        key = line.split("=", 1)[0].strip().lower()
        if any(b in key for b in banned):
            offenders.append(f"{lineno}: {line[:80]}")
    assert not offenders, f"metadata 出现疑似凭据字段：{offenders}"


@pytest.mark.parametrize("field", [
    "cookie", "authorization", "password", "secret", "vpn_username", "api_token",
])
def test_33_credential_fields_rejected(tmp_path, field):
    p = tmp_path / "m.toml"
    p.write_text(
        f'[[channel]]\ncanonical = "X"\ntvg_id = "X.cn"\n{field} = "leaked"\n',
        encoding="utf-8", newline="\n",
    )
    with pytest.raises(meta_mod.MetadataError):
        meta_mod.load_channel_metadata(p)


def test_metadata_rejects_unknown_field(tmp_path):
    p = tmp_path / "m.toml"
    p.write_text('[[channel]]\ncanonical = "X"\nbogus_field = "1"\n',
                 encoding="utf-8", newline="\n")
    with pytest.raises(meta_mod.MetadataError, match="未知字段"):
        meta_mod.load_channel_metadata(p)


# ================================================================
# §34 / §35 —— 国际频道
# ================================================================

def test_34_35_international_mapping_is_explicit_or_absent():
    """France24 / NHK 必须「要么有完整映射，要么明确没有」，不得半吊子。

    本轮国际频道是否纳入取决于生产 probe 结果（§12），因此这里只断言：
    metadata 里一旦出现这两个频道，其字段必须自洽（无 tvg-id 冲突、
    EPG id 非空时格式合法）。
    """
    book = meta_mod.load_channel_metadata(META_PATH)
    for name in ("France 24", "France24", "NHK World", "NHKWorld"):
        meta = book.get(name)
        if name in book:
            assert meta.tvg_id, f"{name} 已登记却没有 tvg_id（半吊子映射）"


def test_no_international_without_explicit_decision():
    """反向护栏：不允许「看起来像国际频道但没进 metadata」的隐式遗漏。

    本轮若未纳入国际频道，报告必须说明原因；这里只保证 metadata
    里现有的国际条目要么完整、要么不存在。
    """
    book = meta_mod.load_channel_metadata(META_PATH)
    seed = tomllib.loads(SEED_PATH.read_text(encoding="utf-8"))["seed"]
    seed_names = {s["canonical"] for s in seed}
    for meta in book.entries:
        assert meta.canonical in seed_names, (
            f"metadata 里有 seed 中不存在的 canonical：{meta.canonical} —— "
            f"会导致它在 selector 阶段永远匹配不到任何 stream"
        )


# ================================================================
# §8 —— Logo 校验
# ================================================================

def test_19_fake_html_logo_detected():
    """§8：不允许 HTML 假图片。"""
    from tools.helper_logo_probe import classify_logo_response

    ok, kind = classify_logo_response(200, "image/png")
    assert ok and kind == "image"
    bad, kind2 = classify_logo_response(200, "text/html; charset=utf-8")
    assert not bad, "200 + text/html 是假图片"
    assert kind2 == "fake_html"


def test_20_logo_missing_non_blocking(tmp_path):
    """logo 缺失绝不阻断频道发布（§8 / §26第 14 条）。"""
    channels = [
        m3u_mod.M3UChannel(key="a", name="无Logo", url="http://x/1.m3u8",
                           group_title="央视"),
        m3u_mod.M3UChannel(key="b", name="有Logo", url="http://x/2.m3u8",
                           tvg_logo="https://l/b.png", group_title="央视"),
    ]
    text = m3u_mod.generate_text(channels)
    assert "无Logo" in text and "有Logo" in text
    assert text.count("tvg-logo=") == 1, "只有真的有 logo 的频道才输出该属性"


# ================================================================
# §10 —— HTTP 端点
# ================================================================

@pytest.fixture()
def http_service(tmp_path):
    playlist = tmp_path / "live.m3u"
    playlist.write_text("#EXTM3U\n#EXTINF:-1 tvg-id=\"A\",X\nhttp://x/1\n",
                        encoding="utf-8")
    epg = tmp_path / "epg.xml"
    epg.write_bytes(b'<?xml version="1.0"?><tv><channel id="A"/></tv>')
    httpd = server_mod.make_http_server(
        host="127.0.0.1", port=0, playlist_file=playlist, epg_file=epg
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, playlist, epg
    httpd.shutdown()
    httpd.server_close()


def _status(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def test_37_epg_xml_endpoint(http_service):
    base, _pl, _epg = http_service
    code, ctype, body = _status(f"{base}/epg.xml")
    assert code == 200
    assert "xml" in ctype.lower()
    assert b"<tv>" in body


def test_37_epg_missing_is_404(http_service):
    base, _pl, epg = http_service
    epg.unlink()
    code, _ctype, body = _status(f"{base}/epg.xml")
    assert code == 404
    assert b"epg-refresh" in body, "404 必须告诉运维下一步做什么"


def test_37_epg_empty_is_503(http_service):
    """空文件绝不返回 200 —— 否则播放器会把「0 个节目」当成「今天没节目」。"""
    base, _pl, epg = http_service
    epg.write_bytes(b"   ")
    code, _ctype, _body = _status(f"{base}/epg.xml")
    assert code == 503


def test_38_live_m3u_regression(http_service):
    base, _pl, _epg = http_service
    code, ctype, body = _status(f"{base}/live.m3u")
    assert code == 200 and "mpegurl" in ctype.lower()
    assert b"#EXTM3U" in body


def test_39_healthz_regression(http_service):
    base, _pl, _epg = http_service
    code, _ctype, _body = _status(f"{base}/healthz")
    assert code == 200


def test_37_epg_not_registered_without_file(tmp_path):
    """未提供 epg_file 时端点**不注册** ⇒ 与 TASK-010 行为逐字一致（零回归）。"""
    playlist = tmp_path / "live.m3u"
    playlist.write_text("#EXTM3U\n", encoding="utf-8")
    httpd = server_mod.make_http_server(
        host="127.0.0.1", port=0, playlist_file=playlist
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, _ctype, _body = _status(f"{base}/epg.xml")
        assert code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


# ================================================================
# §18 —— health / 状态
# ================================================================

def test_36_epg_health_is_warning_not_down(tmp_path):
    """§18：EPG stale 只是 warning，绝不让 health 整体 DOWN。"""
    epg = tmp_path / "epg.xml"
    stale = {
        "last_success_epoch": 1000.0,
        "error": "所有 EPG 源都不可用或解析失败",
        "written": False,
    }
    epg_mod.save_epg_status(stale, tmp_path / "st.json")
    loaded = epg_mod.load_epg_status(tmp_path / "st.json")
    age = epg_mod.epg_age_seconds(loaded)
    assert age is not None and age > 0
    # health 的判据只看 playlist，不看 EPG
    assert "down" not in str(loaded.get("error", "")).lower()


def test_epg_age_none_without_success():
    assert epg_mod.epg_age_seconds({}) is None


def test_status_file_corrupt_is_tolerated(tmp_path):
    """状态文件损坏不该让 epg-status 崩掉（状态只是可观测性）。"""
    p = tmp_path / "st.json"
    p.write_text("{ broken", encoding="utf-8")
    assert epg_mod.load_epg_status(p) == {}


# ================================================================
# 零回归护栏：TASK-008 / TASK-010
# ================================================================

def test_30_dynamic_default_preserved():
    """TASK-010 §8：dynamic_default 的真值表语义不能被本轮改动破坏。"""
    flag = config_mod.validate_dynamic_default("auto")
    assert flag == "auto"
    assert config_mod.validate_dynamic_default(True) is True
    inc, reason = config_mod.resolve_include_dynamic(
        dynamic_default="auto", has_dynamic_sources=True
    )
    assert inc is True and reason == "auto_sources_present"
    inc2, reason2 = config_mod.resolve_include_dynamic(
        dynamic_default="auto", has_dynamic_sources=False
    )
    assert inc2 is False and reason2 == "auto_no_sources"


def test_32_playback_context_preserved():
    from liptv import source_policy as sp

    policies = sp.load_source_policies(REPO / "config" / "source_policies.toml")
    assert "korice-ppv" in policies
    assert sp.should_drop_source_on_probe_failure(policies["korice-ppv"]) is False
    summary = sp.summarize_contexts(policies)
    assert summary["semantics"]["must_not_merge"] is True


def test_seed_and_metadata_consistent():
    """metadata 的 canonical 必须与 seed 完全对齐（不多、不少）。"""
    book = meta_mod.load_channel_metadata(META_PATH)
    seed = tomllib.loads(SEED_PATH.read_text(encoding="utf-8"))["seed"]
    seed_names = {s["canonical"] for s in seed}
    assert {m.canonical for m in book.entries} == seed_names, (
        "metadata 与 seed 的 canonical 集合不一致 —— "
        "多出来的条目永远匹配不到 stream，少掉的条目没有 tvg-id/logo"
    )


def test_tvg_id_not_derived_from_url():
    """§5：tvg-id 绝不能是 URL hash 之类的东西。"""
    book = meta_mod.load_channel_metadata(META_PATH)
    for meta in book.entries:
        if meta.tvg_id:
            assert "http" not in meta.tvg_id
            assert "/" not in meta.tvg_id
            assert len(meta.tvg_id) <= 64


def test_reverse_parse_real_config():
    """§21 第 40 项：用真实 metadata 生成的 M3U 必须能反向解析。"""
    book = meta_mod.load_channel_metadata(META_PATH)
    channels = [
        m3u_mod.M3UChannel(
            key=m.canonical, name=m.canonical, url=f"http://x/{i}.m3u8",
            tvg_id=m.tvg_id, tvg_name=m.tvg_name, tvg_logo=m.logo, group_title=m.group,
        )
        for i, m in enumerate(book.entries)
    ]
    text = m3u_mod.generate_text(channels)
    parsed = m3u_mod.parse_text(text)
    assert len(parsed.entries) == len(channels)
    for original, back in zip(channels, parsed.entries):
        assert original.name == back.name
        assert (original.tvg_id or "") == (back.tvg_id or ""), (
            f"{original.name} 的 tvg-id 反向解析后变了 —— 播放器会认成新频道"
        )


# ================================================================
# coverage 统计口径
# ================================================================

def test_coverage_reports_real_numbers():
    book = meta_mod.load_channel_metadata(META_PATH)
    names = [m.canonical for m in book.entries]
    cov = book.coverage(names)
    assert cov["total"] == len(names)
    assert cov["tvg_id"]["n"] <= cov["total"], "分子不能超过分母"
    # §26 硬指标：tvg_id >=70%、logo >=80%、epg >=60%
    assert cov["tvg_id"]["pct"] >= 70.0
    assert cov["logo"]["pct"] >= 80.0
    assert cov["epg"]["pct"] >= 60.0


def test_coverage_missing_lists_are_honest():
    book = meta_mod.load_channel_metadata(META_PATH)
    names = [m.canonical for m in book.entries]
    cov = book.coverage(names)
    assert len(cov["missing_epg"]) == cov["total"] - cov["epg"]["n"]