"""TASK-005 真实测活测试（**全部离线**）。

硬约束（TASK-005 §12）：

  * 自动测试**不依赖公网**，也**不要求** CI 装了真实 ffprobe；
  * 公网 URL 不得作为自动验收条件。

做法：用 ``tools/fake_ffprobe.py`` 当「假的 ffprobe」，通过
``ffprobe_path=[python, tools/fake_ffprobe.py]`` 的 argv 数组形式注入。
这样跑的是 :mod:`liptv.probe` **真实的**子进程调用路径 ——
argv 数组 / ``shell=False`` / 管道限量读取 / 超时 terminate+kill 全部真实执行，
只是被执行的程序是那个替身（它自己绝不访问网络）。

覆盖 TASK-005 §13 的主要验收点：能力检查与环境错误、成功/失败字段映射、
错误分类稳定性、stderr 不落库、并发与「每 stream 每轮最多 1 条」、停止与孤儿进程、
URL 脱敏、库存过滤、scheduler 顺序与同轮生效。
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from types import SimpleNamespace

import pytest

from liptv import config as config_mod
from liptv import db as db_mod
from liptv import ingest as ingest_mod
from liptv import probe as probe_mod
from liptv import publish as publish_mod
from liptv import repo
from liptv import runtime as runtime_mod
from liptv import select as select_mod
from liptv.cli import main as cli_main
from tests.conftest import NOW

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FAKE_FFPROBE = REPO_ROOT / "tools" / "fake_ffprobe.py"
TOKEN = "SECRET_TOKEN_9f3a"

FAKE_PROBE_MODULE = pathlib.Path(probe_mod.__file__)


# ------------------------------------------------------------------ 夹具与工具

@pytest.fixture()
def probe_settings() -> probe_mod.ProbeSettings:
    """指向离线替身的测活配置（走真实子进程路径）。"""
    return probe_mod.ProbeSettings(
        enabled=True,
        ffprobe_path=sys.executable,
        extra_argv=(str(FAKE_FFPROBE),),
        timeout_seconds=8.0,
        analyze_seconds=1.0,
        max_concurrency=4,
    )


def stream_url(mode: str, *, token: str | None = TOKEN, tag: str | None = None) -> str:
    """构造一个「带模式标记 + 短时 token」的流地址。

    ``tag`` 只影响 query（因此 URL 不同、url_hash 不同），不影响模式识别
    —— 用来在同一 canonical 下造出**多条 URL 不同、行为相同**的 stream。
    """
    query = f"?token={token}" if token else "?"
    if tag:
        query += f"&tag={tag}"
    return f"http://media.example/live/{mode}.m3u8{query}"


def build_inventory(
    conn,
    urls: list[str],
    *,
    source_name: str = "src",
    kind: str = ingest_mod.KIND_FIXED,
    canonical_name: str | None = None,
) -> list[int]:
    """建 source/canonical/binding 并归集 stream；返回**本次新建**的 stream_id。

    注意：必须只返回新建的那几条 —— 之前的版本直接取 ``list_streams()[0]``，
    多次调用时会反复拿到同一行，导致「给 A 设的属性其实设到了 B 上」。
    """
    before = {int(row["id"]) for row in repo.list_streams(conn)}
    source_id = repo.add_source(conn, source_name, kind, now=NOW)
    for index, url in enumerate(urls):
        canonical_id = repo.add_canonical_channel(
            conn, canonical_name or f"频道{index}", category="新闻", now=NOW
        )
        entry = SimpleNamespace(
            name=f"raw-{index}", url=url, tvg_id=None, tvg_logo=None, group_title="新闻"
        )
        channel_id, _ = repo.upsert_source_channel(conn, source_id, entry, now=NOW)
        repo.bind_source_channel(conn, channel_id, canonical_id, now=NOW)
    repo.sync_streams(conn, now=NOW)
    conn.commit()
    return [
        int(row["id"]) for row in repo.list_streams(conn) if int(row["id"]) not in before
    ]


def rows_for(conn, stream_id: int) -> list[sqlite3.Row]:
    return repo.list_probe_results(conn, stream_id)


def run_cli(capsys, *argv) -> tuple[int, str]:
    code = cli_main([str(item) for item in argv])
    return code, capsys.readouterr().out


def write_config(tmp_path: pathlib.Path, *, ffprobe=FAKE_FFPROBE, enabled=True,
                 name: str = "config.toml") -> pathlib.Path:
    """写一份指向离线替身的临时配置（TOML 里 Windows 路径用正斜杠）。"""
    if ffprobe is None:
        probe_line = 'ffprobe_path = "definitely-not-ffprobe-xyz"\n'
    else:
        probe_line = (
            "ffprobe_path = ["
            f'"{pathlib.Path(sys.executable).as_posix()}", '
            f'"{pathlib.Path(ffprobe).as_posix()}"'
            "]\n"
        )
    cfg = tmp_path / name
    cfg.write_text(
        "[database]\n"
        f'path = "{(tmp_path / "liptv.sqlite3").as_posix()}"\n'
        "\n[output]\n"
        f'm3u_path = "{(tmp_path / "live.m3u").as_posix()}"\n'
        "\n[publish]\n"
        f'summary_path = "{(tmp_path / "publish-summary.json").as_posix()}"\n'
        "\n[probe]\n"
        f"enabled = {'true' if enabled else 'false'}\n"
        'name = "windows-local"\n'
        'location = "Windows"\n'
        f"{probe_line}"
        "timeout_seconds = 8.0\n"
        "analyze_seconds = 1.0\n"
        "max_concurrency = 4\n"
        "per_round_limit = 0\n",
        encoding="utf-8",
    )
    return cfg


# --------------------------------------------------- 1. 配置一致性 / argv 安全

def test_probe_default_config_matches_dataclass():
    """[probe] 代码默认值必须与 ProbeSettings 的字段默认值保持一致。"""
    defaults = config_mod.DEFAULT_CONFIG["probe"]
    plain = probe_mod.ProbeSettings()
    assert defaults["enabled"] is plain.enabled is False      # 安全默认：升级后不会突然请求播放流
    assert defaults["name"] == plain.name
    assert defaults["location"] == plain.location
    assert defaults["ffprobe_path"] == plain.ffprobe_path
    assert float(defaults["timeout_seconds"]) == float(plain.timeout_seconds)
    assert float(defaults["analyze_seconds"]) == float(plain.analyze_seconds)
    assert int(defaults["max_concurrency"]) == int(plain.max_concurrency)
    assert int(defaults["per_round_limit"]) == int(plain.per_round_limit)

    # 用户照抄的模板也必须与代码默认值逐字一致（否则抄到的是「文档说的」）
    example = REPO_ROOT / "config" / "config.example.toml"
    if example.exists():
        parsed = tomllib.loads(example.read_text(encoding="utf-8"))
        assert parsed["probe"] == defaults


def test_probe_excluded_source_kind_matches_ingest_constant():
    """repo 里的排除常量必须与 ingest.KIND_DYNAMIC 同值（刻意不做模块级 import）。"""
    assert repo.PROBE_EXCLUDED_SOURCE_KIND == ingest_mod.KIND_DYNAMIC


def test_ffprobe_path_may_be_argv_array():
    """``ffprobe_path`` 写成数组时展开成「可执行文件 + 前置参数」，仍是 argv 直传。"""
    settings = probe_mod.ProbeSettings.from_mapping(
        {"ffprobe_path": ["python", "tools/fake_ffprobe.py"], "enabled": True}
    )
    assert settings.ffprobe_path == "python"
    assert settings.extra_argv == ("tools/fake_ffprobe.py",)
    assert settings.command == ("python", "tools/fake_ffprobe.py")
    # 字符串形式仍然是唯一命令
    assert probe_mod.ProbeSettings.from_mapping({}).command == ("ffprobe",)


def test_argv_isolates_metacharacters_as_single_argument(probe_settings):
    """§13.8：URL 含 query token 与 shell 元字符时，仍是**一个** argv 元素、无 shell 解释。"""
    dangerous = (
        "http://media.example/live/argv.m3u8?token=" + TOKEN
        + "&next=1 | whoami ; $(id) `id` %PATH% ^&x"
    )
    argv = probe_mod.build_argv(probe_settings, dangerous)
    assert argv[-1] == dangerous
    assert argv.count(dangerous) == 1
    # 每个 ffprobe 参数都是独立元素，不是拼成一条字符串
    assert "-print_format" in argv and "json" in argv
    assert not any(item == f"-print_format json" for item in argv)

    outcome = probe_mod.run_process(argv, timeout_seconds=30.0)
    assert outcome.returncode == 0, outcome.stderr
    received = json.loads(outcome.stdout.decode("utf-8"))
    assert received[-1] == dangerous               # 原样到达，没有被任何 shell 拆开
    assert received.count(dangerous) == 1
    assert "whoami" not in " ".join(received[:-1])


def test_probe_module_never_uses_shell_true():
    """静态护栏：本模块只允许 argv 数组调用，绝不出现 shell=True。"""
    source = FAKE_PROBE_MODULE.read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "shell=False" in source


# --------------------------------------------------- 2. 能力检查（probe-check）

def test_probe_check_reports_fake_ffprobe(probe_settings):
    capability = probe_mod.check_ffprobe(probe_settings)
    assert capability.ok is True
    assert capability.version_line.startswith("ffprobe version")
    assert capability.exit_code == 0
    assert capability.error_type is None


def test_probe_check_missing_executable_is_environment_error():
    """§13.2：找不到 ffprobe 属于**环境错误**，不是「流失败」。"""
    capability = probe_mod.check_ffprobe(
        probe_mod.ProbeSettings(enabled=True, ffprobe_path="definitely-not-ffprobe-xyz")
    )
    assert capability.ok is False
    assert capability.error_type == probe_mod.ERROR_FFPROBE_NOT_FOUND


def test_cli_probe_check_ok_and_missing(capsys, tmp_path):
    cfg = write_config(tmp_path)
    code, out = run_cli(capsys, "probe-check", "--config", cfg, "--json")
    assert code == probe_mod.EXIT_OK, out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["status"] == "OK"
    assert pathlib.Path(payload["ffprobe_path"]) == pathlib.Path(sys.executable)
    assert payload["version"].startswith("ffprobe version")

    code, out = run_cli(capsys, "probe-check", "--config", cfg,
                        "--ffprobe-path", "definitely-not-ffprobe-xyz", "--json")
    assert code == probe_mod.EXIT_ENVIRONMENT, out
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error_type"] == probe_mod.ERROR_FFPROBE_NOT_FOUND
    assert payload["exit_code"] == probe_mod.EXIT_ENVIRONMENT


# --------------------------------------------------- 3. 成功路径与字段映射

def test_success_writes_real_fields_and_selector_uses_them(conn, probe_settings):
    """§13.3：真实结构字段写库，既有 selector 立刻可用。"""
    (stream_id,) = build_inventory(conn, [stream_url("ok")])
    summary = probe_mod.run_round(conn, settings=probe_settings, now=NOW)

    assert summary["stage"] == probe_mod.STAGE_OK
    assert (summary["requested"], summary["succeeded"], summary["failed"]) == (1, 1, 0)
    assert summary["written"] == 1

    (row,) = rows_for(conn, stream_id)
    assert row["success"] == 1
    assert row["error_type"] is None
    assert row["checked_at"] == NOW                      # 真实本轮时间（由调用方注入）
    assert isinstance(row["startup_ms"], int) and row["startup_ms"] > 0
    assert (row["resolution_width"], row["resolution_height"]) == (1920, 1080)
    assert row["bitrate_kbps"] == 6128                   # 来自 format.bit_rate，非猜测
    assert row["protocol"] == "http"                     # 来自 URL scheme
    # 无法可靠获得的字段必须是 NULL，而不是猜出来的值
    assert row["ipv_family"] is None
    assert row["http_status"] is None
    assert row["connect_ms"] is None

    # probe.last_seen_at 只在真实落库成功后推进
    probe_row = conn.execute(
        "SELECT * FROM probe WHERE id = ?", (row["probe_id"],)
    ).fetchone()
    assert probe_row["last_seen_at"] == NOW
    assert probe_row["name"] == probe_settings.name

    canonical_id = int(
        conn.execute("SELECT canonical_channel_id AS c FROM stream WHERE id = ?",
                     (stream_id,)).fetchone()["c"]
    )
    best = select_mod.select_best_stream(conn, canonical_id, now=NOW)
    assert best is not None and best.stream_id == stream_id
    assert best.success_rate == 1.0
    assert best.median_startup_ms == row["startup_ms"]


def test_audio_only_and_absent_metadata_stay_success_with_null_fields(conn, probe_settings):
    """纯音频 / 缺码率 / 缺宽高都不能判失败，也不能编造字段。"""
    ids = build_inventory(conn, [stream_url("audio"), stream_url("nobitrate"),
                                 stream_url("widthless")])
    summary = probe_mod.run_round(conn, settings=probe_settings, now=NOW)
    assert (summary["succeeded"], summary["failed"]) == (3, 0)

    audio, nobitrate, widthless = ids
    (audio_row,) = rows_for(conn, audio)
    assert audio_row["success"] == 1
    assert audio_row["resolution_width"] is None
    assert audio_row["resolution_height"] is None
    assert audio_row["bitrate_kbps"] == 96              # 来自音频流 bit_rate

    (no_bitrate_row,) = rows_for(conn, nobitrate)
    assert no_bitrate_row["success"] == 1
    assert no_bitrate_row["bitrate_kbps"] is None       # 拿不到就 NULL，不猜
    assert no_bitrate_row["resolution_width"] == 1920

    (widthless_row,) = rows_for(conn, widthless)
    assert widthless_row["success"] == 1
    assert widthless_row["resolution_width"] is None
    assert widthless_row["resolution_height"] is None


# --------------------------------------------------- 4. 失败分类与隐私

FAILURE_CASES = (
    ("dns", probe_mod.ERROR_DNS),
    ("tls", probe_mod.ERROR_TLS),
    ("http404", probe_mod.ERROR_HTTP),
    ("invalid", probe_mod.ERROR_INVALID_MEDIA),
    ("exit1", probe_mod.ERROR_PROCESS),
    ("badjson", probe_mod.ERROR_OUTPUT_INVALID),
    ("empty", probe_mod.ERROR_INVALID_MEDIA),
)


def test_failures_are_classified_and_stderr_is_not_persisted(conn, probe_settings):
    """§13.4：timeout / invalid media / process error 各写一条 failure，类别稳定，stderr 不落库。"""
    ids = build_inventory(conn, [stream_url(mode) for mode, _ in FAILURE_CASES])
    summary = probe_mod.run_round(conn, settings=probe_settings, now=NOW)
    assert summary["failed"] == len(FAILURE_CASES)
    assert summary["succeeded"] == 0
    assert summary["written"] == len(FAILURE_CASES)
    assert summary["stage"] == probe_mod.STAGE_DEGRADED   # 单条流失败 ≠ 整轮环境失败

    for stream_id, (mode, expected) in zip(ids, FAILURE_CASES):
        (row,) = rows_for(conn, stream_id)
        assert row["success"] == 0, mode
        assert row["error_type"] == expected, mode
        # 失败时不允许伪造任何测量值
        assert row["startup_ms"] is None
        assert row["resolution_width"] is None
        assert row["bitrate_kbps"] is None
        assert row["protocol"] is None
        assert row["error_type"] in probe_mod.ALL_ERROR_TYPES

    # stderr 全文绝不落库（schema 里没有这一列，这里再逐列确认没有夹带）
    forbidden = ("failed to resolve", "tls error", "server returned", "invalid data",
                 "name or service not known", "certificate")
    for row in repo.list_probe_results(conn):
        blob = " ".join(str(value) for value in tuple(row) if isinstance(value, str)).lower()
        assert not any(marker in blob for marker in forbidden), dict(row)


def test_process_error_and_timeout_are_different(conn, probe_settings):
    """timeout 必须有独立类别（不能和普通进程失败混在一起）。"""
    (stream_id,) = build_inventory(conn, [stream_url("sleep")])
    fast = probe_mod.ProbeSettings(
        enabled=True,
        ffprobe_path=probe_settings.ffprobe_path,
        extra_argv=probe_settings.extra_argv,
        timeout_seconds=1.5,
        analyze_seconds=1.0,
    )
    started = time.perf_counter()
    summary = probe_mod.run_round(conn, settings=fast, now=NOW)
    elapsed = time.perf_counter() - started
    assert summary["failed"] == 1
    (row,) = rows_for(conn, stream_id)
    assert row["error_type"] == probe_mod.ERROR_TIMEOUT
    assert row["startup_ms"] is None
    assert elapsed < 30.0                       # 超时被真正执行，没有被无限挂住


# --------------------------------------------------- 5. 输出上限 / 进程收尾

def test_timeout_kills_child_and_leaves_no_orphan(probe_settings):
    """§13.7：超时后必须 terminate/kill + wait，不能留下孤儿 ffprobe。"""
    outcome = probe_mod.run_process(
        probe_mod.build_argv(probe_settings, stream_url("sleep")),
        timeout_seconds=1.5,
    )
    assert outcome.started is True
    assert outcome.timed_out is True
    assert outcome.pid is not None
    assert outcome.returncode is not None
    assert runtime_mod._pid_alive(outcome.pid) is False


def test_oversized_stdout_and_stderr_are_capped_without_deadlock(probe_settings):
    """stdout/stderr 都要限量，且超出部分要读掉（否则子进程会被管道堵死）。"""
    big_out = probe_mod.run_process(
        probe_mod.build_argv(probe_settings, stream_url("oversize")), timeout_seconds=30.0
    )
    assert big_out.returncode == 0
    assert len(big_out.stdout) == probe_mod.STDOUT_CAP_BYTES
    assert big_out.stdout_truncated is True

    noise = probe_mod.run_process(
        probe_mod.build_argv(probe_settings, stream_url("bignoise")), timeout_seconds=30.0
    )
    assert noise.returncode == 1
    assert len(noise.stderr) == probe_mod.STDERR_CAP_BYTES
    assert noise.stderr_truncated is True

    # 超大输出会被判成「输出无效」，而不是被当成成功
    observation = probe_mod.probe_stream(
        stream_url("oversize"), stream_id=1, settings=probe_settings
    )
    assert observation.success is False
    assert observation.error_type == probe_mod.ERROR_OUTPUT_INVALID
    assert observation.stdout_truncated is True


# --------------------------------------------------- 6. 环境级错误不污染历史

def test_missing_ffprobe_writes_zero_rows_and_fails_the_round(conn):
    """§13.2/§13.5：ffprobe 缺失 → 0 条 probe_result，且整轮不得报完全 OK。"""
    ids = build_inventory(conn, [stream_url("ok"), stream_url("dns")])
    before = len(repo.list_probe_results(conn))
    settings = probe_mod.ProbeSettings(enabled=True, ffprobe_path="definitely-not-ffprobe-xyz")

    summary = probe_mod.run_round(conn, settings=settings, now=NOW)
    assert summary["stage"] == probe_mod.STAGE_FAILED
    assert summary["environment_error"] is True
    assert summary["error_type"] == probe_mod.ERROR_FFPROBE_NOT_FOUND
    assert summary["written"] == 0
    assert summary["succeeded"] == 0 and summary["failed"] == 0
    assert len(repo.list_probe_results(conn)) == before       # 一条都没写
    for stream_id in ids:
        assert rows_for(conn, stream_id) == []


def test_partial_environment_failure_is_discarded_not_written(conn, monkeypatch):
    """ffprobe 中途消失：本轮全部环境错误 ⇒ 整批丢弃，绝不写成一批「流失败」。"""
    ids = build_inventory(conn, [stream_url("ok"), stream_url("ok")])
    before = len(repo.list_probe_results(conn))

    def vanishing_probe(url, *, stream_id, settings, cancel=None, registry=None):
        return probe_mod.ProbeObservation(
            stream_id=stream_id,
            error_type=probe_mod.ERROR_FFPROBE_START_FAILED,
            message="ffprobe 消失了",
        )

    monkeypatch.setattr(probe_mod, "probe_stream", vanishing_probe)
    summary = probe_mod.run_round(
        conn,
        settings=probe_mod.ProbeSettings(enabled=True),
        now=NOW,
        capability=probe_mod.FfprobeCapability(ok=True, path="ffprobe"),  # 能力检查先通过
    )
    assert summary["stage"] == probe_mod.STAGE_FAILED
    assert summary["environment_error"] is True
    assert summary["error_type"] == probe_mod.ERROR_FFPROBE_START_FAILED
    assert summary["written"] == 0
    assert len(repo.list_probe_results(conn)) == before
    for stream_id in ids:
        assert rows_for(conn, stream_id) == []


def test_mixed_environment_failure_discards_the_whole_round(conn, monkeypatch):
    """QA-005A 永久回归（Review 01 反例）：一条成功 + 一条 FFPROBE_START_FAILED。

    首版只在「attempted **全部**是环境错误」时才整轮丢弃；混合场景下环境错误那条会掉进
    普通写库循环，被写成「stream 2 失败」—— 把「本机 ffprobe 中途失效」持久化成真实的
    频道健康历史。现在冻结为 fail-closed：**只要出现任一环境级错误 ⇒ 整轮 0 条
    probe_result**，连那条成功的一起丢弃（不做部分样本，避免选线偏斜）。
    """
    ids = build_inventory(conn, [stream_url("ok"), stream_url("ok")])
    before = len(repo.list_probe_results(conn))

    def mixed_probe(url, *, stream_id, settings, cancel=None, registry=None):
        if stream_id == ids[0]:
            return probe_mod.ProbeObservation(
                stream_id=stream_id, success=True, startup_ms=640,
                resolution_width=1920, resolution_height=1080,
                bitrate_kbps=6000, protocol="http",
            )
        return probe_mod.ProbeObservation(
            stream_id=stream_id,
            error_type=probe_mod.ERROR_FFPROBE_START_FAILED,
            message="ffprobe 在处理中途失效",
        )

    monkeypatch.setattr(probe_mod, "probe_stream", mixed_probe)
    summary = probe_mod.run_round(
        conn,
        settings=probe_mod.ProbeSettings(enabled=True),
        now=NOW,
        capability=probe_mod.FfprobeCapability(ok=True, path="ffprobe"),  # 能力检查先通过
    )

    assert summary["stage"] == probe_mod.STAGE_FAILED
    assert summary["environment_error"] is True
    assert summary["error_type"] == probe_mod.ERROR_FFPROBE_START_FAILED
    assert summary["succeeded"] == 0 and summary["failed"] == 0
    assert summary["written"] == 0
    assert summary["environment_failed_streams"] == [ids[1]]
    assert summary["discarded_observations"] == 2
    assert len(repo.list_probe_results(conn)) == before      # 一条都没写（含成功的那条）
    for stream_id in ids:
        assert rows_for(conn, stream_id) == []               # 不产生部分样本
    # 环境错误**不是**「某条流失败」：状态摘要不得把它列成失败线路
    assert probe_mod.summarize_for_status(summary)["failed_stream_ids"] == []
    # 没有任何真实落库 ⇒ last_seen_at 不推进
    row = conn.execute(
        "SELECT last_seen_at FROM probe WHERE name = ?", (probe_mod.ProbeSettings().name,)
    ).fetchone()
    assert row is None or row["last_seen_at"] is None


def test_missing_ffprobe_does_not_block_publish_and_runtime_flags_it(conn, tmp_path, probe_settings):
    """§13.10：环境级 probe 故障不污染历史，仍可用旧历史发布，但 runtime 必须明确告警。"""
    from tests.test_select import add_probe

    (stream_id,) = build_inventory(conn, [stream_url("ok")])
    add_probe(conn, stream_id, when=NOW, ok=True, startup_ms=800, resolution=(1280, 720))
    conn.commit()
    before = len(repo.list_probe_results(conn))

    result = runtime_mod.execute_round(
        conn,
        output_path=tmp_path / "live.m3u",
        group_order=["新闻", "其他"],
        selection_kwargs={"now": NOW},
        summary_path=tmp_path / "publish-summary.json",
        fetch_fixed=False,
        stamp=NOW,
        probe_settings=probe_mod.ProbeSettings(
            enabled=True, ffprobe_path="definitely-not-ffprobe-xyz"
        ),
    )
    assert len(repo.list_probe_results(conn)) == before        # 没污染
    assert result["published"] is True                         # 旧历史照常发布
    assert result["probe"]["stage"] == probe_mod.STAGE_FAILED
    assert result["probe"]["error_type"] == probe_mod.ERROR_FFPROBE_NOT_FOUND
    assert result["outcome"] == "degraded"                     # 不得报成完全 OK
    assert runtime_mod.round_exit_code(result) == runtime_mod.EXIT_ROUND_FAILED
    # 状态子摘要里没有 URL，只有计数与类别
    blob = json.dumps(result["probe"], ensure_ascii=False)
    assert "http" not in blob and TOKEN not in blob


def test_mixed_environment_failure_keeps_runtime_degraded_and_uses_old_history(
    conn, tmp_path, monkeypatch
):
    """QA-005A 的 runtime 侧：能力检查已通过、探测中途才坏 ⇒ 不回滚旧历史、仍能发布，
    但整轮必须明确 ``probe_stage=failed`` / ``outcome=degraded`` / exit 1。
    """
    from tests.test_select import add_probe

    ids = build_inventory(conn, [stream_url("ok"), stream_url("ok")])
    add_probe(conn, ids[0], when=NOW, ok=True, startup_ms=800, resolution=(1280, 720))
    conn.commit()
    before = len(repo.list_probe_results(conn))

    def mixed_probe(url, *, stream_id, settings, cancel=None, registry=None):
        if stream_id == ids[0]:
            return probe_mod.ProbeObservation(stream_id=stream_id, success=True, startup_ms=600)
        return probe_mod.ProbeObservation(
            stream_id=stream_id, error_type=probe_mod.ERROR_FFPROBE_START_FAILED
        )

    monkeypatch.setattr(probe_mod, "probe_stream", mixed_probe)
    # 能力检查必须“先通过”，否则测的就不是「中途坏掉」这条路径
    monkeypatch.setattr(
        probe_mod, "check_ffprobe",
        lambda settings, **kwargs: probe_mod.FfprobeCapability(ok=True, path="ffprobe"),
    )

    result = runtime_mod.execute_round(
        conn,
        output_path=tmp_path / "live.m3u",
        group_order=["新闻", "其他"],
        selection_kwargs={"now": NOW},
        summary_path=tmp_path / "publish-summary.json",
        fetch_fixed=False,
        stamp=NOW,
        probe_settings=probe_mod.ProbeSettings(enabled=True),
    )

    assert result["probe_stage"] == probe_mod.STAGE_FAILED
    assert result["probe"]["written"] == 0
    assert len(repo.list_probe_results(conn)) == before      # 一条新结果都没落
    assert result["published"] is True                       # 旧历史照常发布
    assert result["outcome"] == "degraded"                   # 不得报成完全 OK
    assert runtime_mod.round_exit_code(result) == runtime_mod.EXIT_ROUND_FAILED


# --------------------------------------------------- 7. 并发 / 每轮一条 / 停止

class RunProcessSpy:
    """统计真实子进程调用（含并发峰值与 PID），用来验证并发上限与孤儿进程。"""

    def __init__(self, monkeypatch) -> None:
        self.pids: list[int] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.probe_calls = 0
        self.version_calls = 0
        self._lock = threading.Lock()
        self._original = probe_mod.run_process
        monkeypatch.setattr(probe_mod, "run_process", self.__call__)

    def __call__(self, argv, **kwargs):
        is_stream_probe = bool(argv) and str(argv[-1]).startswith(("http://", "https://"))
        with self._lock:
            if is_stream_probe:
                self.probe_calls += 1
                self.concurrent += 1
                self.max_concurrent = max(self.max_concurrent, self.concurrent)
            else:
                self.version_calls += 1
        try:
            outcome = self._original(argv, **kwargs)
        finally:
            with self._lock:
                if is_stream_probe:
                    self.concurrent -= 1
        if outcome.pid:
            with self._lock:
                self.pids.append(outcome.pid)
        return outcome


def test_concurrency_is_bounded_and_one_row_per_stream_per_round(conn, probe_settings, monkeypatch):
    """§13.6：20+ stream 受 max_concurrency 限制，每 stream 每轮最多 1 条，无 sqlite 线程错误。"""
    ids = build_inventory(conn, [stream_url("ok") for _ in range(20)])
    settings = probe_mod.ProbeSettings(
        enabled=True,
        ffprobe_path=probe_settings.ffprobe_path,
        extra_argv=probe_settings.extra_argv,
        timeout_seconds=30.0,
        analyze_seconds=1.0,
        max_concurrency=4,
    )
    spy = RunProcessSpy(monkeypatch)
    summary = probe_mod.run_round(conn, settings=settings, now=NOW)

    assert summary["requested"] == 20
    assert (summary["succeeded"], summary["failed"], summary["written"]) == (20, 0, 20)
    assert spy.probe_calls == 20
    assert spy.version_calls == 1
    assert spy.max_concurrent <= 4          # 真的被 max_concurrency 限制住了
    assert spy.max_concurrent >= 2          # 而且确实是并发跑的
    assert all(runtime_mod._pid_alive(pid) is False for pid in spy.pids)

    counted = conn.execute(
        "SELECT stream_id, checked_at, probe_id, COUNT(*) AS c FROM probe_result "
        "GROUP BY stream_id, checked_at, probe_id"
    ).fetchall()
    assert len(counted) == 20
    assert all(int(item["c"]) == 1 for item in counted)     # 每 stream 每轮最多 1 条
    assert sorted(int(item["stream_id"]) for item in counted) == sorted(ids)

    # 第二轮用不同时间戳：历史只追加，不覆盖
    later = "2026-10-01T12:00:00+00:00"
    probe_mod.run_round(conn, settings=settings, now=later)
    assert len(repo.list_probe_results(conn)) == 40


def test_stop_request_stops_new_probes_and_reaps_children(conn, probe_settings, monkeypatch):
    """§13.7：收到停止请求后不再启动新 probe，并终止在跑的子进程（无孤儿）。"""
    build_inventory(conn, [stream_url("sleep") for _ in range(8)])
    settings = probe_mod.ProbeSettings(
        enabled=True,
        ffprobe_path=probe_settings.ffprobe_path,
        extra_argv=probe_settings.extra_argv,
        timeout_seconds=30.0,
        analyze_seconds=1.0,
        max_concurrency=4,
    )
    spy = RunProcessSpy(monkeypatch)
    deadline = time.perf_counter() + 1.0
    started = time.perf_counter()
    summary = probe_mod.run_round(
        conn, settings=settings, now=NOW, should_stop=lambda: time.perf_counter() > deadline
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 20.0                       # 没有被 8 条 60s 的假流拖住
    assert spy.probe_calls < 8                  # 不再启动新探测
    assert summary["cancelled"] + summary["skipped"] >= 1
    assert all(runtime_mod._pid_alive(pid) is False for pid in spy.pids)   # 无孤儿
    assert len(repo.list_probe_results(conn)) == 0   # 被取消的探测绝不写库


# --------------------------------------------------- 8. 目标筛选（只测固定库存）

def test_probe_targets_exclude_disabled_stale_orphan_and_dynamic(conn, probe_settings):
    """§13.9：disabled / 失活来源 / stale / orphan / dynamic_event_m3u 一律不测。

    四条排除条件分别对应 :func:`repo.list_probe_candidates` 里的
    ``enabled = 1`` / ``EXISTS(sc.active = 1)`` / ``status <> 'stale'`` / ``kind <> ?``。
    """
    normal_id = build_inventory(conn, [stream_url("ok")])[0]

    # ① disabled：stream 自己被停用
    disabled_id = build_inventory(conn, [stream_url("ok")], source_name="src-disabled")[0]
    conn.execute("UPDATE stream SET enabled = 0 WHERE id = ?", (disabled_id,))

    # ② 失活来源：来源的条目全部 active = 0（来源消失）。
    #    注意 sync_streams 只在**链接被清理**时才把 stream 置 stale；单纯失活会保留历史链接，
    #    所以 status 仍是 observed —— 正是 EXISTS(sc.active = 1) 这一条把它挡住。
    inactive_id = build_inventory(conn, [stream_url("ok")], source_name="src-inactive")[0]
    inactive_source = repo.get_source_by_name(conn, "src-inactive")
    conn.execute("UPDATE source_channel SET active = 0 WHERE source_id = ?",
                 (int(inactive_source["id"]),))
    repo.sync_streams(conn, now=NOW)
    assert conn.execute("SELECT status FROM stream WHERE id = ?",
                        (inactive_id,)).fetchone()["status"] == "observed"

    # 先建完所有库内实体（每次 build_inventory 都会 sync），最后再人为改状态，
    # 避免后续的 sync 把手工构造的状态又改回去。
    stale_id = build_inventory(conn, [stream_url("ok")], source_name="src-stale")[0]
    orphan_id = build_inventory(conn, [stream_url("ok")], source_name="src-orphan")[0]
    # ③ 动态赛事来源：即便有人误把它登记成可绑定的来源并归集出 stream，也绝不测
    dynamic_id = build_inventory(conn, [stream_url("ok")], source_name="src-dynamic",
                                kind=ingest_mod.KIND_DYNAMIC)[0]

    # ③ stale：没有任何来源链接，且已被 sync_streams 标成 stale
    conn.execute("DELETE FROM stream_source WHERE stream_id = ?", (stale_id,))
    conn.execute("UPDATE stream SET status = 'stale' WHERE id = ?", (stale_id,))
    # ④ orphan：没有任何来源链接，但状态被改回 observed（单靠 status 挡不住）
    conn.execute("DELETE FROM stream_source WHERE stream_id = ?", (orphan_id,))
    conn.execute("UPDATE stream SET status = 'observed' WHERE id = ?", (orphan_id,))
    conn.commit()

    # 动态来源那条确实**存在**于 stream 表里（否则下面的断言就没有意义）
    assert conn.execute("SELECT COUNT(*) AS c FROM stream WHERE id = ?",
                        (dynamic_id,)).fetchone()["c"] == 1

    candidates = [int(row["id"]) for row in repo.list_probe_candidates(conn)]
    assert candidates == [normal_id]
    for excluded in (disabled_id, inactive_id, stale_id, orphan_id, dynamic_id):
        assert excluded not in candidates

    summary = probe_mod.run_round(conn, settings=probe_settings, now=NOW)
    assert summary["requested"] == 1
    assert summary["written"] == 1
    assert {int(row["stream_id"]) for row in repo.list_probe_results(conn)} == {normal_id}


def test_limit_and_stream_id_selection(conn, probe_settings):
    ids = build_inventory(conn, [stream_url("ok"), stream_url("ok"), stream_url("ok")])
    limited = probe_mod.run_round(conn, settings=probe_settings, now=NOW, limit=2)
    assert (limited["requested"], limited["written"]) == (2, 2)

    single = probe_mod.run_round(conn, settings=probe_settings, now=NOW, stream_id=ids[2])
    assert (single["requested"], single["written"]) == (1, 1)
    assert rows_for(conn, ids[2]) != []

    # 非库存/不存在的 stream 不会被探测，也不会写伪结果
    nothing = probe_mod.run_round(conn, settings=probe_settings, now=NOW, stream_id=999_999)
    assert (nothing["requested"], nothing["written"]) == (0, 0)


def test_dry_run_writes_nothing(conn, probe_settings):
    build_inventory(conn, [stream_url("ok")])
    summary = probe_mod.run_round(conn, settings=probe_settings, now=NOW, dry_run=True)
    assert summary["succeeded"] == 1
    assert summary["written"] == 0
    assert repo.list_probe_results(conn) == []
    assert conn.execute("SELECT last_seen_at FROM probe WHERE name = ?",
                        (probe_settings.name,)).fetchone()["last_seen_at"] is None


# --------------------------------------------------- 9. 脱敏 / 状态边界

def test_status_summary_and_cli_output_never_leak_tokens(conn, probe_settings, capsys, tmp_path):
    """§13.8 + §13.11：URL 只显示 scheme://host/...，query token 不出现。"""
    build_inventory(conn, [stream_url("ok"), stream_url("dns")])
    summary = probe_mod.run_round(conn, settings=probe_settings, now=NOW)

    compact = probe_mod.summarize_for_status(summary)
    blob = json.dumps(compact, ensure_ascii=False)
    assert TOKEN not in blob
    assert "http" not in blob
    assert compact["stage"] == probe_mod.STAGE_DEGRADED
    assert compact["failed_stream_ids"]                  # 便于排查，且 id 本身不敏感

    for item in summary["results"]:
        assert TOKEN not in json.dumps(item, ensure_ascii=False)
        if item["url"]:
            assert item["url"] == "http://media.example/..."

    # 运行期状态文件里同样不得出现 token / URL
    cfg = write_config(tmp_path)
    code, out = run_cli(capsys, "probe-run", "--config", cfg, "--json")
    assert code == probe_mod.EXIT_OK, out
    assert TOKEN not in out
    assert "/live/" not in out                       # path 也被折叠掉了


def test_stderr_echo_of_url_is_redacted(probe_settings):
    """stderr 回显完整 URL 时，诊断信息里只能看到脱敏后的地址。"""
    url = stream_url("echo", token=TOKEN)
    observation = probe_mod.probe_stream(url, stream_id=7, settings=probe_settings)
    assert observation.success is False
    assert observation.error_type == probe_mod.ERROR_INVALID_MEDIA
    assert TOKEN not in (observation.message or "")
    assert url not in (observation.message or "")
    assert observation.redacted_url == "http://media.example/..."


# --------------------------------------------------- 10. scheduler 集成

def test_disabled_probe_runs_zero_subprocesses(conn, tmp_path, monkeypatch):
    """§13.10：probe.enabled=false 时 0 次 ffprobe，行为与 TASK-004 一致。"""
    from tests.test_select import add_probe

    (stream_id,) = build_inventory(conn, [stream_url("ok")])
    add_probe(conn, stream_id, when=NOW, ok=True, startup_ms=500)
    conn.commit()
    spy = RunProcessSpy(monkeypatch)

    result = runtime_mod.execute_round(
        conn,
        output_path=tmp_path / "live.m3u",
        group_order=["新闻", "其他"],
        selection_kwargs={"now": NOW},
        summary_path=tmp_path / "publish-summary.json",
        fetch_fixed=False,
        stamp=NOW,
        probe_settings=probe_mod.ProbeSettings(enabled=False),
    )
    assert spy.probe_calls == 0 and spy.version_calls == 0
    assert result["probe"] == {
        "enabled": False, "stage": probe_mod.STAGE_DISABLED,
        "probe_name": probe_mod.ProbeSettings().name, "probe_id": None,
        "dry_run": False, "requested": 0, "succeeded": 0, "failed": 0,
        "written": 0, "cancelled": 0, "skipped": 0, "error_type": None,
        "error_counts": {}, "failed_stream_ids": [],
    }
    assert result["outcome"] == "ok"
    assert result["published"] is True
    assert len(repo.list_probe_results(conn)) == 1        # 旧历史仍在用


def test_execute_round_order_is_fetch_sync_probe_publish(conn, tmp_path, monkeypatch, probe_settings):
    """§13.10：顺序可机验证 —— fetch → stream-sync → probe → publish。"""
    events: list[str] = []

    # 先把库内实体准备好（建库自己也会调用 sync_streams，不能算进这一轮的顺序）。
    # 只有**一个** enabled 的 fixed 来源，这样 fetch 恰好一次。
    repo.add_source(conn, "order-src", ingest_mod.KIND_FIXED,
                    url="http://127.0.0.1:1/x.m3u", now=NOW)
    build_inventory(conn, [stream_url("ok")], source_name="order-src")
    conn.commit()
    assert len(repo.list_sources_by_kind(conn, ingest_mod.KIND_FIXED, enabled_only=True)) == 1

    original_ingest = ingest_mod.ingest_fixed_source
    original_sync = repo.sync_streams
    original_probe = probe_mod.run_round
    original_publish = publish_mod.publish

    def spy_ingest(*args, **kwargs):
        events.append("fetch")
        return {"source_id": source_id, "source_name": "order-src", "ok": True,
                "status": "OK", "entries": 0, "created": 0, "updated": 0,
                "deactivated": 0, "reactivated": 0, "duration_ms": 1}

    def spy_sync(*args, **kwargs):
        events.append("sync")
        return original_sync(*args, **kwargs)

    def spy_probe(*args, **kwargs):
        events.append("probe")
        return original_probe(*args, **kwargs)

    def spy_publish(*args, **kwargs):
        events.append("publish")
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(runtime_mod.ingest_mod, "ingest_fixed_source", spy_ingest)
    monkeypatch.setattr(runtime_mod.repo, "sync_streams", spy_sync)
    monkeypatch.setattr(runtime_mod.probe_mod, "run_round", spy_probe)
    monkeypatch.setattr(runtime_mod.publish_mod, "publish", spy_publish)
    events.clear()

    result = runtime_mod.execute_round(
        conn,
        output_path=tmp_path / "live.m3u",
        group_order=["新闻", "其他"],
        selection_kwargs={"now": NOW},
        summary_path=tmp_path / "publish-summary.json",
        stamp=NOW,
        probe_settings=probe_settings,
        fetch_fixed=True,
    )
    assert events == ["fetch", "sync", "probe", "publish"]
    assert result["probe"]["written"] == 1
    assert result["probe_stage"] == probe_mod.STAGE_OK

    # 对照：probe.enabled = false 时顺序里**没有** probe，且一次 ffprobe 都没有
    events.clear()
    disabled = runtime_mod.execute_round(
        conn,
        output_path=tmp_path / "live.m3u",
        group_order=["新闻", "其他"],
        selection_kwargs={"now": NOW},
        summary_path=tmp_path / "publish-summary.json",
        stamp=NOW,
        probe_settings=probe_mod.ProbeSettings(enabled=False),
        fetch_fixed=True,
    )
    assert events == ["fetch", "sync", "publish"]
    assert disabled["probe_stage"] == probe_mod.STAGE_DISABLED


def test_same_round_probe_results_drive_selection_and_publish(conn, tmp_path, probe_settings):
    """§13.10：本轮 probe 结果必须能影响**同轮** selector（否则频道会被跳过）。"""
    source_id = repo.add_source(conn, "same-round", ingest_mod.KIND_FIXED, now=NOW)
    canonical_id = repo.add_canonical_channel(conn, "同轮台", category="新闻", now=NOW)
    for index, mode in enumerate(("ok", "dns")):
        entry = SimpleNamespace(name=f"raw-{index}", url=stream_url(mode), tvg_id=None,
                                tvg_logo=None, group_title="新闻")
        channel_id, _ = repo.upsert_source_channel(conn, source_id, entry, now=NOW)
        repo.bind_source_channel(conn, channel_id, canonical_id, now=NOW)
    repo.sync_streams(conn, now=NOW)
    conn.commit()

    # 没有任何历史 probe_result 时：频道被跳过（不生成空列表冒充成功）
    without_probe = runtime_mod.execute_round(
        conn,
        output_path=tmp_path / "live.m3u",
        group_order=["新闻", "其他"],
        selection_kwargs={"now": NOW},
        summary_path=tmp_path / "publish-summary.json",
        fetch_fixed=False,
        stamp=NOW,
        probe_settings=probe_mod.ProbeSettings(enabled=False),
    )
    assert without_probe["publish"]["fixed_count"] == 0
    assert without_probe["publish"]["status"] == publish_mod.STATUS_REJECTED_VALIDATION

    # 同轮真实测活后：健康线路立即可用
    with_probe = runtime_mod.execute_round(
        conn,
        output_path=tmp_path / "live.m3u",
        group_order=["新闻", "其他"],
        selection_kwargs={"now": NOW},
        summary_path=tmp_path / "publish-summary.json",
        fetch_fixed=False,
        stamp=NOW,
        probe_settings=probe_settings,
    )
    assert with_probe["probe"]["stage"] == probe_mod.STAGE_DEGRADED
    assert with_probe["probe"]["written"] == 2
    assert with_probe["published"] is True
    text = (tmp_path / "live.m3u").read_text(encoding="utf-8")
    assert stream_url("ok") in text
    assert stream_url("dns") not in text


def test_next_round_reversal_flows_through_existing_selector(conn, probe_settings):
    """§10：结果反转后，既有 7 天窗口 / 连续失败规则产生预期变化（不改算法）。"""
    source_id = repo.add_source(conn, "reversal", ingest_mod.KIND_FIXED, now=NOW)
    canonical_id = repo.add_canonical_channel(conn, "反转台", category="新闻", now=NOW)
    # 同一 canonical 下必须有**两条 URL 不同**的 stream（同名同 URL 会被去重成一条）
    for index in ("a", "b"):
        entry = SimpleNamespace(name=f"raw-{index}", url=stream_url("ok", tag=index),
                                tvg_id=None, tvg_logo=None, group_title="新闻")
        channel_id, _ = repo.upsert_source_channel(conn, source_id, entry, now=NOW)
        repo.bind_source_channel(conn, channel_id, canonical_id, now=NOW)
    repo.sync_streams(conn, now=NOW)
    conn.commit()
    stream_a, stream_b = [int(row["id"]) for row in repo.list_streams(conn, canonical_id)]

    first = probe_mod.run_round(conn, settings=probe_settings, now=NOW)
    assert first["succeeded"] == 2
    first_scores = select_mod.score_streams(conn, canonical_id, now=NOW)
    assert all(score.eligible for score in first_scores)
    first_best = select_mod.select_best_stream(conn, canonical_id, now=NOW)
    # 两条都健康时谁赢取决于实测 startup_ms（TASK-001 的既有规则），不作硬编码断言
    assert first_best is not None and first_best.stream_id in (stream_a, stream_b)

    # 人为让 A 连续失败到阈值，B 保持健康 —— 只用**既有** repo API 写历史
    for index, when in enumerate((
        "2026-09-30T13:00:00+00:00",
        "2026-09-30T14:00:00+00:00",
        "2026-09-30T15:00:00+00:00",
    )):
        repo.add_probe_result(
            conn, stream_id=stream_a, probe_id=repo.ensure_probe(conn, probe_settings.name),
            success=False, checked_at=when, error_type=probe_mod.ERROR_CONNECT,
        )
    repo.add_probe_result(
        conn, stream_id=stream_b, probe_id=repo.ensure_probe(conn, probe_settings.name),
        success=True, checked_at="2026-09-30T15:00:00+00:00", startup_ms=700,
    )
    conn.commit()

    second_best = select_mod.select_best_stream(conn, canonical_id, now=NOW)
    assert second_best is not None and second_best.stream_id == stream_b
    scores = {item.stream_id: item for item in select_mod.score_streams(
        conn, canonical_id, now=NOW)}
    assert scores[stream_a].consecutive_failures == 3
    assert scores[stream_a].eligible is False
    assert "连续失败" in (scores[stream_a].reason or "")


# --------------------------------------------------- 11. CLI 端到端（离线）

def test_cli_probe_run_end_to_end(capsys, tmp_path):
    """probe-check + probe-run 走真实 CLI（配置用数组形式指向离线替身）。"""
    cfg = write_config(tmp_path)
    code, out = run_cli(capsys, "init-db", "--config", cfg, "--json")
    assert code == 0, out

    db_file = tmp_path / "liptv.sqlite3"
    connection = db_mod.connect(db_file)
    build_inventory(connection, [stream_url("ok"), stream_url("dns")])
    connection.close()

    code, out = run_cli(capsys, "probe-run", "--config", cfg, "--json",
                        "--now", NOW)
    assert code == probe_mod.EXIT_OK, out
    payload = json.loads(out)
    assert payload["stage"] == probe_mod.STAGE_DEGRADED
    assert (payload["requested"], payload["succeeded"], payload["failed"]) == (2, 1, 1)
    assert payload["written"] == 2
    assert payload["error_counts"] == {probe_mod.ERROR_DNS: 1}
    ids = [item["stream_id"] for item in payload["results"]]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert TOKEN not in out

    code, out = run_cli(capsys, "probe-result-list", "--config", cfg, "--json")
    assert code == 0, out
    rows = json.loads(out)
    assert len(rows) == 2
    assert {row["error_type"] for row in rows} == {None, probe_mod.ERROR_DNS}

    # --dry-run 不写库
    code, out = run_cli(capsys, "probe-run", "--config", cfg, "--dry-run", "--json")
    assert code == probe_mod.EXIT_OK, out
    assert json.loads(out)["written"] == 0

    # probe.enabled = false：显式命令也不会跑（安全默认）
    disabled_cfg = write_config(tmp_path, enabled=False, name="disabled.toml")
    code, out = run_cli(capsys, "probe-run", "--config", disabled_cfg, "--json")
    assert code == probe_mod.EXIT_OK, out
    payload = json.loads(out)
    assert payload["stage"] == probe_mod.STAGE_DISABLED
    assert payload["written"] == 0


def test_cli_run_once_with_probe_enabled_writes_status_without_urls(capsys, tmp_path):
    """`run --once` 在 probe.enabled=true 下也能跑通，且状态文件不含 URL/token。"""
    cfg = write_config(tmp_path)
    code, out = run_cli(capsys, "init-db", "--config", cfg, "--json")
    assert code == 0, out
    connection = db_mod.connect(tmp_path / "liptv.sqlite3")
    build_inventory(connection, [stream_url("ok")])
    connection.close()

    status_path = tmp_path / "runtime-status.json"
    lock_path = tmp_path / "liptv.lock"
    with cfg.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n[runtime]\n"
            f'lock_path = "{lock_path.as_posix()}"\n'
            f'status_path = "{status_path.as_posix()}"\n'
            "interval_seconds = 60\n"
            "stale_after_seconds = 600\n"
        )

    code, out = run_cli(capsys, "run", "--once", "--config", cfg, "--json", "--now", NOW)
    assert code == 0, out
    payload = json.loads(out)
    assert payload["probe_enabled"] is True
    assert payload["round"]["probe_stage"] == probe_mod.STAGE_OK
    assert payload["round"]["probe"]["written"] == 1

    blob = status_path.read_text(encoding="utf-8")
    assert TOKEN not in blob
    assert "http://" not in blob and "https://" not in blob
    assert json.loads(blob)["current_round"]["probe"]["stage"] == probe_mod.STAGE_OK
    assert not lock_path.exists()


def test_fake_ffprobe_state_file_models_same_url_changing_health(tmp_path):
    """fake ffprobe 的「按 URL 覆盖模式」：同一个 URL 可以这轮好、下轮坏。

    ``FAKE_FFPROBE_STATE`` 的用途是模拟「URL 不变、健康反转」——真实 ffprobe 正是如此。
    它是演示脚本 tools/demo_probe_pipeline.py §6 的接缝，优先级高于
    ``FAKE_FFPROBE_MODE``（全局强制）与 URL 推断。
    """
    url = "http://media.example/live/line-a/pc.m3u8?token=SECRET"
    state = tmp_path / "state.json"

    def invoke(env_extra: dict[str, str]) -> tuple[int, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("FAKE_FFPROBE_MODE", "FAKE_FFPROBE_STATE")
        }
        env.update(env_extra)
        proc = subprocess.run(
            [sys.executable, str(FAKE_FFPROBE), "-hide_banner", "-v", "error", url],
            capture_output=True, text=True, env=env, check=False,
        )
        return proc.returncode, proc.stdout + proc.stderr

    # 没有状态文件时：URL 里没有模式标记 → 默认成功
    code, _text = invoke({})
    assert code == 0

    # 状态文件把**这一个 URL** 定向成 dns
    state.write_text(json.dumps({url: "dns"}), encoding="utf-8")
    code, text = invoke({"FAKE_FFPROBE_STATE": str(state)})
    assert code == 1 and "resolve hostname" in text

    # 状态文件优先于 FAKE_FFPROBE_MODE 的全局强制
    code, _text = invoke({"FAKE_FFPROBE_STATE": str(state), "FAKE_FFPROBE_MODE": "ok"})
    assert code == 1

    # 改成 ok 即恢复（下一轮又好了）
    state.write_text(json.dumps({url: "ok"}), encoding="utf-8")
    code, text = invoke({"FAKE_FFPROBE_STATE": str(state)})
    assert code == 0 and '"streams"' in text

    # 键也可以写「末段去扩展名的文件名」
    state.write_text(json.dumps({"pc": "tls"}), encoding="utf-8")
    code, text = invoke({"FAKE_FFPROBE_STATE": str(state)})
    assert code == 1 and "TLS" in text

    # 状态文件损坏 / 值非法时一律回落到 URL 推断（不能把测试搞崩）
    state.write_text("{ not json", encoding="utf-8")
    code, _text = invoke({"FAKE_FFPROBE_STATE": str(state)})
    assert code == 0

    state.write_text(json.dumps({url: "not-a-mode"}), encoding="utf-8")
    code, _text = invoke({"FAKE_FFPROBE_STATE": str(state)})
    assert code == 0


def test_argv_uses_only_real_ffprobe_options(probe_settings):
    """🚨 TASK-009 真机发现：argv 里**绝不能**出现 ffprobe 不存在的选项。

    生产机 ffprobe 6.1.1 实测：``-nostdin``（那是 ffmpeg 的选项）会让 ffprobe
    报 ``Failed to set value '-v' for option 'nostdin': Option not found`` 并非 0 退出。
    后果是**每一条** stream 都被归成 ``HTTP_ERROR`` ——
    看起来像「所有流都播不了」，实际是 argv 本身就不合法。

    本机 PATH 没有 ffprobe，fake 替身又不校验选项合法性，所以离线永远发现不了；
    这条测试把「ffprobe 真实存在的选项」固化成清单。
    """
    argv = probe_mod.build_argv(probe_settings, "http://media.example/live/a.m3u8")
    # ffprobe（6.x）真实支持的这些开关；-nostdin 不在其中
    real_options = {
        "-hide_banner", "-v", "-print_format", "-show_format", "-show_streams",
        "-analyzeduration", "-probesize", "-i", "-show_entries", "-of",
    }
    for item in argv[:-1]:
        if item.startswith("-"):
            assert item in real_options, f"ffprobe 不支持该选项：{item}"
    assert "-nostdin" not in argv
    # URL 仍然是最后一个参数、且只出现一次（隔离性不能因为改选项而破坏）
    assert argv[-1] == "http://media.example/live/a.m3u8"
    assert argv.count(argv[-1]) == 1


def test_probe_stream_survives_strict_ffprobe_option_check(probe_settings, monkeypatch):
    """动态加固：让替身按 ffprobe 的真实行为**拒绝未知选项**，再跑一次探测。

    上一条是清单比对，这条是行为验证：若有人把 ``-nostdin`` 加回来，
    替身会像真 ffprobe 一样报 ``Option not found`` 并非 0 退出，
    于是 :func:`probe_stream` 返回失败 observation 而**不是**成功。
    """
    real_options = {
        "-hide_banner", "-v", "-print_format", "-show_format", "-show_streams",
        "-analyzeduration", "-probesize", "-i", "-show_entries", "-of",
    }
    seen: list[list[str]] = []

    class FakeProc:
        def __init__(self, returncode, stdout, stderr):
            self.returncode = returncode
            # ProcessRegistry.add(proc) 会登记 pid（停止请求时按 pid 终止）
            self.pid = 424242
            self._stdout = stdout.encode("utf-8")
            self._stderr = stderr.encode("utf-8")
            self.stdout = io.BytesIO(self._stdout)
            self.stderr = io.BytesIO(self._stderr)

        def communicate(self, timeout=None):
            return self._stdout, self._stderr

        def kill(self):
            pass

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            pass

    def fake_popen(argv, **kwargs):
        seen.append(list(argv))
        unknown = [a for a in argv[1:] if a.startswith("-") and a not in real_options]
        if unknown:
            return FakeProc(1, "", f"Failed to set value for option '{unknown[0]}': Option not found")
        return FakeProc(0, json.dumps({
            "format": {"protocol": "http", "bit_rate": "1500000"},
            "streams": [{"codec_type": "video", "width": 1280, "height": 720}],
        }), "")

    monkeypatch.setattr(probe_mod.subprocess, "Popen", fake_popen)
    observation = probe_mod.probe_stream(
        "http://media.example/live/a.m3u8",
        stream_id=1, settings=probe_settings,
    )
    assert seen, "替身没被调用，测试无效"
    assert observation.error_type is None, (
        f"严格替身拒绝了 argv：{observation.error_type} / {observation.message}"
    )
    assert observation.resolution_width == 1280
    assert observation.resolution_height == 720
