"""TASK-008 生产多动态源聚合测试。

全部离线：动态源打本机 mock HTTP 服务（`tools/mock_source_server.py`），
**不访问任何公网**，不依赖 JSNZKPG / KORICE 在线。

覆盖任务书 §14 的 12 个场景：
  1. 两源成功且均有条目；2. A 成功 / B HTTP 500；3. A timeout / B 成功；
  4. 两源都失败；5. 一源成功但过滤后 0 条；6. 两源出现完全相同条目；
  7. 两源相同 display name/group、不同 URL；8. [解说]/[原声] 不误合并；
  9. malformed M3U；10. source response 截断；11. query/token 脱敏；
  12. 旧 live.m3u 里的旧动态 URL 在新轮失败时**不得**被组合层读取。

零回归护栏：``all_or_nothing`` 默认语义（TASK-003）与单源行为必须完全不变。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liptv import config as config_mod
from liptv import m3u as m3u_mod
from liptv import publish as publish_mod
from liptv.cli import main as cli_main

# 复用 TASK-003 的夹具与工具，避免两套相似的脚手架（也保证老测试语义不变）。
from tests.test_publish import (  # noqa: E402
    DYNAMIC_GROUP,
    add_dynamic_source,
    bind_and_probe,
    read,
    seed,
)

NOW = "2026-10-05T04:00:00+00:00"

ISOLATE = "isolate"
AON = "all_or_nothing"

# 样本里的签名材料：任何输出（除 live.m3u 本身）都不许出现
SECRETS = ("txSecret", "KOR1K1", "KOR4K4", "AAA111", "GGG777", "COR1C1", "DUP9999")


# ------------------------------------------------------------------ 工具


def build(capsys, *argv) -> tuple[int, str]:
    code = cli_main([str(a) for a in argv])
    return code, capsys.readouterr().out


def _upsert_in_dynamic_table(path: pathlib.Path, key: str, line_text: str) -> None:
    """在 ``[publish.dynamic]`` 段内**改写或插入**某个键（不新建第二张表）。

    TOML 硬约束：同一张表不能出现两次。之前踩过这个坑 —— 在文件末尾追加
    ``[publish.dynamic]`` 会让 tomllib 在后续键上报错，CLI 把异常吞掉后
    表现为「策略没生效」，所有 isolate 用例悄悄退化成 all_or_nothing。
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_dynamic = False
    in_table = False
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_dynamic and in_table and not replaced:
                out.append(line_text)      # 段内缺该键 ⇒ 补在段尾
                replaced = True
            in_dynamic = stripped == "[publish.dynamic]"
            in_table = False
        if in_dynamic and stripped.startswith(f"{key} "):
            if not replaced:
                out.append(line_text)
                replaced = True
            continue
        if in_dynamic and stripped.startswith("["):
            in_table = True
        out.append(line)
    if in_dynamic and in_table and not replaced:
        out.append(line_text)
        replaced = True
    if not replaced:
        out += ["", "[publish.dynamic]", line_text]
    path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")


def set_policy(env, policy: str) -> None:
    """设置 ``[publish.dynamic].failure_policy``。"""
    _upsert_in_dynamic_table(
        env["cfg"], "failure_policy", f'failure_policy = "{policy}"'
    )


def set_allowlist(env, groups: list[str]) -> None:
    """设置 ``[publish.dynamic].include_groups``（严格白名单；空 = 不启用）。"""
    rendered = ", ".join(f'"{g}"' for g in groups)
    text = f"include_groups = [{rendered}]" if groups else "include_groups = []"
    _upsert_in_dynamic_table(env["cfg"], "include_groups", text)


def publish_cli(capsys, env, *extra):
    return build(
        capsys, "publish", "--config", env["cfg"], "--db", env["db"],
        "--now", NOW, "--json", *extra,
    )


def dynamic_names(env) -> list[str]:
    """live.m3u 里属于动态分组的显示名（按出现顺序）。"""
    parsed = m3u_mod.parse_text(read(env["live"]))
    return [e.name for e in parsed.entries if e.group_title == DYNAMIC_GROUP]


def dynamic_urls(env) -> list[str]:
    parsed = m3u_mod.parse_text(read(env["live"]))
    return [e.url for e in parsed.entries if e.group_title == DYNAMIC_GROUP]


def report_of(payload: dict, name: str) -> dict:
    return next(r for r in payload["dynamic_sources"] if r["source_name"] == name)


# ------------------------------------------------------------------ 夹具


@pytest.fixture()
def env(tmp_path, mock_server):
    """TASK-008 双动态源离线环境。

    来源可见性层次（与 TASK-003 的 ``env`` 夹具一致，避免裸 ``--dynamic`` 被坏源带崩）：

      * ``mock-fixed``       fixed，enabled=true  → 3 条固定频道；
      * ``jsnzkpg-sports``   dynamic，enabled=true  → 联赛名分组（3 条合格）；
      * ``korice-ppv``       dynamic，enabled=true  → 赛事类别分组（3 条合格）；
      * ``dyn-bad``          dynamic，enabled=false → 恒 500；
      * ``dyn-slow``         dynamic，enabled=false → 延迟 3 秒（配合 timeout 测超时）；
      * ``dyn-korice-twin``  dynamic，enabled=false → **与 korice 字节完全相同**（精确去重）；
      * ``dyn-collide``      dynamic，enabled=false → **与 korice 首条同名同组、URL 不同**（标签）；
      * ``dyn-malformed``    dynamic，enabled=false → HTML 错误页（malformed M3U）；
      * ``dyn-truncated``    dynamic，enabled=false → 末尾 #EXTINF 缺 URL（截断）。

    ⚠️ 跨源场景一律用**独立注册的源**指向同一端点，**不改 config 里的 URL** ——
    源 URL 在 ``source-register`` 时就写进数据库了，事后再改 config 不会更新已注册的源。
    """
    server, base = mock_server
    server.state.set_ok()

    cfg = tmp_path / "config.toml"
    db = tmp_path / "liptv.sqlite3"
    out_dir = tmp_path / "out"
    (out_dir / "tmp").mkdir(parents=True, exist_ok=True)

    def source(name: str, kind: str, endpoint: str, enabled: bool) -> str:
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
    body += source("mock-fixed", "fixed_m3u", "/seq.m3u", True)
    body += source("jsnzkpg-sports", "dynamic_event_m3u", "/dynamic-publish.m3u", True)
    body += source("korice-ppv", "dynamic_event_m3u", "/dynamic-korice.m3u", True)
    body += source("dyn-bad", "dynamic_event_m3u", "/error.m3u", False)
    body += source("dyn-slow", "dynamic_event_m3u", "/slow.m3u?seconds=3", False)
    # 与 korice 指向**同一端点** ⇒ 产出的条目字节完全相同（精确去重场景）
    body += source("dyn-korice-twin", "dynamic_event_m3u", "/dynamic-korice.m3u", False)
    # 与 korice 首条同名同组、URL 不同（来源标签场景）
    body += source("dyn-collide", "dynamic_event_m3u", "/dynamic-collide.m3u", False)
    body += source("dyn-malformed", "dynamic_event_m3u", "/notm3u.m3u", False)
    body += source("dyn-truncated", "dynamic_event_m3u", "/truncated.m3u", False)

    # §12：promo 排除必须**配置化** —— KORICE 用的是英文 ``Promo`` 分组，
    # 默认排除表只有中文宣传/公告/推广/广告，所以这里显式加上。
    # 这样才能证明「按通用配置规则过滤，而不是硬编码某家的分组名」。
    body += (
        "\n[publish.dynamic]\n"
        'exclude_groups = ["宣传", "公告", "推广", "广告", "Promo"]\n'
    )

    cfg.write_text(body, encoding="utf-8", newline="\n")
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
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    return env


# =============================================== §16.2/§16.3 配置与零回归护栏


def test_failure_policy_defaults_to_all_or_nothing():
    """默认必须是 all_or_nothing（TASK-003 既有语义），旧配置零回归。"""
    assert (
        config_mod.DEFAULT_CONFIG["publish"]["dynamic"]["failure_policy"] == AON
    )
    assert publish_mod.DEFAULT_DYNAMIC_FILTERS["failure_policy"] == AON
    merged = publish_mod.normalize_dynamic_filters(None)
    assert merged["failure_policy"] == AON
    # 显式写旧配置（没有这个键）也必须回落到默认值
    assert publish_mod.normalize_dynamic_filters({"include_replay": True})[
        "failure_policy"
    ] == AON


@pytest.mark.parametrize("value", ["isolate", "all_or_nothing", " isolate "])
def test_failure_policy_accepts_whitelist(value):
    assert publish_mod.validate_failure_policy(value) == value.strip()


@pytest.mark.parametrize(
    "value", ["ISOLATE", "Isolat", "isolate_all", "", "none", None, 1, True, ["isolate"]]
)
def test_failure_policy_rejects_everything_else(value):
    """非法值必须抛错，**绝不**静默回落默认值。"""
    with pytest.raises(ValueError):
        publish_mod.validate_failure_policy(value)


def test_invalid_failure_policy_fails_fast_at_config_load(tmp_path):
    """写错的策略名在**加载配置时**就炸，而不是悄悄按旧策略跑。"""
    cfg = tmp_path / "bad.toml"
    cfg.write_text(
        "[database]\npath = \"x.sqlite3\"\n"
        "\n[publish.dynamic]\nfailure_policy = \"isolate_all\"\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(ValueError, match="failure_policy"):
        config_mod.load_config(cfg)


def test_config_load_rejects_non_string_policy(tmp_path):
    cfg = tmp_path / "bad2.toml"
    cfg.write_text(
        "[database]\npath = \"x.sqlite3\"\n"
        "\n[publish.dynamic]\nfailure_policy = 1\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(ValueError):
        config_mod.load_config(cfg)


def test_all_or_nothing_keeps_task003_semantics_on_two_sources(capsys, ready):
    """§16.3 零回归：默认策略下，部分失败依旧是「只发固定频道」。"""
    set_policy(ready, AON)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "dyn-bad"
    )
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_FIXED_ONLY
    assert payload["dynamic_fail_closed"] is True
    assert payload["dynamic_count"] == 0
    assert payload["dynamic_summary"]["failure_policy"] == AON
    assert report_of(payload, "jsnzkpg-sports")["discarded"] == 3
    # 实际文件里不得有任何动态线路
    assert DYNAMIC_GROUP not in read(ready["live"])


def test_all_or_nothing_never_labels_or_cross_dedupes(capsys, ready):
    """§15：all_or_nothing **不做**跨源去重、不加来源标签（TASK-003 行为不变）。

    用「korice vs dyn-korice-twin」：两源指向同一端点、字节级完全一样，
    isolate 下会被精确去重，all_or_nothing 下必须**原样保留全部**。
    """
    set_policy(ready, AON)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "korice-ppv",
        "--dynamic-source", "dyn-korice-twin",
    )
    payload = json.loads(out)
    assert code == 0, out
    # 一条都没去重、没加标签
    assert payload["cross_source_events"] == []
    assert payload["dynamic_count"] == 8          # 4 + 4，原样保留
    names = dynamic_names(ready)
    assert sum(1 for n in names if "Chiefs vs Bills" in n) == 4
    assert not any("KORICE" in n or "TWIN" in n for n in names)


# ==================================================== §16.4/§16.5 isolate 隔离


def test_isolate_both_sources_ok_publishes_both(capsys, ready):
    """§14-1 / §16 场景 1：两源都成功且均有条目 ⇒ OK，两源条目都在。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "korice-ppv"
    )
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_OK
    assert payload["dynamic_count"] == 7          # JSNZKPG 3 + KORICE 4
    summary = payload["dynamic_summary"]
    assert summary["failure_policy"] == ISOLATE
    assert summary["selected_sources"] == 2
    assert summary["successful_sources"] == 2
    assert summary["failed_sources"] == 0
    assert summary["published_entries"] == 7
    names = dynamic_names(ready)
    assert "[解说] 曼城 vs 阿森纳" in names          # JSNZKPG
    assert any("Chiefs vs Bills" in n for n in names)  # KORICE
    # 两源内容完全不同 ⇒ 没有跨源事件
    assert payload["cross_source_events"] == []


def test_isolate_one_source_http_500_keeps_other_published(capsys, ready):
    """§14-2 / §16.4：JSNZKPG 成功 + KORICE HTTP 500 ⇒ DEGRADED_DYNAMIC_PARTIAL。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "dyn-bad"
    )
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_DYNAMIC_PARTIAL
    assert payload["exit_code"] == publish_mod.EXIT_OK
    assert payload["published"] is True
    assert payload["dynamic_count"] == 3
    assert payload["dynamic_fail_closed"] is False   # isolate 不 fail-closed

    good, bad = report_of(payload, "jsnzkpg-sports"), report_of(payload, "dyn-bad")
    assert good["ok"] is True and good["included"] == 3
    assert bad["ok"] is False
    assert bad["included"] == 0                     # 失败源绝不贡献条目
    assert bad["discarded"] == 0

    summary = payload["dynamic_summary"]
    assert summary["selected_sources"] == 2
    assert summary["successful_sources"] == 1
    assert summary["failed_sources"] == 1
    assert summary["published_entries"] == 3

    # 实际文件：成功源的赛事确实在
    assert "[解说] 曼城 vs 阿森纳" in dynamic_names(ready)
    # 状态不得伪装成 OK
    assert payload["status"] != publish_mod.STATUS_OK


def test_isolate_one_source_timeout_keeps_other_published(capsys, ready):
    """§14-3 / §16.5（对称方向）：坏源在前、好源在后，顺序不影响隔离结果。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "dyn-bad", "--dynamic-source", "korice-ppv"
    )
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_DYNAMIC_PARTIAL
    assert payload["dynamic_count"] == 4
    assert any("Chiefs vs Bills" in n for n in dynamic_names(ready))


def test_isolate_timeout_source_is_isolated(capsys, ready, monkeypatch):
    """§14-3 真实超时：把 KORICE 的抓取打成超时，验证超时同样只丢该源。"""
    set_policy(ready, ISOLATE)
    from liptv import fetch as fetch_mod

    original = fetch_mod.fetch_text

    def flaky(url, **kwargs):
        if "dynamic-korice" in url:
            # FetchError(category, message) —— 参数顺序别写反
            raise fetch_mod.FetchError("TIMEOUT", "模拟超时")
        return original(url, **kwargs)

    # 对照组：不注入时两源都通
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "korice-ppv"
    )
    assert code == 0, out
    assert json.loads(out)["dynamic_count"] == 7

    monkeypatch.setattr(fetch_mod, "fetch_text", flaky)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "korice-ppv"
    )
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_DYNAMIC_PARTIAL
    assert payload["dynamic_count"] == 3   # 只剩 JSNZKPG 的 3 条
    bad = report_of(payload, "korice-ppv")
    assert bad["ok"] is False
    assert bad["error_category"] == "TIMEOUT"
    assert bad["included"] == 0
    assert "[解说] 曼城 vs 阿森纳" in dynamic_names(ready)
    assert not any("Chiefs vs Bills" in n for n in dynamic_names(ready))


def disable_fixed_source(env) -> None:
    """把 ``mock-fixed`` 改成 disabled —— 制造「本轮没有合格固定频道」。

    TASK-008 真机 smoke B 的教训：生产现状**没有**合格固定频道，
    所以「isolate 下一源失败」必须在 ``fixed_count == 0`` 下也被验证过，
    否则真机上会退化成 no-publish（见 test_isolate_one_source_fails_without_fixed）。
    """
    text = env["cfg"].read_text(encoding="utf-8")
    marker = 'name = "mock-fixed"'
    head, sep, tail = text.partition(marker)
    assert sep, "夹具里应存在 mock-fixed"
    tail = tail.replace("enabled = true", "enabled = false", 1)
    env["cfg"].write_text(head + sep + tail, encoding="utf-8", newline="\n")


def test_isolate_one_source_fails_without_fixed_still_publishes(capsys, env):
    """🚨 §16.17 真机回归：一源失败 + **fixed_count == 0** ⇒ 仍须发布存活源。

    真机 smoke B（2026-10-05）实测抓到：isolate 下「一源失败」被旧的
    ``dynamic_failed and fixed_count == 0`` 判据拦成 DEGRADED_NO_PUBLISH，
    成功源已抓到的 24 条赛事被整个丢弃。本测试锁死正确语义：
    成功源写出来的赛事**就是**可发布内容，不该因为没有固定频道而丢。
    """
    seed(capsys, env["cfg"], env["db"])  # 不 bind_and_probe ⇒ 没有合格固定频道
    disable_fixed_source(env)
    set_policy(env, ISOLATE)

    code, out = publish_cli(capsys, env, "--dynamic-source", "korice-ppv", "--dynamic-source", "dyn-bad")
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_DYNAMIC_PARTIAL
    assert payload["published"] is True
    assert payload["fixed_count"] == 0
    assert payload["dynamic_count"] == 4  # korice 4 条（Promo 分组被排除）
    summary = payload["dynamic_summary"]
    assert summary["failure_policy"] == ISOLATE
    assert summary["successful_sources"] == 1
    assert summary["failed_sources"] == 1
    assert summary["published_entries"] == 4
    assert summary["fail_closed"] is False
    # 失败源一条都不许进 playlist，存活源的一条都不许丢
    failed = [s for s in summary["sources"] if not s["ok"]]
    assert len(failed) == 1 and failed[0]["name"] == "dyn-bad"
    assert failed[0]["included"] == 0
    assert "KOR1K1" in env["live"].read_text(encoding="utf-8")


def test_all_or_nothing_one_source_fails_without_fixed_declines(capsys, env):
    """零回归护栏：同样的输入在 all_or_nothing 下**必须仍然**拒绝发布。

    TASK-003 冻结语义「动态失败且没有合格固定频道 ⇒ 无内容可发 ⇒ 不发布」
    与 isolate 无关，不能被 TASK-008 的修正带偏。
    """
    seed(capsys, env["cfg"], env["db"])
    disable_fixed_source(env)
    set_policy(env, AON)

    code, out = publish_cli(capsys, env, "--dynamic-source", "korice-ppv", "--dynamic-source", "dyn-bad")
    payload = json.loads(out)
    assert code == publish_mod.EXIT_NOT_PUBLISHED, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_NO_PUBLISH
    assert payload["published"] is False
    assert payload["fixed_count"] == 0
    assert payload["dynamic_count"] == 0
    # TASK-003：fail-closed ⇒ 本轮动态条目整体舍弃
    assert payload["dynamic_fail_closed"] is True
    assert payload["dynamic_discarded"] == 4
    assert not env["live"].exists()


def test_isolate_both_sources_fail_with_fixed_only(capsys, ready):
    """§14-4 下半 / §16.7：两源全失败但 fixed>0 ⇒ 只发固定，状态明确 degraded。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "dyn-bad", "--dynamic-source", "dyn-malformed"
    )
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_FIXED_ONLY
    assert payload["dynamic_count"] == 0
    assert payload["fixed_count"] == 3
    summary = payload["dynamic_summary"]
    assert summary["failed_sources"] == 2
    assert summary["successful_sources"] == 0
    # 实际文件只有固定频道
    assert DYNAMIC_GROUP not in read(ready["live"])


def test_isolate_both_fail_fixed_zero_keeps_last_known_good(capsys, env):
    """§14-4 / §16.6：fixed=0 且两源全失败 ⇒ 不发布，LKG 字节不变。"""
    seed(capsys, env["cfg"], env["db"])  # 不 bind_and_probe ⇒ 没有合格固定频道
    set_policy(env, ISOLATE)

    # 先用两个正常源发布一版作为 last-known-good
    code, out = publish_cli(
        capsys, env, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "korice-ppv"
    )
    assert code == 0, out
    first_bytes = env["live"].read_bytes()
    assert DYNAMIC_GROUP in first_bytes.decode("utf-8")
    first_dynamic = dynamic_urls(env)
    assert len(first_dynamic) == 7

    # 两源全失败再跑：不得覆盖
    code, out = publish_cli(
        capsys, env, "--dynamic-source", "dyn-bad", "--dynamic-source", "dyn-malformed"
    )
    payload = json.loads(out)
    assert code == publish_mod.EXIT_NOT_PUBLISHED, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_NO_PUBLISH
    assert payload["published"] is False
    assert payload["dynamic_count"] == 0
    assert payload["fixed_count"] == 0
    assert payload["dynamic_summary"]["failed_sources"] == 2
    # §10：LKG 字节**一字节不变**
    assert env["live"].read_bytes() == first_bytes
    # §10/§11：新轮失败时**不得**把旧动态 URL 当成本轮结果
    assert dynamic_urls(env) == first_dynamic
    for secret in ("KOR1K1", "AAA111"):
        assert secret in env["live"].read_text(encoding="utf-8")


def test_isolate_source_success_but_filtered_to_zero(capsys, ready):
    """§14-5 / §16.8：一源成功但过滤后 0 条，另一个源有条目 ⇒ 有条目的正常发布。

    这里用「只收严格白名单且白名单里没有该源分组」制造「fetch 成功但 0 条」。
    """
    set_policy(ready, ISOLATE)
    # include_groups 严格白名单：只放行 KORICE 的分组名 ⇒ JSNZKPG 全被滤掉
    set_allowlist(ready, ["American Football", "Basketball"])

    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "korice-ppv"
    )
    payload = json.loads(out)
    assert code == 0, out
    # JSNZKPG fetch 成功但全被白名单滤掉；KORICE 保留 3 条
    assert report_of(payload, "jsnzkpg-sports")["ok"] is True
    assert report_of(payload, "jsnzkpg-sports")["included"] == 0
    assert report_of(payload, "korice-ppv")["included"] == 4
    assert payload["dynamic_count"] == 4
    assert payload["status"] == publish_mod.STATUS_OK
    assert any("Chiefs vs Bills" in n for n in dynamic_names(ready))


def test_isolate_all_sources_filtered_to_zero_and_no_fixed_does_not_publish(
    capsys, env
):
    """isolate + 两源都成功但过滤后 0 条 + fixed=0 ⇒ 不发布空列表。"""
    seed(capsys, env["cfg"], env["db"])
    set_policy(env, ISOLATE)
    set_allowlist(env, ["不存在的组"])

    code, out = publish_cli(
        capsys, env, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "korice-ppv"
    )
    payload = json.loads(out)
    assert code == publish_mod.EXIT_NOT_PUBLISHED, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_NO_PUBLISH
    assert payload["published"] is False
    assert not env["live"].exists()


# ================================================== §16.9 require_dynamic 语义


def test_require_dynamic_satisfied_by_one_surviving_source(capsys, ready):
    """§16.9：isolate 下 require_dynamic = 「至少 1 条本轮新鲜动态条目」。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready,
        "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "dyn-bad",
        "--require-dynamic",
    )
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_DYNAMIC_PARTIAL
    assert payload["dynamic_count"] == 3


def test_require_dynamic_rejected_when_all_sources_fail(capsys, env):
    """§16.9：isolate 下所有源失败 ⇒ 不满足 require_dynamic ⇒ 整次拒绝。"""
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    set_policy(env, ISOLATE)

    code, out = publish_cli(
        capsys, env, "--dynamic-source", "dyn-bad", "--require-dynamic"
    )
    payload = json.loads(out)
    assert code == publish_mod.EXIT_REJECTED, out
    assert payload["status"] == publish_mod.STATUS_REJECTED_DYNAMIC_REQUIRED
    assert payload["published"] is False
    assert not env["live"].exists()


def test_require_dynamic_not_satisfied_by_fixed_entries(capsys, ready):
    """§4：isolate 下 fixed 条目**不能**满足 require_dynamic。

    制造条件：两个动态源 fetch 都成功，但白名单把它们的条目全滤掉 ⇒ 动态 0 条、
    固定 3 条。此时 ``--require-dynamic`` 必须拒绝，**不能**用固定频道满足。
    """
    set_policy(ready, ISOLATE)
    set_allowlist(ready, ["不存在的组"])

    code, out = publish_cli(capsys, ready, "--require-dynamic")
    payload = json.loads(out)
    assert code == publish_mod.EXIT_REJECTED, out
    assert payload["status"] == publish_mod.STATUS_REJECTED_DYNAMIC_REQUIRED
    assert payload["fixed_count"] == 3        # 固定频道确实存在
    assert payload["dynamic_count"] == 0     # 但一条动态都没进 playlist


def test_require_dynamic_rejected_when_filtered_to_zero(capsys, env):
    """§4：所有源 fetch 成功但过滤后 0 条 ⇒ 不满足 require_dynamic。"""
    seed(capsys, env["cfg"], env["db"])
    bind_and_probe(env["db"])
    set_policy(env, ISOLATE)
    set_allowlist(env, ["不存在的组"])

    code, out = publish_cli(
        capsys, env,
        "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "korice-ppv",
        "--require-dynamic",
    )
    payload = json.loads(out)
    assert code == publish_mod.EXIT_REJECTED, out
    assert payload["status"] == publish_mod.STATUS_REJECTED_DYNAMIC_REQUIRED


def test_require_dynamic_all_or_nothing_semantics_unchanged(capsys, ready):
    """§15 零回归：all_or_nothing 下 require_dynamic 仍是「一源失败即拒绝」。"""
    set_policy(ready, AON)
    code, out = publish_cli(
        capsys, ready,
        "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "dyn-bad",
        "--require-dynamic",
    )
    payload = json.loads(out)
    assert code == publish_mod.EXIT_REJECTED, out
    assert payload["status"] == publish_mod.STATUS_REJECTED_DYNAMIC_REQUIRED


# ================================== §16.10/§16.11 跨源精确去重与显示冲突标签


def test_isolate_exact_cross_source_duplicate_is_deduped(capsys, ready):
    """§14-6 / §16.10：两源**字节完全相同** ⇒ 精确去重并记录来源。"""
    set_policy(ready, ISOLATE)
    # korice 与 dyn-korice-twin 指向同一端点 ⇒ 跨源 (URL,显示名,分组) 字节一致
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "korice-ppv",
        "--dynamic-source", "dyn-korice-twin",
    )
    payload = json.loads(out)
    assert code == 0, out
    assert payload["dynamic_count"] == 4          # 8 条精确去重成 4 条

    events = [
        e for e in payload["cross_source_events"]
        if e["reason"] == publish_mod.REASON_CROSS_SOURCE_DUPLICATE
    ]
    assert events, payload["cross_source_events"]
    for event in events:
        assert event["kept_source"] and event["duplicate_source"]
        assert event["kept_source"] != event["duplicate_source"]
        # 事件记录同样脱敏
        assert "?" not in event["url"]


def test_cross_source_duplicate_record_keeps_earliest_source(capsys, ready):
    """§16.10：kept / duplicate 两侧必须写清是哪两个来源。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "korice-ppv", "--dynamic-source", "dyn-korice-twin"
    )
    payload = json.loads(out)
    assert code == 0, out
    events = [
        e for e in payload["cross_source_events"]
        if e["reason"] == publish_mod.REASON_CROSS_SOURCE_DUPLICATE
    ]
    # KORICE 样本 5 条里 4 条合格 ⇒ 4 条被精确去重，每条都记了来源
    assert len(events) == 4
    assert {e["kept_source"] for e in events} == {"korice-ppv"}
    assert {e["duplicate_source"] for e in events} == {"dyn-korice-twin"}
    assert events[0]["duplicate_source"] == "dyn-korice-twin"


def test_isolate_display_collision_adds_source_label(capsys, ready):
    """§14-7 / §16.11：同名同组但 URL 不同 ⇒ 两条都保留 + 追加稳定来源标签。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "korice-ppv", "--dynamic-source", "dyn-collide"
    )
    payload = json.loads(out)
    assert code == 0, out
    names = dynamic_names(ready)
    # 「Chiefs vs Bills」两源各一条 ⇒ 都保留（**不合并**），且都带来源标签
    # KORICE 首条与 dyn-collide 首条同名同组不同 URL ⇒ 各一条，都带来源标签
    labelled = [n for n in names if n.endswith("[KORICE]") or n.endswith("[DYN]")]
    assert len(labelled) == 2, names
    assert any(n.endswith("[KORICE]") for n in labelled)
    assert any(n.endswith("[DYN]") for n in labelled)   # dyn-collide → 首段 DYN

    events = [
        e for e in payload["cross_source_events"]
        if e["reason"] == publish_mod.REASON_DISPLAY_COLLISION_LABELED
    ]
    assert len(events) == 2
    # §16.11：**非冲突项**不得追加来源标签（精确比对，不用子串 ——
    # 「Chiefs vs Bills … (Backup)」是另一条，用子串会误判）。
    expected_untouched = {
        "Eagles vs Giants 2026-10-05 04:15",
        "Lakers vs Warriors 2026-10-05 10:30",
        "Heat vs Celtics 2026-10-05 08:00",
        "Chiefs vs Bills 2026-10-05 01:20 (Backup)",
    }
    assert expected_untouched <= set(names), sorted(set(names))
    for name in expected_untouched:
        assert "[" not in name and "]" not in name, f"非冲突项被误加标签：{name}"


def test_source_label_is_stable_and_derived_from_name():
    assert publish_mod.source_label("jsnzkpg-sports") == "JSNZKPG"
    assert publish_mod.source_label("korice-ppv") == "KORICE"
    assert publish_mod.source_label("mock_dynamic_alt") == "MOCK"
    assert publish_mod.source_label("solo") == "SOLO"
    assert publish_mod.source_label("") == "?"
    # 同一来源每轮标签恒定（不能带随机/序号成分）
    assert publish_mod.source_label("jsnzkpg-sports") == publish_mod.source_label(
        "jsnzkpg-sports"
    )


def test_isolate_commentary_and_raw_are_never_merged(capsys, ready):
    """§14-8 / §13：``[解说]`` 与 ``[原声]`` 视为不同显示项，不加标签不合并。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(capsys, ready, "--dynamic-source", "jsnzkpg-sports")
    payload = json.loads(out)
    assert code == 0, out
    assert payload["cross_source_events"] == []
    names = dynamic_names(ready)
    assert "[解说] 曼城 vs 阿森纳" in names
    assert "[原声] 曼城 vs 阿森纳" in names
    # 单源时不加任何来源标签
    assert all("JSNZKPG" not in n for n in names)


def test_display_collision_never_fuzzy_merges(capsys, ready):
    """§5/§16.11：不做 fuzzy matching —— 名字「相似」也绝不合并。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "korice-ppv"
    )
    payload = json.loads(out)
    assert code == 0, out
    # 两源全部条目都在，没有任何跨源合并/去重
    assert payload["dynamic_count"] == 7
    assert payload["cross_source_events"] == []


def test_same_source_duplicate_display_names_are_not_labelled(capsys, ready):
    """同源内「显示名+分组相同、URL 不同」**不加**来源标签（不是跨源冲突）。

    KORICE 样本里 ``Chiefs vs Bills`` 有 pc / alt 两条线路（URL 不同、显示名相同）。
    那是**该来源自己**的显示问题，不是多源聚合产生的冲突 ⇒ TASK-003 既有行为不变。
    """
    set_policy(ready, ISOLATE)
    code, out = publish_cli(capsys, ready, "--dynamic-source", "korice-ppv")
    payload = json.loads(out)
    assert code == 0, out
    assert payload["cross_source_events"] == []
    names = dynamic_names(ready)
    chiefs = [n for n in names if "Chiefs vs Bills" in n]
    assert len(chiefs) == 2
    assert all("Chiefs vs Bills" in n and "[" not in n for n in chiefs)


def test_unique_label_suffixes_colliding_source_labels():
    """两个来源派生出同一短标签时，补 ``#2`` 保证播放器仍能区分。"""
    used: dict[str, int] = {}
    assert publish_mod._unique_label("KORICE", used) == "KORICE"
    assert publish_mod._unique_label("KORICE", used) == "KORICE#2"
    assert publish_mod._unique_label("KORICE", used) == "KORICE#3"
    assert publish_mod._unique_label("JSNZKPG", used) == "JSNZKPG"


def test_cross_source_helpers_are_noop_under_all_or_nothing():
    items = [
        {"source_name": "a-src", "name": "X", "url": "http://x/1", "group": "G"},
        {"source_name": "b-src", "name": "X", "url": "http://x/1", "group": "G"},
    ]
    kept, events = publish_mod.resolve_cross_source_collisions(
        items, policy=publish_mod.FAILURE_POLICY_ALL_OR_NOTHING
    )
    assert len(kept) == 2 and events == []       # 一律不动（TASK-003 语义）


# ============================================ §14-9/§14-10 malformed 与截断


def test_isolate_malformed_m3u_isolated_not_fatal(capsys, ready):
    """§14-9：一源返回 HTML（malformed）⇒ 只丢该源，另一源照常发布。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "dyn-malformed"
    )
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_DYNAMIC_PARTIAL
    bad = report_of(payload, "dyn-malformed")
    assert bad["ok"] is False
    assert bad["status"] == "INVALID_M3U"
    assert bad["included"] == 0
    assert payload["dynamic_count"] == 3


def test_isolate_truncated_response_isolated(capsys, ready):
    """§14-10：响应截断（末尾 #EXTINF 缺 URL）⇒ 判失败并只丢该源。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "dyn-truncated"
    )
    payload = json.loads(out)
    assert code == 0, out
    assert payload["status"] == publish_mod.STATUS_DEGRADED_DYNAMIC_PARTIAL
    bad = report_of(payload, "dyn-truncated")
    assert bad["ok"] is False
    assert bad["included"] == 0
    assert payload["dynamic_count"] == 3


# ==================================================== §16.12 脱敏与不泄漏


def test_dynamic_summary_never_leaks_query_or_token(capsys, ready):
    """§14-11 / §16.12：per-source 摘要不含完整 URL / query / token / raw M3U。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "korice-ppv"
    )
    assert code == 0, out
    payload = json.loads(out)
    blob = json.dumps(payload["dynamic_summary"], ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in blob, secret
    # source URL 只到 scheme://host
    # 事件记录同样脱敏：只到 scheme://host（query / fragment / path 全丢）
    for source in payload["dynamic_summary"]["sources"]:
        assert source["url"].startswith("http://")
        assert "?" not in source["url"]
        assert "dynamic-publish" not in source["url"]
    # 跨源事件同样脱敏
    for event in payload["cross_source_events"]:
        assert "?" not in event["url"]
    # stdout 整体也不泄漏
    for secret in ("txSecret=", "KOR1K1", "token="):
        assert secret not in out


def test_error_text_is_redacted_and_length_bounded(capsys, ready):
    """§6：error 文本必须脱敏且限长。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "dyn-bad", "--dynamic-source", "jsnzkpg-sports"
    )
    assert code == 0, out
    payload = json.loads(out)
    for source in payload["dynamic_summary"]["sources"]:
        error = source["error"] or ""
        assert len(error) <= publish_mod.MAX_DYNAMIC_ERROR_CHARS + 1
        assert "http://" not in error and "https://" not in error


def test_dynamic_sources_summary_is_bounded(capsys, ready):
    """§6：runtime-status 里的 per-source 条数必须有界。"""
    payload = {
        "dynamic_report": [
            {"source_name": f"s{i}", "ok": True, "status": "ok", "fetched_entries": 1,
             "included": 1, "discarded": 0, "duration_ms": 1, "url_redacted": "http://h",
             "error": None, "error_category": None}
            for i in range(50)
        ]
    }
    composition = publish_mod.Composition(
        channels=[], fixed_count=0, dynamic_count=0, skipped_fixed=[],
        dynamic_report=payload["dynamic_report"], dynamic_excluded_reasons={},
        warnings=[], composition_errors=[], dynamic_sources_selected=50,
    )
    summary = publish_mod._dynamic_summary(composition)
    assert len(summary["sources"]) == publish_mod.MAX_DYNAMIC_SOURCES_IN_SUMMARY
    assert summary["sources_truncated"] is True
    assert summary["selected_sources"] == 50


# ============================================== §10 旧动态 URL 绝不复用


def test_compose_layer_never_reads_previous_live_m3u(capsys, ready):
    """§14-12 / §10：组合层**不读**旧 live.m3u；新轮失败时旧 URL 不得回流。

    这里做一次「白盒」验证：把 publish 的输出路径指向一个已被污染的旧文件，
    旧文件里含有一个本轮两个源都不会产出的 URL；跑完必须确认它没进新文件。
    """
    set_policy(ready, ISOLATE)
    # 手动造一个含「上一轮遗留 URL」的 live.m3u
    stale = (
        "#EXTM3U\n"
        f'#EXTINF:-1 group-title="{DYNAMIC_GROUP}",[解说] 上一轮遗留赛事\n'
        "http://stale.invalid.example/live/old/pc.m3u8?txSecret=OLD9999\n"
    )
    ready["live"].write_text(stale, encoding="utf-8", newline="\n")

    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "korice-ppv"
    )
    assert code == 0, out
    text = read(ready["live"])
    # 旧条目既没被保留，也没和新条目混在一起
    assert "上一轮遗留赛事" not in text
    assert "OLD9999" not in text
    assert "stale.invalid.example" not in text
    # 本轮两源的条目确实写进去了
    assert "[解说] 曼城 vs 阿森纳" in text
    assert "Chiefs vs Bills" in text


def test_isolate_failure_does_not_reuse_previous_dynamic_urls(capsys, ready):
    """§10：一轮 isolate 失败后，下一轮成功也不得把旧 URL 混进来。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "korice-ppv"
    )
    assert code == 0, out
    first_urls = set(dynamic_urls(ready))
    assert len(first_urls) == 7

    # 中间一轮：一源失败
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "dyn-bad", "--dynamic-source", "korice-ppv"
    )
    assert code == 0, out
    second = set(dynamic_urls(ready))
    assert len(second) == 4
    # 失败源 jsnzkpg 的旧 URL 一个都不许残留
    assert not any("jsnzkpg.invalid.example" in u for u in second)

    # 再一轮：两源都成功，条目集合应与第一轮一致（不含任何中间态残留）
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "korice-ppv"
    )
    assert code == 0, out
    assert set(dynamic_urls(ready)) == first_urls


# ================================================== §12 KORICE 真实结构兼容


def test_korice_real_structure_is_accepted(capsys, ready):
    """§12：KORICE 结构（赛事类别分组 + 时间文本）走**通用**过滤器。

    要求：不硬编码 NFL、不硬编码当前比赛名、不把「非 JSNZKPG 联赛格式」当异常。
    """
    set_policy(ready, ISOLATE)
    code, out = publish_cli(capsys, ready, "--dynamic-source", "korice-ppv")
    payload = json.loads(out)
    assert code == 0, out
    report = report_of(payload, "korice-ppv")
    assert report["ok"] is True
    assert report["fetched_entries"] == 5
    # Promo 分组被通用排除规则剔掉 ⇒ 4 条合格
    assert report["included"] == 4
    labels = report["excluded_by_reason"]
    assert labels[publish_mod.REASON_LABELS[publish_mod.REASON_EXCLUDED_GROUP]] == 1

    names = dynamic_names(ready)
    # 多个不同赛事类别都在（证明没有硬编码 NFL）
    assert any("Chiefs vs Bills" in n for n in names)
    assert any("Lakers vs Warriors" in n for n in names)
    # 同一场的两条不同线路都在（URL 不同不合并）
    assert sum(1 for n in names if "Chiefs vs Bills" in n) == 2
    # 宣传被排除
    assert not any("Official App" in n for n in names)


def test_korice_group_title_format_does_not_need_allowlist(capsys, ready):
    """§12：KORICE 的 ``American Football`` 分组**不需要**写进 include_groups。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(capsys, ready, "--dynamic-source", "korice-ppv")
    assert code == 0, out
    report = json.loads(out)["dynamic_sources"][0]
    # 没开白名单，就不该出现「不在白名单内」这个理由
    assert (
        publish_mod.REASON_LABELS[publish_mod.REASON_NOT_IN_INCLUDE_LIST]
        not in report["excluded_by_reason"]
    )


# ===================================================== §15 其余兼容性护栏


def test_single_source_behaviour_matches_task003(capsys, ready):
    """§15：单动态源行为与旧版本一致（不受 policy 影响）。"""
    for policy in (AON, ISOLATE):
        set_policy(ready, policy)
        code, out = publish_cli(capsys, ready, "--dynamic-source", "jsnzkpg-sports")
        payload = json.loads(out)
        assert code == 0, out
        assert payload["status"] == publish_mod.STATUS_OK
        assert payload["dynamic_count"] == 3
        assert payload["dynamic_fail_closed"] is False
        assert payload["cross_source_events"] == []


def test_publish_without_dynamic_never_hits_network(capsys, ready):
    """§15：不带 --dynamic 时完全不碰公网（policy 无论哪个值）。"""
    for policy in (AON, ISOLATE):
        set_policy(ready, policy)
        code, out = publish_cli(capsys, ready)
        assert code == 0, out
        payload = json.loads(out)
        assert payload["dynamic_sources"] == []
        assert payload["dynamic_count"] == 0


def test_status_exit_mapping_includes_partial():
    """§16：DEGRADED_DYNAMIC_PARTIAL 的退出码是 0（已发布的降级，不是失败）。"""
    assert publish_mod.STATUS_EXIT[publish_mod.STATUS_DEGRADED_DYNAMIC_PARTIAL] == 0
    # 且**不等于** OK：不得被伪装
    assert (
        publish_mod.STATUS_DEGRADED_DYNAMIC_PARTIAL != publish_mod.STATUS_OK
    )


def test_summary_file_records_failure_policy(capsys, ready):
    """写盘的发布摘要也要带上策略与来源统计，便于事后审计。"""
    set_policy(ready, ISOLATE)
    code, out = publish_cli(
        capsys, ready, "--dynamic-source", "jsnzkpg-sports", "--dynamic-source", "dyn-bad"
    )
    assert code == 0, out
    summary = json.loads(read(ready["summary"]))
    assert summary["status"] == publish_mod.STATUS_DEGRADED_DYNAMIC_PARTIAL
    assert summary["dynamic_summary"]["failure_policy"] == ISOLATE
    assert summary["dynamic_summary"]["failed_sources"] == 1
    assert summary["dynamic_summary"]["published_entries"] == 3
    for secret in ("txSecret=", "AAA111"):
        assert secret not in json.dumps(summary, ensure_ascii=False)
