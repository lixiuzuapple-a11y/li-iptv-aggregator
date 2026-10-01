"""TASK-003 统一发布（组合 + 安全发布）测试。

全部离线：固定源与动态源都打本机 mock HTTP 服务（`tools/mock_source_server.py`），
**不访问任何公网地址**，也不依赖 JSNZKPG 在线或任何真实流可播。

覆盖：
  * 组合语义（固定优先、每 canonical 一条、动态独立分组、解说/原声保留、单源字节级去重、
    不跨来源去重、宣传/推广过滤、回放默认关闭）；
  * 失败与时间边界（动态失败降级/拒绝/不发布、不复用旧动态签名线路、动态空集、全空保护）；
  * 文件完整性（写临时文件/备份/replace 注入异常时当前与上一版都不半更新）；
  * 信息边界（签名 URL 只出现在 live.m3u，不进任何日志/摘要/JSON）。
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from liptv import db as db_mod
from liptv import ingest as ingest_mod
from liptv import m3u as m3u_mod
from liptv import publish as publish_mod
from liptv import repo
from liptv.cli import main as cli_main

NOW = "2026-09-30T12:00:00+00:00"

DYNAMIC_GROUP = publish_mod.DEFAULT_DYNAMIC_GROUP_TITLE

# 动态样本里刻意写进去的签名材料；任何输出里都不许出现
SECRETS = ("txSecret", "txTime", "AAA111", "BBB222", "CCC333", "GGG777")

# 可编程动态端点（/seq.m3u）用到的样本：3 条合格 + 1 条字节重复 + 回放/宣传/TG频道推广。
# 与 mock server 的 /dynamic-publish.m3u 语义一致，但由测试自己控制投放时机。
DYNAMIC_PUBLISH_SAMPLE = (
    "#EXTM3U\n"
    '#EXTINF:-1 tvg-id="live-mci-ars" group-title="正在直播",[解说] 曼城 vs 阿森纳\n'
    "http://jsnzkpg.invalid.example/live/mci-ars/pc.m3u8?txSecret=AAA111&txTime=6A1B2C3D\n"
    '#EXTINF:-1 tvg-id="live-mci-ars" group-title="正在直播",[原声] 曼城 vs 阿森纳\n'
    "http://jsnzkpg.invalid.example/live/mci-ars/raw.flv?txSecret=BBB222&txTime=6A1B2C3E\n"
    '#EXTINF:-1 tvg-id="live-mci-ars" group-title="正在直播",[解说] 曼城 vs 阿森纳\n'
    "http://jsnzkpg.invalid.example/live/mci-ars/pc.m3u8?txSecret=AAA111&txTime=6A1B2C3D\n"
    '#EXTINF:-1 group-title="即将开始",[解说] 皇马 vs 巴萨\n'
    "http://jsnzkpg.invalid.example/live/rma-bar/pc.m3u8?txSecret=CCC333&txTime=6A1B2C3F\n"
    '#EXTINF:-1 group-title="赛事回放",[回放] 曼联 vs 利物浦\n'
    "http://jsnzkpg.invalid.example/replay/mun-liv/pc.m3u8?txSecret=DDD444&txTime=6A1B2C40\n"
    '#EXTINF:-1 group-title="宣传",官方 App 下载入口\n'
    "http://jsnzkpg.invalid.example/promo/loop.m3u8?txSecret=EEE555&txTime=6A1B2C41\n"
    '#EXTINF:-1 group-title="✈️TG频道",赛事推送群\n'
    "http://jsnzkpg.invalid.example/tg/join.m3u8?txSecret=FFF666&txTime=6A1B2C42\n"
)


# ------------------------------------------------------------------ 通用工具

def build(capsys, *argv) -> tuple[int, str]:
    code = cli_main([str(a) for a in argv])
    return code, capsys.readouterr().out


def seed(capsys, cfg, db, *, fetch: bool = True) -> None:
    """初始化数据库 + 注册来源（+ 抓取固定源）。全部走 CLI，保持端到端。"""
    code, out = build(capsys, "init-db", "--config", cfg, "--db", db, "--json")
    assert code == 0, out
    code, out = build(capsys, "source-register", "--from-config", "--config", cfg,
                      "--db", db, "--json")
    assert code == 0, out
    if fetch:
        code, out = build(capsys, "fetch", "--all", "--config", cfg, "--db", db, "--json")
        assert code == 0, out


def add_programmable_dynamic(capsys, env, name: str) -> str:
    """注册一个指向 /seq.m3u（内容可由测试改写）的动态来源。"""
    code, out = build(capsys, "source-add", "--name", name, "--kind", "dynamic_event_m3u",
                      "--url", f"{env['base']}/seq.m3u", "--config", env["cfg"],
                      "--db", env["db"], "--json")
    assert code == 0, out
    return name


def add_dynamic_source(capsys, env, name: str, endpoint: str) -> str:
    """注册一个指向 mock server 固定端点的动态来源（如真实结构样本）。"""
    code, out = build(capsys, "source-add", "--name", name, "--kind", "dynamic_event_m3u",
                      "--url", f"{env['base']}{endpoint}", "--config", env["cfg"],
                      "--db", env["db"], "--json")
    assert code == 0, out
    return name


def bind_and_probe(db, *, now: str = NOW, probe: bool = True, probe_ok: bool = True) -> list[int]:
    """给每个 active 的 source_channel 建 canonical + 绑定 + 归集 stream（+ 写测活结果）。"""
    conn = db_mod.connect(db)
    canonical_ids: list[int] = []
    for sc in repo.list_source_channels(conn):
        if not int(sc["active"]):
            continue
        cid = repo.add_canonical_channel(
            conn, sc["raw_name"], category=(sc["raw_group"] or "其他"), now=now
        )
        repo.bind_source_channel(conn, int(sc["id"]), cid, now=now)
        canonical_ids.append(cid)
    repo.sync_streams(conn, now=now)

    if probe:
        probe_id = repo.ensure_probe(conn, "test-probe", now=now)
        for stream in repo.list_streams(conn):
            repo.add_probe_result(
                conn,
                stream_id=int(stream["id"]),
                probe_id=probe_id,
                success=probe_ok,
                checked_at=now,
                startup_ms=1200,
            )
    conn.commit()
    conn.close()
    return canonical_ids


def read(path) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------- 夹具

@pytest.fixture()
def env(tmp_path, mock_server):
    """离线发布环境：临时 config + db + out 目录，来源全部指向 mock server。

    来源可见性刻意做成有层次的：
      * ``mock-fixed``       fixed，enabled=true  → 进固定库存；
      * ``mock-dynamic``     dynamic，enabled=true  → 裸 ``--dynamic`` 默认只取它，
        样本 7 条里恰好 3 条合格，便于断言去重/过滤计数；
      * ``mock-dynamic-alt`` dynamic，enabled=false → 只在显式 ``--dynamic-source`` 时使用
        （验证「不跨来源去重」）；
      * ``mock-dynamic-bad`` dynamic，enabled=false → 恒 500，只在显式指定时用来测失败路径。

    alt/bad 若是 enabled，裸 ``--dynamic`` 就会被 bad 带崩成降级，计数断言全部失真，
    所以这里必须是 false。
    """
    server, base = mock_server
    server.state.set_ok()

    cfg = tmp_path / "config.toml"
    db = tmp_path / "liptv.sqlite3"
    out_dir = tmp_path / "out"
    (out_dir / "tmp").mkdir(parents=True, exist_ok=True)

    cfg.write_text(
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
        "\n[[sources]]\n"
        'name = "mock-fixed"\n'
        'kind = "fixed_m3u"\n'
        f'url = "{base}/seq.m3u"\n'
        "enabled = true\n"
        "\n[[sources]]\n"
        'name = "mock-dynamic"\n'
        'kind = "dynamic_event_m3u"\n'
        f'url = "{base}/dynamic-publish.m3u"\n'
        "enabled = true\n"
        "\n[[sources]]\n"
        'name = "mock-dynamic-alt"\n'
        'kind = "dynamic_event_m3u"\n'
        f'url = "{base}/dynamic-alt.m3u"\n'
        "enabled = false\n"
        "\n[[sources]]\n"
        'name = "mock-dynamic-bad"\n'
        'kind = "dynamic_event_m3u"\n'
        f'url = "{base}/error.m3u"\n'
        "enabled = false\n",
        encoding="utf-8",
    )
    return {
        "cfg": cfg,
        "db": db,
        "out_dir": out_dir,
        "live": out_dir / "live.m3u",
        "previous": out_dir / "live.previous.m3u",
        "summary": out_dir / "publish-summary.json",
        "base": base,
        "server": server,
        "tmp_path": tmp_path,
    }


@pytest.fixture()
def ready(capsys, env):
    """env 的进一步准备：建库、注册、抓固定源、绑定 + 写成功测活。"""
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    return env


def publish_cli(capsys, env, *extra):
    return build(capsys, "publish", "--config", env["cfg"], "--db", env["db"], "--now", NOW,
                 "--json", *extra)


# ==================================================================== 组合语义

def test_publish_composes_fixed_and_dynamic(capsys, ready):
    """离线 E2E：固定 + 动态合成一个合法 M3U，两类条目齐备。"""
    code, out = publish_cli(capsys, ready, "--dynamic")
    assert code == 0, out
    payload = json.loads(out)

    assert payload["status"] == publish_mod.STATUS_OK
    assert payload["fixed_count"] == 3        # mock OK_M3U：新闻/体育/纪录片
    assert payload["dynamic_count"] == 3      # 解说 + 原声 + 即将开始
    assert payload["channel_count"] == 6
    assert payload["published"] is True

    text = read(ready["live"])
    parsed = m3u_mod.parse_text(text)
    assert parsed.has_header is True
    assert parsed.entry_count == 6
    assert parsed.skipped.get("extinf_without_url", 0) == 0

    groups = [e.group_title for e in parsed.entries]
    # 固定频道在前、按配置分类顺序（默认 category_order 里「体育」在「新闻」之前）；
    # 动态赛事独立分组在最后
    assert groups[:3] == ["体育", "新闻", "纪录片"]
    assert groups[3:] == [DYNAMIC_GROUP] * 3

    names = [e.name for e in parsed.entries]
    assert "[解说] 曼城 vs 阿森纳" in names
    assert "[原声] 曼城 vs 阿森纳" in names     # 解说/原声都保留，不合并


def test_publish_fixed_first_and_one_entry_per_canonical(capsys, ready):
    """每个 canonical 最多一条线路；固定频道不得重复出现在输出里。"""
    code, out = publish_cli(capsys, ready)
    assert code == 0, out
    parsed = m3u_mod.parse_text(read(ready["live"]))
    fixed_urls = [e.url for e in parsed.entries]
    assert len(fixed_urls) == len(set(fixed_urls)) == 3


def test_publish_without_dynamic_never_hits_network(capsys, ready, monkeypatch):
    """默认路径完全不联网：不传 --dynamic 时连一次 HTTP 都不该发。"""
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("默认 publish 不应该发起任何网络请求")

    monkeypatch.setattr(ingest_mod.fetch_mod, "fetch_text", boom)
    code, out = publish_cli(capsys, ready)
    assert code == 0, out
    payload = json.loads(out)
    assert payload["include_dynamic"] is False
    assert payload["dynamic_count"] == 0
    assert payload["channel_count"] == 3
    assert called["n"] == 0
    assert DYNAMIC_GROUP not in read(ready["live"])


def test_publish_skips_channels_without_probe_history(capsys, env):
    """未探测过的固定 stream 不得被发布（不绕过最低成功阈值）。"""
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"], probe=False)          # 建了 stream，但没有 probe_result
    code, out = publish_cli(capsys, env)
    assert code == 1, out                            # 组合结果为空 → 拒绝
    payload = json.loads(out)
    assert payload["status"] == publish_mod.STATUS_REJECTED_VALIDATION
    assert payload["published"] is False
    assert not env["live"].exists()


def test_publish_skips_channels_with_failed_probes(capsys, env):
    """探针记录全部失败 → 该频道不可用 → 无内容可发布。"""
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"], probe=True, probe_ok=False)
    code, out = publish_cli(capsys, env)
    assert code == 1, out
    assert json.loads(out)["status"] == publish_mod.STATUS_REJECTED_VALIDATION


def test_publish_dedupes_only_byte_identical_within_one_source(capsys, ready):
    """同源内字节相同的重复条目被去掉，并记录可解释的理由与数量。"""
    code, out = publish_cli(capsys, ready, "--dynamic-source", "mock-dynamic")
    payload = json.loads(out)
    assert code == 0, out

    report = next(r for r in payload["dynamic_sources"] if r["source_name"] == "mock-dynamic")
    assert report["fetched_entries"] == 7
    assert report["included"] == 3
    labels = report["excluded_by_reason"]
    assert labels[publish_mod.REASON_LABELS[publish_mod.REASON_DUPLICATE]] == 1
    assert payload["dynamic_count"] == 3


def test_publish_does_not_dedupe_across_dynamic_sources(capsys, ready):
    """不同动态来源各自保留自己的条目，不跨来源去重。"""
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "mock-dynamic", "--dynamic-source", "mock-dynamic-alt"
    )
    payload = json.loads(out)
    assert code == 0, out
    assert len(payload["dynamic_sources"]) == 2
    assert payload["dynamic_count"] == 4
    names = [e.name for e in m3u_mod.parse_text(read(ready["live"])).entries]
    assert "[解说] 曼城 vs 阿森纳" in names
    assert "[解说] 拜仁 vs 多特" in names


def test_publish_filters_promo_and_keyword_groups(capsys, ready):
    """宣传分组与 ✈️TG频道 推广入口默认都不纳入，且理由分类正确。"""
    code, out = publish_cli(capsys, ready, "--dynamic-source", "mock-dynamic")
    payload = json.loads(out)
    assert code == 0, out
    report = payload["dynamic_sources"][0]
    labels = report["excluded_by_reason"]

    assert labels[publish_mod.REASON_LABELS[publish_mod.REASON_EXCLUDED_GROUP]] == 1
    assert labels[publish_mod.REASON_LABELS[publish_mod.REASON_EXCLUDED_KEYWORD]] == 1

    text = read(ready["live"])
    assert "官方 App" not in text
    assert "TG频道" not in text


def test_publish_real_upstream_structure_by_default(capsys, env):
    """QA-003A 永久回归：真实上游结构（联赛名分组 + `# ===== 直播/回放 =====` 注释分区）。

    样本见 ``tools/mock_source_server.py::DYNAMIC_REAL_STRUCTURE_M3U``，
    **全假域名 + 合成令牌**，不含任何上游真实地址或签名。

    期望：默认策略下各联赛分组被纳入、宣传与推广入口被排除、回放（分组名或分区）被排除、
    [解说]/[原声] 两个变体各自保留、同源字节重复只留一条。
    """
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    add_dynamic_source(capsys, env, "dynamic-real-shape", "/dynamic-real-structure.m3u")

    code, out = publish_cli(capsys, env, "--dynamic-source", "dynamic-real-shape")
    payload = json.loads(out)
    assert code == 0, out

    report = payload["dynamic_sources"][0]
    assert report["fetched_entries"] == 11
    # 这是本次修复的核心断言：旧默认策略下这里会是 0
    assert report["included"] == 5
    assert payload["dynamic_count"] == 5
    assert payload["status"] == publish_mod.STATUS_OK

    labels = report["excluded_by_reason"]
    assert labels[publish_mod.REASON_LABELS[publish_mod.REASON_DUPLICATE]] == 1
    assert labels[publish_mod.REASON_LABELS[publish_mod.REASON_EXCLUDED_GROUP]] == 1
    assert labels[publish_mod.REASON_LABELS[publish_mod.REASON_EXCLUDED_KEYWORD]] == 1
    assert labels[publish_mod.REASON_LABELS[publish_mod.REASON_REPLAY_DISABLED]] == 2
    assert labels[publish_mod.REASON_LABELS[publish_mod.REASON_REPLAY_SECTION]] == 1
    # 没开白名单，就不该出现「不在白名单内」这个理由
    assert publish_mod.REASON_LABELS[publish_mod.REASON_NOT_IN_INCLUDE_LIST] not in labels

    # 分区统计（可解释性）：两个注释分区都看到了
    assert report["sections_seen"]["正在直播"] >= 1
    assert report["sections_seen"]["赛事回放"] >= 1

    text = read(env["live"])
    names = [e.name for e in m3u_mod.parse_text(text).entries]
    # 各联赛分组进来了，解说/原声都保留
    assert "[解说] 纽约自由人 vs 拉斯维加斯王牌" in names
    assert "[原声] 纽约自由人 vs 拉斯维加斯王牌" in names
    assert "[解说] 甲队 vs 乙队" in names
    assert "[解说] 丙队 vs 丁队" in names
    assert "[解说] U21 戊队 vs 己队" in names
    # 宣传、推广入口、回放（含「回放分区里分组写联赛名」那条）一律不进来
    assert "官方 App" not in text
    assert "TG频道@stymei" not in text
    assert "旧比赛之一" not in text
    assert "上周的自由人" not in text
    # 签名材料仍然只允许存在于 live.m3u 之外的地方
    for secret in ("H1H1H1", "H8H8H8", "HAHAHA"):
        assert secret not in out


def test_publish_replay_is_off_by_default_and_switchable(capsys, env, tmp_path):
    """赛事回放默认排除；显式打开后纳入（仍在白名单内）。"""
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])

    code, out = publish_cli(capsys, env, "--dynamic-source", "mock-dynamic")
    assert code == 0, out
    assert "曼联 vs 利物浦" not in read(env["live"])

    env["cfg"].write_text(
        read(env["cfg"]) + "\n[publish.dynamic]\ninclude_replay = true\n", encoding="utf-8"
    )
    code, out = publish_cli(capsys, env, "--dynamic-source", "mock-dynamic")
    assert code == 0, out
    assert "曼联 vs 利物浦" in read(env["live"])


def test_publish_allowlist_mode_excludes_groups_not_listed(capsys, env):
    """**显式**配置 include_groups 后进入严格白名单模式：表外分组不纳入并记录理由。

    （默认是排除法，不给白名单；此用例锁的是「白名单模式仍可用」这条退路。）
    """
    env["cfg"].write_text(
        read(env["cfg"]) + '\n[publish.dynamic]\ninclude_groups = ["正在直播"]\n',
        encoding="utf-8",
    )
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    add_programmable_dynamic(capsys, env, "dynamic-odd")

    # 动态侧返回一个既不在白名单、也不命中排除规则的分组名
    env["server"].state.content = (
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="其他推广",某条内容\n'
        "http://odd.invalid.example/x.m3u8?txSecret=ZZZ\n"
    )
    code, out = publish_cli(capsys, env, "--dynamic-source", "dynamic-odd")
    payload = json.loads(out)
    assert code == 0, out
    labels = payload["dynamic_sources"][0]["excluded_by_reason"]
    assert labels[publish_mod.REASON_LABELS[publish_mod.REASON_NOT_IN_INCLUDE_LIST]] == 1
    assert payload["dynamic_count"] == 0
    assert "某条内容" not in read(env["live"])


def test_publish_unknown_group_is_included_by_default(capsys, env):
    """默认（未配置白名单）时，未被排除规则拦下的分组一律纳入 —— 这就是 QA-003A 的修复点。"""
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    add_programmable_dynamic(capsys, env, "dynamic-league")

    env["server"].state.content = (
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="玻利杯",某场联赛\n'
        "http://league.invalid.example/x.m3u8?txSecret=ZZZ\n"
    )
    code, out = publish_cli(capsys, env, "--dynamic-source", "dynamic-league")
    payload = json.loads(out)
    assert code == 0, out
    assert payload["dynamic_count"] == 1
    assert not payload["dynamic_sources"][0]["excluded_by_reason"]
    assert "某场联赛" in read(env["live"])


def test_classify_dynamic_entry_default_allowlist_and_section_modes(capsys, ready):
    """纯函数层面：默认排除法 / 显式白名单 / 注释分区识别 / include_replay 开关。"""
    filters = publish_mod.normalize_dynamic_filters(None)

    # 默认：只做排除，联赛名与旧的「正在直播」分组都保留
    assert publish_mod.classify_dynamic_entry("正在直播", filters=filters) == (True, None)
    assert publish_mod.classify_dynamic_entry("WNBA", filters=filters) == (True, None)
    assert publish_mod.classify_dynamic_entry("玻利杯", filters=filters) == (True, None)
    # 排除规则
    assert publish_mod.classify_dynamic_entry("赛事回放", filters=filters) == (
        False, publish_mod.REASON_REPLAY_DISABLED
    )
    # 回放**分区**：即使分组名是联赛名，也要按回放排除
    assert publish_mod.classify_dynamic_entry(
        "WNBA", filters=filters, section="赛事回放"
    ) == (False, publish_mod.REASON_REPLAY_SECTION)
    assert publish_mod.classify_dynamic_entry("宣传", filters=filters) == (
        False, publish_mod.REASON_EXCLUDED_GROUP
    )
    assert publish_mod.classify_dynamic_entry("✈️TG频道", filters=filters) == (
        False, publish_mod.REASON_EXCLUDED_KEYWORD
    )

    # 显式白名单模式
    strict = publish_mod.normalize_dynamic_filters({"include_groups": ["正在直播"]})
    assert publish_mod.classify_dynamic_entry("正在直播", filters=strict) == (True, None)
    assert publish_mod.classify_dynamic_entry("WNBA", filters=strict) == (
        False, publish_mod.REASON_NOT_IN_INCLUDE_LIST
    )

    # 打开回放开关后，回放分区与回放分组都放行
    replay_on = publish_mod.normalize_dynamic_filters({"include_replay": True})
    assert publish_mod.classify_dynamic_entry(
        "WNBA", filters=replay_on, section="赛事回放"
    ) == (True, None)
    assert publish_mod.classify_dynamic_entry("赛事回放", filters=replay_on) == (True, None)


# ==================================================== 失败 / 时间边界 / 降级

def test_publish_dynamic_failure_degrades_to_fixed_only(capsys, env):
    """动态源失败 → fail-closed 降级为只发固定频道（exit 0），但不复用旧动态线路。"""
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])

    # 先用正常动态源发布一版（含动态线路）
    code, out = publish_cli(capsys, env, "--dynamic-source", "mock-dynamic")
    assert code == 0, out
    first_text = read(env["live"])
    assert DYNAMIC_GROUP in first_text
    first_dynamic_urls = [
        e.url for e in m3u_mod.parse_text(first_text).entries if e.group_title == DYNAMIC_GROUP
    ]
    assert first_dynamic_urls

    # 动态源坏掉后重跑：必须降级、必须不复用上一次的签名线路
    code, out = publish_cli(capsys, env, "--dynamic-source", "mock-dynamic-bad")
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_FIXED_ONLY
    assert payload["published"] is True
    assert payload["dynamic_count"] == 0

    second_text = read(env["live"])
    assert DYNAMIC_GROUP not in second_text
    for url in first_dynamic_urls:
        assert url not in second_text
    for secret in SECRETS:
        assert secret not in second_text
    # 单源失败：本轮本来也没有别的动态条目可舍弃
    assert payload["dynamic_fail_closed"] is True
    assert payload["dynamic_discarded"] == 0
    # 降级仍是真实发布：上一版应保留第一次（含动态）的内容
    assert env["previous"].exists()


def test_publish_partial_dynamic_failure_publishes_fixed_only(capsys, env):
    """QA-003B 永久回归：多动态源**部分失败**时，状态与文件内容必须一致。

    旧实现把状态写成 ``DEGRADED_FIXED_ONLY``，却仍把成功来源的动态线路写进了文件
    （大G 复现：``FIXED 1 DYNAMIC 1 DYNAMIC_FILE True``）。现在必须真正只发固定频道：
    状态、计数、摘要、**实际文件内容**四者一致。
    """
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])

    code, out = publish_cli(
        capsys, env, "--dynamic-source", "mock-dynamic", "--dynamic-source", "mock-dynamic-bad"
    )
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_FIXED_ONLY
    assert payload["published"] is True
    assert payload["dynamic_fail_closed"] is True
    assert payload["dynamic_discarded"] == 3          # mock-dynamic 本可贡献 3 条
    assert payload["dynamic_count"] == 0
    assert payload["channel_count"] == payload["fixed_count"] == 3

    # 报告如实体现：成功来源的 included 归零、被舍弃数记在 discarded
    good = next(r for r in payload["dynamic_sources"] if r["source_name"] == "mock-dynamic")
    bad = next(r for r in payload["dynamic_sources"] if r["source_name"] == "mock-dynamic-bad")
    assert good["ok"] is True
    assert good["included"] == 0
    assert good["discarded"] == 3
    assert bad["ok"] is False and bad["discarded"] == 0
    # 被舍弃的事实必须在告警里说清楚
    assert any("fail-closed" in w for w in payload["warnings"])

    # ---- 实际文件：不能有任何动态线路 ----
    text = read(env["live"])
    assert DYNAMIC_GROUP not in text
    parsed = m3u_mod.parse_text(text)
    assert parsed.entry_count == 3
    assert all(e.group_title != DYNAMIC_GROUP for e in parsed.entries)
    for name in ("曼城 vs 阿森纳", "皇马 vs 巴萨"):
        assert name not in text
    for secret in SECRETS:
        assert secret not in text

    # ---- 摘要：与状态一致 ----
    summary = json.loads(read(env["summary"]))
    assert summary["status"] == publish_mod.STATUS_DEGRADED_FIXED_ONLY
    assert summary["dynamic_count"] == 0
    assert summary["dynamic_fail_closed"] is True
    assert summary["dynamic_discarded"] == 3


def test_publish_all_dynamic_sources_ok_keeps_dynamic(capsys, env):
    """反向约束：全部动态来源成功时**不得**误触发 fail-closed。"""
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])

    code, out = publish_cli(
        capsys, env, "--dynamic-source", "mock-dynamic", "--dynamic-source", "mock-dynamic-alt"
    )
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_OK
    assert payload["dynamic_fail_closed"] is False
    assert payload["dynamic_discarded"] == 0
    assert payload["dynamic_count"] == 4              # 3 + 1，且不跨来源去重
    included = {r["source_name"]: r["included"] for r in payload["dynamic_sources"]}
    assert included == {"mock-dynamic": 3, "mock-dynamic-alt": 1}
    assert all(r["discarded"] == 0 for r in payload["dynamic_sources"])

    text = read(env["live"])
    assert DYNAMIC_GROUP in text
    parsed = m3u_mod.parse_text(text)
    assert sum(1 for e in parsed.entries if e.group_title == DYNAMIC_GROUP) == 4


def test_publish_require_dynamic_rejects_and_keeps_both_files(capsys, env):
    """--require-dynamic 时动态失败必须整次拒绝，当前与上一版**一字节不改**。

    这里刻意混入一个**成功**的动态来源：它的条目同样不许落盘（QA-003B）。
    """
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])

    code, out = publish_cli(capsys, env)
    assert code == 0, out
    live_before = env["live"].read_bytes()

    code, out = publish_cli(
        capsys, env,
        "--dynamic-source", "mock-dynamic", "--dynamic-source", "mock-dynamic-bad",
        "--require-dynamic",
    )
    payload = json.loads(out)
    assert code == 1, out
    assert payload["status"] == publish_mod.STATUS_REJECTED_DYNAMIC_REQUIRED
    assert payload["published"] is False
    assert payload["dynamic_count"] == 0
    assert env["live"].read_bytes() == live_before
    assert not env["previous"].exists()               # 备份也没被创建
    assert not (env["out_dir"] / "live.tmp.m3u").exists()
    # 成功来源的条目也不许出现在文件里
    assert "曼城 vs 阿森纳" not in read(env["live"])


def test_publish_dynamic_failure_with_empty_fixed_is_degraded_no_publish(capsys, env):
    """动态失败 + 固定频道也为空 → DEGRADED_NO_PUBLISH（exit 2），不发布也不覆盖旧文件。"""
    seed(capsys, env["cfg"], env["db"])       # 注意：不建 canonical/binding
    code, out = publish_cli(capsys, env, "--dynamic-source", "mock-dynamic-bad")
    payload = json.loads(out)
    assert code == 2, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_NO_PUBLISH
    assert payload["published"] is False
    assert payload["fixed_count"] == 0
    assert payload["risk"]
    assert not env["live"].exists()


def test_publish_dynamic_empty_is_treated_as_empty_set(capsys, ready):
    """动态源成功但没有任何合格赛事 → 按动态空集处理，只发布固定频道并给出告警。

    与「动态抓取失败」区分：这里来源是 **成功** 的（HTTP 200 + 合法 M3U），
    只是内容全是推广（命中的是排除规则），所以不降级、不拒绝，正常发布固定频道。
    """
    add_programmable_dynamic(capsys, ready, "dynamic-empty")
    # 可编程端点内容：只有一条「宣传」分组（会被 exclude_groups 排除）
    ready["server"].state.content = (
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="宣传",只有推广\n'
        "http://promo.invalid.example/x.m3u8?txSecret=YYY\n"
    )

    code, out = publish_cli(capsys, ready, "--dynamic-source", "dynamic-empty")
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_OK
    assert payload["published"] is True
    assert payload["fixed_count"] == 3              # 固定频道照常发布
    assert payload["dynamic_count"] == 0            # 动态空集
    assert payload["channel_count"] == 3
    # 来源本身成功，但一条都没纳入 → 必须给出可读告警
    assert any("动态" in w for w in payload["warnings"])
    report = payload["dynamic_sources"][0]
    assert report["source_name"] == "dynamic-empty"
    assert report["ok"] is True
    assert report["included"] == 0

    text = read(ready["live"])
    assert DYNAMIC_GROUP not in text
    assert "promo.invalid.example" not in text


def test_publish_does_not_reuse_previous_file_dynamic_entries(capsys, env):
    """本轮动态为空时，绝不能从上一版 live.m3u 里把旧动态线路拼回来。"""
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    add_programmable_dynamic(capsys, env, "dynamic-promo")

    # 第一轮：可编程端点返回真实动态样本 → 发布出动态线路
    env["server"].state.content = DYNAMIC_PUBLISH_SAMPLE
    code, out = publish_cli(capsys, env, "--dynamic-source", "dynamic-promo")
    assert code == 0, out
    old_dynamic = [
        e.url for e in m3u_mod.parse_text(read(env["live"])).entries
        if e.group_title == DYNAMIC_GROUP
    ]
    assert old_dynamic, "第一轮应当写入动态线路，否则本用例无从验证"

    # 第二轮：同一来源成功，但内容只剩推广（零合格条目）→ 动态必须清空
    env["server"].state.content = (
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="宣传",纯推广\n'
        "http://promo.invalid.example/y.m3u8?txSecret=XXX\n"
    )
    code, out = publish_cli(capsys, env, "--dynamic-source", "dynamic-promo")
    payload = json.loads(out)
    assert code == 0, out
    assert payload["dynamic_count"] == 0

    text = read(env["live"])
    assert DYNAMIC_GROUP not in text
    for url in old_dynamic:
        assert url not in text
    # 旧动态线路只能留在上一版备份里，不得被拼回当前文件
    assert read(env["previous"]) != text


def test_publish_empty_result_refuses_to_overwrite_existing_list(capsys, env):
    """全空结果默认不得覆盖已有的正常列表。"""
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    code, out = publish_cli(capsys, env)
    assert code == 0, out
    good = env["live"].read_bytes()

    # 把 canonical 全部禁用 → 组合结果为空
    conn = db_mod.connect(env["db"])
    conn.execute("UPDATE canonical_channel SET enabled = 0")
    conn.commit()
    conn.close()

    code, out = publish_cli(capsys, env)
    payload = json.loads(out)
    assert code == 1, out
    assert payload["status"] == publish_mod.STATUS_REJECTED_VALIDATION
    assert env["live"].read_bytes() == good


def test_publish_dry_run_writes_nothing(capsys, ready):
    """--dry-run 只组合与校验，不改任何文件（也不写摘要）。"""
    code, out = publish_cli(capsys, ready, "--dynamic", "--dry-run")
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_DRY_RUN
    assert payload["published"] is False
    assert payload["expected_checksum"]
    assert not ready["live"].exists()
    assert not ready["previous"].exists()
    assert not ready["summary"].exists()
    assert payload["summary"] is None


# ============================================================ 文件完整性 / 注入

def test_publish_replace_failure_keeps_current_and_previous(capsys, env, monkeypatch):
    """注入 os.replace 失败：当前与上一版都不能发生半更新。"""
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    code, out = publish_cli(capsys, env)
    assert code == 0, out
    code, out = publish_cli(capsys, env)     # 第二次，让 previous 出现
    assert code == 0, out
    live_before = env["live"].read_bytes()
    prev_before = env["previous"].read_bytes()

    real_replace = os.replace

    def flaky(src, dst, *args, **kwargs):
        if pathlib.Path(dst).name == "live.m3u":
            raise OSError("injected replace failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(m3u_mod.os, "replace", flaky)
    code, out = publish_cli(capsys, env)
    monkeypatch.undo()

    payload = json.loads(out)
    assert code == 1, out
    assert payload["status"] == publish_mod.STATUS_REJECTED_IO
    assert env["live"].read_bytes() == live_before
    assert env["previous"].read_bytes() == prev_before      # 备份被回滚
    assert not (env["out_dir"] / "live.tmp.m3u").exists()
    assert not (env["out_dir"] / "live.previous.m3u.tmp").exists()


def test_write_m3u_backup_is_restored_when_target_replace_fails(tmp_path, monkeypatch):
    """写盘层单测：目标替换失败时 previous 必须回到写入前的状态。"""
    target = tmp_path / "live.m3u"
    m3u_mod.write_m3u([m3u_mod.M3UChannel(key=1, name="A", url="http://a/1.m3u8")], target)
    m3u_mod.write_m3u([m3u_mod.M3UChannel(key=1, name="A", url="http://a/2.m3u8")], target)
    previous = tmp_path / "live.previous.m3u"
    assert "http://a/1.m3u8" in read(previous)

    prev_before = previous.read_bytes()
    live_before = target.read_bytes()
    real_replace = os.replace

    def flaky(src, dst, *args, **kwargs):
        if pathlib.Path(dst) == target:
            raise OSError("injected replace failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(m3u_mod.os, "replace", flaky)
    with pytest.raises(OSError):
        m3u_mod.write_m3u([m3u_mod.M3UChannel(key=1, name="A", url="http://a/3.m3u8")], target)
    monkeypatch.undo()

    assert target.read_bytes() == live_before
    assert previous.read_bytes() == prev_before
    assert not (tmp_path / "live.tmp.m3u").exists()
    assert not (tmp_path / "live.previous.m3u.tmp").exists()


def test_write_m3u_removes_stale_previous_when_none_existed(tmp_path, monkeypatch):
    """目标替换失败且此前没有 previous 时，不得凭空留下一个 previous 文件。"""
    target = tmp_path / "live.m3u"
    m3u_mod.write_m3u([m3u_mod.M3UChannel(key=1, name="A", url="http://a/1.m3u8")], target)
    previous = tmp_path / "live.previous.m3u"
    assert not previous.exists()

    real_replace = os.replace

    def flaky(src, dst, *args, **kwargs):
        if pathlib.Path(dst) == target:
            raise OSError("injected replace failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(m3u_mod.os, "replace", flaky)
    with pytest.raises(OSError):
        m3u_mod.write_m3u([m3u_mod.M3UChannel(key=1, name="A", url="http://a/2.m3u8")], target)
    monkeypatch.undo()

    assert not previous.exists()
    assert "http://a/1.m3u8" in read(target)


def test_publish_previous_file_holds_last_published_content(capsys, ready):
    """第二次发布必须把第一次的内容原样留到 live.previous.m3u。"""
    code, out = publish_cli(capsys, ready)
    assert code == 0, out
    first = read(ready["live"])

    code, out = publish_cli(capsys, ready, "--dynamic")
    assert code == 0, out
    assert read(ready["previous"]) == first
    assert read(ready["live"]) != first


def test_publish_summary_write_failure_does_not_block_publication(
    capsys, env, tmp_path, monkeypatch
):
    """摘要只是监测辅助：写不进去也不能影响已经发布的 live.m3u。"""
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])

    def boom(*_a, **_k):
        raise OSError("injected summary failure")

    monkeypatch.setattr(publish_mod, "_write_summary", boom)
    code, out = publish_cli(capsys, env)
    payload = json.loads(out)
    assert code == 0, out
    assert payload["published"] is True
    assert payload["summary"] is None
    assert payload["summary_error"]
    assert env["live"].exists()


def test_runtime_output_guard_rejects_git_tracked_location(tmp_path):
    """运行期产物护栏：工作树内未被忽略的路径必须拒绝。"""
    root = tmp_path / "wt"
    (root / ".git").mkdir(parents=True)
    (root / ".gitignore").write_text("out/\n", encoding="utf-8", newline="\n")

    publish_mod.guard_runtime_output_path(root / "out" / "summary.json")     # 被忽略 → 放行
    with pytest.raises(ValueError):
        publish_mod.guard_runtime_output_path(root / "SOURCES" / "summary.json")


# ============================================================ 信息边界 / 摘要

def test_publish_output_and_summary_never_leak_signed_urls(capsys, ready):
    """签名参数只允许出现在 live.m3u；stdout JSON 与摘要文件都必须脱敏。

    脱敏口径（与 ``publish.redact_url_light`` 一致）：只保留 ``scheme://host`` 作为
    「哪个 CDN/origin」的线索；**path 与 query 一律抹掉**——签名材料通常藏在
    playpath / query 里，origin 本身不是密钥。
    """
    code, out = publish_cli(capsys, ready, "--dynamic")
    assert code == 0, out

    for secret in SECRETS:
        assert secret not in out
    # origin 允许出现，但 path / query 与任何签名参数都不许出现
    assert "txSecret" not in out
    assert "txTime" not in out
    assert "/live/mci-ars/" not in out
    assert "pc.m3u8" not in out
    assert "?tx" not in out

    summary_text = read(ready["summary"])
    for secret in SECRETS:
        assert secret not in summary_text
    assert "txSecret" not in summary_text
    assert "txTime" not in summary_text
    assert "/live/mci-ars/" not in summary_text

    # 播放器要读的那个文件里，线路必须原样保留（否则没法播）
    live_text = read(ready["live"])
    assert "txSecret=AAA111" in live_text

    summary = json.loads(summary_text)
    assert summary["dynamic_count"] == 3
    assert summary["note"] == publish_mod.PUBLISH_NOTE
    # 排除样本里的 URL 也必须是「只留 origin」的形态
    samples = summary["dynamic_sources"][0]["excluded_samples"]
    assert samples
    for sample in samples:
        assert sample["url"].count("/") == 2, sample["url"]   # scheme://host 恰好两个斜杠
        assert "?" not in sample["url"]


def test_publish_summary_contains_monitoring_fields(capsys, ready):
    """摘要必须包含计数、过滤数量、来源抓取结果、发布日期、checksum 与降级/拒绝原因字段。"""
    code, out = publish_cli(capsys, ready, "--dynamic")
    assert code == 0, out
    summary = json.loads(read(ready["summary"]))

    for field in ("published_at", "status", "fixed_count", "dynamic_count", "channel_count",
                  "dynamic_sources", "dynamic_excluded_by_reason", "warnings",
                  "output_path", "checksum", "bytes", "exit_code", "note",
                  "dynamic_fail_closed", "dynamic_discarded"):
        assert field in summary, field
    assert summary["checksum"] == json.loads(out)["checksum"]
    assert summary["exit_code"] == 0
    report = summary["dynamic_sources"][0]
    assert report["source_name"] == "mock-dynamic"
    assert report["status"] == "ok"
    assert report["fetched_entries"] == 7
    assert report["included"] == 3


def test_publish_summary_written_even_when_rejected(capsys, env):
    """拒绝时也要留下理由（供监测），但仍不得包含签名 URL。"""
    seed(capsys, env["cfg"], env["db"])
    code, out = publish_cli(capsys, env, "--dynamic-source", "mock-dynamic-bad", "--require-dynamic")
    assert code == 1, out
    summary = json.loads(read(env["summary"]))
    assert summary["status"] == publish_mod.STATUS_REJECTED_DYNAMIC_REQUIRED
    assert summary["published"] is False
    assert summary["reason"]
    assert "txSecret" not in read(env["summary"])


# ============================================================ CLI 兼容 / 反向校验

def test_publish_cli_registered_and_generate_m3u_still_works(capsys, ready):
    """新命令不改变旧命令：generate-m3u 仍是本地、非联网、行为一致。"""
    code, out = build(capsys, "generate-m3u", "--config", ready["cfg"], "--db", ready["db"],
                      "--now", NOW, "--out", str(ready["out_dir"] / "legacy.m3u"), "--json")
    assert code == 0, out
    legacy = json.loads(out)
    assert legacy["channel_count"] == 3
    assert legacy["previous"] is None
    assert DYNAMIC_GROUP not in read(ready["out_dir"] / "legacy.m3u")


def test_publish_dynamic_source_must_be_dynamic_kind(capsys, ready):
    """把固定源当作 --dynamic-source 传入必须被拒绝（退出码非 0）。"""
    with pytest.raises(SystemExit):
        build(capsys, "publish", "--dynamic-source", "mock-fixed",
              "--config", ready["cfg"], "--db", ready["db"], "--json")


def test_publish_unknown_dynamic_source_is_rejected(capsys, ready):
    with pytest.raises(SystemExit):
        build(capsys, "publish", "--dynamic-source", "does-not-exist",
              "--config", ready["cfg"], "--db", ready["db"], "--json")


def test_reverse_validate_catches_tampered_composition():
    """反向校验能抓到「生成文本与预期条目不一致」这类问题。"""
    channels = [
        m3u_mod.M3UChannel(key=("fixed", 1), name="A", url="http://a/1.m3u8", group_title="新闻"),
    ]
    text = m3u_mod.generate_text(channels)
    assert publish_mod.reverse_validate(text, channels) == []

    tampered = [m3u_mod.M3UChannel(key=("fixed", 1), name="A", url="http://a/9.m3u8",
                                  group_title="新闻")]
    assert publish_mod.reverse_validate(text, tampered)


def test_validate_composition_flags_empty_and_duplicates():
    assert publish_mod.validate_composition([])
    duplicated = [
        m3u_mod.M3UChannel(key=("fixed", 1), name="A", url="http://a/1.m3u8"),
        m3u_mod.M3UChannel(key=("fixed", 1), name="A 副本", url="http://a/2.m3u8"),
    ]
    assert any("重复" in e for e in publish_mod.validate_composition(duplicated))
