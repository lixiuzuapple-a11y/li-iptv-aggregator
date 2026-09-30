"""CLI 端到端测试（TASK-001 验收目标 1/2/3/4/5/6/7/10）。"""

from __future__ import annotations

import json

import pytest

from liptv.cli import main as cli_main

NOW = "2026-09-30T12:00:00+00:00"
URL_CCTV_A = "http://src-a.example/cctv1/index.m3u8"
URL_CCTV_B = "http://src-b.example/cctv1/hd.m3u8"


def run(capsys, *argv):
    code = cli_main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code, captured.out


def run_json(capsys, *argv):
    code, out = run(capsys, *argv)
    assert code == 0, out
    return json.loads(out)


def test_cli_requires_initialized_db(capsys, db_path, source_a):
    with pytest.raises(SystemExit):
        cli_main(["source-list", "--db", str(db_path)])


def test_cli_init_db(capsys, db_path):
    payload = run_json(capsys, "init-db", "--db", db_path, "--json")
    assert payload["schema_version"] == 1
    assert "source_channel" in payload["tables"]
    assert [p["name"] for p in payload["probes"]] == ["shanghai-cloud", "windows-local"]


def test_cli_select_requires_target(capsys, db_path):
    run_json(capsys, "init-db", "--db", db_path, "--json")
    with pytest.raises(SystemExit):
        cli_main(["select", "--db", str(db_path)])


def test_cli_end_to_end_pipeline(capsys, db_path, source_a, source_b, tmp_path):
    db = str(db_path)
    run_json(capsys, "init-db", "--db", db, "--json")

    # 验收 1：导入本地 M3U
    imp_a = run_json(capsys, "import-m3u", source_a, "--source", "src-a",
                     "--db", db, "--now", NOW, "--json")
    imp_b = run_json(capsys, "import-m3u", source_b, "--source", "src-b",
                     "--db", db, "--now", NOW, "--json")
    assert imp_a["entries"] == 3 and imp_a["created"] == 3
    assert imp_b["entries"] == 3 and imp_b["created"] == 3
    assert imp_b["skipped"] == {"url_without_extinf": 1}

    # 验收 2：条目已写入 SQLite
    raw_a = run_json(capsys, "source-channel-list", "--source-id", imp_a["source_id"],
                     "--db", db, "--json")
    raw_b = run_json(capsys, "source-channel-list", "--source-id", imp_b["source_id"],
                     "--db", db, "--json")
    assert len(raw_a) == 3 and len(raw_b) == 3
    sc_a = next(r["id"] for r in raw_a if r["raw_name"] == "CCTV-1 综合")
    sc_b = next(r["id"] for r in raw_b if r["raw_name"] == "CCTV1 高清")
    sc_sport = next(r["id"] for r in raw_a if r["raw_name"] == "五星体育")

    # 验收 3：两个 source_channel → 一个 canonical_channel
    news = run_json(capsys, "canonical-add", "--name", "CCTV-1 综合", "--category", "新闻",
                    "--tvg-id", "cctv1.cn", "--logo", "http://logo.example/cctv1.png",
                    "--db", db, "--now", NOW, "--json")["canonical_channel_id"]
    sport = run_json(capsys, "canonical-add", "--name", "五星体育", "--category", "体育",
                     "--db", db, "--now", NOW, "--json")["canonical_channel_id"]

    run_json(capsys, "binding-add", "--source-channel-id", sc_a, "--canonical-id", news,
             "--db", db, "--now", NOW, "--json")
    run_json(capsys, "binding-add", "--source-channel-id", sc_b, "--canonical-id", news,
             "--db", db, "--now", NOW, "--json")
    run_json(capsys, "binding-add", "--source-channel-id", sc_sport, "--canonical-id", sport,
             "--db", db, "--now", NOW, "--json")

    bindings = run_json(capsys, "binding-list", "--canonical-id", news, "--db", db, "--json")
    assert len(bindings) == 2

    # 验收 4：一个 canonical_channel 保存多条 stream
    sync = run_json(capsys, "stream-sync", "--db", db, "--now", NOW, "--json")
    assert sync["streams_created"] == 3
    streams = run_json(capsys, "stream-list", "--canonical-id", news, "--sources",
                       "--db", db, "--json")
    assert len(streams) == 2
    assert {s["url"] for s in streams} == {URL_CCTV_A, URL_CCTV_B}
    assert all(s["sources"] for s in streams)
    by_url = {s["url"]: s["id"] for s in streams}

    # 验收 5：插入模拟 probe_result
    for offset, ok, startup in ((1, True, 400), (2, True, 450)):
        run_json(capsys, "probe-result-add", "--stream-id", by_url[URL_CCTV_A],
                 "--probe", "windows-local", "--checked-at", f"2026-09-30T11:0{offset}:00+00:00",
                 "--startup-ms", startup, "--resolution", "1920x1080", "--bitrate", 6000,
                 "--http-status", 200, "--db", db, "--json")
    for offset, ok, startup in ((1, True, 200), (2, True, 210), (3, False, 0)):
        argv = ["probe-result-add", "--stream-id", by_url[URL_CCTV_B],
                "--probe", "windows-local", "--checked-at", f"2026-09-30T11:0{offset}:00+00:00",
                "--startup-ms", startup, "--resolution", "1280x720",
                "--http-status", 200, "--db", db, "--json"]
        if not ok:
            argv.append("--fail")
        run_json(capsys, *argv)

    results = run_json(capsys, "probe-result-list", "--stream-id", by_url[URL_CCTV_A],
                       "--db", db, "--json")
    assert len(results) == 2

    # 验收 6：按简单规则选出最佳 stream（A 成功率 1.0 > B 0.67）
    selection = run_json(capsys, "select", "--canonical-id", news, "--now", NOW,
                         "--db", db, "--json")
    assert selection["best_stream_id"] == by_url[URL_CCTV_A]
    assert len(selection["candidates"]) == 2

    playlist = run_json(capsys, "select", "--all", "--now", NOW, "--db", db, "--json")
    assert [s["name"] for s in playlist["selected"]] == ["CCTV-1 综合"]
    assert [s["name"] for s in playlist["skipped"]] == ["五星体育"]

    # 验收 7：生成标准 live.m3u
    out = tmp_path / "out" / "live.m3u"
    stats = run_json(capsys, "generate-m3u", "--out", out, "--now", NOW, "--db", db, "--json")
    assert stats["channel_count"] == 1
    assert stats["previous"] is None

    text = out.read_text(encoding="utf-8")
    lines = text.strip().split("\n")
    assert lines[0] == "#EXTM3U"
    assert lines[1].startswith("#EXTINF:-1 ")
    assert 'tvg-id="cctv1.cn"' in lines[1]
    assert 'group-title="新闻"' in lines[1]
    assert lines[1].endswith(",CCTV-1 综合")
    assert lines[2] == URL_CCTV_A
    assert URL_CCTV_B not in text
    assert len(lines) == 3

    # 验收 10：状态总览
    status = run_json(capsys, "status", "--db", db, "--json")
    assert status["counts"]["canonical_channel"] == 2
    assert status["counts"]["stream"] == 3
    assert status["counts"]["probe_result"] == 5


def test_cli_generate_m3u_keeps_previous(capsys, db_path, source_a, tmp_path):
    db = str(db_path)
    run_json(capsys, "init-db", "--db", db, "--json")
    imp = run_json(capsys, "import-m3u", source_a, "--source", "src-a",
                   "--db", db, "--now", NOW, "--json")
    raw = run_json(capsys, "source-channel-list", "--db", db, "--json")
    sc = next(r["id"] for r in raw if r["raw_name"] == "CCTV-1 综合")
    cid = run_json(capsys, "canonical-add", "--name", "CCTV-1 综合", "--category", "新闻",
                   "--db", db, "--now", NOW, "--json")["canonical_channel_id"]
    run_json(capsys, "binding-add", "--source-channel-id", sc, "--canonical-id", cid,
             "--db", db, "--now", NOW, "--json")
    run_json(capsys, "stream-sync", "--db", db, "--now", NOW, "--json")
    stream_id = run_json(capsys, "stream-list", "--db", db, "--json")[0]["id"]
    run_json(capsys, "probe-result-add", "--stream-id", stream_id, "--probe", "windows-local",
             "--checked-at", NOW, "--startup-ms", 300, "--db", db, "--json")

    out = tmp_path / "live.m3u"
    run_json(capsys, "generate-m3u", "--out", out, "--now", NOW, "--db", db, "--json")
    second = run_json(capsys, "generate-m3u", "--out", out, "--now", NOW, "--db", db, "--json")
    assert second["previous"] == str(tmp_path / "live.previous.m3u")
    assert (tmp_path / "live.previous.m3u").exists()
    assert imp["source_id"] > 0


def test_cli_json_and_table_output_both_work(capsys, db_path, source_a):
    db = str(db_path)
    run_json(capsys, "init-db", "--db", db, "--json")
    run_json(capsys, "import-m3u", source_a, "--source", "src-a", "--db", db, "--now", NOW, "--json")

    code, out = run(capsys, "source-channel-list", "--db", db)
    assert code == 0
    assert "raw_name" in out
    assert "CCTV-1 综合" in out
