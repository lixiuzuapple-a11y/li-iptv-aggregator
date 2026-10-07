"""选线规则测试（V1_RUNTIME_FLOW §5 / §6）。"""

from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace

from liptv import repo, select
from liptv.util import dt_to_iso, iso_to_dt
from tests.conftest import NOW

BASE = iso_to_dt(NOW)


def ago(**kwargs) -> str:
    return dt_to_iso(BASE - _dt.timedelta(**kwargs))


def build_channel(conn, urls: list[str], *, name="频道X", category="新闻") -> int:
    """建一个 canonical channel，并给每条 URL 建一条 stream。"""
    canonical_id = repo.add_canonical_channel(conn, name, category=category, now=NOW)
    source_id = repo.add_source(conn, "src", "local-m3u", now=NOW)
    for index, url in enumerate(urls):
        entry = SimpleNamespace(name=f"raw-{index}", url=url, tvg_id=None,
                                tvg_logo=None, group_title=category)
        source_channel_id, _ = repo.upsert_source_channel(conn, source_id, entry, now=NOW)
        repo.bind_source_channel(conn, source_channel_id, canonical_id, now=NOW)
    repo.sync_streams(conn, now=NOW)
    return canonical_id


def add_probe(conn, stream_id: int, *, when: str, ok: bool = True,
              startup_ms: int | None = None, resolution: tuple[int, int] | None = None,
              bitrate: int | None = None, probe: str = "windows-local",
              error_type: str | None = None) -> None:
    probe_id = repo.ensure_probe(conn, probe)
    width, height = resolution or (None, None)
    repo.add_probe_result(
        conn, stream_id=stream_id, probe_id=probe_id, success=ok, checked_at=when,
        startup_ms=startup_ms if ok else None,
        resolution_width=width if ok else None,
        resolution_height=height if ok else None,
        bitrate_kbps=bitrate if ok else None,
        error_type=error_type,
    )


def test_higher_success_rate_wins(conn):
    cid = build_channel(conn, ["http://a/1.m3u8", "http://a/2.m3u8"])
    s1, s2 = [int(s["id"]) for s in repo.list_streams(conn, cid)]

    for hour in (1, 2, 3):
        add_probe(conn, s1, when=ago(hours=hour), ok=True, startup_ms=500)
    add_probe(conn, s1, when=ago(hours=4), ok=False, error_type="timeout")
    for hour in (1, 2, 3, 4):
        add_probe(conn, s2, when=ago(hours=hour), ok=True, startup_ms=900)

    best = select.select_best_stream(conn, cid, now=NOW)
    assert best is not None
    assert best.stream_id == s2
    assert best.success_rate == 1.0


def test_no_probe_results_means_no_output(conn):
    """V1_RUNTIME_FLOW §6：没有达到最低阈值的线路则暂不输出。"""
    cid = build_channel(conn, ["http://a/1.m3u8"])
    assert select.select_best_stream(conn, cid, now=NOW) is None

    scores = select.score_streams(conn, cid, now=NOW)
    assert scores[0].eligible is False
    assert scores[0].reason == "窗口内无测活记录"


def test_consecutive_failures_exclude_stream(conn):
    cid = build_channel(conn, ["http://a/1.m3u8", "http://a/2.m3u8"])
    s1, s2 = [int(s["id"]) for s in repo.list_streams(conn, cid)]

    add_probe(conn, s1, when=ago(days=5), ok=True, startup_ms=400)
    for hours in (1, 2, 3):
        add_probe(conn, s1, when=ago(hours=hours), ok=False, error_type="timeout")
    add_probe(conn, s2, when=ago(hours=1), ok=True, startup_ms=1500)

    scores = {s.stream_id: s for s in select.score_streams(conn, cid, now=NOW)}
    assert scores[s1].consecutive_failures == 3
    assert scores[s1].eligible is False
    assert "连续失败" in scores[s1].reason

    best = select.select_best_stream(conn, cid, now=NOW)
    assert best is not None and best.stream_id == s2


def test_max_consecutive_failures_is_configurable(conn):
    cid = build_channel(conn, ["http://a/1.m3u8"])
    stream_id = int(repo.list_streams(conn, cid)[0]["id"])
    add_probe(conn, stream_id, when=ago(days=5), ok=True, startup_ms=400)
    for hours in (1, 2, 3):
        add_probe(conn, stream_id, when=ago(hours=hours), ok=False)

    assert select.select_best_stream(conn, cid, now=NOW) is None
    relaxed = select.select_best_stream(
        conn, cid, now=NOW, max_consecutive_failures=5
    )
    assert relaxed is not None


def test_more_recent_success_wins_when_stability_ties(conn):
    cid = build_channel(conn, ["http://a/1.m3u8", "http://a/2.m3u8"])
    s1, s2 = [int(s["id"]) for s in repo.list_streams(conn, cid)]

    add_probe(conn, s1, when=ago(days=6), ok=True, startup_ms=300)
    add_probe(conn, s2, when=ago(hours=2), ok=True, startup_ms=2000)

    best = select.select_best_stream(conn, cid, now=NOW)
    assert best is not None and best.stream_id == s2


def test_startup_speed_is_secondary_tiebreak(conn):
    cid = build_channel(conn, ["http://a/1.m3u8", "http://a/2.m3u8"])
    s1, s2 = [int(s["id"]) for s in repo.list_streams(conn, cid)]
    same_time = ago(hours=1)

    add_probe(conn, s1, when=same_time, ok=True, startup_ms=2000, resolution=(1920, 1080))
    add_probe(conn, s2, when=same_time, ok=True, startup_ms=300, resolution=(1280, 720))

    best = select.select_best_stream(conn, cid, now=NOW)
    assert best is not None and best.stream_id == s2
    assert best.median_startup_ms == 300


def test_resolution_and_bitrate_break_ties_at_equal_stability(conn):
    cid = build_channel(conn, ["http://a/1.m3u8", "http://a/2.m3u8"])
    s1, s2 = [int(s["id"]) for s in repo.list_streams(conn, cid)]
    same_time = ago(hours=1)

    add_probe(conn, s1, when=same_time, ok=True, startup_ms=500,
              resolution=(1920, 1080), bitrate=6000)
    add_probe(conn, s2, when=same_time, ok=True, startup_ms=500,
              resolution=(1280, 720), bitrate=9000)

    best = select.select_best_stream(conn, cid, now=NOW)
    assert best is not None and best.stream_id == s1
    assert best.pixels == 1920 * 1080


def test_results_outside_window_are_ignored(conn):
    cid = build_channel(conn, ["http://a/1.m3u8"])
    stream_id = int(repo.list_streams(conn, cid)[0]["id"])
    add_probe(conn, stream_id, when=ago(days=10), ok=True, startup_ms=300)

    assert select.select_best_stream(conn, cid, now=NOW) is None
    assert select.select_best_stream(conn, cid, now=NOW, window_days=14) is not None


def test_multiple_probes_are_aggregated(conn):
    """多探针：上海失败、Windows 成功，仍属于有效线路。"""
    cid = build_channel(conn, ["http://a/1.m3u8"])
    stream_id = int(repo.list_streams(conn, cid)[0]["id"])
    add_probe(conn, stream_id, when=ago(hours=1), ok=False, probe="shanghai-cloud",
              error_type="connect_timeout")
    add_probe(conn, stream_id, when=ago(hours=1), ok=True, probe="windows-local", startup_ms=700)

    best = select.select_best_stream(conn, cid, now=NOW)
    assert best is not None
    assert best.probe_count == 2
    assert best.success_rate == 0.5


def test_disabled_stream_is_not_considered(conn):
    cid = build_channel(conn, ["http://a/1.m3u8", "http://a/2.m3u8"])
    s1, s2 = [int(s["id"]) for s in repo.list_streams(conn, cid)]
    add_probe(conn, s1, when=ago(hours=1), ok=True, startup_ms=100)
    add_probe(conn, s2, when=ago(hours=1), ok=True, startup_ms=100)
    conn.execute("UPDATE stream SET enabled = 0 WHERE id = ?", (s1,))

    best = select.select_best_stream(conn, cid, now=NOW)
    assert best is not None and best.stream_id == s2


def test_playlist_selection_orders_by_category_and_priority(conn):
    sport = build_channel(conn, ["http://a/sport.m3u8"], name="体育台", category="体育")
    news = build_channel(conn, ["http://a/news.m3u8"], name="新闻台", category="新闻")
    empty = build_channel(conn, ["http://a/empty.m3u8"], name="无声台", category="音乐")

    for cid in (sport, news):
        stream_id = int(repo.list_streams(conn, cid)[0]["id"])
        add_probe(conn, stream_id, when=ago(hours=1), ok=True, startup_ms=500)

    result = select.select_playlist(
        conn, group_order=["体育", "新闻", "影视", "纪录片", "港澳台", "国际", "音乐"],
        # 🚨 必须显式注入 now：select_playlist 不传时用真实墙钟算 7 日窗口，
        # 而本文件的 probe 记录固定在 conftest.NOW（2026-09-30）。墙钟一旦
        # 跨过 7 日边界，全部记录落到窗口外 ⇒ entries 变空 ⇒ 假失败。
        # 这条测试是写死时间戳 + 真实墙钟的典型时间炸弹，2026-10-07 首次引爆。
        now=NOW,
    )
    assert [e["name"] for e in result["entries"]] == ["体育台", "新闻台"]
    assert [s["name"] for s in result["skipped"]] == ["无声台"]
    assert all(e["url"] for e in result["entries"])
