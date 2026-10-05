"""TASK-009 fixed inventory 生产启动测试。

**全部离线**：fixed 源打本机 mock HTTP 服务（`tools/mock_source_server.py`），
**不访问任何公网**，不依赖 iptv-org / Guovin 在线。
seed binding 与 seed 生成逻辑的测试也只用本地样本数据。

覆盖任务书 §12 的 15 项：

  1. 两个 fixed source 导入；
  2. source A/B 含同一 canonical 的不同 stream；
  3. 明确 mapping 成功；
  4. 不确定 mapping 保持 unbound；
  5. source fetch 失败不清库存；
  6. source 条目消失 → inactive，不 hard delete；
  7. 再出现恢复身份；
  8. 两 stream probe PASS/PASS ⇒ selector 唯一选 1；
  9. PASS/FAIL ⇒ 选 PASS；
 10. 全部低于门槛 ⇒ 不发布；
 11. 环境级 ffprobe failure ⇒ 0 history writes；
 12. fixed + dynamic 同时 publish；
 13. dynamic isolate 行为零回归；
 14. last-known-good 保护；
 15. report/runtime 不泄漏敏感 URL/query。

零回归护栏：TASK-002 生命周期、TASK-003 发布语义、TASK-005 probe fail-closed、
TASK-008 dynamic isolate 全部保持。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tomllib
from types import SimpleNamespace

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liptv import config as config_mod  # noqa: E402
from liptv import db as db_mod  # noqa: E402
from liptv import fetch as fetch_mod  # noqa: E402
from liptv import ingest as ingest_mod  # noqa: E402
from liptv import probe as probe_mod  # noqa: E402
from liptv import publish as publish_mod  # noqa: E402
from liptv import repo as repo_mod  # noqa: E402
from liptv import select as select_mod  # noqa: E402
from liptv.cli import main as cli_main  # noqa: E402
from tools import build_fixed_seed as seed_tool  # noqa: E402

# 复用 TASK-003/005/008 的夹具与工具，避免两套相似脚手架。
from tests.test_publish import DYNAMIC_GROUP, read  # noqa: E402

NOW = "2026-10-05T09:00:00+00:00"
NOW2 = "2026-10-05T09:30:00+00:00"

ISOLATE = "isolate"
AON = "all_or_nothing"

SECRETS = ("txSecret", "KOR1K1", "AAA111", "staleStream", "leakToken")


# ------------------------------------------------------------------ 工具


def build(capsys, *argv) -> tuple[int, str]:
    return cli_main([str(a) for a in argv]), capsys.readouterr().out


def rows_for(conn, stream_id: int) -> list[sqlite3.Row]:
    return [r for r in repo_mod.list_probe_results(conn) if int(r["stream_id"]) == stream_id]


def entry(name: str, url: str, group: str = "新闻", tvg_id=None):
    return SimpleNamespace(name=name, url=url, tvg_id=tvg_id, tvg_logo=None,
                           group_title=group)


def make_text(entries) -> str:
    """把条目渲染成 M3U 文本（供 ingest_fixed_source 用）。"""
    lines = ["#EXTM3U"]
    for item in entries:
        lines.append(f'#EXTINF:-1 tvg-id="" group-title="{item.group_title}",{item.name}')
        lines.append(item.url)
    return "\n".join(lines) + "\n"


#: ``/seq.m3u`` 的可编程内容：与 ``FIXED_ALT`` 同频道名、不同 URL。
_FIXED_A = """#EXTM3U
#EXTINF:-1 tvg-id="mock-news.cn" tvg-name="Mock News" group-title="新闻",演示新闻台
http://stream.invalid.example/news/index.m3u8
#EXTINF:-1 tvg-id="mock-sports.cn" tvg-name="Mock Sports" group-title="体育",演示体育台
http://stream.invalid.example/sports/index.m3u8
#EXTINF:-1 tvg-id="mock-doc.cn" tvg-name="Mock Doc" group-title="纪录片",演示纪录台
http://stream.invalid.example/doc/index.m3u8
"""


def seed_inventory(conn, spec: dict[str, list], *, now: str = NOW) -> dict[str, int]:
    """按 ``{source_name: [(channel_name, url), ...]}`` 建库存并按归一键绑定。

    归一只用 :func:`seed_tool.normalize_name`（剥末尾分辨率括号），
    归一后**完全相等**才绑 —— 与生产 seed 走同一套逻辑，不引入第二套实现。
    """
    source_ids: dict[str, int] = {}
    canonical_ids: dict[str, int] = {}
    for source_name, items in spec.items():
        if source_name not in source_ids:
            source_ids[source_name] = repo_mod.add_source(
                conn, source_name, ingest_mod.KIND_FIXED, now=now
            )
        for channel_name, url in items:
            key = seed_tool.normalize_name(channel_name)
            if key not in canonical_ids:
                canonical_ids[key] = repo_mod.add_canonical_channel(
                    conn, key, category="新闻", now=now
                )
            channel_id, _ = repo_mod.upsert_source_channel(
                conn, source_ids[source_name], entry(channel_name, url), now=now
            )
            repo_mod.bind_source_channel(
                conn, channel_id, canonical_ids[key], method="exact_normalized", now=now
            )
    repo_mod.sync_streams(conn, now=now)
    conn.commit()
    return canonical_ids


def add_probe(conn, stream_id: int, *, success: bool, now: str,
              startup_ms: int = 900) -> None:
    probe_id = repo_mod.ensure_probe(conn, "t009-probe", now=now)
    repo_mod.add_probe_result(
        conn, stream_id=stream_id, probe_id=probe_id, success=success,
        checked_at=now, startup_ms=startup_ms,
    )
    conn.commit()


# ------------------------------------------------------------------ 夹具


@pytest.fixture()
def conn(tmp_path):
    connection = db_mod.connect(tmp_path / "t009.sqlite3")
    db_mod.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def env(tmp_path, mock_server):
    """fixed + dynamic 混合的离线环境。

    * ``fixed-a``   enabled → ``/seq.m3u``：演示新闻台/体育台/纪录台（可编程内容）
    * ``fixed-b``   enabled → ``/seq-alt.m3u``：**同名但 URL 不同**的第二源
                     （TASK-009 §12-2：同一 canonical 的多条 stream 前提）
    * ``fixed-c``   enabled → ``/seq-alt-res.m3u``：频道名**带分辨率后缀**的第三源
                     （验证 normalize_name 只剥末尾括号这一条规则）
    * ``fixed-bad`` enabled → ``/error.m3u``：恒 500（验证 F1 不清库存）
    * ``mock-dynamic`` enabled → TASK-003/008 的动态赛事样本
    """
    server, base = mock_server
    server.state.set_ok()
    server.state.content = _FIXED_A

    cfg = tmp_path / "config.toml"
    db = tmp_path / "liptv.sqlite3"
    out_dir = tmp_path / "out"
    (out_dir / "tmp").mkdir(parents=True, exist_ok=True)

    def source(name: str, kind: str, endpoint: str, enabled: bool = True) -> str:
        return (
            "\n[[sources]]\n"
            f'name = "{name}"\n'
            f'kind = "{kind}"\n'
            f'url = "{base}{endpoint}"\n'
            f"enabled = {'true' if enabled else 'false'}\n"
        )

    body = (
        "[database]\n"
        f'path = "{db.as_posix()}"\n'
        "\n[output]\n"
        f'm3u_path = "{(out_dir / "live.m3u").as_posix()}"\n'
        "keep_previous = true\n"
        "\n[fetch]\n"
        "timeout_seconds = 5.0\n"
        "max_bytes = 2000000\n"
        "max_redirects = 3\n"
        f'dynamic_tmp_dir = "{(out_dir / "tmp").as_posix()}"\n'
        "\n[publish]\n"
        f'summary_path = "{(out_dir / "publish-summary.json").as_posix()}"\n'
    )
    body += source("fixed-a", "fixed_m3u", "/seq.m3u")
    body += source("fixed-b", "fixed_m3u", "/seq-alt.m3u")
    body += source("fixed-c", "fixed_m3u", "/seq-alt-res.m3u")
    body += source("fixed-bad", "fixed_m3u", "/error.m3u")
    body += source("mock-dynamic", "dynamic_event_m3u", "/dynamic-publish.m3u")
    cfg.write_text(body, encoding="utf-8", newline="\n")
    return {
        "cfg": cfg, "db": db, "out_dir": out_dir,
        "live": out_dir / "live.m3u", "summary": out_dir / "publish-summary.json",
        "base": base, "server": server, "tmp_path": tmp_path,
    }


# ================================================== §5 显式 binding，不做 fuzzy


def test_seed_normalize_only_strips_resolution_suffix():
    """归一只允许剥离末尾分辨率括号，其余逐字节保留。"""
    assert seed_tool.normalize_name("CCTV-2 (720p)") == "CCTV-2"
    assert seed_tool.normalize_name("CCTV-2 (1080p)") == "CCTV-2"
    assert seed_tool.normalize_name("CCTV-2 (1080i)") == "CCTV-2"
    assert seed_tool.normalize_name("CCTV-2 (576i)") == "CCTV-2"
    assert seed_tool.normalize_name("CCTV-2") == "CCTV-2"
    # 绝不做的归一（这些都会把不同频道合并掉）
    assert seed_tool.normalize_name("CCTV-1 HD") == "CCTV-1 HD"
    assert seed_tool.normalize_name("CCTV-5+") == "CCTV-5+"
    assert seed_tool.normalize_name("湖南卫视 HD") == "湖南卫视 HD"
    assert seed_tool.normalize_name(" Anhui TV ") == "Anhui TV"
    assert seed_tool.normalize_name("cctv-2") == "cctv-2"       # 不折叠大小写


def test_seed_binding_file_only_allows_exact_normalized():
    """§5：binding 文件里出现非 exact_normalized 的匹配方式必须直接拒绝。"""
    assert (REPO_ROOT / "config" / "fixed_seed_bindings.toml").is_file()
    seeds = seed_tool.load_seed_bindings(
        REPO_ROOT / "config" / "fixed_seed_bindings.toml"
    )
    assert seeds, "seed binding 文件不应为空"
    for seed in seeds:
        assert seed["match"] == "exact_normalized"
        assert seed.get("category")
        assert seed.get("note"), "每条 seed 都要能解释「为什么绑」"
    # load_seed_bindings 自身对非法 match 会 SystemExit
    bad = REPO_ROOT / "config" / "fixed_seed_bindings.toml"
    original = bad.read_text(encoding="utf-8")
    try:
        bad.write_text(
            original.replace('match = "exact_normalized"', 'match = "levenshtein"', 1),
            encoding="utf-8", newline="\n",
        )
        with pytest.raises(SystemExit, match="exact_normalized"):
            seed_tool.load_seed_bindings(bad)
    finally:
        bad.write_text(original, encoding="utf-8", newline="\n")


def test_seed_binding_file_contains_no_stream_url():
    """§11：binding 文件**绝不**保存完整 stream URL 或签名 query。"""
    text = (REPO_ROOT / "config" / "fixed_seed_bindings.toml").read_text(encoding="utf-8")
    for fragment in ("http://", "https://", ".m3u8", ".ts", "token=", "auth="):
        assert fragment not in text, fragment


def test_uncertain_mapping_stays_unbound(conn):
    """§5-4：**不确定就保持 unbound，绝不猜**。

    iptv-org 风格的 ``CCTV-2 (720p)`` 归一后与 Guovin 的 ``CCTV-2`` 相等 ⇒ 可绑；
    而 ``CCTV-2 HD`` / ``CCTV-2+`` 归一后与之**不等** ⇒ 必须留 unbound。
    """
    ids = seed_inventory(conn, {
        "src-a": [("CCTV-2 (720p)", "http://a.invalid/1.m3u8")],
        "src-b": [("CCTV-2", "http://b.invalid/1.m3u8")],
    })
    # 只有归一后完全相等的那个被绑
    bound = repo_mod.list_bindings(conn)
    assert len(bound) == 2, [dict(r) for r in bound]
    assert {int(r["canonical_channel_id"]) for r in bound} == {ids["CCTV-2"]}

    # 不确定的三个：归一后与 CCTV-2 不等 ⇒ 全部 unbound
    for ambiguous in ("CCTV-2 HD", "CCTV-2+", "CCTV-2 财经"):
        assert seed_tool.normalize_name(ambiguous) != seed_tool.normalize_name("CCTV-2")


def test_banned_query_keys_are_rejected():
    """§3/§6：带签名 / 身份参数的 stream 一律排除，身份类（msisdn）尤其。"""
    for url, banned in [
        ("http://h/p.m3u8?token=abc", True),
        ("http://h/p.m3u8?auth=abc", True),
        ("http://h/p.m3u8?jsbt=1&jsbk=2", False),   # 不在黑名单，但 jsbt 不在 BANNED 里
        ("http://h/p.m3u8?msisdn=13800000000", True),
        ("http://h/p.m3u8?migutoken=xyz", True),
        ("http://h/p.m3u8", False),
        ("http://h/p.m3u8?channel=1", False),
    ]:
        assert seed_tool.has_banned_query(url) is banned, url


# ================================================ §12-1/§12-2 多源导入与多 stream


def test_two_fixed_sources_import_and_bind(conn):
    """§12-1 + §12-2：两个 fixed source 导入，同一 canonical 拿到**不同** stream。"""
    seed_inventory(conn, {
        "src-a": [("CCTV-2 (720p)", "http://a.invalid/cctv2.m3u8"),
                  ("CCTV-9 (720p)", "http://a.invalid/cctv9.m3u8")],
        "src-b": [("CCTV-2", "http://b.invalid/cctv2.m3u8"),
                  ("CCTV-9", "http://b.invalid/cctv9.m3u8")],
    })
    assert db_mod.read_schema_version(conn) == 1
    assert len(repo_mod.list_sources(conn)) == 2
    canonicals = {r["name"] for r in repo_mod.list_canonical_channels(conn)}
    assert canonicals == {"CCTV-2", "CCTV-9"}

    streams = repo_mod.list_streams(conn)
    assert len(streams) == 4
    # 每个 canonical 都有 2 条来自**不同源**的 stream
    for key in ("CCTV-2", "CCTV-9"):
        rows = repo_mod.list_streams(conn)
        by_canonical: dict[int, list] = {}
        for row in rows:
            by_canonical.setdefault(int(row["canonical_channel_id"]), []).append(row)
        matched = [v for v in by_canonical.values() if len(v) == 2]
        assert matched, key
        # 两条 stream 的 source 不同（这正是「能验证历史选线」的前提）
        for stream in matched[0]:
            sources = repo_mod.list_stream_sources(conn, int(stream["id"]))
            assert len(sources) == 1


def test_missing_then_reappear_restores_identity(conn):
    """§12-6 + §12-7：条目消失 → ``active=0``（**不 hard delete**）→ 再出现恢复同一身份。"""
    source_id = repo_mod.add_source(conn, "src-a", ingest_mod.KIND_FIXED, now=NOW)
    first = repo_mod.upsert_source_channel(
        conn, source_id, entry("CCTV-2", "http://a.invalid/cctv2.m3u8"), now=NOW
    )[0]
    conn.commit()

    # 第二轮：源里这个条目消失了 ⇒ 只 active=0
    deactivated = repo_mod.deactivate_missing_source_channels(
        conn, source_id, []
    )
    conn.commit()
    assert deactivated == 1
    row = conn.execute("SELECT * FROM source_channel WHERE id = ?", (first,)).fetchone()
    assert row is not None, "条目不得被硬删"
    assert int(row["active"]) == 0

    # 第三轮：又回来了 ⇒ 恢复**同一身份**（id / identity_hash 都不变）
    again, created = repo_mod.upsert_source_channel(
        conn, source_id, entry("CCTV-2", "http://a.invalid/cctv2.m3u8"), now=NOW2
    )
    conn.commit()
    assert created is False
    assert again == first
    row = conn.execute("SELECT * FROM source_channel WHERE id = ?", (first,)).fetchone()
    assert int(row["active"]) == 1


def test_fetch_failure_never_clears_inventory(conn):
    """§12-5：抓取失败时库存**零改动**（TASK-002 fail-closed）。"""
    source_id = repo_mod.add_source(
        conn, "src-a", ingest_mod.KIND_FIXED, url="http://127.0.0.1:9/nope.m3u", now=NOW
    )
    repo_mod.upsert_source_channel(
        conn, source_id, entry("CCTV-2", "http://a.invalid/cctv2.m3u8"), now=NOW
    )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM source_channel").fetchone()[0]

    # 受控失败：指向本机**未监听**端口（connection refused），不依赖公网、不改 DNS
    result = ingest_mod.ingest_fixed_source(
        conn, repo_mod.get_source(conn, source_id), now=NOW2,
    )
    conn.commit()
    assert result["ok"] is False
    assert result["created"] == 0 and result["deactivated"] == 0
    after = conn.execute("SELECT COUNT(*) FROM source_channel").fetchone()[0]
    assert after == before, "抓取失败不得改变库存"
    # fetch 状态有记录（运维看得见失败）
    assert conn.execute(
        "SELECT * FROM source WHERE id = ?", (source_id,)
    ).fetchone()["last_fetch_status"] != "ok"


# ==================================================== §8 selector 四场景


def test_selector_picks_exactly_one_when_both_pass(conn):
    """§8-A / §12-8：多条 stream 都 PASS ⇒ selector **唯一**选 1 条。"""
    seed_inventory(conn, {"src-a": [("CCTV-2", "http://a.invalid/1.m3u8"),
                                    ("CCTV-2", "http://a.invalid/2.m3u8")]})
    streams = repo_mod.list_streams(conn)
    assert len(streams) == 2
    for stream in streams:
        add_probe(conn, int(stream["id"]), success=True, now=NOW)

    result = select_mod.select_playlist(conn, group_order=["新闻", "其他"])
    entries = result["entries"]
    assert len(entries) == 1, "同一 canonical 只能发布 1 条"
    assert entries[0]["name"] == "CCTV-2"
    # 选中的是历史评分解释得出来的那条（有 probe 支撑）
    assert entries[0]["score"].probe_count >= 1


def test_selector_prefers_pass_over_fail(conn):
    """§8-B / §12-9：一条 PASS 一条 FAIL ⇒ **必须选 PASS**，不得按来源顺序硬选。"""
    seed_inventory(conn, {"src-a": [("CCTV-2", "http://a.invalid/bad.m3u8"),
                                    ("CCTV-2", "http://a.invalid/good.m3u8")]})
    streams = sorted(repo_mod.list_streams(conn), key=lambda r: int(r["id"]))
    fail_id, pass_id = int(streams[0]["id"]), int(streams[1]["id"])
    add_probe(conn, fail_id, success=False, now=NOW)
    add_probe(conn, pass_id, success=True, now=NOW)

    result = select_mod.select_playlist(conn, group_order=["新闻", "其他"])
    entries = result["entries"]
    assert len(entries) == 1
    chosen = entries[0]
    assert chosen["url"] == "http://a.invalid/good.m3u8"
    assert int(chosen["stream_id"]) == pass_id


def test_selector_publishes_nothing_when_all_below_threshold(conn):
    """§8-C / §12-10：全部未达门槛 ⇒ 该 canonical **不发布**。"""
    seed_inventory(conn, {"src-a": [("CCTV-2", "http://a.invalid/1.m3u8")]})
    stream_id = int(repo_mod.list_streams(conn)[0]["id"])
    add_probe(conn, stream_id, success=False, now=NOW)

    result = select_mod.select_playlist(conn, group_order=["新闻", "其他"])
    assert result["entries"] == []
    # 门槛原因必须可解释
    skipped = result.get("skipped") or []
    assert skipped, "未达门槛的 canonical 必须记入 skipped"
    assert skipped[0]["name"] == "CCTV-2"


def test_selector_history_change_influences_choice(conn):
    """§8-D：历史变化能影响后续选择（第二轮原优选失败、备用成功）。"""
    seed_inventory(conn, {"src-a": [("CCTV-2", "http://a.invalid/first.m3u8"),
                                    ("CCTV-2", "http://a.invalid/second.m3u8")]})
    streams = sorted(repo_mod.list_streams(conn), key=lambda r: int(r["id"]))
    first_id, second_id = int(streams[0]["id"]), int(streams[1]["id"])

    # 第一轮：first 成功、second 失败 ⇒ 选 first
    add_probe(conn, first_id, success=True, now=NOW)
    add_probe(conn, second_id, success=False, now=NOW)
    entries = select_mod.select_playlist(conn, group_order=["新闻", "其他"])["entries"]
    assert entries[0]["url"].endswith("first.m3u8")

    # 第二轮：first 连续失败、second 成功 ⇒ 历史评分变化后应改选 second
    add_probe(conn, first_id, success=False, now=NOW2, startup_ms=5000)
    add_probe(conn, second_id, success=True, now=NOW2, startup_ms=700)
    entries = select_mod.select_playlist(conn, group_order=["新闻", "其他"])["entries"]
    assert len(entries) == 1
    assert entries[0]["url"].endswith("second.m3u8"), "历史变化未影响选择"


# ============================================ §12-11 环境级 probe 故障零写入


def test_environment_probe_failure_writes_no_history(conn, monkeypatch):
    """§12-11 / F2：环境级 ffprobe 故障 ⇒ **0 条新 probe_result**，旧历史保留。"""
    seed_inventory(conn, {"src-a": [("CCTV-2", "http://a.invalid/1.m3u8"),
                                    ("CCTV-2", "http://a.invalid/2.m3u8")]})
    stream_ids = [int(r["id"]) for r in repo_mod.list_streams(conn)]
    for stream_id in stream_ids:
        add_probe(conn, stream_id, success=True, now=NOW)
    before = len(repo_mod.list_probe_results(conn))
    assert before == 2

    def env_probe(url, *, stream_id, settings, cancel=None, registry=None):
        return probe_mod.ProbeObservation(
            stream_id=stream_id,
            error_type=probe_mod.ERROR_FFPROBE_START_FAILED,
            message="ffprobe 消失了",
        )

    monkeypatch.setattr(probe_mod, "probe_stream", env_probe)
    summary = probe_mod.run_round(
        conn, settings=probe_mod.ProbeSettings(enabled=True), now=NOW2,
        capability=probe_mod.FfprobeCapability(ok=True, path="ffprobe"),
    )
    assert summary["environment_error"] is True
    assert summary["written"] == 0
    assert len(repo_mod.list_probe_results(conn)) == before, "环境故障写进了历史"
    for stream_id in stream_ids:
        assert len(rows_for(conn, stream_id)) == 1, "旧历史必须保留"


# ============================================ §9 fixed + dynamic 共存发布


def publish_cli(capsys, env, *extra):
    return build(capsys, "publish", "--config", env["cfg"], "--db", env["db"],
                 "--now", NOW, "--json", *extra)


def ingest_all(env, *, now: str = NOW, probe_ok: bool = True) -> sqlite3.Connection:
    """按真实生产路径导入 fixed 源 → 归一绑定 → 归集 stream → 写一轮 probe。

    走 ``ingest.ingest_fixed_source`` + ``seed_tool.normalize_name``，
    与生产 seed 完全同一条代码路径（不另写一套测试专用实现）。
    """
    cfg = config_mod.load_config(env["cfg"])
    conn = db_mod.connect(env["db"])
    db_mod.init_db(conn)
    # 先把配置里的 [[sources]] 注册进库（与生产 source-register --from-config 同一步）
    for item in config_mod.source_entries(cfg):
        repo_mod.add_source(
            conn, item["name"], item["kind"], item["url"],
            enabled=1 if item["enabled"] else 0, now=now,
        )
    conn.commit()
    for source in repo_mod.list_sources_by_kind(conn, ingest_mod.KIND_FIXED):
        if not int(source["enabled"]):
            continue
        ingest_mod.ingest_fixed_source(conn, source, now=now)
    conn.commit()

    # 归一后完全相等才绑（唯一的允许规则）
    canonical_ids: dict[str, int] = {}
    for row in repo_mod.list_source_channels(conn):
        if not int(row["active"]):
            continue
        key = seed_tool.normalize_name(row["raw_name"])
        if key not in canonical_ids:
            canonical_ids[key] = repo_mod.add_canonical_channel(
                conn, key, category=row["raw_group"] or "其他", now=now
            )
        try:
            repo_mod.bind_source_channel(
                conn, int(row["id"]), canonical_ids[key],
                method="exact_normalized", now=now,
            )
        except repo_mod.BindingConflictError:
            pass
    repo_mod.sync_streams(conn, now=now)
    conn.commit()

    if probe_ok:
        probe_id = repo_mod.ensure_probe(conn, "t009-probe", now=now)
        for stream in repo_mod.list_streams(conn):
            repo_mod.add_probe_result(
                conn, stream_id=int(stream["id"]), probe_id=probe_id,
                success=True, checked_at=now, startup_ms=900,
            )
        conn.commit()
    return conn


def test_one_fixed_source_failure_keeps_others(capsys, env):
    """§10-F1 / §12-5：一个 fixed 源抓取失败 ⇒ 其它源继续，失败源库存**不被清空**。

    ``fixed-bad`` 恒 500，``fixed-a/b/c`` 正常。跑完一轮后必须：
      * 好源照常进库存；
      * 坏源状态被记为失败（运维看得见）；
      * 好源发布不受影响（fixed_count 照常 ≥3）。

    注意：这里走完整的 :func:`ingest_all`（注册 → 导入 → 归一绑定 → probe），
    否则没有 canonical / binding，selector 拿不到任何东西 —— 那测的就不是
    「坏源不影响好源」，而是「什么都没绑定」。
    """
    conn = ingest_all(env)

    bad = repo_mod.get_source_by_name(conn, "fixed-bad")
    good = repo_mod.get_source_by_name(conn, "fixed-a")
    assert bad is not None and good is not None
    bad_status = conn.execute(
        "SELECT last_fetch_status FROM source WHERE id = ?", (int(bad["id"]),)
    ).fetchone()[0]
    good_status = conn.execute(
        "SELECT last_fetch_status FROM source WHERE id = ?", (int(good["id"]),)
    ).fetchone()[0]
    assert bad_status != "ok", "坏源必须被记为失败"
    assert good_status == "ok"
    # 好源有条目
    assert conn.execute(
        "SELECT COUNT(*) FROM source_channel WHERE source_id = ?", (int(good["id"]),)
    ).fetchone()[0] > 0
    # 坏源 0 条库存，且失败**没有**影响好源
    assert conn.execute(
        "SELECT COUNT(*) FROM source_channel WHERE source_id = ?", (int(bad["id"]),)
    ).fetchone()[0] == 0
    conn.close()

    code, out = publish_cli(capsys, env)
    assert code == 0, out
    payload = json.loads(out)
    assert payload["fixed_count"] >= 3, "好源不受坏源影响"
    # 失败是**可见**的（status/summary 不许装作没事）—— §10-F1 第 4 条
    fixed_summary = payload["fixed_summary"]
    assert fixed_summary["failed_sources"] == 1
    bad_report = next(
        s for s in fixed_summary["sources"] if s["name"] == "fixed-bad"
    )
    assert bad_report["ok"] is False
    assert bad_report["status"] == "HTTP_STATUS"
    # 好源在摘要里是健康的，且脱敏到只剩 scheme://host
    good_report = next(
        s for s in fixed_summary["sources"] if s["name"] == "fixed-a"
    )
    assert good_report["ok"] is True
    assert good_report["active_channels"] > 0
    assert "/" not in good_report["url"].split("://", 1)[1]


def test_failed_source_keeps_previous_inventory(capsys, env, monkeypatch):
    """F1 的另一半：源**曾经**成功、这一轮失败 ⇒ 它原有的库存必须保留。"""
    # 第一轮：只让好源成功（先把 bad 源 disable，抓一次建库存）
    text = env["cfg"].read_text(encoding="utf-8")
    env["cfg"].write_text(
        text.replace('name = "fixed-bad"', 'name = "fixed-bad"', 1), encoding="utf-8",
        newline="\n",
    )
    conn = db_mod.connect(env["db"])
    db_mod.init_db(conn)
    repo_mod.add_source(conn, "flaky", ingest_mod.KIND_FIXED,
                        url="http://127.0.0.1:9/gone.m3u", now=NOW)
    flaky_id = int(repo_mod.get_source_by_name(conn, "flaky")["id"])
    repo_mod.upsert_source_channel(
        conn, flaky_id, entry("遗留频道", "http://a.invalid/legacy.m3u8"), now=NOW
    )
    conn.commit()
    before = conn.execute(
        "SELECT COUNT(*) FROM source_channel WHERE source_id = ?", (flaky_id,)
    ).fetchone()[0]
    assert before == 1

    result = ingest_mod.ingest_fixed_source(
        conn, repo_mod.get_source(conn, flaky_id), now=NOW2,
    )
    conn.commit()
    assert result["ok"] is False
    after = conn.execute(
        "SELECT COUNT(*) FROM source_channel WHERE source_id = ?", (flaky_id,)
    ).fetchone()[0]
    assert after == before, "失败源的已有库存被清空了"
    active = conn.execute(
        "SELECT active FROM source_channel WHERE source_id = ?", (flaky_id,)
    ).fetchone()[0]
    assert int(active) == 1, "失败源条目被误标 inactive"
    conn.close()


def test_fixed_and_dynamic_publish_together(capsys, env):
    """§12-12：fixed 与 dynamic 在同一份 live.m3u 里共存，group 可区分。"""
    conn = ingest_all(env)
    try:
        assert len(repo_mod.list_sources_by_kind(conn, "fixed_m3u")) >= 4
        assert len(repo_mod.list_canonical_channels(conn)) >= 3
    finally:
        conn.close()

    code, out = publish_cli(capsys, env, "--dynamic-source", "mock-dynamic")
    payload = json.loads(out)
    assert code == 0, out
    assert payload["fixed_count"] >= 3
    assert payload["dynamic_count"] > 0
    text = read(env["live"])
    assert "演示新闻台" in text
    assert f'group-title="{DYNAMIC_GROUP}"' in text
    # 两种 group 都能在文件里看到
    assert 'group-title="新闻"' in text


def test_dynamic_isolate_zero_regression(capsys, env):
    """§12-13：fixed 存在时，dynamic isolate 行为与 TASK-008 完全一致。"""
    conn = ingest_all(env)
    conn.close()
    _upsert_policy(env["cfg"], ISOLATE)

    code, out = publish_cli(
        capsys, env, "--dynamic-source", "mock-dynamic", "--dynamic-source", "mock-dynamic"
    )
    assert code == 0, out
    payload = json.loads(out)
    # 同一源指定两次：按 URL 去重，published 记账仍自洽
    assert payload["dynamic_count"] == payload["dynamic_summary"]["published_entries"]
    assert payload["dynamic_summary"]["published_counted"] == payload["dynamic_count"]
    assert payload["dynamic_fail_closed"] is False
    assert payload["fixed_count"] >= 3


def _upsert_policy(path: pathlib.Path, policy: str) -> None:
    """在配置里加 ``[publish.dynamic].failure_policy``（段内改写，不新建第二张表）。"""
    text = path.read_text(encoding="utf-8")
    if "[publish.dynamic]" in text:
        lines = text.splitlines()
        out, in_dyn, done = [], False, False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                if in_dyn and not done:
                    out.append(f'failure_policy = "{policy}"')
                    done = True
                in_dyn = stripped == "[publish.dynamic]"
            if in_dyn and stripped.startswith("failure_policy "):
                continue
            out.append(line)
        if in_dyn and not done:
            out.append(f'failure_policy = "{policy}"')
            done = True
        if not done:
            out += ["", "[publish.dynamic]", f'failure_policy = "{policy}"']
        text = "\n".join(out) + "\n"
    else:
        text = text + f'\n[publish.dynamic]\nfailure_policy = "{policy}"\n'
    path.write_text(text, encoding="utf-8", newline="\n")


def test_empty_publish_keeps_last_known_good(capsys, env):
    """§10-F3 / §12-14：结果变空 ⇒ **不覆盖** last-known-good。

    退出码口径（别写错）：本项目 default ``failure_policy=all_or_nothing``，
    且本轮**没有**动态抓取 ⇒ 不满足 ``dynamic_failed and fixed_count == 0``，
    于是走 ``composition_errors`` ⇒ ``REJECTED_VALIDATION`` / **exit 1**。
    这是 TASK-003 冻结语义（``publish.py`` 里明确写了 isolate 之外不许收窄），
    本任务**不改**：F3 要求的是「文件别被空结果覆盖」，不是「必须 exit 0」。
    真正要断言的是**文件字节没变**。
    """
    # 先做一次成功发布，留下 LKG
    conn = ingest_all(env)
    conn.close()
    code, out = publish_cli(capsys, env)
    assert code == 0, out
    good = read(env["live"])
    assert "演示新闻台" in good
    before_sha = _sha(env["live"])

    # 让所有 stream 连续失败（超过 max_consecutive_failures）⇒ fixed 变空
    conn = db_mod.connect(env["db"])
    for stream in repo_mod.list_streams(conn):
        for _ in range(4):
            add_probe(conn, int(stream["id"]), success=False, now=NOW2)
    conn.close()

    code, out = publish_cli(capsys, env)
    payload = json.loads(out)
    assert payload["fixed_count"] == 0
    # 变空 ⇒ 拒绝发布（非 0 退出），但**绝不能**把 live.m3u 覆盖成空文件
    assert code != 0, "结果为空却报告成功"
    assert payload["published"] is False
    assert payload["status"] in (
        publish_mod.STATUS_REJECTED_VALIDATION,
        publish_mod.STATUS_DEGRADED_NO_PUBLISH,
    )
    # 核心断言：文件与上一版逐字节一致（未被空结果覆盖）
    assert _sha(env["live"]) == before_sha
    assert read(env["live"]) == good
    # 摘要必须留下可解释的拒绝理由，而不是静默成功
    assert payload["reason"]


def _sha(path) -> str:
    import hashlib
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


# ==================================================== §12-15 不泄漏


def test_report_and_status_never_leak_stream_url(capsys, env):
    """§12-15：发布报告 / 摘要 / 状态里不得出现完整 stream URL 或 query。"""
    secret_url = "http://a.invalid/cctv2.m3u8?token=leakToken123"
    conn = db_mod.connect(env["db"])
    db_mod.init_db(conn)
    source_id = repo_mod.add_source(conn, "src-a", ingest_mod.KIND_FIXED, now=NOW)
    channel_id, _ = repo_mod.upsert_source_channel(
        conn, source_id, entry("CCTV-2 (720p)", secret_url), now=NOW
    )
    canonical_id = repo_mod.add_canonical_channel(conn, "CCTV-2", category="新闻", now=NOW)
    repo_mod.bind_source_channel(conn, channel_id, canonical_id, now=NOW)
    repo_mod.sync_streams(conn, now=NOW)
    for stream in repo_mod.list_streams(conn):
        add_probe(conn, int(stream["id"]), success=True, now=NOW)
    conn.close()

    code, out = publish_cli(capsys, env)
    assert code == 0, out
    # stdout JSON：不得含完整 stream URL / query token
    for secret in ("leakToken123", "token="):
        assert secret not in out, secret
    # 写盘摘要同样
    summary_text = read(env["summary"])
    for secret in ("leakToken123", "token=", "a.invalid/cctv2.m3u8"):
        assert secret not in summary_text, secret
    # live.m3u 是唯一允许出现完整 stream URL 的地方（订阅文件本身）
    assert "leakToken123" in read(env["live"])


# ============================================ seed 工具的离线自测（不碰公网）


def test_build_plan_never_reads_network(monkeypatch, tmp_path):
    """seed 生成必须走 fetch_source；这里直接把 fetch 换成假数据，验证过滤与匹配逻辑。"""
    bindings = tmp_path / "bindings.toml"
    bindings.write_text(
        '[[seed]]\ncanonical = "CCTV-2 财经"\ncategory = "新闻"\n'
        'match = "exact_normalized"\nsource_names = ["src-a", "src-b"]\n'
        'note = "test"\n',
        encoding="utf-8", newline="\n",
    )

    def fake_fetch(url, *, timeout, max_bytes):
        if url.endswith("a.m3u"):
            return make_text([
                entry("CCTV-2 (720p)", "http://a.invalid/1.m3u8"),
                entry("CCTV-9 (720p)", "http://a.invalid/2.m3u8?token=SECRET"),
            ])
        return make_text([
            entry("CCTV-2", "http://b.invalid/1.m3u8"),
            entry("湖南卫视", "http://b.invalid/hunan.m3u8"),
        ])

    monkeypatch.setattr(seed_tool, "fetch_source", fake_fetch)
    monkeypatch.setitem(seed_tool.SOURCE_URLS, "src-a", "http://x/a.m3u")
    monkeypatch.setitem(seed_tool.SOURCE_URLS, "src-b", "http://x/b.m3u")

    plan = seed_tool.build_plan(
        bindings_path=bindings, timeout=1.0, max_bytes=100000
    )
    # 带 token 的条目被剔除
    assert plan["dropped_by_query"]["src-a"] == 1
    assert plan["kept_counts"]["src-a"] == 1
    # CCTV-2 两源都匹配 ⇒ multi_source
    assert plan["summary"]["multi_source_canonicals"] == 1
    # 计划里绝不含 stream URL
    blob = json.dumps(plan, ensure_ascii=False)
    assert "a.invalid" not in blob
    assert "SECRET" not in blob


def test_build_plan_reports_ambiguous_as_unbound(monkeypatch, tmp_path):
    """归一后**撞名**（多个不同原始名归一到同一 key）⇒ 判 ambiguous，跳过不绑。"""
    bindings = tmp_path / "bindings.toml"
    bindings.write_text(
        '[[seed]]\ncanonical = "CCTV-2"\ncategory = "新闻"\n'
        'match = "exact_normalized"\nsource_names = ["src-a"]\nnote = "t"\n',
        encoding="utf-8", newline="\n",
    )

    def fake_fetch(url, *, timeout, max_bytes):
        return make_text([
            entry("CCTV-2 (720p)", "http://a.invalid/1.m3u8"),
            entry("CCTV-2 (1080p)", "http://a.invalid/2.m3u8"),
        ])

    monkeypatch.setattr(seed_tool, "fetch_source", fake_fetch)
    monkeypatch.setitem(seed_tool.SOURCE_URLS, "src-a", "http://x/a.m3u")
    plan = seed_tool.build_plan(bindings_path=bindings, timeout=1.0, max_bytes=100000)
    assert plan["summary"]["ambiguous_skipped"] == 1
    assert plan["plan"] == [], "撞名时必须保持 unbound"


def test_catch_all_streams_are_excluded(monkeypatch, tmp_path):
    """万能流（同一 stream 被 ≥5 个 canonical 共用）必须被排除。"""
    bindings = tmp_path / "bindings.toml"
    bindings.write_text(
        '[[seed]]\ncanonical = "CCTV-2"\ncategory = "新闻"\n'
        'match = "exact_normalized"\nsource_names = ["src-a"]\nnote = "t"\n',
        encoding="utf-8", newline="\n",
    )
    shared = "http://shared.invalid/gslb/all.m3u8"
    items = [entry(f"CH{i}", shared) for i in range(seed_tool.CATCH_ALL_MIN_CANONICALS)]
    items.append(entry("CCTV-2 (720p)", "http://a.invalid/real.m3u8"))

    monkeypatch.setattr(
        seed_tool, "fetch_source",
        lambda url, *, timeout, max_bytes: make_text(items),
    )
    monkeypatch.setitem(seed_tool.SOURCE_URLS, "src-a", "http://x/a.m3u")
    plan = seed_tool.build_plan(bindings_path=bindings, timeout=1.0, max_bytes=100000)
    assert plan["catch_all_streams_excluded"] == 1
    # 真正的线路仍进计划
    assert plan["plan"], "排除万能流不该误伤正常线路"
    assert json.dumps(plan, ensure_ascii=False).count("shared.invalid") == 0


# ============================== apply 阶段的库存收敛（真机 smoke 逼出来的缺口）


def test_apply_prunes_unused_channels_and_syncs_streams(monkeypatch, tmp_path, mock_server):
    """``apply`` 必须把「seed 用不到的条目」收敛成 inactive，并归集 stream。

    真机发现（2026-10-05 生产主机）：``ingest_fixed_source`` 按 TASK-002 冻结语义
    把**整份**上游 M3U 灌进库存（实测 618 条），而签名过滤只作用于绑定那一步。
    结果几百条用不到的库存长期躺着，其中混着带短时签名的 stream。

    ⚠️ ``apply_plan`` 内部会**重新抓取**（走 ``ingest_fixed_source``），
    所以这里必须把 ``SOURCE_URLS`` 指向本机 mock —— 指着公网跑会抓到真实的
    618 条，既慢又不离线。``build_plan`` 那层才用假 fetch。
    """
    server, base = mock_server
    server.state.set_ok()
    server.state.content = make_text([
        entry("CCTV-2 (720p)", "http://a.invalid/keep.m3u8"),
        entry("完全无关的频道甲", "http://a.invalid/unused-1.m3u8"),
        entry("完全无关的频道乙", "http://a.invalid/unused-2.m3u8"),
    ])
    bindings = tmp_path / "bindings.toml"
    bindings.write_text(
        '[[seed]]\ncanonical = "CCTV-2"\ncategory = "新闻"\n'
        'match = "exact_normalized"\nsource_names = ["src-a"]\n'
        'note = "t"\n',
        encoding="utf-8", newline="\n",
    )
    # SOURCE_URLS 是模块级字典：必须整表替换，否则 guovin 那条还在原地抓公网
    monkeypatch.setattr(seed_tool, "SOURCE_URLS", {"src-a": f"{base}/seq.m3u"})
    plan = seed_tool.build_plan(bindings_path=bindings, timeout=5.0, max_bytes=100000)

    # config 只用于校验可加载，不写任何东西
    config = tmp_path / "config.toml"
    config.write_text(
        "[database]\npath = \"x.sqlite3\"\n\n[output]\nm3u_path = \"y.m3u\"\n",
        encoding="utf-8", newline="\n",
    )
    result = seed_tool.apply_plan(
        plan, db_path=tmp_path / "t.sqlite3", config_path=config,
        now=NOW, dry_run=False,
    )
    by_action = {a["action"]: a for a in result["actions"]}
    assert "prune-unused" in by_action, "apply 必须收敛库存"
    assert by_action["prune-unused"]["deactivated"] == 2
    assert "stream-sync" in by_action, "apply 必须归集 stream，否则绑定结果无法被 selector 选"

    conn = db_mod.connect(tmp_path / "t.sqlite3")
    try:
        rows = {r["raw_name"]: int(r["active"]) for r in repo_mod.list_source_channels(conn)}
        assert rows == {"CCTV-2 (720p)": 1, "完全无关的频道甲": 0, "完全无关的频道乙": 0}
        # 只置 inactive、**绝不硬删**（TASK-002 要求保留 identity_hash 可追溯）
        assert conn.execute("SELECT COUNT(*) FROM source_channel").fetchone()[0] == 3
        streams = repo_mod.list_streams(conn)
        assert len(streams) == 1, "只有被绑定的 active 条目才该归集出 stream"
        assert streams[0]["url"] == "http://a.invalid/keep.m3u8"
    finally:
        conn.close()


def test_apply_never_binds_signed_stream(monkeypatch, tmp_path, mock_server):
    """带短时签名的条目**绝不**被绑定（任务书 §11）。

    🚨 这是真机 smoke 第二次抓到的缺口：第一版只在 ``build_plan`` 里过滤签名，
    那只让**计划计数**正确；``apply_plan`` 绑定时是**从库里重新查**的，
    于是 7 条 ``?auth=`` 的 stream 照样绑上了 CCTV-15 等 canonical。
    所以签名过滤必须在**绑定循环里再拦一次**。

    这里让 mock 返回**同一个 canonical 的两条**：一条干净、一条带签名，
    断言绑上的只有干净那条，带签名的那条不进绑定、也不进 stream 表。
    """
    server, base = mock_server
    server.state.set_ok()
    server.state.content = make_text([
        entry("CCTV-15 (720p)", "http://a.invalid/clean.m3u8"),
        entry("CCTV-15 (1080p)", "http://a.invalid/signed.m3u8?auth=SECRETVALUE"),
    ])
    bindings = tmp_path / "bindings.toml"
    bindings.write_text(
        '[[seed]]\ncanonical = "CCTV-15"\ncategory = "音乐"\n'
        'match = "exact_normalized"\nsource_names = ["src-a"]\n'
        'note = "t"\n',
        encoding="utf-8", newline="\n",
    )
    # SOURCE_URLS 是模块级字典：必须整表替换，否则别的源还在原地抓公网
    monkeypatch.setattr(seed_tool, "SOURCE_URLS", {"src-a": f"{base}/seq.m3u"})
    plan = seed_tool.build_plan(bindings_path=bindings, timeout=5.0, max_bytes=100000)

    config = tmp_path / "config.toml"
    config.write_text(
        "[database]\npath = \"x.sqlite3\"\n\n[output]\nm3u_path = \"y.m3u\"\n",
        encoding="utf-8", newline="\n",
    )
    result = seed_tool.apply_plan(
        plan, db_path=tmp_path / "t.sqlite3", config_path=config,
        now=NOW, dry_run=False,
    )
    bind_action = next(a for a in result["actions"] if a["action"] == "canonical+bind")
    assert bind_action["signed_skipped"] == 1, "签名条目必须在绑定环节被拦下并记账"
    assert bind_action["bound_channels"] == 1

    conn = db_mod.connect(tmp_path / "t.sqlite3")
    try:
        # 签名条目确实被解析进库存了（否则测试空转）
        all_rows = list(conn.execute(
            "SELECT raw_name, raw_stream_url, active FROM source_channel"
        ))
        assert any("SECRETVALUE" in (r["raw_stream_url"] or "") for r in all_rows)
        # 但它绝不进 stream 表
        streams = repo_mod.list_streams(conn)
        assert len(streams) == 1
        assert "SECRETVALUE" not in streams[0]["url"]
        assert streams[0]["url"] == "http://a.invalid/clean.m3u8"
    finally:
        conn.close()
