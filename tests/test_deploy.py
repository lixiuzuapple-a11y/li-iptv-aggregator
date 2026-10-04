"""TASK-006 部署内核的离线回归测试。

全部离线：临时 ``--root`` 前缀 + ``tools/fake_systemctl.py`` 替身，
**绝不**碰真实 ``/etc/systemd/system``、不托管真实服务、不访问公网。

本文件里有两处**针对本轮真实缺陷**的永久回归（都在 demo 里第一次暴露）：

1. :func:`test_render_unit_drops_template_comment` —— 渲染 unit 时不能用子串
   ``index("[Unit]")`` 定位小节头（模板注释里也写着 "[Unit]"，会把注释切一半留下，
   顺带把未替换的 ``@@`` 带进生产 unit）。
2. :func:`test_upgrade_starts_the_new_release` —— ``upgrade()`` 起新版本时必须换用
   「新 release」的服务管理器；沿用停旧服务时的那个，会把**旧版本**当新版本起起来，
   健康闸门就永远验不到新 release（"注入坏 release" 场景会静默通过）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import re
import socket
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liptv import backup as backup_mod  # noqa: E402
from liptv import cli as cli_mod  # noqa: E402
from liptv import deploy as deploy_mod  # noqa: E402
from liptv import publish as publish_mod  # noqa: E402
from liptv import runtime as runtime_mod  # noqa: E402

FAKE_SYSTEMCTL = REPO_ROOT / "tools" / "fake_systemctl.py"

_UNIT_TEMPLATE = (REPO_ROOT / "deploy" / "systemd" / "li-iptv.service.in").read_text(
    encoding="utf-8"
)
_CONFIG_TEMPLATE = (REPO_ROOT / "deploy" / "config.production.example.toml").read_text(
    encoding="utf-8"
)


def rendered_config_base() -> str:
    """把配置模板里的路径占位符替换成绝对路径（模拟安装器渲染后的形态）。"""
    text = _CONFIG_TEMPLATE
    for key in ("@@DB_PATH@@", "@@OUTPUT_PATH@@", "@@SUMMARY_PATH@@",
                "@@STATUS_PATH@@", "@@LOCK_PATH@@", "@@DYNAMIC_TMP_DIR@@"):
        text = text.replace(key, "/var/lib/li-iptv-aggregator/x")
    return text


# ==================================================================== fixtures

@pytest.fixture()
def prefix(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "root"


@pytest.fixture()
def layout(prefix: pathlib.Path) -> deploy_mod.Layout:
    return deploy_mod.build_layout(prefix, user="liptv", group="liptv")


@pytest.fixture()
def fake_systemctl(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """把 fake systemctl 的日志/状态文件放到临时目录，并注入环境变量。"""
    log = tmp_path / "fake-systemctl.log"
    state = tmp_path / "fake-systemctl-state.json"
    state.write_text(json.dumps({"active": "inactive", "enabled": "disabled"}),
                     encoding="utf-8", newline="\n")
    monkeypatch.setenv("FAKE_SYSTEMCTL_LOG", str(log))
    monkeypatch.setenv("FAKE_SYSTEMCTL_STATE", str(state))

    def actions() -> list[str]:
        if not log.is_file():
            return []
        return [json.loads(line)["action"] for line in log.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    return {"log": log, "state": state, "actions": actions}


def make_options(layout: deploy_mod.Layout, **overrides) -> deploy_mod.DeployOptions:
    base = {
        "layout": layout,
        "source_dir": REPO_ROOT,
        "python": sys.executable,
        "method": "copy",
        "no_venv": True,
        "service_manager": "none",
    }
    base.update(overrides)
    return deploy_mod.DeployOptions(**base)


def install(layout: deploy_mod.Layout, **overrides) -> dict:
    return deploy_mod.Deployer(make_options(layout, **overrides)).install()


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_runtime_config(layout: deploy_mod.Layout, *, port: int) -> None:
    """写一份可直接被 ``run --serve`` 使用的运行配置（run_on_start=false）。"""
    layout.config_path.write_text(
        "[database]\n"
        f'path = "{layout.db_path.as_posix()}"\n'
        "\n[output]\n"
        f'm3u_path = "{layout.output_path.as_posix()}"\n'
        "keep_previous = true\n"
        "\n[fetch]\n"
        "timeout_seconds = 5.0\n"
        "max_bytes = 2000000\n"
        "max_redirects = 3\n"
        f'dynamic_tmp_dir = "{layout.dynamic_tmp_dir.as_posix()}"\n'
        "\n[publish]\n"
        f'summary_path = "{layout.summary_path.as_posix()}"\n'
        "dynamic_sources = []\n"
        "\n[runtime]\n"
        "interval_seconds = 3600\n"
        "run_on_start = false\n"
        f'lock_path = "{layout.lock_path.as_posix()}"\n'
        f'status_path = "{layout.status_path.as_posix()}"\n'
        "stale_after_seconds = 21600\n"
        "status_history = 5\n"
        "include_dynamic = false\n"
        "dynamic_sources = []\n"
        "require_dynamic = false\n"
        "\n[server]\n"
        "enabled = true\n"
        'host = "127.0.0.1"\n'
        f"port = {port}\n"
        'playlist_path = "/live.m3u"\n'
        'health_path = "/healthz"\n'
        "\n[probe]\n"
        "enabled = false\n"
        'ffprobe_path = "/usr/bin/ffprobe"\n',
        encoding="utf-8", newline="\n",
    )


# ================================================================== 布局模型

def test_layout_matches_frozen_directory_model(layout: deploy_mod.Layout) -> None:
    """TASK-006 §2 的目录模型：/opt（代码）、/etc（配置）、/var/lib（持久）、
    /var/cache（可丢弃）、/run（短生命周期）。"""
    assert layout.app_dir.parts[-2:] == ("opt", "li-iptv-aggregator")
    assert layout.etc_dir.parts[-2:] == ("etc", "li-iptv-aggregator")
    assert layout.lib_dir.parts[-3:] == ("var", "lib", "li-iptv-aggregator")
    assert layout.cache_dir.parts[-3:] == ("var", "cache", "li-iptv-aggregator")
    assert layout.run_dir.parts[-2:] == ("run", "li-iptv-aggregator")
    assert layout.unit_path.parts[-4:] == ("etc", "systemd", "system", "li-iptv.service")
    assert layout.unit_path.parent.parts[-3:] == ("etc", "systemd", "system")
    assert layout.releases_dir == layout.app_dir / "releases"
    assert layout.backups_dir == layout.lib_dir / "backups"
    assert layout.venv_dir == layout.app_dir / "venv"


def test_default_root_is_posix_only() -> None:
    """非 POSIX 本机必须显式 --root，绝不默认写 ``/``（保护开发机）。"""
    import os

    if os.name == "posix":
        assert deploy_mod.default_root() == pathlib.Path("/")
    else:
        with pytest.raises(deploy_mod.DeployError):
            deploy_mod.default_root()


def test_production_permissions_are_least_privilege(layout: deploy_mod.Layout) -> None:
    table = {row["path"]: row for row in layout.mode_table("v1")}
    assert table[str(layout.backups_dir)]["mode"] == 0o700
    assert table[str(layout.lib_dir)]["mode"] == 0o750
    assert table[str(layout.etc_dir)]["mode"] == 0o750
    assert table[str(layout.config_path)]["mode"] == 0o640
    assert table[str(layout.unit_path)]["owner"] == "root:root"


# ===================================================== QA-006A 生产属主模型

def test_production_ownership_matrix_is_frozen(layout: deploy_mod.Layout) -> None:
    """QA-006A 永久回归：三类属主逐项落在冻结矩阵上。

    低权限服务账号（liptv）**不得**拥有应用代码、venv、release、生产配置或 release 指针；
    它只拥有数据 / 缓存 / 运行期目录。
    """
    table = {row["path"]: row for row in layout.mode_table("v1")}
    root_owned = {
        layout.app_dir, layout.releases_dir, layout.release_dir("v1"),
        layout.venv_dir, layout.current_pointer, layout.state_path, layout.unit_path,
    }
    config_owned = {layout.etc_dir, layout.config_path}
    data_owned = {
        layout.lib_dir, layout.db_path, layout.backups_dir, layout.cache_dir,
        layout.dynamic_tmp_dir, layout.run_dir,
    }

    for path in root_owned:
        assert table[str(path)]["owner"] == "root:root", f"{path} 必须是 root:root"
    for path in config_owned:
        assert table[str(path)]["owner"] == f"root:{layout.group}", \
            f"{path} 必须是 root:{layout.group}（服务用户组可读、不可写）"
    for path in data_owned:
        assert table[str(path)]["owner"] == f"{layout.user}:{layout.group}", \
            f"{path} 必须是 {layout.user}:{layout.group}"

    # 反向断言：没有任何代码/配置路径落进服务用户手里
    service_owner = f"{layout.user}:{layout.group}"
    for path in (layout.app_dir, layout.releases_dir, layout.release_dir("v1"),
                 layout.venv_dir, layout.etc_dir, layout.config_path,
                 layout.current_pointer, layout.state_path, layout.unit_path):
        assert table[str(path)]["owner"] != service_owner, \
            f"{path} 不能属于服务用户（模型被破坏）"


def test_create_layout_dirs_agrees_with_mode_table(layout: deploy_mod.Layout) -> None:
    """QA-006A：``_create_layout_dirs()`` 的 (mode, owner) 意图必须与 ``mode_table()`` 一致。

    这两处曾经一个说 liptv、一个说 root，报告与实现互相矛盾 —— 本用例把它们钉死。
    """
    payload = deploy_mod.Deployer(
        make_options(layout, dry_run=True, release_id="v1")
    ).install()
    planned = {a["path"]: a for a in payload["actions"] if a["kind"] == "mkdir"}
    assert planned, "plan 应当记录目录创建意图"
    table = {row["path"]: row for row in layout.mode_table("v1") if row["kind"] == "dir"}
    for path, action in planned.items():
        assert path in table, f"{path} 不在 mode_table() 里：两处模型不一致"
        assert action["owner"] == table[path]["owner"], f"{path} 的 owner 意图不一致"
        assert action["mode"] == f"{table[path]['mode']:04o}", f"{path} 的 mode 意图不一致"


def test_apply_modes_chowns_exactly_what_mode_table_declares(
    layout: deploy_mod.Layout,
) -> None:
    """QA-006A：真实 install 时逐项 chown 的属主 = ``mode_table()`` 的声明（单一来源）。"""
    payload = install(layout, release_id="v1")
    assert payload["status"] == "OK"
    table = {row["path"]: row for row in layout.mode_table("v1")}
    chowns = {a["path"]: a for a in payload["actions"] if a["kind"] == "chown"}
    assert chowns, "install 必须记录 chown 意图"
    for path, action in chowns.items():
        assert path in table, f"{path} 被 chown 但不在 mode_table() 里"
        assert action["owner"] == table[path]["owner"], \
            f"{path} 的 chown 属主与 mode_table() 不一致（{action['owner']} vs {table[path]['owner']}）"
    # 关键路径必须真的出现在 chown 列表里（而不是被静默跳过）
    for path in (layout.app_dir, layout.releases_dir, layout.etc_dir, layout.lib_dir,
                 layout.config_path, layout.current_pointer, layout.state_path):
        assert str(path) in chowns, f"{path} 没有被应用属主"


# =================================================================== plan

def test_plan_writes_nothing_and_does_not_call_systemd(
    prefix: pathlib.Path, layout: deploy_mod.Layout, fake_systemctl: dict
) -> None:
    payload = deploy_mod.Deployer(make_options(
        layout, dry_run=True, release_id="plan", service_manager="systemd",
        systemctl=str(FAKE_SYSTEMCTL),
    )).install()

    assert payload["status"] == "PLAN"
    assert payload["exit_code"] == 0
    assert payload["changed"] == []
    assert not prefix.exists(), "plan 阶段一个字节都不该写"
    assert fake_systemctl["actions"]() == [], "plan 不能执行任何 systemctl"
    assert all(step["status"] in {"planned", "ok", "skipped"} for step in payload["steps"])
    assert any(step["step"] == "config" and step["status"] == "planned"
               for step in payload["steps"])


# ================================================================== install

def test_install_creates_frozen_layout(
    prefix: pathlib.Path, layout: deploy_mod.Layout, fake_systemctl: dict
) -> None:
    payload = install(layout, release_id="v1", service_manager="systemd",
                      systemctl=str(FAKE_SYSTEMCTL))

    assert payload["status"] == "OK"
    assert payload["exit_code"] == 0
    assert layout.release_dir("v1").is_dir()
    assert layout.config_path.is_file()
    assert layout.db_path.is_file()
    assert layout.unit_path.is_file()
    assert layout.current_pointer.read_text(encoding="utf-8").strip() == "v1"
    for directory in (layout.app_dir, layout.etc_dir, layout.lib_dir, layout.cache_dir,
                      layout.run_dir):
        assert directory.is_dir(), directory
    actions = fake_systemctl["actions"]()
    assert "daemon-reload" in actions
    assert "enable" in actions


def test_install_is_idempotent_and_never_overwrites(
    layout: deploy_mod.Layout, fake_systemctl: dict
) -> None:
    first = install(layout, release_id="v1", service_manager="systemd",
                    systemctl=str(FAKE_SYSTEMCTL))
    assert any(step["step"] == "config" and step["status"] == "ok" for step in first["steps"])

    config_before = layout.config_path.read_bytes()
    db_before = layout.db_path.read_bytes()

    second = install(layout, release_id="v1", service_manager="systemd",
                     systemctl=str(FAKE_SYSTEMCTL))
    assert second["status"] == "OK"
    assert second["changed"] == [], "幂等 install 不得修改任何路径"
    config_step = next(s for s in second["steps"] if s["step"] == "config")
    assert config_step["status"] == "skipped"
    db_step = next(s for s in second["steps"] if s["step"] == "init-db")
    assert db_step["reason"].startswith("数据库已存在")
    assert layout.config_path.read_bytes() == config_before
    assert layout.db_path.read_bytes() == db_before


def test_install_never_overwrites_existing_production_config(
    layout: deploy_mod.Layout, fake_systemctl: dict
) -> None:
    layout.config_path.parent.mkdir(parents=True, exist_ok=True)
    custom = "# 运维手工改过的配置\n[database]\npath = \"/tmp/x.sqlite3\"\n"
    layout.config_path.write_text(custom, encoding="utf-8", newline="\n")

    install(layout, release_id="v1", service_manager="systemd", systemctl=str(FAKE_SYSTEMCTL))

    assert layout.config_path.read_text(encoding="utf-8") == custom, "生产配置必须永不覆盖"


def test_install_does_not_recreate_existing_database(
    layout: deploy_mod.Layout, fake_systemctl: dict
) -> None:
    layout.lib_dir.mkdir(parents=True, exist_ok=True)
    layout.db_path.write_bytes(b"this-is-not-a-real-db-but-must-not-be-touched")

    payload = install(layout, release_id="v1", service_manager="systemd",
                      systemctl=str(FAKE_SYSTEMCTL))

    step = next(s for s in payload["steps"] if s["step"] == "init-db")
    assert step["status"] == "skipped"
    assert layout.db_path.read_bytes() == b"this-is-not-a-real-db-but-must-not-be-touched"


# ====================================================================== unit

def test_render_unit_drops_template_comment(layout: deploy_mod.Layout) -> None:
    """回归（本轮真实缺陷）：模板注释里含 "[Unit]" 字样，子串查找会把注释切一半留下，
    并静默把未替换的 ``@@`` 带进生产 unit。"""
    text = deploy_mod.Deployer(make_options(layout)).render_unit(release_id="v1")

    body = [line for line in text.splitlines() if line.strip()]
    assert body[0] == "[Unit]", f"第一行应是 [Unit]，实际是 {body[0]!r}"
    assert "@@" not in text
    assert "给人看的模板说明" not in text
    assert "deploy 会静态校验" not in text
    # 真正的节标题一个不少、一个不多（说明文字不会被误判成节标题）
    sections = [line.strip() for line in body if re.fullmatch(r"\[[A-Za-z]+\]", line.strip())]
    assert sections == ["[Unit]", "[Service]", "[Install]"]
    assert text.count("[Unit]") == 1


def test_rendered_unit_passes_static_validation(layout: deploy_mod.Layout) -> None:
    text = deploy_mod.Deployer(make_options(layout)).render_unit(release_id="v1")
    problems = deploy_mod.validate_unit(text, required_writable_dirs=[
        layout.lib_dir, layout.cache_dir, layout.run_dir,
    ])
    assert problems == []


def test_rendered_unit_uses_posix_paths_even_on_windows(layout: deploy_mod.Layout) -> None:
    """TOML/unit 里一律正斜杠：反斜杠会被 TOML 当转义符（仓库既有惯例）。"""
    text = deploy_mod.Deployer(make_options(layout)).render_unit(release_id="v1")
    for line in text.splitlines():
        if line.startswith(("WorkingDirectory=", "Environment=PYTHONPATH=")):
            assert "\\" not in line, line


def test_validate_unit_rejects_missing_readwrite_paths(layout: deploy_mod.Layout) -> None:
    good = deploy_mod.Deployer(make_options(layout)).render_unit(release_id="v1")
    broken = good.replace(f"ReadWritePaths={layout.lib_dir.as_posix()} "
                          f"{layout.cache_dir.as_posix()} {layout.run_dir.as_posix()}",
                          f"ReadWritePaths={layout.cache_dir.as_posix()}")
    problems = deploy_mod.validate_unit(broken, required_writable_dirs=[layout.lib_dir])
    assert any("ReadWritePaths" in problem for problem in problems)


@pytest.mark.parametrize("needle,expected", [
    ("User=liptv", "User=root"),
    ("RestartPreventExitStatus=3", "RestartPreventExitStatus=0"),
    ("TimeoutStopSec=90", "TimeoutStopSec=5"),
    ("RestartSec=30", "RestartSec=1"),
])
def test_validate_unit_rejects_unsafe_mutations(
    layout: deploy_mod.Layout, needle: str, expected: str
) -> None:
    text = deploy_mod.Deployer(make_options(layout)).render_unit(release_id="v1")
    assert needle in text
    problems = deploy_mod.validate_unit(text.replace(needle, expected))
    assert problems, f"{expected} 应当被静态校验拒绝"


def test_validate_unit_rejects_shell_metacharacters(layout: deploy_mod.Layout) -> None:
    text = deploy_mod.Deployer(make_options(layout)).render_unit(release_id="v1")
    broken = re.sub(r"(ExecStart=[^\n]*)", r"\1 && echo pwned", text)
    problems = deploy_mod.validate_unit(broken)
    assert any("shell 元字符" in problem for problem in problems)


def test_unit_and_config_templates_have_no_secrets() -> None:
    """模板本身不得含带 query 的 URL / 凭据（否则渲染进生产产物就是泄漏）。"""
    pattern = re.compile(r"\bhttps?://[^\s\"']*\?")
    assert not pattern.search(_UNIT_TEMPLATE)
    assert not pattern.search(_CONFIG_TEMPLATE)
    assert not re.search(r"(?i)\b(token|secret|password)\s*=", _UNIT_TEMPLATE)
    assert not re.search(r"(?i)\b(token|secret|password)\s*=", _CONFIG_TEMPLATE)


def test_render_config_is_all_absolute_and_carries_no_sources(layout: deploy_mod.Layout) -> None:
    text = deploy_mod.Deployer(make_options(layout)).render_config()
    assert "@@" not in text, "渲染后的生产配置不该残留任何占位符（含注释里的说明文字）"
    assert "占位符" not in text
    # 用与生产代码同一个判定：本机跑测试时 ``--root`` 是 Windows 路径（``C:/...``），
    # 只有 ``_is_absolute_path`` 才同时接受 POSIX 形态（``/var/lib/...``，真机形态）
    # 与盘符形态，直接用 ``PurePosixPath.is_absolute()`` 会在本机假失败。
    seen = 0
    for key in ("path", "m3u_path", "summary_path", "status_path", "lock_path", "dynamic_tmp_dir"):
        for match in re.finditer(rf'^{key}\s*=\s*"([^"]*)"', text, re.MULTILINE):
            seen += 1
            assert deploy_mod._is_absolute_path(match.group(1)), match.group(0)
    assert seen >= 6, "生产配置里应当至少有 6 条绝对路径"
    assert "[[sources]]" not in text.split("# 来源清单默认留空")[0]
    assert "enabled = false\n" in text  # [probe] 默认关闭


@pytest.mark.parametrize("snippet,should_fail", [
    ('\n[probe]\nenabled = true\n', True),
    ('\n[[sources]]\nname = "x"\nurl = "https://example.invalid/a.m3u"\nenabled = true\n', True),
    ('\n[runtime]\ninclude_dynamic = true\n', True),
    ('\n[runtime]\nrequire_dynamic = true\n', True),
    ('\n[server]\nenabled = true\n', False),          # 生产必需，不能误伤
    ('\n[probe]\nenabled = false\n', False),
    ('\n[database]\npath = "relative/x.sqlite3"\n', True),
    ('\n[[sources]]\nname = "x"\nurl = "https://example.invalid/a.m3u?token=abc"\n', True),
])
def test_validate_config_text_rules(snippet: str, should_fail: bool) -> None:
    problems = deploy_mod.Deployer.validate_config_text(rendered_config_base() + snippet)
    assert bool(problems) is should_fail, problems


# ==================================================================== guard

def test_guard_allows_paths_outside_the_git_worktree(tmp_path: pathlib.Path) -> None:
    """TASK-006 §12：``/var/lib/...`` 这种工作树外路径必须可用（不需要改 guard）。"""
    publish_mod.guard_runtime_output_path(tmp_path / "var" / "lib" / "liptv" / "liptv.sqlite3")


def test_guard_still_rejects_tracked_paths_inside_the_worktree() -> None:
    """反向：工作树内已跟踪/未忽略的路径仍必须被拒绝（护栏没有被部署绕开）。"""
    with pytest.raises(ValueError):
        publish_mod.guard_runtime_output_path(REPO_ROOT / "README.md")
    with pytest.raises(ValueError):
        publish_mod.guard_runtime_output_path(REPO_ROOT / "liptv" / "deploy.py")
    with pytest.raises(ValueError):
        publish_mod.guard_runtime_output_path(REPO_ROOT / "TASKS" / "TASK-006.md")


# ============================================================ upgrade / 回滚

def _seed_bytes(layout: deploy_mod.Layout) -> tuple[str, str]:
    layout.output_path.write_text("#EXTM3U\n#EXTINF:-1,测试台\nhttp://stream.invalid/a.m3u8\n",
                                  encoding="utf-8", newline="\n")
    return sha256(layout.db_path), sha256(layout.output_path)


def test_upgrade_starts_the_new_release(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归（本轮真实缺陷）：``upgrade()`` 必须用**新 release** 的管理器去 start。

    否则 process 管理器会把旧版本当新版本起起来，健康闸门永远验不到新 release。
    """
    install(layout, release_id="v1")

    managers: list = []

    class _Recorder(deploy_mod.ServiceManager):
        name = "recorder"

        def __init__(self, release_dir: pathlib.Path):
            self.release_dir = release_dir
            self.actions: list[str] = []

        def daemon_reload(self):
            self.actions.append("daemon-reload")
            return {"action": "daemon-reload", "status": "ok"}

        def enable(self):
            self.actions.append("enable")
            return {"action": "enable", "status": "ok"}

        def start(self):
            self.actions.append("start")
            return {"action": "start", "status": "ok"}

        def stop(self):
            self.actions.append("stop")
            return {"action": "stop", "status": "ok"}

    def fake_build(options, *, release_dir, python):
        manager = _Recorder(release_dir)
        managers.append(manager)
        return manager

    monkeypatch.setattr(deploy_mod, "build_service_manager", fake_build)
    monkeypatch.setattr(deploy_mod.Deployer, "_wait_health",
                        lambda self: {"ok": True, "health_status": "ok"})

    payload = deploy_mod.Deployer(make_options(
        layout, release_id="v2", service_manager="process",
    )).upgrade()

    assert payload["status"] == "OK"
    starters = [m for m in managers if "start" in m.actions]
    assert starters, "应当至少有一个管理器执行过 start"
    assert starters[-1].release_dir == layout.release_dir("v2"), (
        f"起起来的必须是新 release，实际是 {starters[-1].release_dir}"
    )
    assert layout.current_pointer.read_text(encoding="utf-8").strip() == "v2"


def test_upgrade_health_failure_rolls_back_and_keeps_data(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TASK-006 §12：升级健康失败 ⇒ 自动回滚代码，且数据库 / live.m3u 逐字节不变。"""
    install(layout, release_id="v1")
    db_before, playlist_before = _seed_bytes(layout)

    answers = [{"ok": False, "health_status": "missing"}, {"ok": True, "health_status": "ok"}]
    monkeypatch.setattr(deploy_mod.Deployer, "_wait_health",
                        lambda self: answers.pop(0) if answers else {"ok": True})

    payload = deploy_mod.Deployer(make_options(
        layout, release_id="v2-broken", service_manager="none",
    )).upgrade()

    assert payload["exit_code"] == deploy_mod.EXIT_HEALTH
    assert payload["status"] == "ROLLED_BACK"
    assert payload["health_after_rollback"]["ok"] is True
    assert layout.current_pointer.read_text(encoding="utf-8").strip() == "v1"
    assert sha256(layout.db_path) == db_before, "回滚绝不能改数据库"
    assert sha256(layout.output_path) == playlist_before, "回滚绝不能改 live.m3u"
    rollback_step = next(s for s in payload["steps"] if s["step"] == "rollback")
    assert rollback_step["target"] == "v1"


def test_upgrade_takes_a_backup_before_touching_anything(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(layout, release_id="v1")
    monkeypatch.setattr(deploy_mod.Deployer, "_wait_health",
                        lambda self: {"ok": True, "health_status": "ok"})

    payload = deploy_mod.Deployer(make_options(layout, release_id="v2")).upgrade()

    assert payload["status"] == "OK"
    backups = backup_mod.list_backups(layout.backups_dir)
    assert len(backups) == 1
    assert backup_mod.verify_backup(backups[0]).ok


def test_upgrade_refuses_same_release_without_force(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(layout, release_id="v1")
    monkeypatch.setattr(deploy_mod.Deployer, "_wait_health",
                        lambda self: {"ok": True, "health_status": "ok"})
    payload = deploy_mod.Deployer(make_options(layout, release_id="v1")).upgrade()
    assert payload["exit_code"] == deploy_mod.EXIT_ERROR
    assert payload["status"] == "FAILED"


def test_dry_run_upgrade_makes_no_changes(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(layout, release_id="v1")
    db_before = sha256(layout.db_path)

    payload = deploy_mod.Deployer(make_options(
        layout, release_id="v2", dry_run=True,
    )).upgrade()

    assert payload["status"] == "PLAN"
    assert not layout.release_dir("v2").exists()
    assert layout.current_pointer.read_text(encoding="utf-8").strip() == "v1"
    assert backup_mod.list_backups(layout.backups_dir) == []
    assert sha256(layout.db_path) == db_before


def test_rollback_without_previous_fails_cleanly(layout: deploy_mod.Layout) -> None:
    install(layout, release_id="v1")
    payload = deploy_mod.Deployer(make_options(layout)).rollback()
    assert payload["exit_code"] == deploy_mod.EXIT_ERROR
    assert payload["status"] == "FAILED"


def test_upgrade_requires_an_installed_release(layout: deploy_mod.Layout) -> None:
    payload = deploy_mod.Deployer(make_options(layout, release_id="v1")).upgrade()
    assert payload["status"] == "PREFLIGHT_FAILED"
    assert payload["exit_code"] == deploy_mod.EXIT_PREFLIGHT


# ==================================================================== 备份

def test_backup_and_verify_roundtrip(layout: deploy_mod.Layout) -> None:
    install(layout, release_id="v1")
    result = backup_mod.backup_sqlite(layout.db_path, layout.backups_dir, retention=5)
    assert result["skipped"] is False
    verification = result["verification"]
    assert verification["ok"] is True
    assert verification["integrity"] == "ok"
    assert verification["table_count"] > 0
    assert verification["schema_version"] == 1


def test_backup_retention_prunes_old_copies(layout: deploy_mod.Layout) -> None:
    install(layout, release_id="v1")
    for index in range(7):
        backup_mod.backup_sqlite(layout.db_path, layout.backups_dir, retention=3,
                                 label=f"r{index}")
    assert len(backup_mod.list_backups(layout.backups_dir)) == 3


def test_backup_of_missing_database_is_skipped_not_faked(layout: deploy_mod.Layout) -> None:
    layout.backups_dir.mkdir(parents=True, exist_ok=True)
    result = backup_mod.backup_sqlite(layout.db_path, layout.backups_dir)
    assert result["skipped"] is True
    assert backup_mod.list_backups(layout.backups_dir) == []


def test_restore_requires_explicit_confirmation(layout: deploy_mod.Layout) -> None:
    install(layout, release_id="v1")
    result = backup_mod.backup_sqlite(layout.db_path, layout.backups_dir)
    with pytest.raises(backup_mod.BackupError):
        backup_mod.restore_sqlite(result["path"], layout.db_path, allow_overwrite=False)


def test_restore_keeps_a_safety_copy(layout: deploy_mod.Layout) -> None:
    install(layout, release_id="v1")
    result = backup_mod.backup_sqlite(layout.db_path, layout.backups_dir)
    restored = backup_mod.restore_sqlite(result["path"], layout.db_path, allow_overwrite=True)
    assert restored["restored"] is True
    assert restored["safety_copy"] is not None
    copy_path = pathlib.Path(restored["safety_copy"])
    assert copy_path.is_file()
    # QA-006B：安全副本必须是**一致性快照**（SQLite backup API），不是裸字节复制
    assert restored["safety_copy_method"] == "sqlite-backup-api"
    assert backup_mod.verify_backup(copy_path).ok, "安全副本自身必须可重开、可验证"
    assert backup_mod.verify_backup(layout.db_path).ok


# ==================================================== QA-006B 停机门禁 fail-closed

def _seed_restorable_db(layout: deploy_mod.Layout) -> pathlib.Path:
    """装好 + 生成一份可验证备份，返回备份路径（当前库与备份内容一致）。"""
    payload = install(layout, release_id="v1")
    assert payload["status"] == "OK", payload
    made = backup_mod.backup_sqlite(layout.db_path, layout.backups_dir)
    assert made.get("skipped") is False, made
    return pathlib.Path(made["path"])


def _set_service_state(fake_systemctl: dict, *, active: str) -> None:
    fake_systemctl["state"].write_text(
        json.dumps({"active": active, "enabled": "enabled"}), encoding="utf-8", newline="\n"
    )


def _restore_deployer(layout: deploy_mod.Layout, **overrides) -> deploy_mod.Deployer:
    base = {"service_manager": "systemd", "systemctl": str(FAKE_SYSTEMCTL)}
    base.update(overrides)
    return deploy_mod.Deployer(make_options(layout, **base))


def test_restore_db_requires_yes_even_when_service_is_stopped(
    layout: deploy_mod.Layout, fake_systemctl: dict
) -> None:
    """``--yes`` 不能被省略：缺它就拒绝，且不碰数据库。"""
    backup_path = _seed_restorable_db(layout)
    before = sha256(layout.db_path)
    with pytest.raises(deploy_mod.DeployError):
        _restore_deployer(layout).restore_db(backup_path, yes=False)
    assert sha256(layout.db_path) == before


def test_restore_db_refuses_when_service_active_and_stop_fails(
    layout: deploy_mod.Layout, fake_systemctl: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-006B 核心：服务 active 且 stop 失败 ⇒ **拒绝恢复，数据库字节不变**。"""
    backup_path = _seed_restorable_db(layout)
    _set_service_state(fake_systemctl, active="active")
    monkeypatch.setenv("FAKE_SYSTEMCTL_MUTABLE", "1")   # stop 若能成功会改写状态
    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL", "stop")   # 但这里注入 stop 失败
    before = sha256(layout.db_path)

    payload = _restore_deployer(layout).restore_db(backup_path, yes=True)

    assert payload["status"] == "BLOCKED_SERVICE_RUNNING"
    assert payload["exit_code"] == deploy_mod.EXIT_PREFLIGHT
    assert payload.get("restore") is None
    assert payload["service_gate"]["blocked"] is True
    assert payload["service_gate"]["stopped"] is False
    assert sha256(layout.db_path) == before, "门禁不通过时绝不能写库"
    assert "stop" in fake_systemctl["actions"](), "应当尝试过停机"
    assert "start" not in fake_systemctl["actions"]()


def test_restore_db_stops_active_service_then_restores(
    layout: deploy_mod.Layout, fake_systemctl: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-006B 核心：服务 active ⇒ 先 stop 并**复查** inactive，再恢复；不自动重启。"""
    backup_path = _seed_restorable_db(layout)
    _set_service_state(fake_systemctl, active="active")
    monkeypatch.setenv("FAKE_SYSTEMCTL_MUTABLE", "1")

    payload = _restore_deployer(layout).restore_db(backup_path, yes=True)

    assert payload["status"] == "OK"
    assert payload["exit_code"] == deploy_mod.EXIT_OK
    gate = payload["service_gate"]
    assert gate["blocked"] is False and gate["stopped"] is True
    assert gate["before"]["active"] == "active"
    assert gate["after"]["active"] == "inactive", "stop 之后必须复查为 inactive"
    actions = fake_systemctl["actions"]()
    assert "stop" in actions
    assert actions.count("is-active") >= 2, "至少要有 before / after 两次状态核实"
    assert "start" not in actions, "保守口径：恢复后不自动重启"
    # 恢复成功 ⇒ 备份可验证、schema 正常、安全副本走一致性方式
    assert payload["restore"]["restored"] is True
    assert payload["restore"]["safety_copy_method"] == "sqlite-backup-api"
    assert payload["restore"]["verification"]["ok"] is True
    assert payload["restore"]["verification"]["table_count"] > 0
    assert pathlib.Path(payload["restore"]["safety_copy"]).is_file()
    assert any("不会自动重启" in note for note in payload["notes"])


def test_restore_db_refuses_when_service_state_is_unknown(layout: deploy_mod.Layout) -> None:
    """QA-006B：判断不了服务状态 ⇒ 默认拒绝（``--yes`` 不代表「服务已停」）。"""
    backup_path = _seed_restorable_db(layout)
    before = sha256(layout.db_path)
    payload = deploy_mod.Deployer(
        make_options(layout, service_manager="none")
    ).restore_db(backup_path, yes=True)

    assert payload["status"] == "BLOCKED_SERVICE_RUNNING"
    assert payload["exit_code"] == deploy_mod.EXIT_PREFLIGHT
    assert payload["service_gate"]["blocked"] is True
    assert payload.get("restore") is None
    assert sha256(layout.db_path) == before


def test_restore_db_break_glass_is_explicit_and_labelled(layout: deploy_mod.Layout) -> None:
    """``--force-offline-restore`` 是**显式** break-glass：允许恢复，但必须自陈这一点。"""
    backup_path = _seed_restorable_db(layout)
    payload = deploy_mod.Deployer(
        make_options(layout, service_manager="none")
    ).restore_db(backup_path, yes=True, force_offline=True)

    assert payload["status"] == "OK"
    assert payload["service_gate"]["forced"] is True
    assert payload["service_gate"]["blocked"] is False
    assert payload["restore"]["restored"] is True
    assert any("force-offline-restore" in note for note in payload["notes"])


def test_restore_db_does_not_start_service_after_restore(
    layout: deploy_mod.Layout, fake_systemctl: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务原本就 inactive ⇒ 恢复后也不得擅自 start（避免掩盖人工确认步骤）。"""
    backup_path = _seed_restorable_db(layout)
    _set_service_state(fake_systemctl, active="inactive")
    monkeypatch.setenv("FAKE_SYSTEMCTL_MUTABLE", "1")

    payload = _restore_deployer(layout).restore_db(backup_path, yes=True)

    assert payload["status"] == "OK"
    actions = fake_systemctl["actions"]()
    assert "stop" not in actions, "服务非 active 时不该 stop"
    assert "start" not in actions, "恢复后不得擅自 start"
    assert payload["restore"]["restored"] is True


# ============================================================ QA-007C
# restore-db 门禁的 systemd 三态归一化（Review 03 唯一剩余阻断项）
#
# 旧实现 `if state.get("active") == "active": ... ; return blocked=False`
# 把**所有**非精确 "active" 的状态（activating / deactivating / reloading /
# maintenance / unknown）都落到「允许恢复」⇒ 服务正在启停的窗口里，
# restore-db 会直接 replace 掉生产 SQLite。违反 QA-006B 冻结的 fail-closed。


@pytest.mark.parametrize("state", ["activating", "deactivating", "reloading",
                                   "maintenance", "unknown", "totally-bogus"])
def test_restore_db_blocks_on_transitional_service_state(
    layout: deploy_mod.Layout, fake_systemctl: dict, state: str
) -> None:
    """QA-007C 回归 1：过渡态/未知态 ⇒ **必须 BLOCKED**，数据库字节不变。"""
    backup_path = _seed_restorable_db(layout)
    _set_service_state(fake_systemctl, active=state)
    before = sha256(layout.db_path)

    payload = _restore_deployer(layout).restore_db(backup_path, yes=True)

    assert payload["status"] == "BLOCKED_SERVICE_RUNNING", payload["service_gate"]
    assert payload["exit_code"] == deploy_mod.EXIT_PREFLIGHT
    gate = payload["service_gate"]
    assert gate["blocked"] is True
    assert gate["service_active_at_gate"] is None, \
        f"状态 {state!r} 是过渡态/未知态，必须归 None 而非 False"
    assert gate.get("forced") is not True, "未加 --force-offline-restore 时不得翻案"
    assert payload.get("restore") is None, "门禁不通过绝不能进入写库步骤"
    assert sha256(layout.db_path) == before, "数据库字节必须不变"


@pytest.mark.parametrize("bogus", ["", None, "   "])
def test_restore_db_blocks_on_blank_or_missing_active_state(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch, bogus: object
) -> None:
    """QA-007C 回归 1b：``is-active`` 返回空/None（查不到）⇒ 归 None ⇒ BLOCKED。

    这一项**不能**用 ``fake_systemctl`` 表达：替身的
    ``str(state.get("active") or "inactive")`` 会把空值兜底成 ``inactive``，
    那是替身的失真（真实 systemctl 查不到时退出码非 0、stdout 为空，
    ``_CommandServiceManager.query()`` 会落 ``None``）。故直接打桩 ``query()``。
    """
    backup_path = _seed_restorable_db(layout)
    before = sha256(layout.db_path)
    monkeypatch.setattr(deploy_mod.SystemdServiceManager, "query",
                        lambda self: {"managed": True, "active": bogus,
                                       "enabled": "enabled"})

    payload = _restore_deployer(layout).restore_db(backup_path, yes=True)

    assert payload["status"] == "BLOCKED_SERVICE_RUNNING", payload["service_gate"]
    gate = payload["service_gate"]
    assert gate["blocked"] is True
    assert gate["service_active_at_gate"] is None, f"active={bogus!r} 必须归 None"
    assert payload.get("restore") is None
    assert sha256(layout.db_path) == before, "数据库字节必须不变"


@pytest.mark.parametrize("after_state", ["deactivating", "unknown", "activating", "reloading"])
def test_restore_db_blocks_when_post_stop_state_is_not_clearly_inactive(
    layout: deploy_mod.Layout, fake_systemctl: dict, monkeypatch: pytest.MonkeyPatch,
    after_state: str,
) -> None:
    """QA-007C 回归 2：active → stop 后状态**未明确 inactive** ⇒ 必须 BLOCKED。

    停机「可能仍在进行中」时不能判定停机成功；这是 Review 03 第 2/3 条。
    """
    backup_path = _seed_restorable_db(layout)
    _set_service_state(fake_systemctl, active="active")
    monkeypatch.setenv("FAKE_SYSTEMCTL_MUTABLE", "1")
    # stop 把状态改成「未明确 inactive」的过渡/未知态（模拟停机卡住）
    monkeypatch.setenv("FAKE_SYSTEMCTL_STOP_STATE", after_state)
    before = sha256(layout.db_path)

    payload = _restore_deployer(layout).restore_db(backup_path, yes=True)

    assert payload["status"] == "BLOCKED_SERVICE_RUNNING", payload["service_gate"]
    gate = payload["service_gate"]
    assert gate["blocked"] is True
    assert gate["service_active_at_gate"] is True, "before 明确 active"
    assert gate["service_active_after_stop"] is None, \
        f"stop 后状态 {after_state!r} 未明确 inactive，必须归 None"
    assert payload.get("restore") is None
    assert sha256(layout.db_path) == before, "数据库字节必须不变"
    assert "stop" in fake_systemctl["actions"](), "应当尝试过停机"
    assert "start" not in fake_systemctl["actions"]()


def test_restore_db_blocks_when_post_stop_state_is_blank(
    layout: deploy_mod.Layout, fake_systemctl: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-007C 回归 2b：stop 后**查不到状态**（空）⇒ 不得判停机成功。

    这一项走打桩而非 ``FAKE_SYSTEMCTL_STOP_STATE``：替身把空值兜底成
    ``inactive``，无法表达「stop 后查询无输出」——而真实 systemctl 在
    停机尚未落定时正是这种表现，必须拦住。
    """
    backup_path = _seed_restorable_db(layout)
    _set_service_state(fake_systemctl, active="active")
    before = sha256(layout.db_path)
    calls = {"n": 0}

    def flaky_query(self):
        calls["n"] += 1
        # 第一次（门禁前）明确 active；第二次（stop 后复查）查不到
        return {"managed": True, "enabled": "enabled",
                "active": "active" if calls["n"] == 1 else ""}

    monkeypatch.setattr(deploy_mod.SystemdServiceManager, "query", flaky_query)

    payload = _restore_deployer(layout).restore_db(backup_path, yes=True)

    assert payload["status"] == "BLOCKED_SERVICE_RUNNING", payload["service_gate"]
    gate = payload["service_gate"]
    assert gate["service_active_at_gate"] is True
    assert gate["service_active_after_stop"] is None, "空状态必须归 None"
    assert payload.get("restore") is None
    assert sha256(layout.db_path) == before, "数据库字节必须不变"


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_restore_db_allows_on_clearly_stopped_service(
    layout: deploy_mod.Layout, fake_systemctl: dict, monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """QA-007C 回归 3（对照组）：明确 inactive / failed ⇒ **继续允许恢复**。

    钉住 fail-closed 不是一刀切 —— 这两种状态与「unit 已停、锁目录已回收」等价。
    """
    backup_path = _seed_restorable_db(layout)
    _set_service_state(fake_systemctl, active=state)
    monkeypatch.setenv("FAKE_SYSTEMCTL_MUTABLE", "1")

    payload = _restore_deployer(layout).restore_db(backup_path, yes=True)

    assert payload["status"] == "OK", payload["service_gate"]
    gate = payload["service_gate"]
    assert gate["blocked"] is False
    assert gate["service_active_at_gate"] is False
    assert gate.get("forced") is not True, "这是正常路径，不是 break-glass"
    assert payload["restore"]["restored"] is True
    assert "stop" not in fake_systemctl["actions"](), "服务本就没跑，不该多此一举 stop"


def test_restore_db_break_glass_unlocks_transitional_state(
    layout: deploy_mod.Layout, fake_systemctl: dict
) -> None:
    """QA-007C 回归 4：过渡态 + ``--force-offline-restore`` ⇒ 允许，但必须自陈 forced。"""
    backup_path = _seed_restorable_db(layout)
    _set_service_state(fake_systemctl, active="activating")

    payload = _restore_deployer(layout).restore_db(
        backup_path, yes=True, force_offline=True)

    assert payload["status"] == "OK", payload["service_gate"]
    gate = payload["service_gate"]
    assert gate["blocked"] is False
    assert gate["forced"] is True
    assert gate["service_active_at_gate"] is None
    assert payload["restore"]["restored"] is True
    assert any("force-offline-restore" in note for note in payload["notes"]), \
        "break-glass 必须在 notes 里显式自陈"


def test_restore_db_break_glass_cannot_override_observed_active(
    layout: deploy_mod.Layout, fake_systemctl: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-007C 回归 5：break-glass **只覆盖「判不出来」，不覆盖「明确在跑」**。

    ``--force-offline-restore`` 的语义是「调用方声明服务不在运行」。
    若门禁**明确观察到 active**（且 stop 失败），该声明已被事实证伪，
    此时不得放行 —— 否则一个错误声明就能 replace 掉生产库。
    """
    backup_path = _seed_restorable_db(layout)
    _set_service_state(fake_systemctl, active="active")
    monkeypatch.setenv("FAKE_SYSTEMCTL_MUTABLE", "1")
    monkeypatch.setenv("FAKE_SYSTEMCTL_FAIL", "stop")
    before = sha256(layout.db_path)

    payload = _restore_deployer(layout).restore_db(
        backup_path, yes=True, force_offline=True)

    assert payload["status"] == "BLOCKED_SERVICE_RUNNING", payload["service_gate"]
    gate = payload["service_gate"]
    assert gate["blocked"] is True
    assert gate["service_active_at_gate"] is True
    assert gate.get("forced") is not True
    assert payload.get("restore") is None
    assert sha256(layout.db_path) == before, "数据库字节必须不变"


def test_restore_db_rejects_non_string_active_state(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-007C 回归 6：查询返回非字符串异常值（int/None/对象）⇒ 归 None ⇒ BLOCKED。

    直接打桩 ``query()``：``fake_systemctl`` 的 ``is-active`` 会把状态
    ``str()`` 成文本（真实 systemctl 也是输出文本），无法表达「返回了非字符串」
    这种查询异常，所以这里绕过替身，直接构造畸形返回值。
    """
    backup_path = _seed_restorable_db(layout)
    before = sha256(layout.db_path)
    for bogus in (1, 0, None, object(), ["active"]):
        monkeypatch.setattr(deploy_mod.SystemdServiceManager, "query",
                            lambda self, _v=bogus: {"managed": True, "active": _v,
                                                    "enabled": "enabled"})
        payload = _restore_deployer(layout).restore_db(backup_path, yes=True)
        assert payload["status"] == "BLOCKED_SERVICE_RUNNING", \
            f"active={bogus!r} 必须被门禁拦住"
        assert payload["service_gate"]["service_active_at_gate"] is None, \
            f"active={bogus!r} 必须归 None"
        assert sha256(layout.db_path) == before, "数据库字节必须不变"


def test_restore_service_gate_has_no_binary_active_comparison() -> None:
    """QA-007C 守护测试：门禁**不得**再出现 `== "active"` 二值判断（防静默回归）。

    只扫**代码行**（跳过 docstring 与注释）——docstring 里刻意引用了旧写法
    作反例（``state.get("active") == "active"``），全文匹配会误伤。
    """
    import ast
    import tokenize

    src = pathlib.Path(deploy_mod.__file__).read_text(encoding="utf-8")
    start = src.find("def _restore_service_gate")
    assert start != -1, "找不到 _restore_service_gate"
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_restore_service_gate")
    # 只看语句行：函数体内出现过的行号集合
    stmt_lines = {n.lineno for n in ast.walk(fn) if isinstance(n, ast.stmt)}
    # 逐行剔除注释与字符串常量：用 tokenize 精确判定
    offenders: list[int] = []
    with open(deploy_mod.__file__, "r", encoding="utf-8") as handle:
        tokens = list(tokenize.generate_tokens(handle.readline))
    for tok in tokens:
        if tok.type != tokenize.OP or tok.string != "==":
            continue
        if tok.start[0] in stmt_lines and "active" in src.splitlines()[tok.start[0] - 1]:
            offenders.append(tok.start[0])
    assert not offenders, \
        f"门禁内这些行又出现 `== \"active\"` 二值判断：{offenders}（必须走 _service_active_state）"


def test_verify_backup_rejects_garbage(tmp_path: pathlib.Path) -> None:
    junk = tmp_path / "liptv-junk.sqlite3"
    junk.write_bytes(b"definitely not sqlite")
    verification = backup_mod.verify_backup(junk)
    assert verification.ok is False


# ==================================================================== status

def test_status_is_read_only(layout: deploy_mod.Layout) -> None:
    install(layout, release_id="v1")
    _seed_bytes(layout)
    db_before = sha256(layout.db_path)
    playlist_before = sha256(layout.output_path)

    payload = deploy_mod.Deployer(make_options(layout)).status(probe_health=False)

    assert payload["status"] == "OK"
    assert payload["release_id"] == "v1"
    assert payload["artifacts"]["config"]["exists"] is True
    assert payload["artifacts"]["database"]["exists"] is True
    assert payload["artifacts"]["playlist"]["exists"] is True
    assert sha256(layout.db_path) == db_before
    assert sha256(layout.output_path) == playlist_before
    assert any("只读" in note for note in payload["notes"])


def test_service_manager_factory_branches(layout: deploy_mod.Layout, fake_systemctl: dict) -> None:
    assert isinstance(deploy_mod.build_service_manager(
        make_options(layout, service_manager="none"),
        release_dir=layout.release_dir("v1"), python=sys.executable,
    ), deploy_mod.NullServiceManager)
    systemd = deploy_mod.build_service_manager(
        make_options(layout, service_manager="systemd", systemctl=str(FAKE_SYSTEMCTL)),
        release_dir=layout.release_dir("v1"), python=sys.executable,
    )
    assert isinstance(systemd, deploy_mod.SystemdServiceManager)
    process = deploy_mod.build_service_manager(
        make_options(layout, service_manager="process"),
        release_dir=layout.release_dir("v1"), python=sys.executable,
    )
    assert isinstance(process, deploy_mod.ProcessServiceManager)


def test_service_manager_factory_rejects_unknown(layout: deploy_mod.Layout) -> None:
    with pytest.raises(deploy_mod.DeployError):
        deploy_mod.build_service_manager(
            make_options(layout, service_manager="k8s"),
            release_dir=layout.release_dir("v1"), python=sys.executable,
        )


def test_deploy_requires_cleanup_of_process_manager(layout: deploy_mod.Layout) -> None:
    """``process`` 管理器的 query 只反映它自己起过的进程（演示/测试里要如实呈现）。"""
    manager = deploy_mod.ProcessServiceManager(
        layout=layout, python=sys.executable, release_dir=layout.release_dir("v1"),
        config_path=layout.config_path,
    )
    assert manager.query()["active"] == "inactive"
    assert manager.stop()["status"] == "skipped"


# ======================================================= 真实进程 + 真 HTTP

def test_upgrade_with_real_process_passes_health_gate(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真起一个 ``run --serve`` 子进程 + 真打 ``/healthz``：健康闸门不是纸面逻辑。

    只验证「服务能起来 + /healthz 业务状态 ok + /live.m3u 非空」，
    完整的 fetch→publish 链路由 tools/demo_deploy_linux.py 覆盖。
    """
    install(layout, release_id="v1")
    port = free_port()
    write_runtime_config(layout, port=port)
    _seed_bytes(layout)

    deployer = deploy_mod.Deployer(make_options(
        layout, release_id="v2", service_manager="process",
        health_timeout=30.0, health_interval=0.5,
    ))
    try:
        payload = deployer.upgrade()
    finally:
        if deployer.service is not None:
            deployer.service.cleanup()

    assert payload["status"] == "OK", payload
    assert payload["health"]["ok"] is True
    assert payload["health"]["health_status"] == "ok"
    assert payload["health"]["playlist_http_status"] == 200
    assert payload["health"]["playlist_bytes"] > 0
    assert layout.current_pointer.read_text(encoding="utf-8").strip() == "v2"


# ======================================================================= CLI

def test_cli_deploy_install_and_plan(prefix: pathlib.Path, tmp_path: pathlib.Path,
                                     fake_systemctl: dict, capsys: pytest.CaptureFixture) -> None:
    common = ["--root", str(prefix), "--no-venv", "--service-manager", "systemd",
              "--systemctl", str(FAKE_SYSTEMCTL), "--json"]

    assert cli_mod.main(["deploy", "plan", *common, "--release-id", "cli-plan"]) == 0
    capsys.readouterr()
    assert not prefix.exists()

    assert cli_mod.main(["deploy", "install", *common, "--release-id", "cli1"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload["status"] == "OK"
    assert (prefix / "etc" / "li-iptv-aggregator" / "config.toml").is_file()

    assert cli_mod.main(["deploy", "status", "--root", str(prefix), "--no-health", "--json"]) == 0
    out = capsys.readouterr().out
    status = json.loads(out[out.index("{"):])
    assert status["release_id"] == "cli1"


def test_cli_deploy_reports_missing_root_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """非 POSIX 本机省略 --root：必须给一句人话，而不是抛栈（保护开发机）。"""
    monkeypatch.setattr(deploy_mod, "default_root",
                        lambda: (_ for _ in ()).throw(deploy_mod.DeployError("非 POSIX，请给 --root")))
    code = cli_mod.main(["deploy", "install", "--no-venv", "--json"])
    out = capsys.readouterr().out
    assert code != 0
    assert "非 POSIX" in out


def test_cli_doctor_reports_failure_for_missing_config(tmp_path: pathlib.Path,
                                                       capsys: pytest.CaptureFixture) -> None:
    assert cli_mod.main(["doctor", "--config", str(tmp_path / "nope.toml"), "--json"]) != 0
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload["ok"] is False
    assert payload["checks"][0]["id"] == "config"


# =================================================================== deploy-state

def test_state_file_records_events_and_never_holds_secrets(layout: deploy_mod.Layout) -> None:
    install(layout, release_id="v1")
    deploy_mod.Deployer(make_options(layout, release_id="v1")).rollback()

    state = json.loads(layout.state_path.read_text(encoding="utf-8"))
    assert state["schema"] == 1
    assert [event["action"] for event in state["events"]] == ["install"]
    text = layout.state_path.read_text(encoding="utf-8")
    assert "://" not in text, "deploy-state 不该出现任何 URL"
    assert "token" not in text.lower()


def test_deploy_error_is_raised_for_bad_source(tmp_path: pathlib.Path) -> None:
    layout = deploy_mod.build_layout(tmp_path / "root")
    payload = deploy_mod.Deployer(make_options(layout, source_dir=tmp_path)).install()
    assert payload["status"] == "PREFLIGHT_FAILED"
    assert payload["exit_code"] == deploy_mod.EXIT_PREFLIGHT


def test_min_python_version_reads_pyproject() -> None:
    assert deploy_mod.min_python_version(REPO_ROOT) == (3, 12)
    assert deploy_mod.min_python_version(pathlib.Path("/nonexistent")) == \
        deploy_mod.FALLBACK_MIN_PYTHON


def test_sanitize_release_id() -> None:
    assert deploy_mod.sanitize_release_id("v1.2.3-abc") == "v1.2.3-abc"
    assert deploy_mod.sanitize_release_id("2026/01/02 abc") == "2026-01-02-abc"
    with pytest.raises(deploy_mod.DeployError):
        deploy_mod.sanitize_release_id("///")


def test_deploy_options_are_dataclass_replaceable(layout: deploy_mod.Layout) -> None:
    options = make_options(layout)
    clone = dataclasses.replace(options, dry_run=True)
    assert clone.dry_run is True
    assert options.dry_run is False


# ================================ QA-007B：upgrade / rollback 的 RuntimeDirectory 语义
#
# systemd unit 用 ``RuntimeDirectory=li-iptv-aggregator`` 托管锁目录，服务一停
# systemd 就把它删掉。修复前 upgrade 的两道门互斥：
#
#   服务在跑 ⇒ doctor.lock fail（活实例持锁）
#   服务已停 ⇒ doctor.dirs fail（/run 目录消失）
#
# ⇒ 生产 ``/`` 上 upgrade / rollback 永远 PREFLIGHT_FAILED。
#
# 修复：① upgrade 用 service-aware preflight（``allow_live_lock=True`` +
# ``service_active=query()``）；② 停服务后显式重建 run_dir（``_ensure_run_dir``），
# rollback 走同一条路径。


def test_upgrade_preflight_passes_while_service_active_with_live_lock(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-007B 回归 1：服务 active + 活实例持锁 ⇒ preflight 放行到 stop 阶段。"""
    install(layout, release_id="v1")
    layout.run_dir.mkdir(parents=True, exist_ok=True)
    # 让 process 管理器报告 active（QA-007B 场景 1：服务在跑、锁被活实例持有）
    monkeypatch.setattr(deploy_mod.ProcessServiceManager, "query",
                        lambda self: {"managed": True, "active": "active",
                                      "enabled": "static", "pid": os.getpid()})
    monkeypatch.setattr(deploy_mod.Deployer, "_wait_health",
                        lambda self: {"ok": True, "health_status": "ok"})
    lock = runtime_mod.SingleInstanceLock(
        layout.lock_path, stale_after_seconds=21600, interval_seconds=10800,
    )
    lock.acquire()
    try:
        payload = deploy_mod.Deployer(make_options(
            layout, release_id="v2", service_manager="process",
        )).upgrade()
    finally:
        lock.release()

    assert payload["status"] == "OK", payload["steps"]
    assert payload["service_active_at_preflight"] is True
    lock_item = [c for c in payload["doctor"]["checks"] if c["id"] == "lock"][0]
    assert lock_item["status"] == "ok"
    assert lock_item["detail"]["preflight_only"] is True


def test_upgrade_recreates_run_dir_after_stop(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-007B 回归 2：stop 之后 run_dir 消失，upgrade 必须重建它再装新 release。"""
    install(layout, release_id="v1")
    assert layout.run_dir.is_dir()

    def stop_and_wipe(self):
        # 模拟 systemd：服务停止即删除 RuntimeDirectory
        if self.release_dir is not None and layout.run_dir.exists():
            for child in layout.run_dir.iterdir():
                child.unlink()
            layout.run_dir.rmdir()
        return {"action": "stop", "status": "ok"}

    class _Manager(deploy_mod.ServiceManager):
        name = "wiper"

        def __init__(self, release_dir):
            self.release_dir = release_dir

        def stop(self):
            return stop_and_wipe(self)

        def start(self):
            return {"action": "start", "status": "ok"}

        def daemon_reload(self):
            return {"action": "daemon-reload", "status": "ok"}

    monkeypatch.setattr(deploy_mod, "build_service_manager",
                        lambda options, *, release_dir, python: _Manager(release_dir))
    monkeypatch.setattr(deploy_mod.Deployer, "_wait_health",
                        lambda self: {"ok": True, "health_status": "ok"})

    payload = deploy_mod.Deployer(make_options(
        layout, release_id="v2", service_manager="process",
    )).upgrade()

    assert payload["status"] == "OK", payload["steps"]
    assert layout.run_dir.is_dir(), "停服务后 run_dir 必须被重建（QA-007B）"
    lock_step = [s for s in payload["steps"] if s["step"] == "lock-dir"]
    assert lock_step and lock_step[0]["status"] == "ok"
    assert "QA-007B" in lock_step[0]["detail"]
    assert layout.current_pointer.read_text(encoding="utf-8").strip() == "v2"


def test_rollback_recreates_run_dir_after_stop(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-007B 回归 3：rollback 走同一套 RuntimeDirectory 语义。"""
    install(layout, release_id="v1")
    # 先做一次成功的 upgrade，制造 previous=v1
    monkeypatch.setattr(deploy_mod.Deployer, "_wait_health",
                        lambda self: {"ok": True, "health_status": "ok"})
    up = deploy_mod.Deployer(make_options(
        layout, release_id="v2", service_manager="process",
    )).upgrade()
    assert up["status"] == "OK", up["steps"]

    def stop_and_wipe(self):
        if layout.run_dir.exists():
            for child in layout.run_dir.iterdir():
                child.unlink()
            layout.run_dir.rmdir()
        return {"action": "stop", "status": "ok"}

    class _Manager(deploy_mod.ServiceManager):
        name = "wiper"

        def __init__(self, release_dir):
            self.release_dir = release_dir

        def stop(self):
            return stop_and_wipe(self)

        def start(self):
            return {"action": "start", "status": "ok"}

        def daemon_reload(self):
            return {"action": "daemon-reload", "status": "ok"}

    monkeypatch.setattr(deploy_mod, "build_service_manager",
                        lambda options, *, release_dir, python: _Manager(release_dir))

    payload = deploy_mod.Deployer(make_options(
        layout, service_manager="process",
    )).rollback()

    assert payload["status"] == "ROLLED_BACK", payload["steps"]
    assert layout.run_dir.is_dir()
    assert layout.current_pointer.read_text(encoding="utf-8").strip() == "v1"


def test_upgrade_health_failure_rolls_back_after_run_dir_recreate(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-007B 回归 4：健康失败仍必须走正常 rollback（不能因 health helper 崩）。"""
    install(layout, release_id="v1")
    db_before = layout.db_path.read_bytes()

    calls = {"n": 0}

    def flaky_health(self):
        calls["n"] += 1
        return {"ok": calls["n"] > 1, "health_status": "ok" if calls["n"] > 1 else "missing"}

    monkeypatch.setattr(deploy_mod.Deployer, "_wait_health", flaky_health)

    payload = deploy_mod.Deployer(make_options(
        layout, release_id="v2", service_manager="process",
    )).upgrade()

    assert payload["status"] == "ROLLED_BACK", payload["steps"]
    assert layout.current_pointer.read_text(encoding="utf-8").strip() == "v1"
    assert layout.db_path.read_bytes() == db_before


# ============================================================ QA-007B-1
# systemd service state 三态归一化（Review 02 唯一剩余阻断项）
#
# 旧实现 ``raw.strip() == "active"`` 把**所有**非 active 的非空字符串
# （含 activating / deactivating / reloading / maintenance / unknown）
# 一律压成 False，等于宣称「服务已明确停止」而实际并未如此，
# 于是 doctor 对 RuntimeDirectory 缺失采用了宽松语义。必须 fail-closed。


@pytest.mark.parametrize("raw,expected", [
    # --- 明确 True：只有 active ---
    (True, True),
    ("active", True),
    ("ACTIVE", True),          # systemctl 一律小写，但大小写不敏感更稳
    ("  active  ", True),      # stdout 可能带换行/空白
    # --- 明确 False：只有 inactive / failed（等价于「unit 已停，目录已回收」）---
    (False, False),
    ("inactive", False),
    ("failed", False),
    ("inactive\n", False),
    ("FAILED", False),
    # --- None：过渡态 / 未知 / 查不到 —— 保持严格判定 ---
    ("activating", None),
    ("deactivating", None),
    ("reloading", None),
    ("maintenance", None),
    ("unknown", None),
    ("", None),
    ("   ", None),
    (None, None),
    (0, None),
    (1, None),                 # 非 bool 的真值刻意不当作 active
    (object(), None),
    # --- None：将来 systemd 出现的新状态也必须落到 None（不得默认 False）---
    ("reloading-or-restarting", None),
])
def test_service_active_state_normalisation(raw: object, expected: bool | None) -> None:
    """QA-007B-1 回归 1：三态归一化——只有 active=True、只有 inactive/failed=False。"""
    assert deploy_mod._service_active_state(raw) is expected


@pytest.mark.parametrize("state", ["activating", "deactivating", "reloading",
                                   "maintenance", "unknown", "", None])
def test_upgrade_transitional_service_state_keeps_preflight_strict(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch, state: str | None
) -> None:
    """QA-007B-1 回归 2：过渡态/未知态 ⇒ ``service_active_at_preflight`` 不得记成 False，
    且缺失的 run_dir 必须**继续阻断** preflight（不得套用「服务已停」的宽松语义）。"""
    install(layout, release_id="v1")
    # 模拟 systemd 已回收 RuntimeDirectory，但服务状态判不出来
    for child in layout.run_dir.iterdir():
        child.unlink()
    layout.run_dir.rmdir()
    assert not layout.run_dir.exists()

    monkeypatch.setattr(deploy_mod.ProcessServiceManager, "query",
                        lambda self: {"managed": True, "active": state,
                                      "enabled": "static", "pid": os.getpid()})
    monkeypatch.setattr(deploy_mod.Deployer, "_wait_health",
                        lambda self: {"ok": True, "health_status": "ok"})

    payload = deploy_mod.Deployer(make_options(
        layout, release_id="v2", service_manager="process",
    )).upgrade()

    assert payload["status"] == "PREFLIGHT_FAILED", payload["steps"]
    assert payload["service_active_at_preflight"] is None, \
        f"状态 {state!r} 是过渡态/未知态，必须记 None 而非 False"
    dirs = [c for c in payload["doctor"]["checks"] if c["id"] == "dirs"][0]
    assert dirs["status"] == "fail", "状态判不出来时 run_dir 缺失必须严格判 fail"
    assert not layout.run_dir.exists(), "preflight 失败不得留下任何变更"
    assert layout.current_pointer.read_text(encoding="utf-8").strip() == "v1", \
        "preflight 失败不得切换 release"


def test_upgrade_failed_service_state_still_allows_missing_run_dir(
    layout: deploy_mod.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA-007B-1 回归 3：对照组——``failed`` 是**明确**停止态，仍走「目录缺失属正常」。

    这条钉住 fail-closed 不是一刀切拒绝一切：稳定 inactive/failed 必须放行，
    否则真机上反复启停后 unit 处于 failed 时 upgrade 会被无谓阻断。
    """
    install(layout, release_id="v1")
    for child in layout.run_dir.iterdir():
        child.unlink()
    layout.run_dir.rmdir()

    monkeypatch.setattr(deploy_mod.ProcessServiceManager, "query",
                        lambda self: {"managed": True, "active": "failed",
                                      "enabled": "static", "pid": None})
    monkeypatch.setattr(deploy_mod.Deployer, "_wait_health",
                        lambda self: {"ok": True, "health_status": "ok"})

    payload = deploy_mod.Deployer(make_options(
        layout, release_id="v2", service_manager="process",
    )).upgrade()

    assert payload["status"] == "OK", payload["steps"]
    assert payload["service_active_at_preflight"] is False
    assert layout.run_dir.is_dir(), "明确停止后 upgrade 应重建 run_dir"
    assert layout.current_pointer.read_text(encoding="utf-8").strip() == "v2"
