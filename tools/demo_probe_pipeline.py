"""TASK-005 真实固定频道 ffprobe 测活「纯离线」端到端演示。

一条命令跑完全部场景，**不访问任何公网地址、不请求任何真实播放地址**
（探测目标是本机 mock HTTP 服务上的假地址，真正被执行的程序是本地 fake ffprobe）：

    python tools/demo_probe_pipeline.py

十项（对应 TASKS/TASK-005.md §14）：

  1. fake ffprobe capability check（只查可执行文件与版本，不请求任何 stream）；
  2. 两条固定 stream（同一 canonical）；动态赛事源同时启用，但它**绝不**进库存；
  3. 第一轮真实测活：线路 A 成功、线路 B 因 DNS 失败（真子进程 + 真错误分类）；
  4. probe_result 真的写库（字段映射 / 不可靠字段留 NULL / 历史只追加）；
  5. selector 选中健康线路（算法未改，只是喂了真数据）；
  6. 下一轮**同一批 stream** 结果反转 → selector 改选；连输 3 次触发硬阈值；
  7. scheduler 的 fetch → sync → probe → publish 顺序，且本轮测活影响同轮发布；
  8. [probe] enabled = false 时 0 个子进程，发布产物与 TASK-004 逐字节一致；
  9. ffprobe 缺失 = 环境级故障：0 条 probe_result、不批量写失败、整轮不报 OK；
 10. 隐私：URL 一律脱敏（CLI 文本 / --json / 状态文件都不含 token / path / query）。

产物全部落在 out/demo-task005/（已被 .gitignore 忽略）。
"""

from __future__ import annotations

import contextlib
import functools
import io
import json
import os
import pathlib
import shutil
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.mock_source_server import RunningMock  # noqa: E402

from liptv import __version__  # noqa: E402
from liptv import config as config_mod  # noqa: E402
from liptv import db as db_mod  # noqa: E402
from liptv import fetch as fetch_mod  # noqa: E402
from liptv import ingest as ingest_mod  # noqa: E402
from liptv import probe as probe_mod  # noqa: E402
from liptv import publish as publish_mod  # noqa: E402
from liptv import repo  # noqa: E402
from liptv import runtime as runtime_mod  # noqa: E402
from liptv import select as select_mod  # noqa: E402
from liptv.cli import main as cli_main  # noqa: E402
from liptv.util import utcnow_iso  # noqa: E402

WORK_DIR = REPO_ROOT / "out" / "demo-task005"
FAKE_FFPROBE = REPO_ROOT / "tools" / "fake_ffprobe.py"

#: 刻意放进两条线路 URL 的短时签名材料；任何输出里都不许出现
TOKEN = "SECRET_TOKEN_9f3a"
LINE_A = "line-a"
LINE_B = "line-b"
CANONICAL_NAME = "演示新闻台"

_results: list[tuple[bool, str, str]] = []


# ------------------------------------------------------------------ 小工具

def run(*argv) -> tuple[int, object]:
    """调用 CLI 并解析输出（``--json`` 时返回 dict）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main([str(a) for a in argv])
    text = buf.getvalue()
    try:
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, text


def check(label: str, ok: bool, detail: str) -> None:
    _results.append((ok, label, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


class FakeClock:
    """假时钟：``sleep`` 只把时间往前推，不真的等待。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += float(seconds)
        self.now += float(seconds)


class SubprocessSpy:
    """统计**真实**子进程调用（包住 :func:`liptv.probe.run_process`）。"""

    def __init__(self) -> None:
        self._original = probe_mod.run_process
        self.version_calls = 0
        self.stream_calls: list[str] = []

    def __enter__(self) -> "SubprocessSpy":
        probe_mod.run_process = self._wrapped
        return self

    def __exit__(self, *_exc) -> bool:
        probe_mod.run_process = self._original
        return False

    def _wrapped(self, argv, **kwargs):
        target = argv[-1] if argv else ""
        if "-version" in argv:
            self.version_calls += 1
        elif target.startswith(("http://", "https://")):
            self.stream_calls.append(target)
        return self._original(argv, **kwargs)

    def reset(self) -> None:
        self.version_calls = 0
        self.stream_calls.clear()


@contextlib.contextmanager
def record_order(events: list[str]):
    """记录 ``execute_round`` 四阶段的**真实**调用顺序（包住模块级函数）。"""

    def wrap(inner, label: str):
        @functools.wraps(inner)
        def wrapper(*args, **kwargs):
            events.append(label)
            return inner(*args, **kwargs)

        return wrapper

    patches = [
        (ingest_mod, "ingest_fixed_source", "fetch"),
        (repo, "sync_streams", "sync"),
        (probe_mod, "run_round", "probe"),
        (publish_mod, "publish", "publish"),
    ]
    originals = [(mod, name, getattr(mod, name)) for mod, name, _ in patches]
    for mod, name, label in patches:
        setattr(mod, name, wrap(getattr(mod, name), label))
    try:
        yield events
    finally:
        for mod, name, fn in originals:
            setattr(mod, name, fn)


def write_state(path: pathlib.Path, mapping: dict) -> None:
    """写 fake ffprobe 的「按 URL 覆盖模式」文件（模拟同一 URL 的健康变化）。"""
    path.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_config(path: pathlib.Path, *, db, live, summary, status, lock, tmp, base,
                 ffprobe: list[str], probe_enabled: bool) -> None:
    probe = [
        "[probe]",
        f"enabled = {'true' if probe_enabled else 'false'}",
        'name = "demo-probe"',
        'location = "Windows"',
        "ffprobe_path = [" + ", ".join(f'"{p}"' for p in ffprobe) + "]",
        "timeout_seconds = 15.0",
        "analyze_seconds = 1.0",
        "max_concurrency = 4",
        "per_round_limit = 0",
        "",
    ]
    path.write_text(
        "\n".join(
            [
                "[database]",
                f'path = "{db}"',
                "",
                "[output]",
                f'm3u_path = "{live}"',
                "keep_previous = true",
                "",
                "[fetch]",
                "timeout_seconds = 5.0",
                "max_bytes = 2000000",
                "max_redirects = 3",
                f'dynamic_tmp_dir = "{tmp}"',
                "",
                "[publish]",
                f'summary_path = "{summary}"',
                "",
                "[runtime]",
                "interval_seconds = 10800",
                "run_on_start = true",
                f'lock_path = "{lock}"',
                f'status_path = "{status}"',
                "stale_after_seconds = 21600",
                "status_history = 5",
                "include_dynamic = false",
                "",
                "[server]",
                "enabled = false",
                'host = "127.0.0.1"',
                "port = 0",
                'playlist_path = "/live.m3u"',
                'health_path = "/healthz"',
                "",
                *probe,
                "[[sources]]",
                'name = "demo-fixed"',
                'kind = "fixed_m3u"',
                f'url = "{base}/seq.m3u"',
                "enabled = true",
                "",
                "[[sources]]",
                'name = "demo-dynamic"',
                'kind = "dynamic_event_m3u"',
                f'url = "{base}/dynamic-publish.m3u"',
                "enabled = true",
                "",
            ]
        ),
        encoding="utf-8",
    )


# ------------------------------------------------------------------ 主流程

def main() -> int:  # noqa: PLR0915 - 演示脚本刻意线性展开，便于逐段阅读
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    (WORK_DIR / "tmp").mkdir(parents=True, exist_ok=True)
    out_dir = WORK_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = WORK_DIR / "config.toml"
    cfg_off = WORK_DIR / "config-probe-off.toml"
    cfg_missing = WORK_DIR / "config-ffprobe-missing.toml"
    state = WORK_DIR / "ffprobe-state.json"
    db = WORK_DIR / "liptv.sqlite3"
    live = out_dir / "live.m3u"
    status = out_dir / "runtime-status.json"
    lock = out_dir / "liptv.lock"
    summary = out_dir / "publish-summary.json"
    tmp = (WORK_DIR / "tmp").as_posix()
    python = pathlib.Path(sys.executable).as_posix()
    fake = FAKE_FFPROBE.as_posix()
    seed_now = utcnow_iso()

    def fresh_conn():
        return db_mod.connect(db)

    def probe_state() -> tuple[int, object]:
        conn = fresh_conn()
        try:
            total = int(conn.execute("SELECT COUNT(*) FROM probe_result").fetchone()[0])
            row = conn.execute(
                "SELECT last_seen_at FROM probe WHERE name = ?", ("demo-probe",)
            ).fetchone()
            return total, (row["last_seen_at"] if row else None)
        finally:
            conn.close()

    spy = SubprocessSpy()
    try:
        with RunningMock() as (mock, base):
            url_a = f"{base}/live/{LINE_A}/pc.m3u8?token={TOKEN}"
            url_b = f"{base}/live/{LINE_B}/raw.m3u8?token={TOKEN}"
            mock.state.content = (
                "#EXTM3U\n"
                '#EXTINF:-1 tvg-id="mock-news.cn" tvg-name="Mock News" '
                f'group-title="新闻",{CANONICAL_NAME} · 线路A\n{url_a}\n'
                '#EXTINF:-1 tvg-id="mock-news.cn" tvg-name="Mock News" '
                f'group-title="新闻",{CANONICAL_NAME} · 线路B\n{url_b}\n'
            )

            write_config(cfg, db=db.as_posix(), live=live.as_posix(),
                         summary=summary.as_posix(), status=status.as_posix(),
                         lock=lock.as_posix(), tmp=tmp, base=base,
                         ffprobe=[python, fake], probe_enabled=True)
            write_config(cfg_off, db=db.as_posix(), live=(out_dir / "live-off.m3u").as_posix(),
                         summary=(out_dir / "off-summary.json").as_posix(),
                         status=(out_dir / "off-status.json").as_posix(),
                         lock=(out_dir / "off.lock").as_posix(), tmp=tmp, base=base,
                         ffprobe=[python, fake], probe_enabled=False)
            write_config(cfg_missing,
                         db=db.as_posix(), live=(out_dir / "live-missing.m3u").as_posix(),
                         summary=(out_dir / "missing-summary.json").as_posix(),
                         status=(out_dir / "missing-status.json").as_posix(),
                         lock=(out_dir / "missing.lock").as_posix(), tmp=tmp, base=base,
                         ffprobe=["definitely-not-ffprobe-xyz"], probe_enabled=True)

            print("liptv TASK-005 测活管线演示（纯离线）")
            print(f"mock server : {base}")
            print(f"工作目录    : {WORK_DIR}")

            # ------------------------------------------------------ 0
            section("0. 准备：同一频道两条固定线路 + 一条动态赛事源（都落本机 mock）")
            code, payload = run("init-db", "--config", cfg, "--json")
            check("init-db", code == 0 and payload["schema_version"] == 1, f"exit={code}")
            code, payload = run("source-register", "--from-config", "--config", cfg, "--json")
            check(
                "source-register（固定源 + 动态源均启用）",
                code == 0 and payload["registered"] == 2 and payload["enabled_count"] == 2,
                f"registered={payload['registered']} enabled={payload['enabled_count']}",
            )
            code, payload = run("fetch", "--all", "--config", cfg, "--json")
            check(
                "fetch --all（只抓 fixed 源；动态赛事条目**不落库存**）",
                code == 0 and payload["requested"] == 1 and payload["total_created"] == 2,
                f"exit={code} requested={payload['requested']} "
                f"created={payload['total_created']}",
            )

            # canonical/binding 属于「人工决策」，脚本直接写库（runtime 自己绝不创建它们）
            conn = fresh_conn()
            try:
                fixed_src = repo.get_source_by_name(conn, "demo-fixed")
                channels = [
                    c for c in repo.list_source_channels(conn, int(fixed_src["id"]))
                    if int(c["active"])
                ]
                canonical_id = repo.add_canonical_channel(
                    conn, CANONICAL_NAME, category="新闻", now=seed_now
                )
                for channel in channels:
                    repo.bind_source_channel(conn, int(channel["id"]), canonical_id, now=seed_now)
                repo.sync_streams(conn, now=seed_now)
                conn.commit()
                by_url = {
                    row["url"]: int(row["id"])
                    for row in repo.list_streams(conn, canonical_id)
                }
            finally:
                conn.close()
            id_a, id_b = by_url[url_a], by_url[url_b]
            check(
                "同一 canonical 下 2 条 stream（线路A / 线路B）",
                len(channels) == 2 and len(by_url) == 2 and id_a != id_b,
                f"canonical_id={canonical_id} streams={sorted(by_url.values())}",
            )

            # ------------------------------------------------------ 1
            section("1. fake ffprobe capability check（只查可执行文件与版本，不请求 stream）")
            spy.reset()
            with spy:
                code, payload = run("probe-check", "--config", cfg, "--json")
            check(
                "probe-check 成功（拿到版本行）",
                code == 0 and payload["ok"] is True
                and "ffprobe version" in (payload["version"] or ""),
                f"exit={code} version={((payload['version'] or '').splitlines() or [''])[0][:46]}",
            )
            check(
                "capability check 期间 0 次「真探测」（只跑了 -version）",
                spy.version_calls >= 1 and spy.stream_calls == [],
                f"version_calls={spy.version_calls} stream_calls={len(spy.stream_calls)}",
            )

            # ------------------------------------------------------ 2
            section("2. 测活目标 = 只测固定库存；动态赛事源绝不进 stream / probe_result")
            conn = fresh_conn()
            try:
                cands = repo.list_probe_candidates(conn)
                dynamic_links = conn.execute(
                    "SELECT COUNT(*) FROM stream_source ss "
                    "JOIN source_channel sc ON sc.id = ss.source_channel_id "
                    "JOIN source src ON src.id = sc.source_id WHERE src.kind = ?",
                    (ingest_mod.KIND_DYNAMIC,),
                ).fetchone()[0]
                signed_streams = conn.execute(
                    "SELECT COUNT(*) FROM stream WHERE url LIKE '%txSecret%'"
                ).fetchone()[0]
            finally:
                conn.close()
            check(
                "候选 = 2 条固定线路，且都带 canonical 名",
                sorted(int(r["id"]) for r in cands) == sorted([id_a, id_b])
                and all(r["canonical_name"] == CANONICAL_NAME for r in cands),
                f"stream_ids={sorted(int(r['id']) for r in cands)}",
            )
            check(
                "动态来源既没有 stream，也没有 stream_source 链接",
                int(dynamic_links) == 0 and int(signed_streams) == 0,
                f"dynamic_links={dynamic_links} signed_streams={signed_streams}",
            )

            # ------------------------------------------------------ 3
            section("3. 第一轮真实测活：线路A 成功、线路B DNS 失败（真子进程 + 真分类）")
            write_state(state, {url_a: "ok", url_b: "dns"})
            os.environ["FAKE_FFPROBE_STATE"] = str(state)
            spy.reset()
            with spy:
                code, payload = run("probe-run", "--config", cfg, "--json")
            check(
                "probe-run：requested=2 / succeeded=1 / failed=1 / written=2（stage=degraded）",
                code == 0 and payload["stage"] == probe_mod.STAGE_DEGRADED
                and payload["requested"] == 2 and payload["succeeded"] == 1
                and payload["failed"] == 1 and payload["written"] == 2,
                f"exit={code} stage={payload['stage']} requested={payload['requested']} "
                f"succeeded={payload['succeeded']} failed={payload['failed']} "
                f"written={payload['written']}",
            )
            check(
                "失败分类落到 DNS_ERROR（不是「一锅端」的 UNKNOWN）",
                payload["error_counts"] == {probe_mod.ERROR_DNS: 1},
                f"error_counts={payload['error_counts']}",
            )
            check(
                "两条流各跑了一次真实子进程（argv 直传，shell=False）",
                len(spy.stream_calls) == 2 and payload["ffprobe"]["ok"] is True,
                f"stream_calls={len(spy.stream_calls)}",
            )

            # ------------------------------------------------------ 4
            section("4. probe_result 真的落库（字段映射 / 不可靠字段留 NULL / 历史只追加）")
            conn = fresh_conn()
            try:
                rows = {int(r["stream_id"]): r for r in repo.list_probe_results(conn)}
                probes = repo.list_probes(conn)
            finally:
                conn.close()
            row_a, row_b = rows[id_a], rows[id_b]
            check(
                "线路A：success=1，真实分辨率 / 码率 / 协议入库，startup_ms 是实测耗时",
                bool(row_a["success"]) and row_a["resolution_width"] == 1920
                and row_a["resolution_height"] == 1080 and row_a["bitrate_kbps"] == 6128
                and row_a["protocol"] == "http" and isinstance(row_a["startup_ms"], int)
                and row_a["startup_ms"] >= 0,
                f"startup_ms={row_a['startup_ms']} "
                f"res={row_a['resolution_width']}x{row_a['resolution_height']} "
                f"bitrate={row_a['bitrate_kbps']} protocol={row_a['protocol']}",
            )
            check(
                "线路A：不可靠字段一律 NULL（ipv_family / http_status / connect_ms）",
                row_a["ipv_family"] is None and row_a["http_status"] is None
                and row_a["connect_ms"] is None,
                "ipv_family / http_status / connect_ms 全部 NULL（不猜）",
            )
            check(
                "线路B：error_type=DNS_ERROR，且**没有任何**伪造的测量值",
                not bool(row_b["success"]) and row_b["error_type"] == probe_mod.ERROR_DNS
                and all(
                    row_b[f] is None
                    for f in ("startup_ms", "resolution_width", "resolution_height",
                              "bitrate_kbps", "protocol")
                ),
                f"error_type={row_b['error_type']} startup_ms={row_b['startup_ms']} "
                f"res={row_b['resolution_width']} bitrate={row_b['bitrate_kbps']}",
            )
            check(
                "probe.last_seen_at 在**真实落库成功后**推进",
                any(p["name"] == "demo-probe" and p["last_seen_at"] for p in probes),
                f"probes={[p['name'] for p in probes]}",
            )

            # ------------------------------------------------------ 5
            section("5. selector 选中健康线路（算法未改，只是喂了真数据）")
            conn = fresh_conn()
            try:
                best = select_mod.select_best_stream(conn, canonical_id)
                scores = {s.stream_id: s for s in select_mod.score_streams(conn, canonical_id)}
            finally:
                conn.close()
            check(
                "select_best_stream → 线路A",
                best is not None and best.stream_id == id_a,
                f"best={None if best is None else best.stream_id} "
                f"url={probe_mod.redact_stream_url(best.url) if best else None}",
            )
            check(
                "线路B 因「窗口内成功次数 0 < 阈值 1」不可用（TASK-001 既有规则）",
                scores[id_b].eligible is False and "成功次数" in (scores[id_b].reason or ""),
                f"eligible={scores[id_b].eligible} reason={scores[id_b].reason}",
            )

            # ------------------------------------------------------ 6
            section("6. 下一轮**同一批 stream** 结果反转 → 改选；连输 3 次触发硬阈值")
            write_state(state, {url_a: "dns", url_b: "ok"})
            code, payload = run("probe-run", "--config", cfg, "--json")
            conn = fresh_conn()
            try:
                best2 = select_mod.select_best_stream(conn, canonical_id)
            finally:
                conn.close()
            check(
                "URL 未变、只有健康反转：selector 改选线路B",
                code == 0 and payload["succeeded"] == 1
                and best2 is not None and best2.stream_id == id_b,
                f"exit={code} succeeded={payload['succeeded']} "
                f"best={None if best2 is None else best2.stream_id}",
            )
            for _ in range(2):  # 合计连输 3 次
                run("probe-run", "--config", cfg, "--json")
            conn = fresh_conn()
            try:
                scores3 = {s.stream_id: s for s in select_mod.score_streams(conn, canonical_id)}
                best3 = select_mod.select_best_stream(conn, canonical_id)
                total_rows = len(repo.list_probe_results(conn))
                rows_a = len(repo.list_probe_results(conn, stream_id=id_a))
                rows_b = len(repo.list_probe_results(conn, stream_id=id_b))
            finally:
                conn.close()
            check(
                "线路A 连续失败 3 次 → 触发连续失败硬阈值，不再可用",
                scores3[id_a].consecutive_failures == 3 and scores3[id_a].eligible is False
                and "连续失败" in (scores3[id_a].reason or ""),
                f"consecutive={scores3[id_a].consecutive_failures} "
                f"eligible={scores3[id_a].eligible} reason={scores3[id_a].reason}",
            )
            check(
                "历史只追加：每条流每轮最多 1 条（4 轮 × 2 条 = 8 条）",
                total_rows == 8 and rows_a == 4 and rows_b == 4,
                f"total={total_rows} A={rows_a} B={rows_b}",
            )
            check(
                "反转后最佳线路 = B（A 已因连续失败出局）",
                best3 is not None and best3.stream_id == id_b,
                f"best={None if best3 is None else best3.stream_id}",
            )

            # ------------------------------------------------------ 7
            section("7. scheduler：fetch → sync → probe → publish，且本轮测活影响同轮发布")
            loaded = config_mod.load_config(cfg)
            runtime_settings = runtime_mod.RuntimeSettings.from_mapping(
                config_mod.runtime_settings(loaded)
            )
            group_order = config_mod.category_order(loaded)
            pub_cfg = config_mod.publish_settings(loaded)
            limits = fetch_mod.FetchLimits.from_mapping(config_mod.fetch_settings(loaded))
            enabled_probe = probe_mod.ProbeSettings.from_mapping(
                config_mod.probe_settings(loaded)
            )
            events: list[str] = []

            def round_fn(*, round_id):  # noqa: ARG001 - 演示不需要 round_id
                events.clear()
                conn = fresh_conn()
                try:
                    return runtime_mod.execute_round(
                        conn,
                        output_path=live,
                        group_order=group_order,
                        include_dynamic=True,
                        dynamic_tokens=("demo-dynamic",),
                        dynamic_filters=pub_cfg.get("dynamic"),
                        dynamic_group_title=publish_mod.DEFAULT_DYNAMIC_GROUP_TITLE,
                        limits=limits,
                        summary_path=pub_cfg["summary_path"],
                        probe_settings=probe_mod.ProbeSettings.from_mapping(
                            config_mod.probe_settings(loaded)
                        ),
                    )
                finally:
                    conn.close()

            clock = FakeClock()
            scheduler = runtime_mod.Scheduler(
                settings=runtime_settings,
                round_fn=round_fn,
                clock=clock.monotonic,
                sleep=clock.sleep,
                status_store=runtime_mod.StatusStore(
                    status, max_rounds=runtime_settings.status_history, version=__version__
                ),
            )
            write_state(state, {url_a: "dns", url_b: "ok"})  # 保持 A 坏 B 好
            with record_order(events):
                loop_result = scheduler.run(max_rounds=1)
            check(
                "阶段顺序 = fetch → sync → probe → publish",
                events == ["fetch", "sync", "probe", "publish"],
                f"events={events} rounds={loop_result['rounds']}",
            )
            entry = json.loads(status.read_text(encoding="utf-8"))["rounds"][-1]
            check(
                "状态文件带 probe 子摘要（同一轮只测了 2 条固定流）",
                entry["probe"]["stage"] == probe_mod.STAGE_DEGRADED
                and entry["probe"]["requested"] == 2 and entry["probe"]["written"] == 2,
                f"probe={json.dumps(entry['probe'], ensure_ascii=False)}",
            )
            playlist = live.read_text(encoding="utf-8")
            check(
                "同轮生效：本轮发布的固定频道用的是**本轮**测活选出的线路B",
                url_b in playlist and url_a not in playlist
                and entry["publish"]["fixed_count"] == 1,
                f"fixed_count={entry['publish']['fixed_count']} status={entry['publish']['status']}",
            )
            check(
                "动态赛事条目照常临时发布，但从不被测活（requested 仍是 2）",
                entry["publish"]["dynamic_count"] >= 1
                and entry["publish"]["status"] == publish_mod.STATUS_OK
                and entry["probe"]["requested"] == 2,
                f"dynamic_count={entry['publish']['dynamic_count']} "
                f"probe_requested={entry['probe']['requested']}",
            )

            # ------------------------------------------------------ 8
            section("8. [probe] enabled = false → 0 个子进程，发布产物与 TASK-004 逐字节一致")
            off_probe = probe_mod.ProbeSettings.from_mapping(
                config_mod.probe_settings(config_mod.load_config(cfg_off))
            )
            rows_before, seen_before = probe_state()
            live_off = out_dir / "live-off.m3u"
            live_none = out_dir / "live-none.m3u"
            spy.reset()
            with spy:
                conn = fresh_conn()
                try:
                    entry_off = runtime_mod.execute_round(
                        conn, output_path=live_off, group_order=group_order, limits=limits,
                        probe_settings=off_probe, stamp=seed_now,
                    )
                finally:
                    conn.close()
                conn = fresh_conn()
                try:
                    entry_none = runtime_mod.execute_round(
                        conn, output_path=live_none, group_order=group_order, limits=limits,
                        probe_settings=None, stamp=seed_now,
                    )
                finally:
                    conn.close()
            check(
                "[probe] disabled：整轮 0 次 ffprobe（含 0 次 capability check）",
                spy.version_calls == 0 and spy.stream_calls == [],
                f"version_calls={spy.version_calls} stream_calls={len(spy.stream_calls)}",
            )
            check(
                "probe 子摘要 = disabled（enabled=False / 计数全 0 / probe_id=None）",
                entry_off["probe_stage"] == probe_mod.STAGE_DISABLED
                and entry_off["probe"]["enabled"] is False
                and entry_off["probe"]["written"] == 0
                and entry_off["probe"]["requested"] == 0
                and entry_off["probe"]["probe_id"] is None,
                f"probe={json.dumps(entry_off['probe'], ensure_ascii=False)}",
            )
            check(
                "disabled 与「不传 probe_settings」的发布产物逐字节一致（TASK-004 行为原样）",
                live_off.read_bytes() == live_none.read_bytes()
                and entry_off["outcome"] == entry_none["outcome"],
                f"bytes={len(live_off.read_bytes())} outcome={entry_off['outcome']}",
            )
            rows_off, seen_off = probe_state()
            check(
                "disabled 轮次不写 probe_result、也不推进 last_seen_at",
                rows_off == rows_before and seen_off == seen_before,
                f"rows {rows_before}→{rows_off} last_seen unchanged={seen_off == seen_before}",
            )

            # ------------------------------------------------------ 9
            section("9. ffprobe 缺失 = 环境级故障：0 条污染、不批量写失败、整轮不报 OK")
            rows_before, seen_before = probe_state()
            code, payload = run("probe-run", "--config", cfg_missing, "--json")
            check(
                "probe-run 明确失败：exit=1 / stage=failed / FFPROBE_NOT_FOUND",
                code == probe_mod.EXIT_ENVIRONMENT
                and payload["stage"] == probe_mod.STAGE_FAILED
                and payload["error_type"] == probe_mod.ERROR_FFPROBE_NOT_FOUND,
                f"exit={code} stage={payload['stage']} error_type={payload['error_type']}",
            )
            check(
                "**没有**把候选流批量写成失败：requested / failed / written 全 0，error_counts 为空",
                payload["requested"] == 0 and payload["failed"] == 0
                and payload["written"] == 0 and payload["succeeded"] == 0
                and payload["error_counts"] == {},
                f"requested={payload['requested']} failed={payload['failed']} "
                f"written={payload['written']} error_counts={payload['error_counts']}",
            )
            conn = fresh_conn()
            try:
                cand_count = len(repo.list_probe_candidates(conn))
            finally:
                conn.close()
            check(
                "（对照）库里确实有 2 条可测活固定线路，但它们一条都没被记成失败",
                cand_count == 2,
                f"candidates={cand_count} 但本轮 0 条 probe_result",
            )
            rows_after, seen_after = probe_state()
            check(
                "0 条 probe_result 写入、last_seen_at 不推进（历史未被污染）",
                rows_after == rows_before and seen_after == seen_before,
                f"rows {rows_before}→{rows_after} last_seen unchanged={seen_after == seen_before}",
            )
            code, payload = run("probe-check", "--config", cfg_missing, "--json")
            check(
                "probe-check 同样明确报环境故障（exit=1）",
                code == probe_mod.EXIT_ENVIRONMENT and payload["ok"] is False
                and payload["error_type"] == probe_mod.ERROR_FFPROBE_NOT_FOUND,
                f"exit={code} error_type={payload['error_type']}",
            )
            missing_probe = probe_mod.ProbeSettings.from_mapping(
                config_mod.probe_settings(config_mod.load_config(cfg_missing))
            )
            live_missing = out_dir / "live-missing.m3u"
            conn = fresh_conn()
            try:
                entry_missing = runtime_mod.execute_round(
                    conn, output_path=live_missing, group_order=group_order, limits=limits,
                    probe_settings=missing_probe, stamp=seed_now,
                )
            finally:
                conn.close()
            check(
                "整轮结论不得报 OK：outcome=degraded 且退出码非 0",
                entry_missing["probe_stage"] == probe_mod.STAGE_FAILED
                and entry_missing["outcome"] == "degraded"
                and runtime_mod.round_exit_code(entry_missing) != 0,
                f"probe_stage={entry_missing['probe_stage']} outcome={entry_missing['outcome']} "
                f"exit={runtime_mod.round_exit_code(entry_missing)}",
            )
            check(
                "但可以继续用**旧历史**发布（ffprobe 挂了不影响订阅可用性）",
                entry_missing["publish"]["status"] == publish_mod.STATUS_OK
                and url_b in live_missing.read_text(encoding="utf-8"),
                f"publish={entry_missing['publish']['status']} published="
                f"{entry_missing['publish']['published']}",
            )

            # ------------------------------------------------------ 10
            section("10. 隐私：URL 一律脱敏（CLI 文本 / --json / 状态文件都不含 token）")
            os.environ["FAKE_FFPROBE_STATE"] = str(state)
            code, payload = run("probe-run", "--config", cfg, "--json")
            blob = json.dumps(payload, ensure_ascii=False)
            check(
                "--json 输出不含 token / 不含路径段 / 不含查询串",
                TOKEN not in blob and LINE_A not in blob and LINE_B not in blob
                and "/pc.m3u8" not in blob and "?" not in blob,
                f"bytes={len(blob)} token={'在' if TOKEN in blob else '不在'}",
            )
            urls = [r.get("url") for r in payload["results"]]
            check(
                "结果里的 url 字段就是 scheme://host/...（无 path / query / userinfo）",
                urls and all(u and u.endswith("/...") for u in urls),
                f"urls={urls}",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli_main(["probe-run", "--config", str(cfg)])
            text = buf.getvalue()
            check(
                "人类可读输出同样脱敏（只显示 scheme://host/...）",
                TOKEN not in text and LINE_A not in text and LINE_B not in text
                and "?" not in text,
                f"chars={len(text)} token={'在' if TOKEN in text else '不在'}",
            )
            status_blob = status.read_text(encoding="utf-8")
            check(
                "runtime-status.json 不含 token / 不含线路标识 / 不含任何 URL",
                TOKEN not in status_blob and LINE_A not in status_blob
                and LINE_B not in status_blob and "http://" not in status_blob,
                f"bytes={len(status_blob)}",
            )
            summary_blob = summary.read_text(encoding="utf-8")
            check(
                "publish-summary.json 同样脱敏（无 token / 无查询串）",
                TOKEN not in summary_blob and "?" not in summary_blob
                and "/pc.m3u8" not in summary_blob,
                f"bytes={len(summary_blob)}",
            )
    finally:
        os.environ.pop("FAKE_FFPROBE_STATE", None)

    failed = [r for r in _results if not r[0]]
    print(f"\n===== 汇总：{len(_results) - len(failed)}/{len(_results)} 通过 =====")
    for _ok, label, detail in failed:
        print(f"  FAIL {label}: {detail}")
    print(f"session_start={seed_now}  wall_clock={utcnow_iso()}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
