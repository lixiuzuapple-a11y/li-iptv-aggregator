#!/usr/bin/env python3
"""TASK-006 §13：单机 Linux 生产部署的**离线端到端演示**（Windows 开发机也能跑）。

    python tools/demo_deploy_linux.py            # 全流程；结束清理临时前缀
    python tools/demo_deploy_linux.py --keep     # 保留临时前缀，便于人工复核

演示的是**部署机制**，不是业务：

    临时前缀(DESTDIR) → plan(零落盘) → install → 生产配置 → fake systemd
    → 真实服务进程 + /healthz + /live.m3u → SQLite 一致性备份
    → upgrade(带健康闸门) → 注入坏 release（模拟新版本有致命缺陷）
    → 健康闸门失败 → 自动回滚 → 数据逐字节不变 → 全程无 token

硬约束（TASK-006 §13）：

* **绝不**访问任何公网 IPTV 源 —— 来源指向本机 ``tools/mock_source_server.py`` 起的线程内服务；
* **绝不**写真实 ``/etc/systemd/system``、**绝不**托管真实服务 —— systemd 面用
  ``tools/fake_systemctl.py`` 替身，真实服务进程只在临时前缀里跑、只用本机临时端口；
* 输出里**不含**任何带 query 的 URL / token（脚本结束前会自己扫一遍产物）。

两处**刻意的演示偏差**（都写在屏幕上，且不改变被测代码路径）：

1. ``run_on_start = false``：演示配置不随启动跑一轮。这样「升级/回滚**没有**碰过数据库
   与 live.m3u」可以用**逐字节 checksum 相等**来证明，而不是靠"看起来没变"。
   发号前的最后一份成品由一次显式 ``run --once`` 真实轮次产生（同一套代码路径）。
2. ``--no-venv``：不建 venv，直接用当前解释器 + ``PYTHONPATH=<release>``，
   这样在非 POSIX 的开发机上也能跑完整流程。生产用真实 venv（见 DEPLOYMENT.md）。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import shutil
import socket
import sys
import tempfile
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liptv import backup as backup_mod  # noqa: E402
from liptv import config as config_mod  # noqa: E402
from liptv import deploy as deploy_mod  # noqa: E402
from liptv import doctor as doctor_mod  # noqa: E402
from liptv import health as health_mod  # noqa: E402
from liptv import runtime as runtime_mod  # noqa: E402
from tools.mock_source_server import RunningMock  # noqa: E402

FAKE_SYSTEMCTL = REPO_ROOT / "tools" / "fake_systemctl.py"

#: 产物里绝不允许出现的东西（§12「不泄漏带 query 的 URL/token」）
_SECRET_PATTERNS = (
    (re.compile(r"https?://[^\s\"']*\?[^\s\"']*"), "带 query 的 URL"),
    (re.compile(r"(?i)(txsecret|txtime|access[_-]?key|api[_-]?key|authorization|passwd|secret)\s*[=:]"),
     "疑似凭据字段"),
)


# ==================================================================== 小工具

def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_in_use(host: str, port: int) -> bool:
    """本机该端口是否已被占用（与本次部署无关的环境事实）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, int(port))) == 0


def scan_secrets(text: str) -> list[str]:
    hits: list[str] = []
    for pattern, label in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            hits.append(f"{label}: {match.group(0)[:100]}")
    return hits


def copytree(src: pathlib.Path, dst: pathlib.Path) -> None:
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.pyc", "*.pyo", ".pytest_cache",
            "out", "data", ".venv", "venv", "*.sqlite3",
        ),
    )


# ==================================================================== 演示器

class Demo:
    """把每一步的结论收成「OK / FAIL」，最后给一张总表。"""

    def __init__(self, *, keep: bool, explicit_root: str | None) -> None:
        self.keep = keep
        self.explicit_root = explicit_root
        self.results: list[dict] = []
        self.section = "(start)"
        self.failures = 0
        self.tmp: pathlib.Path | None = None
        self.managers: list[deploy_mod.ProcessServiceManager] = []

    # ------------------------------------------------------------ 输出
    def head(self, title: str) -> None:
        self.section = title
        print(f"\n──── {title} " + "─" * max(0, 66 - len(title)))

    def note(self, text: str) -> None:
        print(f"       · {text}")

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        ok = bool(ok)
        if not ok:
            self.failures += 1
        self.results.append({"section": self.section, "label": label, "ok": ok, "detail": detail})
        tail = f"   {detail}" if detail else ""
        print(f"  [{'OK  ' if ok else 'FAIL'}] {label}{tail}")
        return ok

    def equal(self, label: str, actual, expected) -> bool:
        return self.check(label, actual == expected, f"实际={actual!r} 期望={expected!r}")

    # ------------------------------------------------------------ 生命周期
    def track(self, manager: deploy_mod.ProcessServiceManager):
        self.managers.append(manager)
        return manager

    def stop_all(self) -> None:
        for manager in self.managers:
            try:
                manager.stop()
            except Exception as exc:  # pragma: no cover - 清理尽力而为
                print(f"       · 清理进程失败（忽略）：{exc}")
        self.managers.clear()

    def cleanup(self) -> None:
        self.stop_all()
        if self.tmp is None or self.keep:
            if self.tmp is not None:
                print(f"\n临时前缀已保留：{self.tmp}")
            return
        shutil.rmtree(self.tmp, ignore_errors=True)


# ==================================================================== 各步骤

def write_demo_config(path: pathlib.Path, layout: deploy_mod.Layout, *,
                      port: int, source_url: str) -> None:
    """演示配置：路径与生产模板一致，只加一个本机 mock 来源 + 随机空闲端口。"""
    text = (
        "# TASK-006 演示用配置 —— **不是**生产模板。\n"
        "# 与 install 生成的生产配置相比只有两处演示改动：\n"
        "#   1. 加一个指向**本机 mock** 的固定来源（绝不访问公网）；\n"
        "#   2. 端口取本机随机空闲端口；run_on_start = false（见脚本头部说明）。\n"
        "\n[database]\n"
        f'path = "{layout.db_path.as_posix()}"\n'
        "\n[output]\n"
        f'm3u_path = "{layout.output_path.as_posix()}"\n'
        "keep_previous = true\n"
        "\n[selection]\n"
        "window_days = 7\n"
        "max_consecutive_failures = 3\n"
        "min_successes = 1\n"
        "\n[fetch]\n"
        "timeout_seconds = 5.0\n"
        "max_bytes = 2000000\n"
        "max_redirects = 3\n"
        f'dynamic_tmp_dir = "{layout.dynamic_tmp_dir.as_posix()}"\n'
        "\n[publish]\n"
        f'summary_path = "{layout.summary_path.as_posix()}"\n'
        "dynamic_group_title = \"体育赛事（实时）\"\n"
        "dynamic_sources = []\n"
        "\n[publish.dynamic]\n"
        "include_replay = false\n"
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
        'name = "demo-host"\n'
        'location = "Offline demo (local mock)"\n'
        'ffprobe_path = "/usr/bin/ffprobe"\n'
        "\n[[sources]]\n"
        'name = "demo-mock-fixed"\n'
        'kind = "fixed_m3u"\n'
        f'url = "{source_url}"\n'
        "enabled = true\n"
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def _run_cli(layout: deploy_mod.Layout, release_dir: pathlib.Path, argv: list[str],
             *, timeout: float = 180.0):
    """在 release 的解释器环境里跑一次 CLI（等价生产 unit 的 ExecStart 环境）。"""
    import subprocess

    env = dict(os.environ)
    env["PYTHONPATH"] = str(release_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, "-m", "liptv", *argv],
                          capture_output=True, text=True, timeout=timeout, check=False, env=env)


def _extract_json(text: str):
    for index, char in enumerate(text):
        if char in "{[":
            try:
                return json.JSONDecoder().raw_decode(text[index:])[0]
            except json.JSONDecodeError:
                continue
    return None


def _cli_json(layout: deploy_mod.Layout, release_dir: pathlib.Path, argv: list[str]):
    out = _run_cli(layout, release_dir, [*argv, "--config", str(layout.config_path), "--json"])
    if out.returncode != 0:
        raise RuntimeError(f"CLI 失败 {argv}: rc={out.returncode} {out.stderr.strip()[:300]}")
    return _extract_json(out.stdout or "")


def seed_end_to_end(demo: Demo, layout: deploy_mod.Layout, release_dir: pathlib.Path) -> bool:
    """按**真实运维顺序**把链路喂出第一份成品（全程只打本机 mock）。

    source-register → fetch → canonical-add + binding-add → stream-sync
    → probe-result-add（模拟既有测活历史）→ run --once

    这条链是 TASK-001..005 冻结的流程；runtime 刻意**不**自动创建 canonical/binding
    （见 liptv/runtime.py 模块 docstring），所以必须显式走一遍。
    """
    print("       · ① source-register --from-config")
    registered = _cli_json(layout, release_dir,
                           ["source-register", "--from-config", "--only-enabled"])
    print(f"         registered={registered.get('registered')} sources={len(registered.get('sources') or [])}")
    if not (registered.get("sources") or []):
        return False

    print("       · ② fetch --all（抓本机 mock）")
    fetched = _cli_json(layout, release_dir, ["fetch", "--all"])
    print(f"         requested={fetched.get('requested')} ok={fetched.get('ok_count')} "
          f"created={fetched.get('total_created')}")

    rows = _cli_json(layout, release_dir, ["source-channel-list"]) or []
    print(f"       · ③ canonical-add + binding-add（{len(rows)} 条原始条目）")
    for row in rows:
        if not int(row.get("active") or 0):
            continue
        canonical = _cli_json(layout, release_dir, [
            "canonical-add", "--name", str(row["raw_name"]),
            "--category", str(row.get("raw_group") or "其他"),
        ])
        _cli_json(layout, release_dir, [
            "binding-add", "--source-channel-id", str(row["id"]),
            "--canonical-id", str(canonical["canonical_channel_id"]),
        ])

    print("       · ④ stream-sync")
    synced = _cli_json(layout, release_dir, ["stream-sync"])
    print(f"         {json.dumps(synced, ensure_ascii=False)[:160]}")

    streams = _cli_json(layout, release_dir, ["stream-list"]) or []
    print(f"       · ⑤ probe-result-add（{len(streams)} 条线路，模拟既有测活历史）")
    for stream in streams:
        _cli_json(layout, release_dir, [
            "probe-result-add", "--stream-id", str(stream["id"]), "--probe", "demo-probe",
            "--startup-ms", "1200", "--resolution", "1920x1080", "--protocol", "https",
        ])

    print("       · ⑥ run --once（fetch → stream-sync → publish）")
    out = _run_cli(layout, release_dir,
                   ["run", "--once", "--no-serve", "--config", str(layout.config_path)])
    for line in (out.stdout or "").splitlines()[-7:]:
        print(f"         {line}")
    return out.returncode == 0


def start_real_service(demo: Demo, layout: deploy_mod.Layout, release_dir: pathlib.Path,
                       *, base_url: str):
    """真实起一个 ``run --serve`` 进程，并等 /healthz 真的 ok；返回该管理器。"""
    manager = demo.track(deploy_mod.ProcessServiceManager(
        layout=layout, python=sys.executable, release_dir=release_dir,
        config_path=layout.config_path,
    ))
    started = manager.start()
    demo.check("真实服务进程已启动（等价 unit 的 ExecStart）",
               started.get("status") == "ok", f"pid={started.get('pid')}")
    result = health_mod.wait_until_healthy(base_url, timeout=25.0, interval=0.5)
    demo.check("健康闸门：/healthz 业务状态 == ok（不是只看 systemd active）",
               result.ok and result.health_status == health_mod.HEALTH_STATUS_OK,
               f"http={result.health_http_status} status={result.health_status} "
               f"attempts={result.attempts}")
    demo.check("/live.m3u 可订阅且非空",
               result.playlist_http_status == 200 and bool(result.playlist_bytes),
               f"http={result.playlist_http_status} bytes={result.playlist_bytes}")
    return manager if result.ok else None


def wait_lock_released(layout: deploy_mod.Layout, *, timeout: float = 15.0) -> dict:
    """等服务进程退出后锁文件被释放（或退化成可安全接管的 stale 锁）。"""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = runtime_mod.inspect_lock(layout.lock_path)
        if last.get("state") in {"free", "stale"}:
            return last
        time.sleep(0.2)
    return last


def stop_service(demo: Demo, manager) -> None:
    if manager is None:
        return
    manager.stop()
    info = wait_lock_released(manager.layout)
    if os.name == "posix":
        demo.check("停止后单实例锁已释放（SIGTERM 干净停止）",
                   info.get("state") == "free", f"state={info.get('state')}")
    else:
        demo.note(f"Windows 无法投递 SIGTERM（terminate=TerminateProcess），"
                  f"锁状态退化为 {info.get('state')!r}：属**可安全接管**，"
                  f"真实 SIGTERM 路径由 POSIX 测试覆盖")
        demo.check("停止后锁处于可安全接管状态（free 或 stale）",
                   info.get("state") in {"free", "stale"}, f"state={info.get('state')}")


def make_broken_source(dest: pathlib.Path) -> pathlib.Path:
    """复制一份源码树并注入致命缺陷，模拟「新 release 起不来」的健康失败。"""
    copytree(REPO_ROOT, dest)
    init = dest / "liptv" / "__init__.py"
    init.write_text(
        init.read_text(encoding="utf-8")
        + "\nraise RuntimeError('TASK-006 demo: 故意破坏的 release（模拟新版本有致命缺陷）')\n",
        encoding="utf-8", newline="\n",
    )
    return dest


# ==================================================================== 主流程

def run(demo: Demo) -> int:  # noqa: C901 - 演示脚本，线性流程更好读
    print("liptv 单机 Linux 生产部署 · 离线端到端演示（TASK-006 §13）")
    print(f"  python      : {sys.executable}")
    print(f"  平台        : {os.name}（{'POSIX' if os.name == 'posix' else '非 POSIX：权限只记意图'}）")
    print(f"  临时前缀    : 见下（--keep 可保留）")

    collected: list[str] = []  # 所有要扫 token 的文本

    with RunningMock() as (mock_server, mock_url):
        mock_server.state.set_ok()
        demo.note(f"本机 mock 来源就绪：{mock_url}/seq.m3u（全程不碰公网）")

        # ---------------------------------------------------------- 0. 前缀
        demo.head("0. 临时 DESTDIR 前缀")
        if demo.explicit_root:
            root = pathlib.Path(demo.explicit_root).expanduser()
            root.mkdir(parents=True, exist_ok=True)
            demo.note("使用显式前缀：演示结束**不会**删除它")
            demo.check("使用显式前缀", root.is_dir(), str(root))
        else:
            demo.tmp = pathlib.Path(tempfile.mkdtemp(prefix="liptv-t6-demo-"))
            root = demo.tmp / "root"
            demo.check("创建临时前缀", True, str(root))
        layout = deploy_mod.build_layout(root, user=deploy_mod.DEFAULT_USER,
                                         group=deploy_mod.DEFAULT_GROUP)
        demo.note(f"prefix = {layout.root}")
        demo.check("非 POSIX 本机必须显式 --root（不会误改开发机）",
                   os.name == "posix" or layout.root != pathlib.Path("/"), str(layout.root))

        # ---------------------------------------------------------- 1. plan
        demo.head("1. deploy plan：一个字节都不写")
        plan_opts = deploy_mod.DeployOptions(
            layout=layout, source_dir=REPO_ROOT, release_id="plan-probe",
            python=sys.executable, method="copy", no_venv=True, dry_run=True,
        )
        plan = deploy_mod.Deployer(plan_opts).install()
        demo.equal("plan 状态", plan["status"], "PLAN")
        demo.equal("plan 退出码", plan["exit_code"], 0)
        demo.check("plan 未落盘（前缀目录不存在）", not layout.root.exists(),
                   f"exists={layout.root.exists()}")
        demo.check("plan 不执行服务动作（没有 systemctl）",
                   all(s["status"] == "planned" for s in plan["steps"]
                       if s["step"].startswith("systemctl")), "")
        demo.note("plan 的动作表与 install 一致，只是全部标成 planned（不真跑）")

        # ---------------------------------------------------------- 2. install
        demo.head("2. install（systemd 面用 fake systemctl）")
        meta_dir = (demo.tmp / "demo-meta") if demo.tmp is not None else (root / "demo-meta")
        fake_log = meta_dir / "fake-systemctl.log"
        fake_state = meta_dir / "fake-systemctl-state.json"
        fake_state.parent.mkdir(parents=True, exist_ok=True)
        fake_state.write_text(json.dumps({"active": "inactive", "enabled": "disabled"}),
                              encoding="utf-8", newline="\n")
        os.environ["FAKE_SYSTEMCTL_LOG"] = str(fake_log)
        os.environ["FAKE_SYSTEMCTL_STATE"] = str(fake_state)

        install_opts = deploy_mod.DeployOptions(
            layout=layout, source_dir=REPO_ROOT, release_id="v1",
            python=sys.executable, method="copy", no_venv=True,
            service_manager="systemd", systemctl=str(FAKE_SYSTEMCTL),
        )
        installer = deploy_mod.Deployer(install_opts)
        install = installer.install()
        demo.equal("install 状态", install["status"], "OK")
        demo.equal("install 退出码", install["exit_code"], 0)
        demo.check("release 目录已就位", layout.release_dir("v1").is_dir())
        demo.check("生产配置已生成", layout.config_path.is_file(), str(layout.config_path))
        demo.check("数据库已显式 init（schema V1）", layout.db_path.is_file())
        demo.check("systemd unit 已渲染并落位", layout.unit_path.is_file(), str(layout.unit_path))
        demo.check("release 指针已写入", layout.current_pointer.read_text(encoding="utf-8").strip() == "v1")

        for name, path, kind in (
            ("/opt（代码，只读）", layout.app_dir, "dir"),
            ("/etc（配置）", layout.etc_dir, "dir"),
            ("/var/lib（持久数据）", layout.lib_dir, "dir"),
            ("/var/cache（可丢弃）", layout.cache_dir, "dir"),
            ("/run（短生命周期锁）", layout.run_dir, "dir"),
        ):
            demo.check(f"目录模型 {name} 存在", path.is_dir(), str(path))
        demo.check("备份目录权限意图 0700（在 mode_table 里声明）",
                   any(r["path"] == str(layout.backups_dir) and int(r["mode"]) == 0o700
                       for r in layout.mode_table("v1")), "")

        # QA-006A：把**生产属主模型**当场打印并逐类断言（报告里的权限表必须有代码证据）
        matrix = {row["path"]: row for row in layout.mode_table("v1")}
        code_paths = (layout.app_dir, layout.releases_dir, layout.release_dir("v1"),
                      layout.venv_dir, layout.current_pointer, layout.state_path,
                      layout.unit_path)
        config_paths = (layout.etc_dir, layout.config_path)
        data_paths = (layout.lib_dir, layout.db_path, layout.backups_dir,
                      layout.cache_dir, layout.dynamic_tmp_dir, layout.run_dir)
        demo.check("代码 / venv / release / 指针 / state / unit 归 root:root",
                   all(matrix[str(p)]["owner"] == "root:root" for p in code_paths))
        demo.check("生产配置归 root:liptv（服务用户组可读、不可写）",
                   all(matrix[str(p)]["owner"] == f"root:{layout.group}" for p in config_paths))
        demo.check("数据 / 缓存 / 运行期 / 数据库 归 liptv:liptv",
                   all(matrix[str(p)]["owner"] == f"{layout.user}:{layout.group}"
                       for p in data_paths))
        demo.check("服务账号不拥有任何代码或配置路径",
                   all(matrix[str(p)]["owner"] != f"{layout.user}:{layout.group}"
                       for p in (*code_paths, *config_paths)))
        print("       · 权限意图矩阵（root install 时逐项 chown 的就是这些）：")
        for path in (*code_paths, *config_paths, *data_paths):
            row = matrix[str(path)]
            shown = str(path).replace(str(layout.root), "ROOT")
            print(f"         {shown:<46} {row['owner']:<14} {int(row['mode']):04o}")
        demo.check("安装未启用低权限用户创建（演示未给 --create-user）",
                   install["steps"][0]["status"] in {"ok", "skipped"}, install["steps"][0].get("reason", ""))

        unit_text = layout.unit_path.read_text(encoding="utf-8")
        collected.append(unit_text)
        demo.check("unit 不含模板注释残留（[Unit] 之前被丢弃）",
                   "给人看的模板说明" not in unit_text)
        demo.check("unit 以非 root 用户运行",
                   "User=liptv" in unit_text and "User=root" not in unit_text)
        demo.check("unit 含 EXIT_LOCKED 抗重启风暴（RestartPreventExitStatus=3）",
                   "RestartPreventExitStatus=3" in unit_text)
        demo.check("unit 声明了 ReadWritePaths（ProtectSystem=strict 联动）",
                   "ReadWritePaths=" in unit_text and "ProtectSystem=strict" in unit_text)
        demo.check("unit 无残留占位符", "@@" not in unit_text)

        print("       · 渲染后的 unit：")
        for line in unit_text.splitlines():
            if line.strip() and not line.strip().startswith("#"):
                print(f"         {line}")

        # fake systemd：证明 unit 管理路径确实被走到（替身不真启动服务）
        fake_state.write_text(json.dumps({"active": "active", "enabled": "enabled"}),
                              encoding="utf-8", newline="\n")
        started = installer.service.start()
        queried = installer.service.query()
        calls = [json.loads(ln) for ln in fake_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        collected.append(fake_log.read_text(encoding="utf-8"))
        actions = [c["action"] for c in calls]
        demo.check("fake systemctl 收到 daemon-reload / enable / start",
                   all(a in actions for a in ("daemon-reload", "enable", "start")), f"actions={actions}")
        demo.equal("fake systemctl start 返回 ok", started.get("status"), "ok")
        demo.equal("fake systemctl is-active 读到 active", queried.get("active"), "active")
        demo.note("fake systemctl 只是**记录调用**，不会真的启动服务（真服务由后面的真实进程负责）")

        # ---------------------------------------------------------- 3. 幂等
        demo.head("3. install 幂等：配置与数据库永不覆盖")
        second = deploy_mod.Deployer(install_opts).install()
        demo.equal("第二次 install 状态", second["status"], "OK")
        skipped = {s["step"]: s for s in second["steps"] if s["step"] in {"config", "init-db"}}
        demo.check("已存在的生产配置被跳过（不覆盖）",
                   skipped.get("config", {}).get("status") in {"skipped", "ok"}
                   and install["steps"] and any(
                       s["step"] == "config" and s["status"] == "ok" for s in install["steps"]),
                   f"second.config={skipped.get('config', {}).get('status')}")
        demo.check("已存在的数据库被跳过（绝不重建）",
                   skipped.get("init-db", {}).get("reason", "").startswith("数据库已存在"),
                   skipped.get("init-db", {}).get("reason", ""))
        demo.check("第二次 install 没有修改任何路径",
                   not second["changed"], f"changed={len(second['changed'])}")

        # ---------------------------------------------------------- 4. 生产配置
        demo.head("4. production config 断言 + doctor")
        prod_text = layout.config_path.read_text(encoding="utf-8")
        collected.append(prod_text)
        cfg = config_mod.load_config(layout.config_path)
        server = config_mod.server_settings(cfg)
        probe_cfg = config_mod.probe_settings(cfg)
        runtime_cfg = config_mod.runtime_settings(cfg)
        paths = [cfg["database"]["path"], cfg["output"]["m3u_path"],
                 cfg["publish"]["summary_path"], runtime_cfg["status_path"],
                 runtime_cfg["lock_path"], cfg["fetch"]["dynamic_tmp_dir"]]
        demo.check("全部运行期路径都是绝对路径",
                   all(str(p).startswith("/") or pathlib.Path(str(p)).is_absolute() for p in paths),
                   "; ".join(paths))
        demo.equal("默认只绑 loopback", server["host"], "127.0.0.1")
        demo.check("测活默认关闭（probe.enabled=false）", probe_cfg.get("enabled") is False,
                   f"enabled={probe_cfg.get('enabled')}")
        demo.check("动态源默认关闭", cfg.get("runtime", {}).get("include_dynamic") is False
                   and not cfg.get("sources") or all(
                       s.get("enabled") is False for s in (cfg.get("sources") or [])),
                   f"sources={len(cfg.get('sources') or [])}")
        demo.check("模板里没有真实来源清单（不会自动联网抓取）", not cfg.get("sources"),
                   f"sources={len(cfg.get('sources') or [])}")
        demo.check("模板无凭据/带 query 的 URL", not scan_secrets(prod_text), "")

        doctor_busy = port_in_use(str(server["host"]), int(server["port"]))
        if doctor_busy:
            demo.note(f"本机 {server['host']}:{server['port']} 已被占用（与本次部署无关的既有进程），"
                      f"doctor 改用 --no-port-check 口径（端口分支由单元测试覆盖）")
        doctor = doctor_mod.collect(layout.config_path, check_port=not doctor_busy)
        demo.check("doctor 体检通过（ok = 没有 fail）", doctor["ok"],
                   f"summary={doctor['summary']}")
        for check in doctor["checks"]:
            print(f"         [{check['status']:>4}] {check['id']:<9} {check['message']}")
        demo.check("doctor 只诊断（不抓取/不发布/不改库）",
                   "只诊断" in doctor["note"])

        # ---------------------------------------------------------- 5. 演示配置 + 真实一轮
        demo.head("5. 演示配置 + 真实一轮（本机 mock，绝不联网）")
        port = free_port()
        base_url = health_mod.health_base_url("127.0.0.1", port)
        write_demo_config(layout.config_path, layout, port=port, source_url=f"{mock_url}/seq.m3u")
        demo.note(f"演示配置已写入（端口 {port}，来源 = 本机 mock）")
        demo.note("偏差 1：run_on_start = false —— 让「部署动作不碰数据」可用 checksum 证明")

        seeded = seed_end_to_end(demo, layout, layout.release_dir("v1"))
        demo.check("按真实运维顺序喂出第一份成品（source-register → … → run --once）", seeded)
        demo.check("第一份 /live.m3u 已生成且非空",
                   layout.output_path.is_file() and layout.output_path.stat().st_size > 0,
                   f"bytes={layout.output_path.stat().st_size if layout.output_path.is_file() else 0}")

        db_sha0 = sha256_file(layout.db_path) if layout.db_path.is_file() else "(数据库缺失)"
        m3u_sha0 = sha256_file(layout.output_path) if layout.output_path.is_file() else "(成品缺失)"
        demo.note(f"基线 checksum：db={db_sha0[:16]}…  live.m3u={m3u_sha0[:16]}…")

        if not (layout.output_path.is_file() and layout.output_path.stat().st_size > 0):
            demo.check("无法得到第一份成品：后续升级/回滚演示无法继续", False)
            raise SystemExit(1)

        # ---------------------------------------------------------- 6. 真实服务
        demo.head("6. 真实服务进程 + /healthz + /live.m3u")
        service_manager = start_real_service(
            demo, layout, layout.release_dir("v1"), base_url=base_url)
        demo.check("服务启动后进入健康状态", service_manager is not None)
        demo.note("注意：systemd active / HTTP 200 都**不等于**业务健康；这里判的是 /healthz 的 status=ok")

        # ---------------------------------------------------------- 7. 备份
        demo.head("7. SQLite 一致性备份（在线备份 API + 重开验证）")
        backup = backup_mod.backup_sqlite(layout.db_path, layout.backups_dir, retention=5, label="demo")
        demo.check("备份已生成", not backup["skipped"] and pathlib.Path(backup["path"]).is_file(),
                   pathlib.Path(backup["path"]).name if not backup.get("skipped") else "")
        verification = backup["verification"]
        demo.check("备份可重开且完整性 ok", verification["ok"],
                   f"integrity={verification['integrity']} tables={verification['table_count']} "
                   f"schema=V{verification['schema_version']}")
        demo.equal("备份内 schema 版本仍是 V1", verification["schema_version"], 1)
        demo.check("备份文件不在 Git 工作树内（无泄漏风险）",
                   not str(layout.backups_dir).startswith(str(REPO_ROOT)))

        # ---------------------------------------------------------- 8. upgrade
        demo.head("8. upgrade（真实健康闸门，v1 → v2）")
        stop_service(demo, service_manager)
        demo.managers.clear()

        d1 = deploy_mod.Deployer(deploy_mod.DeployOptions(
            layout=layout, source_dir=REPO_ROOT, release_id="v2",
            python=sys.executable, no_venv=True, service_manager="process",
            health_timeout=25.0, health_interval=0.5,
        ))
        up1 = d1.upgrade()
        demo.equal("upgrade 状态", up1["status"], "OK")
        demo.equal("upgrade 退出码", up1["exit_code"], 0)
        demo.check("升级后指针指向 v2",
                   layout.current_pointer.read_text(encoding="utf-8").strip() == "v2")
        demo.check("unit 的 PYTHONPATH 已切到 releases/v2",
                   f"releases/v2" in layout.unit_path.read_text(encoding="utf-8"))
        demo.note("升级前已自动备份数据库（见 steps 里的 backup 步）")
        demo.check("升级后服务真的健康",
                   (up1.get("health") or {}).get("ok") is True,
                   f"status={(up1.get('health') or {}).get('health_status')}")
        stop_service(demo, d1.service)

        # ---------------------------------------------------------- 9. rollback
        demo.head("9. 显式 rollback（v2 → v1，只回代码）")
        d2 = deploy_mod.Deployer(deploy_mod.DeployOptions(
            layout=layout, source_dir=REPO_ROOT,
            python=sys.executable, no_venv=True, service_manager="process",
            health_timeout=25.0, health_interval=0.5,
        ))
        rb = d2.rollback()
        demo.equal("rollback 状态", rb["status"], "ROLLED_BACK")
        demo.check("回滚后指针回到 v1",
                   layout.current_pointer.read_text(encoding="utf-8").strip() == "v1")
        demo.check("回滚后服务真的健康", (rb.get("health") or {}).get("ok") is True)
        stop_service(demo, d2.service)

        # ---------------------------------------------------- 10. 健康失败 → 自动回滚
        demo.head("10. 注入坏 release（新版本起不来）→ 健康闸门失败 → 自动回滚")
        broken_src = make_broken_source(root / "broken-src")
        demo.note("坏 release = 源码副本 + liptv/__init__.py 里注入致命 raise（模拟新版本有缺陷）")
        d3 = deploy_mod.Deployer(deploy_mod.DeployOptions(
            layout=layout, source_dir=broken_src, release_id="v3-broken",
            python=sys.executable, no_venv=True, service_manager="process",
            health_timeout=8.0, health_interval=0.5,
        ))
        fail = d3.upgrade()
        demo.equal("健康闸门把这次升级判为失败（exit 3 = EXIT_HEALTH）", fail["exit_code"], 3)
        demo.equal("自动回滚已执行", fail["status"], "ROLLED_BACK")
        demo.check("回滚目标 = 上一个好 release",
                   (fail.get("rollback") if isinstance(fail.get("rollback"), dict) else None) is None
                   and any(s["step"] == "rollback" and s["target"] == "v1" for s in fail["steps"]),
                   "target=v1")
        demo.check("回滚后服务恢复健康",
                   (fail.get("health_after_rollback") or {}).get("ok") is True,
                   f"status={(fail.get('health_after_rollback') or {}).get('health_status')}")
        demo.equal("当前指针已回到 v1",
                   layout.current_pointer.read_text(encoding="utf-8").strip(), "v1")
        stop_service(demo, d3.service)

        # ---------------------------------------------------- 11. 数据未被改动
        demo.head("11. 部署动作没有碰数据（逐字节对比）")
        demo.check("数据库仍然存在", layout.db_path.is_file())
        demo.check("/live.m3u 仍然存在且非空",
                   layout.output_path.is_file() and layout.output_path.stat().st_size > 0)
        db_sha1 = sha256_file(layout.db_path) if layout.db_path.is_file() else "(数据库缺失)"
        m3u_sha1 = sha256_file(layout.output_path) if layout.output_path.is_file() else "(成品缺失)"
        demo.check("数据库 checksum 不变（升级/回滚从不写库）",
                   db_sha1 == db_sha0, f"{db_sha1[:16]}… == {db_sha0[:16]}…")
        demo.check("/live.m3u checksum 不变（升级/回滚从不改成品）",
                   m3u_sha1 == m3u_sha0, f"{m3u_sha1[:16]}… == {m3u_sha0[:16]}…")
        backups = backup_mod.list_backups(layout.backups_dir)
        demo.check("备份仍在（升级前的自动备份 + 演示备份）", len(backups) >= 2,
                   f"count={len(backups)}")
        demo.note("恢复数据库是**显式**动作（restore-db --yes）；任何回滚都不会自动覆盖数据库")

        # ------------------------------------------- 11b. restore-db 停机门禁
        demo.head("11b. restore-db 停机门禁（QA-006B：服务在跑就拒绝恢复）")
        gate_opts = deploy_mod.DeployOptions(
            layout=layout, source_dir=REPO_ROOT, python=sys.executable,
            no_venv=True, service_manager="systemd", systemctl=str(FAKE_SYSTEMCTL),
        )
        os.environ["FAKE_SYSTEMCTL_MUTABLE"] = "1"
        fake_state.write_text(json.dumps({"active": "active", "enabled": "enabled"}),
                              encoding="utf-8", newline="\n")
        os.environ["FAKE_SYSTEMCTL_FAIL"] = "stop"       # 注入「停机失败」
        db_before_gate = sha256_file(layout.db_path)
        blocked = deploy_mod.Deployer(gate_opts).restore_db(
            pathlib.Path(backup["path"]), yes=True)
        demo.equal("服务在跑且停不下来 ⇒ 拒绝恢复", blocked["status"], "BLOCKED_SERVICE_RUNNING")
        demo.equal("拒绝时退出码 = 2（EXIT_PREFLIGHT）", blocked["exit_code"], 2)
        demo.check("拒绝时数据库字节未变", sha256_file(layout.db_path) == db_before_gate)
        demo.check("拒绝时没有产生任何恢复动作", blocked.get("restore") is None)
        os.environ.pop("FAKE_SYSTEMCTL_FAIL", None)

        fake_log.unlink(missing_ok=True)                 # 只统计这一次的调用顺序
        allowed = deploy_mod.Deployer(gate_opts).restore_db(
            pathlib.Path(backup["path"]), yes=True)
        gate_actions = [json.loads(ln)["action"] for ln in
                        fake_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        demo.equal("停机门禁通过后恢复成功", allowed["status"], "OK")
        demo.check("恢复前已自动停机并复查为 inactive",
                   allowed["service_gate"]["stopped"] is True
                   and allowed["service_gate"]["after"]["active"] == "inactive",
                   f"actions={gate_actions}")
        demo.check("恢复后不自动重启（保守口径，待人确认后再手工 start）",
                   "start" not in gate_actions)
        demo.check("pre-restore 安全副本走 SQLite 一致性方式",
                   allowed["restore"]["safety_copy_method"] == "sqlite-backup-api")
        demo.check("恢复后数据库可重开、schema 仍为 V1",
                   backup_mod.verify_backup(layout.db_path).ok
                   and backup_mod.verify_backup(layout.db_path).schema_version == 1)
        collected.append(json.dumps(allowed, ensure_ascii=False, default=str))
        os.environ.pop("FAKE_SYSTEMCTL_MUTABLE", None)
        demo.note("--force-offline-restore 是唯一 break-glass 开关：只表示「调用方声明服务已停」，"
                  "与 --yes（只确认覆盖数据库）是两次独立确认")

        # ---------------------------------------------------- 12. 无 token
        demo.head("12. 产物与输出不含带 query 的 URL / 凭据")
        service_log = layout.service_log_path
        if service_log.is_file():
            collected.append(service_log.read_text(encoding="utf-8", errors="replace"))
        collected.append(json.dumps(install, ensure_ascii=False, default=str))
        collected.append(json.dumps(up1, ensure_ascii=False, default=str))
        collected.append(json.dumps(fail, ensure_ascii=False, default=str))
        problem_lines: list[str] = []
        for index, text in enumerate(collected):
            problem_lines.extend(f"#{index}: {hit}" for hit in scan_secrets(text))
        demo.check("渲染产物 / 部署输出 / 服务日志均无泄漏", not problem_lines,
                   "; ".join(problem_lines[:3]))

        # ---------------------------------------------------- 13. 观察
        demo.head("13. 关键观察（供人工复核）")
        st = deploy_mod.Deployer(dataclasses.replace(install_opts, service_manager="none")).status(
            probe_health=False)
        print(f"       · releases          : {st['releases']}")
        print(f"       · release 指针      : {layout.current_pointer.read_text(encoding='utf-8').strip()}")
        print(f"       · deploy-state 事件 : "
              f"{[e.get('action') for e in st['state'].get('events') or []]}")
        print(f"       · 备份              : {[pathlib.Path(p).name for p in st['backups']]}")
        v3 = layout.release_dir("v3-broken")
        demo.note(f"坏 release 目录保留在原处（{v3.name}）——真实运维里由人工清理，"
                  f"rollback 不会删它")

    # -------------------------------------------------------------- 汇总
    print("\n" + "=" * 72)
    print(f"演示结论：{'全部通过' if demo.failures == 0 else f'{demo.failures} 项失败'}"
          f"（共 {len(demo.results)} 项断言）")
    if demo.failures:
        for item in demo.results:
            if not item["ok"]:
                print(f"  FAIL  [{item['section']}] {item['label']}   {item['detail']}")
    print("=" * 72)
    return 0 if demo.failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TASK-006 单机 Linux 部署离线演示")
    parser.add_argument("--keep", action="store_true", help="保留临时前缀（默认删除）")
    parser.add_argument("--root", help="显式安装前缀（默认 = 系统临时目录下的新目录）")
    args = parser.parse_args(argv)

    demo = Demo(keep=args.keep, explicit_root=args.root)
    try:
        return run(demo)
    except Exception as exc:  # noqa: BLE001 - 演示脚本：任何意外都要说清楚
        print(f"\n演示异常中止：{type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        return 1
    finally:
        demo.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
