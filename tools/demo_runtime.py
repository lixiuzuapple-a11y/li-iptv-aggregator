"""本地调度 + 只读 HTTP 订阅「纯离线」端到端演示（TASK-004）。

一条命令跑完全部场景，**不访问任何公网地址、不请求任何真实播放地址**：

    python tools/demo_runtime.py

流程（全部打本机 mock HTTP 服务；注入假 clock/sleep，因此不真实等待 3 小时）：

  0. mock 固定源 + mock 动态源；初始化数据库、注册来源、抓取、
     建立 canonical/binding 与**既有**的模拟测活历史；
  1. 三轮 fake scheduler（真的走 Scheduler.run 循环，只把 sleep 换成假实现）：
       第 1 轮 固定 + 动态统一发布（OK）；
       第 2 轮 多动态源有一个失败 → fail-closed 只发固定，**loop 不死**；
       第 3 轮 动态恢复 → 重新发布 OK；
  2. 单轮抛异常时 loop 仍然继续（异常被收敛进状态文件，下一轮照常发布）；
  3. 只读 HTTP：GET/HEAD `/live.m3u`、`GET /healthz`、缺文件 503、未知路径 404；
  4. 单实例锁：第二个 scheduler 被明确拒绝（退出码 3），且**没有**执行任何 fetch/publish；
  4b. 锁心跳与保守 release（QA-004A / QA-004B 返工）：长跑 240s（stale 只有 10s）心跳持续推进、
      他机观察仍判「不抢」、token 被替换后调度器停止且不再跑轮次、损坏/空/异 token 一律不删；
  5. 关闭后：锁释放、HTTP 地址不可再连接；
  6. 信息边界：状态文件与 /healthz 都不含任何 stream URL / 签名参数。

产物全部落在 out/demo-task004/（已被 .gitignore 忽略），没有真实公网地址。
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import pathlib
import shutil
import socket
import sys
import time
from datetime import timedelta

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.mock_source_server import RunningMock  # noqa: E402

from liptv import __version__  # noqa: E402
from liptv import config as config_mod  # noqa: E402
from liptv import db as db_mod  # noqa: E402
from liptv import fetch as fetch_mod  # noqa: E402
from liptv import publish as publish_mod  # noqa: E402
from liptv import repo  # noqa: E402
from liptv import runtime as runtime_mod  # noqa: E402
from liptv import server as server_mod  # noqa: E402
from liptv.cli import main as cli_main  # noqa: E402
from liptv.util import dt_to_iso, iso_to_dt, utcnow_iso  # noqa: E402

WORK_DIR = REPO_ROOT / "out" / "demo-task004"

# 演示用动态样本里刻意带上的短时签名材料；任何输出里都不许出现
SECRETS = ("txSecret", "txTime", "AAA111", "BBB222", "CCC333",
           "DEADBEEF1234567890", "CAFEBABE9876543210")

_results: list[tuple[bool, str, str]] = []


# ------------------------------------------------------------------ 小工具

def run(*argv) -> tuple[int, object]:
    """调用 CLI 并解析输出（--json 时返回 dict）。"""
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


class WallClock:
    """假墙钟（秒级 ISO）：**只有显式推进才前进**。

    用来观察锁心跳是否真的在刷新：调度器「休眠两小时」在演示里是瞬时的，
    而锁文件里的 ``heartbeat_at`` 仍严格跟着这个假钟走，因此可以断言「推进了多少」。
    """

    def __init__(self, start: str) -> None:
        self._dt = iso_to_dt(start)

    def advance(self, seconds: float) -> None:
        self._dt = self._dt + timedelta(seconds=float(seconds))

    def now_iso(self) -> str:
        return dt_to_iso(self._dt)

    def age_of(self, iso_text: str) -> float:
        return (self._dt - iso_to_dt(iso_text)).total_seconds()


def read_json(path):
    """读 JSON 文件；解析不出来返回 ``None``（与锁的读侧同口径）。"""
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_text(path, text: str) -> None:
    pathlib.Path(path).write_text(text, encoding="utf-8", newline="\n")


def http_request(service, method: str, path: str) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", service.port, timeout=10)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        body = response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body
    finally:
        conn.close()


# ------------------------------------------------------------------ 主流程

def main() -> int:  # noqa: PLR0915 - 演示脚本刻意线性展开，便于逐段阅读
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    (WORK_DIR / "tmp").mkdir(parents=True, exist_ok=True)
    out_dir = WORK_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = WORK_DIR / "config.toml"
    db = WORK_DIR / "liptv.sqlite3"
    live = out_dir / "live.m3u"
    status = out_dir / "runtime-status.json"
    lock = out_dir / "liptv.lock"
    summary = out_dir / "publish-summary.json"

    # 测活历史与选线窗口都用**真实当前时刻**：scheduler 走真实时间，
    # 若用固定过去/未来时刻会让「窗口内合格线路」判定失真。
    seed_now = utcnow_iso()

    with RunningMock() as (mock, base):
        cfg.write_text(
            "[database]\n"
            f'path = "{db.as_posix()}"\n'
            "\n[output]\n"
            f'm3u_path = "{live.as_posix()}"\n'
            "keep_previous = true\n"
            "\n[fetch]\n"
            "timeout_seconds = 5.0\n"
            "max_bytes = 2000000\n"
            "max_redirects = 3\n"
            f'dynamic_tmp_dir = "{(WORK_DIR / "tmp").as_posix()}"\n'
            "\n[publish]\n"
            f'summary_path = "{summary.as_posix()}"\n'
            "\n[runtime]\n"
            "interval_seconds = 10800\n"
            "run_on_start = true\n"
            f'lock_path = "{lock.as_posix()}"\n'
            f'status_path = "{status.as_posix()}"\n'
            "stale_after_seconds = 21600\n"
            "status_history = 5\n"
            "include_dynamic = false\n"
            "\n[server]\n"
            "enabled = false\n"
            'host = "127.0.0.1"\n'
            "port = 0\n"
            'playlist_path = "/live.m3u"\n'
            'health_path = "/healthz"\n'
            "\n[[sources]]\n"
            'name = "demo-fixed"\n'
            'kind = "fixed_m3u"\n'
            f'url = "{base}/seq.m3u"\n'
            "enabled = true\n"
            "\n[[sources]]\n"
            'name = "demo-dynamic"\n'
            'kind = "dynamic_event_m3u"\n'
            f'url = "{base}/dynamic-publish.m3u"\n'
            "enabled = false\n"
            "\n[[sources]]\n"
            'name = "demo-dynamic-bad"\n'
            'kind = "dynamic_event_m3u"\n'
            f'url = "{base}/error.m3u"\n'
            "enabled = false\n",
            encoding="utf-8",
        )

        print("liptv TASK-004 本地运行期演示（纯离线）")
        print(f"mock server : {base}")
        print(f"工作目录    : {WORK_DIR}")

        # ---------------------------------------------------------- 0
        section("0. 初始化 / 注册来源 / 抓取 / 归集 / 既有测活历史")
        mock.state.set_ok()
        code, payload = run("init-db", "--config", cfg, "--json")
        check("init-db", code == 0 and payload["schema_version"] == 1, f"exit={code}")
        code, payload = run("source-register", "--from-config", "--config", cfg, "--json")
        check(
            "source-register（动态源默认关闭）",
            code == 0 and payload["registered"] == 3 and payload["enabled_count"] == 1,
            f"registered={payload['registered']} enabled={payload['enabled_count']}",
        )
        code, payload = run("fetch", "--all", "--config", cfg, "--json")
        check(
            "fetch --all（只抓 enabled 的 fixed 源）",
            code == 0 and payload["total_created"] == 3,
            f"exit={code} requested={payload['requested']} created={payload['total_created']}",
        )

        # 下面这段是**人工决策与既有测活历史**的准备：脚本直接写库以免演示过长。
        # 注意 runtime 自己从不写 canonical/binding/probe —— 守住这两条边界
        # （不自动 canonicalization、不伪造 probe）正是 TASK-004 的验收要求。
        conn = db_mod.connect(db)
        canonical_ids = []
        for source_channel in repo.list_source_channels(conn):
            if not int(source_channel["active"]):
                continue
            cid = repo.add_canonical_channel(
                conn, source_channel["raw_name"],
                category=(source_channel["raw_group"] or "其他"), now=seed_now,
            )
            canonical_ids.append(cid)
            repo.bind_source_channel(conn, int(source_channel["id"]), cid, now=seed_now)
        repo.sync_streams(conn, now=seed_now)
        probe_id = repo.ensure_probe(conn, "demo-probe", now=seed_now)
        streams = repo.list_streams(conn)
        for stream in streams:
            repo.add_probe_result(
                conn, stream_id=int(stream["id"]), probe_id=probe_id,
                success=True, checked_at=seed_now, startup_ms=1200,
            )
        conn.commit()
        conn.close()
        check(
            "固定侧准备完成（3 canonical / 3 stream / 3 条模拟测活）",
            len(canonical_ids) == 3 and len(streams) == 3,
            f"canonical={len(canonical_ids)} stream={len(streams)}",
        )

        # ---------------------------------------------------------- 1
        section("1. 三轮 fake scheduler（走真实 Scheduler.run，只把 sleep 换成假实现）")
        loaded_cfg = config_mod.load_config(cfg)
        runtime_settings = runtime_mod.RuntimeSettings.from_mapping(
            config_mod.runtime_settings(loaded_cfg)
        )
        group_order = config_mod.category_order(loaded_cfg)
        pub_cfg = config_mod.publish_settings(loaded_cfg)
        limits = fetch_mod.FetchLimits.from_mapping(config_mod.fetch_settings(loaded_cfg))

        def read_rounds() -> list[dict]:
            return json.loads(status.read_text(encoding="utf-8"))["rounds"]

        def execute(dynamic_tokens) -> dict:
            conn = db_mod.connect(db)
            try:
                return runtime_mod.execute_round(
                    conn,
                    output_path=live,
                    group_order=group_order,
                    include_dynamic=True,
                    dynamic_tokens=dynamic_tokens,
                    dynamic_filters=pub_cfg.get("dynamic"),
                    dynamic_group_title=publish_mod.DEFAULT_DYNAMIC_GROUP_TITLE,
                    limits=limits,
                    summary_path=pub_cfg["summary_path"],
                )
            finally:
                conn.close()

        # 每轮用哪几个动态源：第 2 轮故意混进一个坏源，第 3 轮再摘掉
        plan: dict[str, int] = {"round": 0}
        captured: dict[str, str] = {}

        def round_fn(*, round_id: str) -> dict:  # noqa: ARG001 - 演示不需要 round_id
            plan["round"] += 1
            tokens = (
                ("demo-dynamic", "demo-dynamic-bad")
                if plan["round"] == 2
                else ("demo-dynamic",)
            )
            entry = execute(tokens)
            # 随手把「降级那一轮实际写出的文件」留存下来，供下面逐字节核对
            if entry["publish"]["status"] == publish_mod.STATUS_DEGRADED_FIXED_ONLY:
                captured["degraded_text"] = live.read_text(encoding="utf-8")
            return entry

        clock = FakeClock()
        scheduler = runtime_mod.Scheduler(
            settings=runtime_settings, round_fn=round_fn,
            clock=clock.monotonic, sleep=clock.sleep,
            status_store=runtime_mod.StatusStore(
                status, max_rounds=runtime_settings.status_history, version=__version__
            ),
        )
        loop_result = scheduler.run(max_rounds=3)
        rounds = read_rounds()

        check(
            "三轮全部跑完，且**没有**真实等待任何 interval",
            loop_result["rounds"] == 3 and loop_result["failed_rounds"] == 0
            and clock.slept == 2 * runtime_settings.interval_seconds,
            f"rounds={loop_result['rounds']} failed={loop_result['failed_rounds']} "
            f"fake_slept={clock.slept:.0f}s（真实耗时毫秒级）",
        )
        first, second, third = rounds[0], rounds[1], rounds[2]
        check(
            "第 1 轮：固定 + 动态统一发布",
            first["publish"]["status"] == publish_mod.STATUS_OK
            and first["publish"]["fixed_count"] == 3
            and first["publish"]["dynamic_count"] == 3
            and first["publish"]["published"] is True,
            f"status={first['publish']['status']} fixed={first['publish']['fixed_count']} "
            f"dynamic={first['publish']['dynamic_count']}",
        )
        check(
            "第 2 轮：动态部分失败 → fail-closed 只发固定（状态与计数一致）",
            second["publish"]["status"] == publish_mod.STATUS_DEGRADED_FIXED_ONLY
            and second["publish"]["dynamic_fail_closed"] is True
            and second["publish"]["dynamic_count"] == 0
            and second["publish"]["dynamic_discarded"] == 3,
            f"status={second['publish']['status']} "
            f"discarded={second['publish']['dynamic_discarded']}",
        )
        check(
            "第 3 轮：动态恢复 → 重新统一发布（loop 从未被打断）",
            third["publish"]["status"] == publish_mod.STATUS_OK
            and third["publish"]["dynamic_count"] == 3,
            f"status={third['publish']['status']} dynamic={third['publish']['dynamic_count']}",
        )

        # 逐轮核对「状态说只发固定」与「那一轮实际写出的文件」是否一致 ——
        # 这是 QA-003B 的口径，必须在运行期也不被打破。
        # （注意：live.m3u 是**交付物本身**，其中动态条目带签名 URL 是正常的；
        #  「不许出现签名」的约束只针对状态文件 / 摘要 / 日志 / /healthz。）
        degraded_text = captured.get("degraded_text", "")
        check(
            "第 2 轮实际写出的文件：没有任何动态分组、也没有任何动态条目名",
            degraded_text != ""
            and publish_mod.DEFAULT_DYNAMIC_GROUP_TITLE not in degraded_text
            and "曼城 vs 阿森纳" not in degraded_text,
            f"degraded_bytes={len(degraded_text)} "
            f"动态分组出现="
            f"{'是' if publish_mod.DEFAULT_DYNAMIC_GROUP_TITLE in degraded_text else '否'}",
        )
        ok_text = live.read_text(encoding="utf-8")
        check(
            "第 3 轮文件覆盖了降级版本（动态分组回来了）",
            publish_mod.DEFAULT_DYNAMIC_GROUP_TITLE in ok_text
            and ok_text != degraded_text,
            f"动态分组出现={'是' if publish_mod.DEFAULT_DYNAMIC_GROUP_TITLE in ok_text else '否'} "
            f"与降级版不同={'是' if ok_text != degraded_text else '否'}",
        )
        check(
            "发布摘要与状态文件口径一致",
            json.loads(summary.read_text(encoding="utf-8"))["status"]
            == third["publish"]["status"],
            f"summary_status="
            f"{json.loads(summary.read_text(encoding='utf-8'))['status']}",
        )

        # ---------------------------------------------------------- 2
        section("2. 单轮抛异常：异常被收敛进状态，下一轮照常发布")
        plan2: dict[str, int] = {"round": 0}

        def boom_then_ok(*, round_id: str) -> dict:
            plan2["round"] += 1
            if plan2["round"] == 1:
                raise RuntimeError("模拟 fetch 层未预期异常")
            return execute(("demo-dynamic",))

        clock2 = FakeClock()
        scheduler2 = runtime_mod.Scheduler(
            settings=runtime_settings, round_fn=boom_then_ok,
            clock=clock2.monotonic, sleep=clock2.sleep,
            status_store=runtime_mod.StatusStore(
                status, max_rounds=runtime_settings.status_history, version=__version__
            ),
        )
        loop2 = scheduler2.run(max_rounds=2)
        tail = read_rounds()[-1]
        check(
            "第 1 轮异常被记为 failed 轮次（不向上抛、不杀死 loop）",
            loop2["rounds"] == 2 and loop2["failed_rounds"] == 1,
            f"rounds={loop2['rounds']} failed={loop2['failed_rounds']}",
        )
        check(
            "第 2 轮仍然正常发布（loop 结构完好）",
            tail["outcome"] == "ok"
            and tail["publish"]["status"] == publish_mod.STATUS_OK,
            f"outcome={tail['outcome']} status={tail['publish']['status']}",
        )

        # ---------------------------------------------------------- 3
        section("3. 只读 HTTP：GET/HEAD /live.m3u、/healthz、503、404")
        service = server_mod.SubscriptionServer(
            host="127.0.0.1", port=0, playlist_file=live, status_file=status,
            stale_after_seconds=runtime_settings.stale_after_seconds,
            version=__version__, started_at=seed_now, quiet=True,
        )
        service.start()
        try:
            get_status, get_headers, get_body = http_request(service, "GET", "/live.m3u")
            head_status, head_headers, head_body = http_request(service, "HEAD", "/live.m3u")
            check(
                "GET /live.m3u：200 + 原样返回 + 正确的 Content-Type / Content-Length",
                get_status == 200
                and get_body == live.read_bytes()
                and get_headers["content-type"] == server_mod.PLAYLIST_CONTENT_TYPE
                and int(get_headers["content-length"]) == len(get_body),
                f"status={get_status} type={get_headers['content-type']} "
                f"len={get_headers['content-length']}",
            )
            check(
                "HEAD /live.m3u：与 GET 同一 Content-Length，且不发正文",
                head_status == 200 and head_body == b""
                and head_headers["content-length"] == get_headers["content-length"],
                f"status={head_status} len={head_headers['content-length']} "
                f"body={len(head_body)}B",
            )
            check(
                "ETag / Last-Modified 都给出（供播放器做条件请求）",
                "etag" in get_headers and "last-modified" in get_headers,
                f"etag={get_headers.get('etag')}",
            )

            health_status, _, health_body = http_request(service, "GET", "/healthz")
            health = json.loads(health_body.decode("utf-8"))
            check(
                "GET /healthz：200 + ok + 文件存在 + 新鲜度字段齐全",
                health_status == 200 and health["status"] == runtime_mod.FRESHNESS_OK
                and health["playlist"]["exists"] is True
                and health["freshness"]["seconds_since_last_success"] is not None
                and health["service"]["read_only"] is True,
                f"status={health_status} state={health['status']} "
                f"age={health['freshness']['seconds_since_last_success']}s",
            )

            offenders = []
            for path in ("/", "/live.m3u/extra", "/../etc/passwd", "/live.previous.m3u",
                         "/publish-summary.json", "/liptv.sqlite3"):
                status_code, _, body = http_request(service, "GET", path)
                if status_code != 404 or b"#EXTM3U" in body:
                    offenders.append(f"{path}→{status_code}")
            check(
                "未知路径 / 越界路径 / 其它磁盘文件一律 404",
                offenders == [],
                "6 条路径全部 404，且没有泄漏任何文件内容" if not offenders
                else f"异常：{offenders}",
            )

            hidden = live.with_name("live.hidden-by-demo.m3u")
            live.rename(hidden)
            try:
                missing_status, _, missing_body = http_request(service, "GET", "/live.m3u")
            finally:
                hidden.rename(live)
            check(
                "缺文件：503（不生成空列表冒充成功）",
                missing_status == runtime_mod.HTTP_PLAYLIST_MISSING == 503
                and b"#EXTM3U" not in missing_body,
                f"status={missing_status} body={missing_body[:32]!r}",
            )

            # ------------------------------------------------------ 4
            section("4. 单实例锁：第二个 scheduler 被明确拒绝")
            holder = runtime_mod.SingleInstanceLock(
                lock,
                stale_after_seconds=runtime_settings.stale_after_seconds,
                interval_seconds=runtime_settings.interval_seconds,
                version=__version__,
            )
            holder.acquire()
            try:
                before = live.read_bytes()
                code, payload = run("run", "--once", "--config", cfg, "--db", db, "--json")
                after = live.read_bytes()
                holder_still_owns = lock.exists()
            finally:
                holder.release()
            check(
                "第二个实例退出码 = 3（EXIT_LOCKED），并给出可解释原因",
                code == runtime_mod.EXIT_LOCKED == 3
                and payload["status"] == "LOCKED"
                and payload["reason"] == runtime_mod.LOCK_REASON_HELD_LIVE_PID,
                f"exit={code} status={payload['status']} reason={payload['reason']}",
            )
            check(
                "被锁定的一轮没有执行任何 fetch/publish（live.m3u 字节未变）",
                before == after and b"#EXTM3U" in before,
                f"bytes_before={len(before)} bytes_after={len(after)}",
            )
            check(
                "持有者的锁没有被误删（token 不同就不动别人的锁）",
                holder_still_owns,
                f"lock_exists={holder_still_owns}",
            )
        finally:
            service.stop()

        # ---------------------------------------------------------- 4b
        section("4b. 锁心跳与保守 release（QA-004A / QA-004B 返工）")

        hb_lock = out_dir / "liptv-heartbeat.lock"
        wall = WallClock(utcnow_iso())
        probe = runtime_mod.SingleInstanceLock(
            hb_lock, stale_after_seconds=10, interval_seconds=60, now=wall.now_iso,
        )
        probe.acquire()
        hb_first = read_json(hb_lock)["heartbeat_at"]

        hb_ages: list[float] = []

        def hb_sleep(seconds: float) -> None:
            wall.advance(seconds)
            hb_ages.append(wall.age_of(read_json(hb_lock)["heartbeat_at"]))

        hb_entry = {
            "started_at": utcnow_iso(), "finished_at": utcnow_iso(), "duration_ms": 1,
            "fetch": {"requested": 1, "ok": 1, "failed": 0, "sources": []},
            "stream_sync": {}, "published": True, "publish_status": publish_mod.STATUS_OK,
            "dynamic_fail_closed": False, "errors": [], "outcome": "ok",
            "publish": {"status": publish_mod.STATUS_OK, "exit_code": 0, "published": True},
        }
        hb_settings = runtime_mod.RuntimeSettings.from_mapping({
            "interval_seconds": 60,
            "stale_after_seconds": 10,
            "lock_path": str(hb_lock),
            "status_path": str(out_dir / "heartbeat-status.json"),
        })
        hb_sched = runtime_mod.Scheduler(
            settings=hb_settings, round_fn=lambda *, round_id: dict(hb_entry),
            sleep=hb_sleep,
            status_store=runtime_mod.StatusStore(hb_settings.status_path),
            lock=probe, heartbeat_thread=False,   # 用确定性的「轮次 + 休眠」路径驱动
        )
        hb_result = hb_sched.run(max_rounds=5)
        hb_last = read_json(hb_lock)["heartbeat_at"]

        check(
            "长跑 240s（stale 阈值只有 10s）时心跳持续推进，年龄从未逼近阈值",
            hb_result["rounds"] == 5 and hb_last != hb_first and max(hb_ages) < 10,
            f"rounds={hb_result['rounds']} heartbeat_at {hb_first} -> {hb_last} "
            f"max_age={max(hb_ages):.2f}s（周期 {hb_sched.heartbeat_interval:g}s）",
        )
        check(
            "轮次结束把心跳对齐到当前时刻（不是只在 acquire 时刷一次）",
            hb_last == wall.now_iso(),
            f"heartbeat_at={hb_last} now={wall.now_iso()}",
        )

        real_hostname = socket.gethostname
        try:
            socket.gethostname = lambda: "another-host.invalid"   # 模拟「另一台主机」观察
            rival = runtime_mod.SingleInstanceLock(
                hb_lock, stale_after_seconds=10, interval_seconds=60, now=wall.now_iso,
            )
            try:
                rival.acquire()
                rival_reason = "ACQUIRED"
            except runtime_mod.LockError as exc:
                rival_reason = exc.reason
        finally:
            socket.gethostname = real_hostname
        lock_intact = read_json(hb_lock)["token"] == probe.token
        check(
            "同一时刻由「他机」观察同一把锁：判 held_by_remote_host，不抢锁、锁未被改动",
            rival_reason == runtime_mod.LOCK_REASON_HELD_REMOTE and lock_intact,
            f"reason={rival_reason} lock_intact={lock_intact}",
        )

        hb_calls = {"n": 0}

        def counted_round(*, round_id):
            hb_calls["n"] += 1
            return dict(hb_entry)

        stolen = read_json(hb_lock)
        stolen["token"] = "intruder-token"          # 锁被另一个实例接管
        write_text(hb_lock, json.dumps(stolen))
        stopper = runtime_mod.Scheduler(
            settings=hb_settings, round_fn=counted_round, sleep=lambda _s: None,
            status_store=runtime_mod.StatusStore(hb_settings.status_path),
            lock=probe, heartbeat_thread=False,
        )
        hb_stop = stopper.run()
        still_foreign = read_json(hb_lock)["token"] == "intruder-token"
        check(
            "token 被替换后：调度器停止（lock_lost），一轮都没跑，也不碰别人的锁",
            hb_calls["n"] == 0
            and hb_stop["lock_lost"] is True
            and hb_stop["stop_reason"] == runtime_mod.LOCK_REASON_LOST
            and still_foreign
            and hb_lock.exists(),
            f"rounds={hb_calls['n']} lost={hb_stop['lock_lost']} "
            f"stop={hb_stop['stop_reason']} 锁仍是别人的={still_foreign}",
        )

        release_cases = (
            ("损坏 JSON", "not-json{{{"),
            ("空文件", ""),
            ("能解析但缺 pid", json.dumps({"token": "someone-else"})),
        )
        for idx, (label, raw) in enumerate(release_cases):
            target = out_dir / f"release-case{idx}.lock"
            owner = runtime_mod.SingleInstanceLock(target, stale_after_seconds=3600)
            owner.acquire()
            write_text(target, raw)
            deleted = owner.release()
            check(
                f"release 遇到「{label}」：保守不删（无法证明归属）",
                deleted is False and target.exists(),
                f"deleted={deleted} exists={target.exists()}",
            )

        own_lock = out_dir / "release-own.lock"
        own = runtime_mod.SingleInstanceLock(own_lock, stale_after_seconds=3600)
        own.acquire()
        own_released = own.release()
        check(
            "release 遇到「自己的锁」：照常删除（保守语义不许误伤正常路径）",
            own_released is True and not own_lock.exists(),
            f"released={own_released} exists={own_lock.exists()}",
        )

        # ---------------------------------------------------------- 5
        section("5. 关闭后：锁释放、HTTP 地址不可再连接")
        check("锁已释放", not lock.exists(), f"lock_exists={lock.exists()}")
        closed_port = service.port
        try:
            with contextlib.closing(
                socket.create_connection(("127.0.0.1", closed_port), timeout=2)
            ):
                reachable = True
        except OSError:
            reachable = False
        check(
            "只读服务已关闭（端口不再接受连接）",
            reachable is False,
            f"port={closed_port} reachable={reachable}",
        )

        # ---------------------------------------------------------- 6
        section("6. 信息边界：状态文件与 /healthz 不含任何 URL / 签名")
        status_doc = json.loads(status.read_text(encoding="utf-8"))
        blob = json.dumps(status_doc, ensure_ascii=False)
        check(
            "runtime-status.json：脱敏、条数封顶、关键字段齐全",
            not any(s in blob for s in SECRETS)
            and "http://" not in blob and "https://" not in blob
            and len(status_doc["rounds"]) <= runtime_settings.status_history
            and {"current_round", "last_run_outcome", "last_success_publish_at"} <= set(status_doc),
            f"rounds={len(status_doc['rounds'])} bytes={len(blob)} "
            f"last_success={status_doc.get('last_success_publish_at')}",
        )
        check(
            "失败轮次没有伪造「最后成功发布时间」（它必须等于最后一次成功发布）",
            status_doc["last_success_publish_at"] == tail["finished_at"],
            f"last_success={status_doc['last_success_publish_at']} "
            f"tail_finished={tail['finished_at']}",
        )
        health_blob = json.dumps(
            runtime_mod.compute_health(
                playlist_path=live, status_path=status,
                stale_after_seconds=runtime_settings.stale_after_seconds,
                version=__version__,
            ),
            ensure_ascii=False,
        )
        check(
            "/healthz 同口径：无 URL / 无签名 / 无来源清单",
            not any(s in health_blob for s in SECRETS)
            and "http://" not in health_blob and "https://" not in health_blob
            and "'sources'" not in health_blob,
            f"bytes={len(health_blob)}",
        )
        summary_blob = summary.read_text(encoding="utf-8")
        check(
            "发布摘要同样脱敏（签名/路径/查询串都不落盘，最多保留 origin）",
            not any(s in summary_blob for s in SECRETS)
            and "/pc.m3u8" not in summary_blob
            and "/raw.flv" not in summary_blob
            and "?" not in summary_blob,
            f"bytes={len(summary_blob)} "
            f"含签名={'是' if any(s in summary_blob for s in SECRETS) else '否'}",
        )

    failed = [r for r in _results if not r[0]]
    print(f"\n===== 汇总：{len(_results) - len(failed)}/{len(_results)} 通过 =====")
    for _ok, label, detail in failed:
        print(f"  FAIL {label}: {detail}")
    print(f"session_start={seed_now}  wall_clock={time.strftime('%H:%M:%S')}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
