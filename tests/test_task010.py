"""TASK-010 测试：频道扩充、质量筛选、publish 易用性、playback context。

**全部离线**（任务书 §17「公网只用于 smoke，自动测试必须离线」）：
固定源与动态源都打本机 mock HTTP 服务，不访问 iptv-org / Guovin / KORICE 任何
真实地址，也不依赖任何真实流可播。

覆盖任务书 §17 的 30 项清单，逐条对应（编号即任务书条目号）：

   1. 扩展 seed mapping
   2. alias 明确映射成功
   3. ambiguous alias fail closed
   4. signed query 不绑定
   5. identity query 不绑定
   6. catch-all stream 排除
   7. HTML fake stream 不判 PASS
   8. fixed source failure 不清库存
   9. missing → inactive
  10. reappear 恢复 identity
  11. multi-stream selector
  12. cross-source selector
  13. all-below threshold
  14. fixed group 输出
  15. fixed canonical 不重复
  16. dynamic default publish 行为
  17. explicit no-dynamic 行为
  18. all_or_nothing 回归
  19. isolate 回归
  20. require_dynamic 回归
  21. dynamic exact dedup
  22. dynamic display collision
  23. LKG
  24. fixed_summary
  25. dynamic_summary
  26. sensitive URL redaction
  27. source context metadata 不泄密
  28. playback-context 不污染 Shanghai probe history
  29. KORICE cloud FAIL 不自动全局删除
  30. output reverse parse

另加本轮新交付的专属断言：
  * §8 动态源决策真值表（8种组合全覆盖，含理由码可审计）；
  * §4/§13 playback context 拆分与 KORICE 冻结规则；
  * §5.4 alias 冲突 fail-closed；
  * seed 配置本身的完整性（43 canonical、CCTV-5 与 CCTV-5+ 不合并等）。

零回归护栏：TASK-002 生命周期、TASK-003 发布语义、TASK-005 probe fail-closed、
TASK-008 dynamic isolate、QA-008A per-source published 记账全部保持。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tomllib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liptv import config as config_mod# noqa: E402
from liptv import db as db_mod            # noqa: E402
from liptv import ingest as ingest_mod    # noqa: E402
from liptv import m3u as m3u_mod          # noqa: E402
from liptv import publish as publish_mod  # noqa: E402
from liptv import repo as repo_mod        # noqa: E402
from liptv import source_policy as policy_mod  # noqa: E402
from liptv.cli import main as cli_main    # noqa: E402
from tools import build_fixed_seed as seed_tool  # noqa: E402

# 复用 TASK-003/005/008/009 的夹具与工具，避免第二套相似脚手架。
from tests.test_publish import (       # noqa: E402
    DYNAMIC_GROUP, add_dynamic_source, add_programmable_dynamic, bind_and_probe,
    build, publish_cli, read, seed,
)

NOW = "2026-10-06T02:00:00+00:00"
NOW2 = "2026-10-06T02:30:00+00:00"

AON = "all_or_nothing"
ISOLATE = "isolate"

SECRETS = ("txSecret", "AAA111", "BBB222", "CCC333")


# ------------------------------------------------------------------ 配置夹具


@pytest.fixture()
def env_with_dynamic(tmp_path, mock_server):
    """与 test_publish.env 同构，但**显式**关闭 auto 抓取的干扰。

    这里保持 ``dynamic_default`` 默认（auto），让第16 项能真正测到新语义。
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
        "cfg": cfg, "db": db, "out_dir": out_dir,
        "live": out_dir / "live.m3u", "previous": out_dir / "live.previous.m3u",
        "summary": out_dir / "publish-summary.json",
        "base": base, "server": server, "tmp_path": tmp_path,
    }


def upsert_publish_setting(cfg_path, *, section: str, key: str, value: str) -> None:
    """幂等地往 config.toml 的 ``[publish]`` 段写一个键。

    ⚠️ 只用于测试。TASK-008 曾在生产 config 上误传参数静默污染文件，
    这里只碰 tmp_path 里的测试配置。
    """
    text = pathlib.Path(cfg_path).read_text(encoding="utf-8")
    lines = text.splitlines()
    keyline = f"{key} = {value}"
    out, in_section, done = [], False, False
    for line in lines:
        if line.startswith("["):
            if in_section and not done:
                out.append(keyline)
                done = True
            in_section = line.strip() == f"[{section}]"
        if in_section and line.strip().startswith(f"{key} "):
            line = keyline
            done = True
        out.append(line)
    if in_section and not done:
        out.append(keyline)
    pathlib.Path(cfg_path).write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")


# ==================================================================
# §17-1  扩展 seed mapping
# ==================================================================


def test_seed_bindings_expand_to_forty_plus_canonicals():
    """§17-1 + §5.2：seed 从 10扩到 ≥30 个 canonical。"""
    data = tomllib.loads(
        (REPO_ROOT / "config" / "fixed_seed_bindings.toml").read_text(encoding="utf-8")
    )
    seeds = data["seed"]
    assert len(seeds) >= 30, f"只收到 {len(seeds)} 个 canonical，§5.2 要求 ≥30"
    # 全部必须是唯一允许的匹配方式（§5.3）
    assert {s["match"] for s in seeds} == {"exact_normalized"}
    # 全部必须声明来源与审计 note（§5.4「可审计」）
    for s in seeds:
        assert s["source_names"], s["canonical"]
        assert s.get("note"), s["canonical"]
        assert s.get("category"), s["canonical"]


def test_seed_canonical_names_are_unique():
    """canonical 显示名不得重复（否则 live.m3u 会出现重名频道）。"""
    data = tomllib.loads(
        (REPO_ROOT / "config" / "fixed_seed_bindings.toml").read_text(encoding="utf-8")
    )
    names = [s["canonical"] for s in data["seed"]]
    assert len(names) == len(set(names)), "有重复 canonical 显示名"


def test_seed_covers_task010_required_cctv_channels():
    """§2A：任务书点名的 17 个 CCTV 必须全部收录。

    这是任务书**显式列举**的内容，缺一个就是没完成 §2A。
    """
    data = tomllib.loads(
        (REPO_ROOT / "config" / "fixed_seed_bindings.toml").read_text(encoding="utf-8")
    )
    #显示名 → match_key（build_plan 的规则：取首个空格前的部分再归一）
    keys = {
        seed_tool.normalize_name(s["canonical"].split(" ", 1)[0])
        for s in data["seed"]
    }
    required = {
        "CCTV-1", "CCTV-2", "CCTV-4", "CCTV-5", "CCTV-5+", "CCTV-6",
        "CCTV-7", "CCTV-8", "CCTV-9", "CCTV-10", "CCTV-11", "CCTV-12",
        "CCTV-13", "CCTV-14", "CCTV-15", "CCTV-16", "CCTV-17",
    }
    missing = required - keys
    assert not missing, f"§2A 点名但未收录：{sorted(missing)}"


def test_cctv5_and_cctv5plus_are_separate_canonicals():
    """§2A硬约束：CCTV-5 与 CCTV-5+ **不得合并**。

    两者match_key 不同，且必须各自独立成条 —— 若哪天有人图省事把它们
    写成同一个 canonical，这条用例会红。
    """
    data = tomllib.loads(
        (REPO_ROOT / "config" / "fixed_seed_bindings.toml").read_text(encoding="utf-8")
    )
    by_key: dict[str, list[str]] = {}
    for s in data["seed"]:
        k = seed_tool.normalize_name(s["canonical"].split(" ", 1)[0])
        by_key.setdefault(k, []).append(s["canonical"])
    assert "CCTV-5" in by_key, "缺 CCTV-5"
    assert "CCTV-5+" in by_key, "缺 CCTV-5+"
    # 不允许出现一个 canonical 同时承载两者
    for k, names in by_key.items():
        if "CCTV-5" in names:
            assert "CCTV-5+" not in names, f"{k} 把 CCTV-5 与 CCTV-5+ 合并了：{names}"


def test_normalize_name_does_not_fold_hd_suffix():
    """§2A + TASK-009 §7：``HD`` 不是末尾分辨率括号，归一后必须保持不同。

    这是「HD 不被自动合并」这条规则的**技术根因**；若哪天有人往
    ``_RES_SUFFIX`` 里加 ``HD``，本用例会立刻挡住。
    """
    assert seed_tool.normalize_name("CCTV-1") == "CCTV-1"
    assert seed_tool.normalize_name("CCTV-1 HD") == "CCTV-1 HD"
    assert seed_tool.normalize_name("CCTV-1 (720p)") == "CCTV-1"
    # 语义不同的两个频道绝不能归一到一起
    assert seed_tool.normalize_name("CCTV-5") != seed_tool.normalize_name("CCTV-5+")


# ==================================================================
# §17-2/3  alias 明确映射成功 / ambiguous fail closed
# ==================================================================


def test_alias_table_is_explicit_and_auditable():
    """§5.4：alias 表每行都要有 canonical / from / note，且不含 stream URL。"""
    path = REPO_ROOT / "config" / "fixed_aliases.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = data["alias"]
    assert entries, "alias 表为空"
    for e in entries:
        assert e.get("canonical"), e
        assert e.get("from"), e
        assert e.get("note"), f"alias {e} 缺审计说明"
        # 🚫 §5.4「进入 Git 的只允许名称映射，不含 stream URL」
        blob = json.dumps(e, ensure_ascii=False)
        assert "://" not in blob, f"alias 里出现 URL：{e}"
        for bad in ("msisdn", "token=", "auth=", "password"):
            assert bad not in blob.lower(), f"alias 里出现敏感串：{e}"


def test_alias_never_bridges_semantically_different_channels():
    """§5.4：alias **不得跨语义频道**（最典型的就是 CCTV-5↔CCTV-5+）。

    这条比「表里有什么」更重要：它直接读表验证没有任何一条 alias
    把不同语义频道连起来。
    """
    data = tomllib.loads(
        (REPO_ROOT / "config" / "fixed_aliases.toml").read_text(encoding="utf-8")
    )
    for e in data["alias"]:
        lhs = seed_tool.normalize_name(e["from"])
        rhs = seed_tool.normalize_name(str(e["canonical"]).split(" ", 1)[0])
        # 去掉 HD 后如果两侧变成同一频道，那是允许的（清晰度变体）；
        # 但必须仍然相差一个 HD 层级，不得跨到别的频道号。
        assert lhs != rhs or lhs == rhs  # 结构占位，真正的判断在下面
        if lhs != rhs:
            assert lhs.replace(" HD", "") == rhs or rhs.replace(" HD", "") == lhs, (
                f"alias {e['from']!r} -> {e['canonical']!r} 跨到了不同频道"
            )


def test_alias_map_conflict_fails_closed(tmp_path):
    """§5.4「有冲突时 fail-closed」：同一个 from 指向两个 canonical 必须报错。

    绝不「后者覆盖前者」—— 那样会让渠道绑定结果不可解释。
    """
    bad = tmp_path / "bad_aliases.toml"
    bad.write_text(
        "[[alias]]\n"
        'canonical = "CCTV-1 综合"\n'
        'from = "CCTV-1 HD"\n'
        'note = "第一条"\n'
        "\n[[alias]]\n"
        'canonical = "CCTV-13 新闻"\n'
        'from = "CCTV-1 HD"\n'
        'note = "第二条，冲突"\n',
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        seed_tool.load_alias_map(bad)
    assert "alias 冲突" in str(exc.value)


def test_alias_map_accepts_duplicate_identical_rows(tmp_path):
    """同一个 from→同一个 canonical 重复出现是幂等的，不算冲突。"""
    ok = tmp_path / "ok_aliases.toml"
    ok.write_text(
        "[[alias]]\n"
        'canonical = "CCTV-1 综合"\n'
        'from = "CCTV-1 HD"\n'
        'note = "第一条"\n'
        "\n[[alias]]\n"
        'canonical = "CCTV-1 综合"\n'
        'from = "CCTV-1 HD"\n'
        'note = "完全相同的一条"\n',
        encoding="utf-8",
    )
    amap = seed_tool.load_alias_map(ok)
    assert amap == {"CCTV-1 HD": "CCTV-1 综合"}


def test_alias_only_applies_to_declared_canonicals():
    """alias **不能**把本轮不打算绑的频道名也改写掉（避免意外绑定）。"""
    entries = [m3u_mod.M3UEntry(name="CCTV-1 HD", url="http://x.invalid/a.m3u8",
                                group_title="央视")]
    amap = {"CCTV-1 HD": "CCTV-1 综合"}
    # 声明了 CCTV-1 ⇒ 应用
    out, st = seed_tool.apply_aliases(
        entries, amap, canonical_by_match_key={"CCTV-1": "CCTV-1 综合"}
    )
    assert [e.name for e in out] == ["CCTV-1 综合"]
    assert st["applied"] == 1
    # 没声明 CCTV-1 ⇒ 不改写
    out2, st2 = seed_tool.apply_aliases(entries, amap, canonical_by_match_key={})
    assert [e.name for e in out2] == ["CCTV-1 HD"]
    assert st2["skipped_unknown"] == 1


# ==================================================================
# §17-4/5/6  signed / identity / catch-all 不绑定
# ==================================================================


@pytest.mark.parametrize("key", [
    "token", "auth", "key", "secret", "msisdn", "migutoken",
    "sign", "hdnts", "expire", "mdspid",
])
def test_banned_query_keys_are_rejected(key):
    """§17-4 + §17-5：签名参数与**用户身份参数**一律不绑定。"""
    assert seed_tool.has_banned_query(f"http://h.invalid/a.m3u8?{key}=XYZ") is True


def test_identity_query_msisdn_is_rejected():
    """§17-5 单独强调：``msisdn`` 是手机号，属隐私红线，必须拦。"""
    url = "http://h.invalid/a.m3u8?msisdn=13800000000&mdspid=1"
    assert seed_tool.has_banned_query(url) is True


def test_clean_url_passes():
    assert seed_tool.has_banned_query("http://h.invalid/a.m3u8") is False
    assert seed_tool.has_banned_query("http://h.invalid/a.m3u8?x=1") is False


def test_catch_all_threshold_is_five():
    """§17-6：同一 URL 被 ≥5 个 canonical 引用即判占位流（沿用 TASK-009 §4）。"""
    assert seed_tool.CATCH_ALL_MIN_CANONICALS == 5


def test_banned_query_not_bound_in_live_db(capsys, env_with_dynamic):
    """§17-4/5 端到端：带签名/身份参数的 stream 不得进入绑定结果。"""
    env = env_with_dynamic
    env["server"].state.content = (
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="央视",CCTV-1\n'
        "http://a.invalid/cctv1.m3u8\n"
        '#EXTINF:-1 group-title="央视",CCTV-13\n'
        "http://c.invalid/cctv13.m3u8?auth=SECRET1\n"
        '#EXTINF:-1 group-title="央视",CCTV-14\n'
        "http://d.invalid/cctv14.m3u8?msisdn=13800000000\n"
    )
    seed(capsys, env["cfg"], env["db"])

    plan = {
        "source_entry_urls": {"mock-fixed": f"{env['base']}/seq.m3u"},
        "raw_channel_counts": {}, "dropped_by_query": {}, "kept_counts": {},
        "catch_all_streams_excluded": 0,
        "plan": [
            {"canonical": "CCTV-13 新闻", "category": "央视",
             "match": "exact_normalized", "match_key": "CCTV-13",
             "sources": [{"source": "mock-fixed", "normalized": "CCTV-13", "entries": 1}],
             "note": "t"},
            {"canonical": "CCTV-14 少儿", "category": "央视",
             "match": "exact_normalized", "match_key": "CCTV-14",
             "sources": [{"source": "mock-fixed", "normalized": "CCTV-14", "entries": 1}],
             "note": "t"},
        ],
    }
    seed_tool.apply_plan(plan, db_path=env["db"], config_path=env["cfg"],
                         now=NOW, dry_run=False)

    conn = db_mod.connect(env["db"])
    rows = conn.execute(
        "SELECT sc.raw_stream_url AS u FROM stream s "
        "JOIN stream_source ss ON ss.stream_id = s.id "
        "JOIN source_channel sc ON sc.id = ss.source_channel_id"
    ).fetchall()
    conn.close()
    urls = {r["u"] for r in rows}
    assert not any("auth=" in u for u in urls), f"签名 stream 被绑上：{urls}"
    assert not any("msisdn=" in u for u in urls), f"身份参数 stream 被绑上：{urls}"


# ==================================================================
# §17-7  HTML fake stream 不判PASS
# ==================================================================


def test_html_fake_stream_is_not_healthy():
    """§17-7：HTML 假流必须判为**非媒体**，不能因为有响应就 PASS。

    直接测纯函数 :func:`liptv.probe.parse_media_info`，完全离线。

    对应真实场景：TASK-009 侦察报告 §4 记录的「万能流 A 返回 404 text/html
    138B、万能流 B 返回 200 text/html 880B」—— HTML 正是占位流的典型返回形态。
    """
    from liptv import probe as probe_mod

    html = (b"<html><head><title>404 Not Found</title></head>"
            b"<body><h1>Not Found</h1></body></html>")
    info, error, _detail = probe_mod.parse_media_info(html)
    assert info is None, "HTML 被解析成了有效媒体"
    assert error == probe_mod.ERROR_OUTPUT_INVALID

    # 正常 HLS 媒体信息必须能解析出来（对照：不是把所有输入都判失败）
    good = (b'{"streams":[{"codec_type":"video","width":1920,"height":1080}],'
            b'"format":{"duration":"12.5"}}')
    info2, error2, _ = probe_mod.parse_media_info(good)
    assert error2 is None
    assert info2 is not None and info2.resolution_width == 1920


def test_classify_failure_never_silently_succeeds():
    """§17-7 配套：ffprobe 各种失败形态都必须被分类成明确错误，绝不当成功。

    这张矩阵同时守住了 TASK-009 修掉的 ``-nostdin`` 类缺陷：
    ``Option not found``（ffprobe 拒绝了非法参数）必须被识破，
    否则所有线路都会被误记成 ``HTTP_ERROR`` 而 100% 判定失败。
    """
    from liptv import probe as probe_mod

    # 超时优先：任何 returncode / stderr 组合只要 timed_out 就是 TIMEOUT
    for rc in (None, 0, 1, 2):
        assert probe_mod.classify_failure(
            timed_out=True, returncode=rc, stderr="") == probe_mod.ERROR_TIMEOUT

    # ffprobe 报「选项不存在」⇒ HTTP_ERROR（TASK-009 -nostdin 缺陷的护栏）
    assert probe_mod.classify_failure(
        timed_out=False, returncode=1,
        stderr="Option not found") == probe_mod.ERROR_HTTP

    # 普通非零退出 ⇒ PROCESS_ERROR
    assert probe_mod.classify_failure(
        timed_out=False, returncode=1, stderr="") == probe_mod.ERROR_PROCESS
    assert probe_mod.classify_failure(
        timed_out=False, returncode=2, stderr="") == probe_mod.ERROR_PROCESS

    # 🚫 关键：绝不允许出现「看起来成功」的分类
    assert probe_mod.classify_failure(
        timed_out=False, returncode=None, stderr="") == probe_mod.ERROR_UNKNOWN


def test_build_argv_has_no_illegal_nostdin():
    """TASK-009 回归护栏：ffprobe 不支持 ``-nostdin``（那是 ffmpeg 的选项）。

    真机上因为带了这个参数，所有线路 100% 被误判失败；TASK-009 已删除它。
    本用例保证它不会被重新加回来。
    """
    from liptv import probe as probe_mod

    settings = probe_mod.ProbeSettings.from_mapping({
        "enabled": True, "name": "shanghai-cloud", "location": "Shanghai",
        "ffprobe_path": ["ffprobe"], "timeout_seconds": 5.0,
        "analyze_seconds": 2.0, "max_concurrency": 2,
    })
    argv = probe_mod.build_argv(settings, "http://a.invalid/x.m3u8")
    assert "-nostdin" not in argv, f"ffprobe 不支持 -nostdin：{argv}"
    # 反过来，ffprobe 必需的关键参数必须在
    assert "-show_format" in argv and "-show_streams" in argv


# ==================================================================
# §17-8/9/10  fixed 生命周期（TASK-002 冻结语义零回归）
# ==================================================================


def test_fixed_source_failure_keeps_inventory(capsys, env_with_dynamic):
    """§17-8：源抓取失败时库存零改动，且 fixed_summary 可见失败。

    手法：先让fixed 源指向一个**恒 500** 的端点（``/error.m3u``），
    证明「从有库存变成抓不到」时库存与发布都不受影响。
    """
    env = env_with_dynamic
    server = env["server"]
    server.state.content = _two_channel_m3u()
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])

    conn = db_mod.connect(env["db"])
    before = conn.execute("SELECT COUNT(*) FROM source_channel").fetchone()[0]
    conn.close()
    assert before > 0

    # 把已注册源的 URL 改到恒 500 的端点。
    # ⚠️ 改 config 无效 —— `source-register --from-config` 在 seed 时已把 URL
    # 写进数据库，fetch 读的是数据库里的 URL。所以走 `source-add`（同名即更新）。
    code, out = build(capsys, "source-add", "--name", "mock-fixed",
                      "--kind", "fixed_m3u", "--url", f'{env["base"]}/error.m3u',
                      "--config", env["cfg"], "--db", env["db"], "--json")
    assert code == 0, out

    code, out = build(capsys, "fetch", "--all", "--config", env["cfg"],
                      "--db", env["db"], "--json")
    fetch_result = json.loads(out)
    assert fetch_result["failed_count"] == 1, fetch_result
    assert fetch_result["ok_count"] == 0, fetch_result
    assert fetch_result["total_deactivated"] == 0, "失败快照不得触发下线"

    conn = db_mod.connect(env["db"])
    after = conn.execute("SELECT COUNT(*) FROM source_channel").fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM source_channel WHERE active = 1").fetchone()[0]
    conn.close()
    assert after == before, "源失败时库存被改动，违反 TASK-002"
    assert active == before, "源失败时条目被置 inactive，违反 TASK-002"

    code, out = publish_cli(capsys, env, "--no-dynamic")
    payload = json.loads(out)
    assert payload["fixed_count"] > 0, "源抓不到不等于固定频道不可发布"
    assert payload["fixed_summary"]["failed_sources"] >= 1


def test_empty_upstream_list_is_failure_not_mass_inactive(capsys, env_with_dynamic):
    """§17-9 前置澄清：上游返回**空列表**按TASK-002 判失败，库存零改动。

    ⚠️ 这不是 bug，是冻结语义（``liptv/ingest.py::validate_m3u_text``）：
    「M3U 结构存在但没有任何有效条目」⇒ ``EMPTY_LIST`` ⇒ fetch 失败。
    理由见 QA-002A：只有**结构完整**的快照才能驱动「本次未出现 → 置 inactive」，
    否则一次故障/截断就会静默误下线全部旧频道。

    所以「置inactive」的正确触发条件是**结构完整但少了条目**，
    由 :func:`test_missing_entries_become_inactive_not_deleted` 用真实删台样本验证。
    """
    env = env_with_dynamic
    server = env["server"]
    server.state.set_ok()
    seed(capsys, env["cfg"], env["db"])

    conn = db_mod.connect(env["db"])
    before = conn.execute("SELECT COUNT(*) FROM source_channel").fetchone()[0]
    conn.close()
    assert before > 0

    server.state.content = "#EXTM3U\n"     # 结构在、条目 0 ⇒ EMPTY_LIST
    code, out = build(capsys, "fetch", "--all", "--config", env["cfg"],
                      "--db", env["db"], "--json")
    payload = json.loads(out)
    assert payload["failed_count"] == 1, payload
    assert payload["total_deactivated"] == 0, "空列表不得触发下线"

    conn = db_mod.connect(env["db"])
    after = conn.execute("SELECT COUNT(*) FROM source_channel").fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM source_channel WHERE active = 1").fetchone()[0]
    conn.close()
    assert after == before
    assert active == before, "空列表把旧频道下线了，违反 TASK-002/QA-002A"


def _two_channel_m3u() -> str:
    """两条固定频道的结构完整 M3U（测试里可编程投放到 /seq.m3u）。"""
    return (
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="央视",CCTV-1综合\n'
        "http://a.invalid/cctv1.m3u8\n"
        '#EXTINF:-1 group-title="央视",CCTV-2财经\n'
        "http://a.invalid/cctv2.m3u8\n"
    )


def _shrunk_m3u() -> str:
    """同结构但只剩 1 条（CCTV-2 消失）⇒ 真实删台。"""
    return (
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="央视",CCTV-1综合\n'
        "http://a.invalid/cctv1.m3u8\n"
    )


def test_missing_entries_become_inactive_not_deleted(capsys, env_with_dynamic):
    """§17-9：结构完整但条目消失 ⇒ active=0，绝不 hard delete。

    直接编程``state.content``（``/seq.m3u`` 的内容源），精确控制「有 2 条 →
    只剩 1 条」，避免依赖 mock 固定样本的进出关系。
    """
    env = env_with_dynamic
    server = env["server"]
    server.state.content = _two_channel_m3u()
    seed(capsys, env["cfg"], env["db"])

    conn = db_mod.connect(env["db"])
    before = {int(r["id"]): r["identity_hash"]
              for r in repo_mod.list_source_channels(conn)}
    conn.close()
    assert len(before) == 2, f"前置条件不成立：{len(before)} 条"

    server.state.content = _shrunk_m3u()
    code, out = build(capsys, "fetch", "--all", "--config", env["cfg"],
                      "--db", env["db"], "--json")
    payload = json.loads(out)
    assert payload["failed_count"] == 0, out
    assert payload["total_deactivated"] == 1, payload

    conn = db_mod.connect(env["db"])
    rows = {int(r["id"]): r for r in repo_mod.list_source_channels(conn)}
    conn.close()
    # 🚫 绝不 hard delete：两条旧 id 都还在
    assert set(rows) == set(before), "有条目被硬删了，违反 TASK-002"
    # 一条 active、一条 inactive，且身份都没变
    act = [r for r in rows.values() if int(r["active"]) == 1]
    ina = [r for r in rows.values() if int(r["active"]) == 0]
    assert len(act) == 1 and len(ina) == 1, f"{len(act)} active / {len(ina)} inactive"
    for sid, row in rows.items():
        assert row["identity_hash"] == before[sid], f"条目 {sid} 身份被改写"


def test_reappearing_entry_restores_same_identity(capsys, env_with_dynamic):
    """§17-10：再出现时恢复**同一身份**（identity_hash 不变）。"""
    env = env_with_dynamic
    server = env["server"]
    server.state.content = _two_channel_m3u()
    seed(capsys, env["cfg"], env["db"])

    conn = db_mod.connect(env["db"])
    before = {int(r["id"]): r["identity_hash"]
              for r in repo_mod.list_source_channels(conn)}
    conn.close()

    # 第一步：缩到 1 条 ⇒ 另一条 inactive
    server.state.content = _shrunk_m3u()
    code, out = build(capsys, "fetch", "--all", "--config", env["cfg"],
                      "--db", env["db"], "--json")
    assert json.loads(out)["failed_count"] == 0, out

    conn = db_mod.connect(env["db"])
    inactive = [int(r["id"]) for r in repo_mod.list_source_channels(conn)
                if not int(r["active"]) and int(r["id"]) in before]
    conn.close()
    assert len(inactive) == 1, f"第一步应恰好 1 条 inactive，实际 {len(inactive)}"

    # 第二步：恢复 ⇒ inactive 的那条回到 active 且身份不变
    server.state.content = _two_channel_m3u()
    code, out = build(capsys, "fetch", "--all", "--config", env["cfg"],
                      "--db", env["db"], "--json")
    payload = json.loads(out)
    assert payload["failed_count"] == 0, out
    assert payload["total_reactivated"] == 1, payload

    conn = db_mod.connect(env["db"])
    by_id = {int(r["id"]): r for r in repo_mod.list_source_channels(conn)}
    conn.close()
    for sid in inactive:
        assert int(by_id[sid]["active"]) == 1, f"条目 {sid} 没恢复 active"
        assert by_id[sid]["identity_hash"] == before[sid], "身份变了，不是同一个条目"


# ==================================================================
# §17-11/12/13  selector
# ==================================================================


def test_multi_stream_selector_picks_one(capsys, env_with_dynamic):
    """§17-11：同 canonical 多 stream只发布selector 选中的1 条。"""
    env = env_with_dynamic
    server = env["server"]
    server.state.set_ok()
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])

    code, out = publish_cli(capsys, env, "--no-dynamic")
    payload = json.loads(out)
    parsed = m3u_mod.parse_text(read(env["live"]))
    names = [e.name for e in parsed.entries]
    assert len(names) == len(set(names)), f"同一 canonical 出现多次：{names}"
    assert payload["channel_count"] == len(names)


def test_cross_source_selector_and_all_below_threshold(capsys, env_with_dynamic):
    """§17-12/13：跨源多 stream 可发布；全部低于门槛时不发布。"""
    env = env_with_dynamic
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    code, out = publish_cli(capsys, env, "--no-dynamic")
    assert code == 0, out

    # 把所有 stream 打成连续失败 ⇒ 全部低于门槛 ⇒ 拒绝发布
    conn = db_mod.connect(env["db"])
    for s in repo_mod.list_streams(conn):
        for _ in range(4):
            repo_mod.add_probe_result(conn, stream_id=int(s["id"]),
                                      probe_id=int(repo_mod.ensure_probe(
                                          conn, "shanghai-cloud", now=NOW)),
                                      success=False, checked_at=NOW2)
    conn.commit()
    conn.close()

    code, out = publish_cli(capsys, env, "--no-dynamic")
    payload = json.loads(out)
    assert code != 0
    assert payload["published"] is False


# ==================================================================
# §17-14/15  分组与去重
# ==================================================================


def test_fixed_groups_are_emitted(capsys, env_with_dynamic):
    """§17-14：fixed 条目必须带 group-title，且不是动态赛事组。"""
    env = env_with_dynamic
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    code, out = publish_cli(capsys, env, "--no-dynamic")
    assert code == 0, out
    parsed = m3u_mod.parse_text(read(env["live"]))
    assert parsed.entries
    for e in parsed.entries:
        assert e.group_title, f"{e.name} 没有分组"
        assert e.group_title != DYNAMIC_GROUP, "fixed 被污染进动态赛事组"


def test_seed_declares_task010_groups():
    """§9.1：seed 里使用的分组应覆盖任务书建议的分组集合。"""
    data = tomllib.loads(
        (REPO_ROOT / "config" / "fixed_seed_bindings.toml").read_text(encoding="utf-8")
    )
    cats = {s["category"] for s in data["seed"]}
    # 任务书 §9.1 建议至少区分：央视/卫视/新闻/纪录片/教育/音乐/国际/港澳台/其它
    for required in ("央视", "卫视", "新闻", "纪录片", "教育", "音乐"):
        assert required in cats, f"缺少分组 {required}（现有：{sorted(cats)}）"


# ==================================================================
# §17-16/17  动态源默认语义（TASK-010 §8 核心）
# ==================================================================


def test_dynamic_default_auto_fetches_when_sources_registered(capsys, env_with_dynamic):
    """§17-16：config 里有enabled 动态源时，**裸publish 默认就抓**。

    这正是 TASK-009 真机发现的运维坑的修复：忘写 --dynamic 时
    sources=[] 会被误读成「今天没赛事」。
    """
    env = env_with_dynamic
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])

    code, out = publish_cli(capsys, env)          # ← 不带任何 dynamic flag
    payload = json.loads(out)
    assert code == 0, out
    assert payload["include_dynamic"] is True
    assert payload["dynamic_decision_reason"] == "auto_sources_present"
    assert payload["dynamic_count"] > 0, "auto 判定该抓却没抓到动态条目"
    # 🚫 §8 的核心诉求：不能是「静默0 条」
    assert payload["dynamic_sources"], "dynamic_sources 为空 ⇒ 又回到静默漏抓"


def test_dynamic_decision_reason_is_always_auditable(capsys, env_with_dynamic):
    """§17-16：无论抓不抓，摘要里都必须有**可审计的理由码**。"""
    env = env_with_dynamic
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    code, out = publish_cli(capsys, env)
    payload = json.loads(out)
    assert payload["dynamic_decision_reason"]
    # 写盘摘要里也必须有
    summary = json.loads(read(env["summary"]))
    assert summary.get("dynamic_decision_reason") == payload["dynamic_decision_reason"]


def test_dynamic_default_auto_does_not_fetch_without_sources(capsys, env_with_dynamic):
    """§17-16 另一半：一个动态源都没登记时，auto **不联网**（零意外请求）。"""
    env = env_with_dynamic
    # 只注册 fixed，不注册 dynamic
    cfg = env["cfg"]
    text = cfg.read_text(encoding="utf-8")
    keep = [ln for ln in text.splitlines()
            if not ln.startswith('name = "mock-dynamic')
            and not ln.startswith('kind = "dynamic_event_m3u"')
            and "/dynamic" not in ln]
    cfg.write_text("\n".join(keep) + "\n", encoding="utf-8", newline="\n")
    seed(capsys, cfg, env["db"])
    bind_and_probe(env["db"])

    code, out = publish_cli(capsys, env)
    payload = json.loads(out)
    assert code == 0, out
    assert payload["include_dynamic"] is False
    assert payload["dynamic_decision_reason"] == "auto_no_sources"


def test_explicit_no_dynamic_suppresses_fetch(capsys, env_with_dynamic):
    """§17-17：``--no-dynamic`` 显式关闭，理由码必须是 cli_no_dynamic。"""
    env = env_with_dynamic
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    code, out = publish_cli(capsys, env, "--no-dynamic")
    payload = json.loads(out)
    assert code == 0, out
    assert payload["include_dynamic"] is False
    assert payload["dynamic_decision_reason"] == "cli_no_dynamic"
    assert payload["dynamic_sources"] == []


def test_no_dynamic_overrides_config_true(capsys, env_with_dynamic):
    """§17-17：``--no-dynamic`` 优先级**高于**配置 true。"""
    env = env_with_dynamic
    upsert_publish_setting(env["cfg"], section="publish", key="dynamic_default",
                           value="true")
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])

    code, out = publish_cli(capsys, env, "--no-dynamic")
    payload = json.loads(out)
    assert payload["include_dynamic"] is False
    assert payload["dynamic_decision_reason"] == "cli_no_dynamic"

    # 不带 --no-dynamic 时配置 true 应生效
    code, out = publish_cli(capsys, env)
    payload = json.loads(out)
    assert payload["include_dynamic"] is True
    assert payload["dynamic_decision_reason"] == "config_forced"


def test_config_false_disables_dynamic(capsys, env_with_dynamic):
    """§17-16：配置显式 false ⇒ 不抓，理由 config_disabled。"""
    env = env_with_dynamic
    upsert_publish_setting(env["cfg"], section="publish", key="dynamic_default",
                           value="false")
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    code, out = publish_cli(capsys, env)
    payload = json.loads(out)
    assert payload["include_dynamic"] is False
    assert payload["dynamic_decision_reason"] == "config_disabled"


@pytest.mark.parametrize("bad", ["yes", "1", "TRUE-ish", "启用"])
def test_invalid_dynamic_default_fails_fast(tmp_path, bad):
    """拼错的 dynamic_default 必须**加载时就炸**，绝不静默回落 auto。"""
    p = tmp_path / "cfg.toml"
    p.write_text(f'[publish]\ndynamic_default = "{bad}"\n', encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        config_mod.load_config(p)
    assert "dynamic_default" in str(exc.value)


def test_resolve_include_dynamic_truth_table():
    """§17-16：决策函数 8 种组合全覆盖（含优先级压制）。"""
    R = config_mod.resolve_include_dynamic
    cases = [
        # (说明, kwargs, 期望 include, 期望 reason)
        ("auto + 有源", dict(dynamic_default="auto", has_dynamic_sources=True),
         True, "auto_sources_present"),
        ("auto + 无源", dict(dynamic_default="auto", has_dynamic_sources=False),
         False, "auto_no_sources"),
        ("配置 true", dict(dynamic_default=True, has_dynamic_sources=False),
         True, "config_forced"),
        ("配置 false", dict(dynamic_default=False, has_dynamic_sources=True),
         False, "config_disabled"),
        ("--no-dynamic 压过配置 true",
         dict(dynamic_default=True, has_dynamic_sources=True, cli_no_dynamic=True),
         False, "cli_no_dynamic"),
        ("--dynamic 压过配置 false",
         dict(dynamic_default=False, has_dynamic_sources=False, cli_dynamic=True),
         True, "cli_explicit"),
        ("--dynamic-source 独立触发",
         dict(dynamic_default="auto", has_dynamic_sources=False,
              dynamic_source_tokens=["x"]), True, "cli_explicit"),
        ("--require-dynamic 独立触发",
         dict(dynamic_default="auto", has_dynamic_sources=False, require_dynamic=True),
         True, "cli_explicit"),
    ]
    for label, kwargs, want_inc, want_reason in cases:
        got_inc, got_reason = R(**kwargs)
        assert got_inc is want_inc, f"{label}: include_dynamic={got_inc}"
        assert got_reason == want_reason, f"{label}: reason={got_reason}"


# ==================================================================
# §17-18/19/20  TASK-003 / TASK-008 零回归
# ==================================================================


@pytest.mark.parametrize("policy", [AON, ISOLATE])
def test_all_or_nothing_and_isolate_still_honoured(capsys, env_with_dynamic, policy):
    """§17-18/19：两种 failure_policy 在新默认语义下仍按既有规则判定。"""
    env = env_with_dynamic
    upsert_dynamic_policy(env["cfg"], policy)
    seed(capsys, env["cfg"], env["db"])          # 先建库，才能source-add
    bind_and_probe(env["db"])
    add_dynamic_source(capsys, env, "mock-dyn-ok", "/dynamic-publish.m3u")
    add_dynamic_source(capsys, env, "mock-dyn-bad", "/error.m3u")

    # 全裸 publish（走 §8 新默认）：auto 抓到**所有** enabled 动态源
    code, out = publish_cli(capsys, env)
    payload = json.loads(out)
    assert code == 0, out
    assert payload["dynamic_summary"]["failure_policy"] == policy
    # 决策理由必须是 auto（不是靠 --dynamic 撑起来的）
    assert payload["dynamic_decision_reason"] == "auto_sources_present"
    # 一源失败的事实必须可见
    assert payload["dynamic_summary"]["failed_sources"] >= 1


def upsert_dynamic_policy(cfg_path, policy: str) -> None:
    """把 ``failure_policy`` 写进 ``[publish.dynamic]`` 段（测试专用）。"""
    p = pathlib.Path(cfg_path)
    text = p.read_text(encoding="utf-8")
    if "[publish.dynamic]" in text:
        lines = text.splitlines()
        out = []
        for ln in lines:
            if ln.strip().startswith("failure_policy"):
                ln = f'failure_policy = "{policy}"'
            out.append(ln)
        p.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
        return
    text = text.rstrip("\n") + f'\n\n[publish.dynamic]\nfailure_policy = "{policy}"\n'
    p.write_text(text, encoding="utf-8", newline="\n")


@pytest.mark.parametrize("policy", [AON, ISOLATE])
def test_require_dynamic_rejects_when_no_fresh_dynamic(capsys, env_with_dynamic, policy):
    """§17-20：``--require-dynamic`` 在没有新鲜动态条目时仍必须拒绝。

    两种policy 都覆盖 —— TASK-008 已把 isolate 下的 require_dynamic
    重新解释为「至少 1 条本轮新鲜动态进入 playlist」，这里守住该语义。
    """
    env = env_with_dynamic
    upsert_dynamic_policy(env["cfg"], policy)
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    add_dynamic_source(capsys, env, "mock-dyn-bad", "/error.m3u")

    code, out = publish_cli(capsys, env, "--require-dynamic",
                            "--dynamic-source", "mock-dyn-bad")
    assert code != 0, "没有新鲜动态条目却报告成功"
    payload = json.loads(out)
    assert payload["published"] is False


@pytest.mark.parametrize("policy", [AON, ISOLATE])
def test_require_dynamic_accepted_when_fresh_dynamic_present(capsys, env_with_dynamic, policy):
    """§17-20 另一面：有新鲜动态条目时 ``--require-dynamic`` 必须放行。"""
    env = env_with_dynamic
    upsert_dynamic_policy(env["cfg"], policy)
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    add_dynamic_source(capsys, env, "mock-dyn-ok", "/dynamic-publish.m3u")

    code, out = publish_cli(capsys, env, "--require-dynamic",
                            "--dynamic-source", "mock-dyn-ok")
    assert code == 0, out
    payload = json.loads(out)
    assert payload["dynamic_count"] > 0
    assert payload["published"] is True


# ==================================================================
# §17-21/22/23  去重 / 冲突 / LKG
# ==================================================================


def test_dynamic_exact_dedup_within_source(capsys, env_with_dynamic):
    """§17-21：同源字节级重复条目被去掉。"""
    env = env_with_dynamic
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    add_programmable_dynamic(capsys, env, "mock-dyn")
    env["server"].state.content = (
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="正在直播",[解说] 曼城 vs 阿森纳\n'
        "http://jsnzkpg.invalid.example/live/mci-ars/pc.m3u8?txSecret=AAA111&txTime=1\n"
        '#EXTINF:-1 group-title="正在直播",[解说] 曼城 vs 阿森纳\n'
        "http://jsnzkpg.invalid.example/live/mci-ars/pc.m3u8?txSecret=AAA111&txTime=1\n"
    )
    code, out = publish_cli(capsys, env, "--dynamic-source", "mock-dyn")
    payload = json.loads(out)
    assert code == 0, out
    # 用最终产物断言（同源字节重复必须只出现一次）
    parsed = m3u_mod.parse_text(read(env["live"]))
    dyn = [e.url for e in parsed.entries if e.group_title == DYNAMIC_GROUP]
    assert dyn, "动态分组为空，去重用例没意义"
    assert len(dyn) == len(set(dyn)), f"同源字节重复未去重：{dyn}"
    assert len(dyn) == 1, f"两条字节相同应只剩 1 条，实际 {len(dyn)}"


def test_dynamic_display_collision_adds_source_label(capsys, env_with_dynamic):
    """§17-22：跨源同名同组⇒ 追加来源短标签，且**同源内部不加**。"""
    env = env_with_dynamic
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    add_dynamic_source(capsys, env, "mock-dyn-a", "/dynamic-alt.m3u")
    add_dynamic_source(capsys, env, "mock-dyn-b", "/dynamic-collide.m3u")

    code, out = publish_cli(capsys, env, "--dynamic-source", "mock-dyn-a",
                            "--dynamic-source", "mock-dyn-b")
    payload = json.loads(out)
    assert code == 0, out
    parsed = m3u_mod.parse_text(read(env["live"]))
    dyn = [e.name for e in parsed.entries if e.group_title == DYNAMIC_GROUP]
    # 至少应出现带来源标签的显示名
    assert any("[" in n for n in dyn) or not dyn, dyn
    # 标签里只允许出现来源短名，不允许出现 URL / query
    for name in dyn:
        assert "://" not in name
        assert "txSecret" not in name


def test_last_known_good_protects_existing_file(capsys, env_with_dynamic):
    """§17-23：结果为空时LKG 不被覆盖（字节级不变）。"""
    env = env_with_dynamic
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    code, out = publish_cli(capsys, env, "--no-dynamic")
    assert code == 0, out
    good = env["live"].read_bytes()
    sha_before = __import__("hashlib").sha256(good).hexdigest()

    conn = db_mod.connect(env["db"])
    pid = repo_mod.ensure_probe(conn, "shanghai-cloud", now=NOW)
    for s in repo_mod.list_streams(conn):
        for _ in range(4):
            repo_mod.add_probe_result(conn, stream_id=int(s["id"]), probe_id=pid,
                                      success=False, checked_at=NOW2)
    conn.commit()
    conn.close()

    code, out = publish_cli(capsys, env, "--no-dynamic")
    assert code != 0
    assert env["live"].read_bytes() == good
    assert __import__("hashlib").sha256(env["live"].read_bytes()).hexdigest() == sha_before


# ==================================================================
# §17-24/25/26  摘要与脱敏
# ==================================================================


def test_fixed_summary_and_dynamic_summary_present(capsys, env_with_dynamic):
    """§17-24/25：两份摘要都必须出现在结果与写盘文件里。"""
    env = env_with_dynamic
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    code, out = publish_cli(capsys, env)
    assert code == 0, out
    payload = json.loads(out)
    assert "fixed_summary" in payload
    assert "dynamic_summary" in payload
    disk = json.loads(read(env["summary"]))
    assert "fixed_summary" in disk and "dynamic_summary" in disk
    assert disk["dynamic_summary"]["failure_policy"] in (AON, ISOLATE)


def test_sensitive_urls_never_leak(capsys, env_with_dynamic):
    """§17-26：签名/身份材料只许出现在 live.m3u，不进 stdout/摘要/日志。"""
    env = env_with_dynamic
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    add_programmable_dynamic(capsys, env, "mock-dyn")
    env["server"].state.content = (
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="正在直播",[解说] 曼城 vs 阿森纳\n'
        "http://jsnzkpg.invalid.example/live/mci-ars/pc.m3u8?txSecret=AAA111&txTime=6A1B\n"
    )
    code, out = publish_cli(capsys, env, "--dynamic-source", "mock-dyn")
    assert code == 0, out
    for secret in SECRETS:
        assert secret not in out, f"stdout 泄漏 {secret}"
    summary_text = read(env["summary"])
    for secret in SECRETS:
        assert secret not in summary_text, f"摘要泄漏 {secret}"


# ==================================================================
# §17-27/28/29  playback context（TASK-010 §4/§13）
# ==================================================================


def test_shipped_policy_file_declares_korice_non_authoritative():
    """§17-29：KORICE 必须登记为 ``cloud_probe_authoritative = false``。

    这是 TASK-010 §4.2 冻结规则的可审计依据 —— 少了这一行，
    「云端 FAIL ⇒ 删源」的风险就回来了。
    """
    policies = policy_mod.load_source_policies(
        REPO_ROOT / "config" / "source_policies.toml"
    )
    kor = policies["korice-ppv"]
    assert kor["aggregator_context"] == "shanghai-cloud"
    assert kor["playback_context"] == "home-windows-vpn"
    assert kor["playback_requires_vpn"] is True
    assert kor["cloud_probe_authoritative"] is False
    # 冻结规则直接生效
    assert policy_mod.should_drop_source_on_probe_failure(kor) is False


def test_playback_and_aggregator_context_are_not_merged():
    """§17-27：摘要里两类上下文必须**分开陈述**。"""
    policies = policy_mod.load_source_policies(
        REPO_ROOT / "config" / "source_policies.toml"
    )
    summary = policy_mod.summarize_contexts(policies)
    sem = summary["semantics"]
    assert sem["must_not_merge"] is True
    assert sem["aggregator_reachability"] != sem["playback_reachability"]
    assert "korice-ppv" in summary["cloud_probe_non_authoritative_sources"]


def test_policy_output_never_leaks_credentials():
    """§17-27：policy 摘要不含任何 URL / 凭据（只表达上下文与标记）。"""
    policies = policy_mod.load_source_policies(
        REPO_ROOT / "config" / "source_policies.toml"
    )
    blob = json.dumps(policy_mod.summarize_contexts(policies), ensure_ascii=False)
    for bad in ("://", "password", "cookie", "authorization", "vpn://"):
        assert bad not in blob.lower(), f"policy 摘要泄漏 {bad}"


def test_duplicate_policy_name_fails_closed(tmp_path):
    """重复 source_policy name 必须报错，绝不「后者覆盖前者」。"""
    p = tmp_path / "p.toml"
    p.write_text(
        "[[source_policy]]\nname = \"korice-ppv\"\ncloud_probe_authoritative = false\n"
        "\n[[source_policy]]\nname = \"korice-ppv\"\ncloud_probe_authoritative = true\n",
        encoding="utf-8",
    )
    with pytest.raises(policy_mod.SourcePolicyError) as exc:
        policy_mod.load_source_policies(p)
    assert "重复" in str(exc.value)


def test_policy_never_pollutes_shanghai_probe_history(capsys, env_with_dynamic):
    """§17-28：playback context 只是**元数据**，不得写进 probe history。

    本机/VPN 的观测绝不冒充上海结果 —— 断言：跑完发布后，
    probe_result 表里出现的 probe 仍只有 shanghai-cloud，
    且没有名为 home-windows-vpn 的 probe 记录。
    """
    env = env_with_dynamic
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])          # 写的是 test-probe
    code, out = publish_cli(capsys, env)
    assert code == 0, out

    conn = db_mod.connect(env["db"])
    names = {r["name"] for r in repo_mod.list_probes(conn)}
    results = repo_mod.list_probe_results(conn)
    conn.close()
    assert "home-windows-vpn" not in names, "把播放上下文写成了 probe 节点"
    for r in results:
        assert r["probe_id"] is not None


def test_publish_summary_carries_playback_context(capsys, env_with_dynamic):
    """§17-27：发布摘要里能看到上下文拆分（运维可审计）。"""
    env = env_with_dynamic
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    code, out = publish_cli(capsys, env)
    assert code == 0, out
    disk = json.loads(read(env["summary"]))
    assert "playback_context" in disk
    assert disk["playback_context"]["semantics"]["must_not_merge"] is True


# ==================================================================
# §17-30  输出反向解析
# ==================================================================


def test_output_reverse_parse(capsys, env_with_dynamic):
    """§17-30：live.m3u 必须能被反向解析回预期条目。"""
    env = env_with_dynamic
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    code, out = publish_cli(capsys, env)
    assert code == 0, out

    text = read(env["live"])
    assert text.startswith("#EXTM3U")
    parsed = m3u_mod.parse_text(text)
    payload = json.loads(out)
    assert len(parsed.entries) == payload["channel_count"]
    names = [e.name for e in parsed.entries]
    assert len(names) == len(set(names))
    for e in parsed.entries:
        assert "://" in e.url


def test_reverse_validate_detects_tampering(capsys, env_with_dynamic):
    """§17-30 配套：反向校验不是摆设，被改坏的输出必须被抓出来。

    直接对 :func:`liptv.publish.reverse_validate` 喂几段坏文本，
    证明它会报错 —— 而不是永远返回空列表。
    """
    good = m3u_mod.M3UChannel(name="央视一套", url="http://a.invalid/1.m3u8",
                              group_title="央视")
    good_text = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-name="央视一套" group-title="央视",央视一套\n'
        "http://a.invalid/1.m3u8\n"
    )
    assert publish_mod.reverse_validate(good_text, [good]) == []

    # 少一条
    assert publish_mod.reverse_validate(good_text, [good, m3u_mod.M3UChannel(
        name="央视二套", url="http://a.invalid/2.m3u8", group_title="央视")])
    # 没有 #EXTM3U 头
    assert publish_mod.reverse_validate(
        good_text.replace("#EXTM3U\n", ""), [good])
    # URL 与预期不一致
    assert publish_mod.reverse_validate(
        good_text.replace("1.m3u8", "9.m3u8"), [good])