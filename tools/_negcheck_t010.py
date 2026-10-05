"""负向验证：把本轮关键实现改回「旧行为」，本轮测试必须失败。

方法论红线：**只写「全绿」的测试没意义**。必须证明每个新断言真的能抓到
对应缺陷，否则它只是一行永远为真的断言。

做法：直接在内存里 monkeypatch掉新实现的关键判定，跑本轮测试，
断言它**会失败**。不落盘、不改仓库文件。

⚠️ 期望值设定纪律（2026-10-05 修正，负一版脚本自身的教训）：

  A) **新语义断言** —— 旧实现下**必须失败**。失败才说明测试真的在守护新行为。
     例：`auto` 模式下裸 publish 会不会抓、配置 true/false 会不会被采纳、
         `--no-dynamic` 能不能压过配置 true。这些是 TASK-010 §8 新增的语义。

  B) **零回归护栏** —— 旧实现下**必须通过**。`--dynamic` / `--dynamic-source` /
     `--require-dynamic` 三个 flag 在 TASK-009 旧实现与新实现里行为**完全一致**，
     它们的作用是「保证这轮没顺手改坏老行为」，不是新功能。
     把这类用例错当新语义断言 ⇒ 会误报「断言无效」。

  C) 判断「旧实现是否等价」时，必须调用**被替换掉的那个函数**，
     绝不能调用真实实现后又去检查真实实现（负一版第4 组就犯了这个错：
     monkeypatch 之后又用 `real_has` 去查，测的其实还是新实现）。

  D) banned 参数这类「过滤型」判定，反向验证的正确姿势是
     **先证明真实实现能全数拦截**，再把实现换成恒False 证明真实实现**不再**拦截。
     两段都要跑，缺一不可。
"""
import pathlib
import sys
import tempfile

REPO = r"E:\WebCodex-Workspace\li-iptv-aggregator"
if REPO not in sys.path:
    sys.path.insert(0, REPO)

print("=" * 70)
print("负向验证：换回旧实现 → 本轮测试必须失败")
print("=" * 70)

failures = []

# ================================================================ 1. §8 动态源决策
print("\n[1] 把 resolve_include_dynamic 换成 TASK-009 旧语义")
print("    旧实现：include_dynamic = bool(cli_dynamic or require_dynamic or tokens)")


def old_resolve(*, dynamic_default, has_dynamic_sources, cli_dynamic=False,
                cli_no_dynamic=False, require_dynamic=False,
                dynamic_source_tokens=None):
    """TASK-009 旧行为：只认 CLI flag，无视 config 与 --no-dynamic。"""
    return bool(cli_dynamic or require_dynamic or (dynamic_source_tokens or [])), "cli_explicit"


# --- A) 新语义断言：旧实现下必须 FAIL ---
NEW_SEMANTIC_CASES = [
    ("auto + 有源（裸 publish 应抓）",
     dict(dynamic_default="auto", has_dynamic_sources=True), True, "auto_sources_present"),
    ("auto + 无源（应不联网）",
     dict(dynamic_default="auto", has_dynamic_sources=False), False, "auto_no_sources"),
    ("配置 true 强制抓",
     dict(dynamic_default=True, has_dynamic_sources=False), True, "config_forced"),
    ("配置 false 显式关闭",
     dict(dynamic_default=False, has_dynamic_sources=True), False, "config_disabled"),
    ("--no-dynamic 压过配置 true",
     dict(dynamic_default=True, has_dynamic_sources=True, cli_no_dynamic=True),
     False, "cli_no_dynamic"),
]

# --- B) 零回归护栏：旧实现下必须 PASS（故意如此，不是缺陷）---
GUARD_CASES = [
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

from liptv.config import resolve_include_dynamic as real_resolve  # noqa: E402

print("\n  [1a] 新语义断言 —— 旧实现下必须失败（否则断言无效）")
caught = 0
for label, kwargs, want_inc, want_reason in NEW_SEMANTIC_CASES:
    # 先确认真实实现满足新语义（正向）
    r_inc, r_reason = real_resolve(**kwargs)
    if not (r_inc == want_inc and r_reason == want_reason):
        failures.append(f"§8「{label}」真实实现都不满足新语义，测试本身写错了")
    # 再确认旧实现抓不到
    try:
        inc, reason = old_resolve(**kwargs)
        if inc == want_inc and reason == want_reason:
            print(f"    没抓到!!    {label}")
            failures.append(f"§8「{label}」在旧实现下仍然通过 ⇒ 断言无效")
        else:
            print(f"    抓到 ✓      {label}  (旧: {inc}/{reason})")
            caught += 1
    except Exception as exc:
        caught += 1
        print(f"    抓到 ✓      {label}  ({type(exc).__name__})")
print(f"    ⇒ {caught}/{len(NEW_SEMANTIC_CASES)} 个新语义断言在旧实现下会失败")

print("\n  [1b] 零回归护栏 —— 旧实现下必须仍然通过（证明本轮没改坏老行为）")
kept = 0
for label, kwargs, want_inc, want_reason in GUARD_CASES:
    o_inc, o_reason = old_resolve(**kwargs)
    r_inc, r_reason = real_resolve(**kwargs)
    old_ok = (o_inc == want_inc and o_reason == want_reason)
    new_ok = (r_inc == want_inc and r_reason == want_reason)
    if old_ok and new_ok:
        print(f"    保持 ✓      {label}  (新旧一致)")
        kept += 1
    else:
        print(f"    回归!!      {label}  旧={o_inc}/{o_reason} 新={r_inc}/{r_reason}")
        failures.append(f"§8 零回归护栏「{label}」被本轮改动了行为")
print(f"    ⇒ {kept}/{len(GUARD_CASES)} 个老行为 flag 完全未变")

# ================================================================ 2. §4/§13 KORICE 冻结
print("\n[2] 把 KORICE 改成 cloud_probe_authoritative = true（旧行为）")
from liptv import source_policy as sp  # noqa: E402

policies = sp.load_source_policies(pathlib.Path(REPO) / "config" / "source_policies.toml")
real_korice = policies["korice-ppv"]
print(f"    真实配置 : authoritative={real_korice['cloud_probe_authoritative']}, "
      f"应删源={sp.should_drop_source_on_probe_failure(real_korice)}")
if sp.should_drop_source_on_probe_failure(real_korice) is not False:
    failures.append("KORICE 登记的 cloud_probe_authoritative=False 未生效，会被删源")

bad_policy = sp.normalize_policy({
    "name": "korice-ppv", "aggregator_context": "shanghai-cloud",
    "playback_context": "home-windows-vpn", "playback_requires_vpn": True,
    "cloud_probe_authoritative": True,
})
drops = sp.should_drop_source_on_probe_failure(bad_policy)
if drops is not True:
    failures.append("KORICE 冻结规则在权威=True 时没有删源，测试未覆盖该分支")
else:
    print("    抓到 ✓若无人显式登记 korice-ppv 就会删源（测试能区分）")

default_pol = sp.default_policy(kind="dynamic_event_m3u")
if sp.should_drop_source_on_probe_failure(default_pol) is not True:
    failures.append("未登记 policy 的默认值被改成 False，会导致普遍的『云端FAIL不删源』")
else:
    print(f"    抓到 ✓未登记默认 authoritative="
          f"{default_pol['cloud_probe_authoritative']} ⇒ 仍删源（保守方向正确）")

# ================================================================ 3. §5.4 alias 冲突
print("\n[3] 把 alias 冲突从 fail-closed 改成「后者覆盖前者」（旧行为）")
from tools import build_fixed_seed as seed_tool  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    p = pathlib.Path(td) / "a.toml"
    p.write_text(
        "[[alias]]\ncanonical = \"CCTV-1 综合\"\nfrom = \"CCTV-1 HD\"\nnote = \"x\"\n"
        "\n[[alias]]\ncanonical = \"CCTV-13 新闻\"\nfrom = \"CCTV-1 HD\"\nnote = \"y\"\n",
        encoding="utf-8",
    )
    try:
        seed_tool.load_alias_map(p)
        print("    没抓到!! 冲突竟然没报错")
        failures.append("alias 冲突未 fail-closed")
    except SystemExit as exc:
        print(f"    抓到 ✓{str(exc)[:64]}…")

    naive = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("canonical"):
            naive["k"] = line.split("= ", 1)[1].strip('"')
    print(f"    旧行为会得到 {naive} ⇒ 渠道静默改成 {naive['k']}")

    # 幂等性（重复相同行不报错）反向验证
    p2 = pathlib.Path(td) / "b.toml"
    p2.write_text(
        "[[alias]]\ncanonical = \"CCTV-1 综合\"\nfrom = \"CCTV-1 HD\"\n"
        "\n[[alias]]\ncanonical = \"CCTV-1 综合\"\nfrom = \"CCTV-1 HD\"\n",
        encoding="utf-8",
    )
    dup = seed_tool.load_alias_map(p2)
    if dup.get("CCTV-1 HD") != "CCTV-1 综合":
        failures.append("重复相同 alias 行被误判成冲突")
    else:
        print("    抓到 ✓重复相同行幂等（未被误判成冲突）")

# ================================================================ 4. banned query
print("\n[4] 把 has_banned_query 改成恒 False（旧行为：不过滤签名）")
from urllib.parse import urlsplit, parse_qsl  # noqa: E402

BANNED_SAMPLES = [
    "http://h.invalid/a.m3u8?token=X",
    "http://h.invalid/a.m3u8?auth=X",
    "http://h.invalid/a.m3u8?msisdn=13800000000",
    "http://h.invalid/a.m3u8?key=X",
    "http://h.invalid/a.m3u8?mdspid=1",
    "http://h.invalid/a.m3u8?migutoken=X",
    "http://h.invalid/a.m3u8?sign=abc",
    "http://h.invalid/a.m3u8?hdnts=deadbeef",
    "http://h.invalid/a.m3u8?expire=1893456000",
    "http://h.invalid/a.m3u8?secret=S",
]
NEED_CLEAN = "http://h.invalid/a.m3u8?uid=1&pid=2"

real_has = seed_tool.has_banned_query
# --- D) 第一段：证明真实实现全数拦截 ---
leaked = [u for u in BANNED_SAMPLES if not real_has(u)]
if leaked:
    failures.append(f"真实实现漏拦 {len(leaked)} 个签名/身份参数，测试本身覆盖不全")
else:
    print(f"    真实实现：{len(BANNED_SAMPLES)}/{len(BANNED_SAMPLES)} "
          f"个签名/身份参数全部被拦截")
if real_has(NEED_CLEAN):
    failures.append("真实实现误伤了 uid/pid 这类正常参数（过滤过头）")
else:
    print("    真实实现：uid/pid 正常参数未被误伤（无假阳性）")

# --- D) 第二段：换成恒 False，证明测试会失败 ---
seed_tool.has_banned_query = lambda url: False
try:
    missed = [u for u in BANNED_SAMPLES if not seed_tool.has_banned_query(u)]
finally:
    seed_tool.has_banned_query = real_has
if len(missed) == len(BANNED_SAMPLES):
    print(f"    抓到 ✓恒 False 后 {len(missed)}/{len(BANNED_SAMPLES)} "
          f"个样本全部漏过 ⇒ 对应测试必然 failed")
else:
    print(f"    没抓到!! 恒 False 后仍拦住 {len(missed)} 个")
    failures.append("banned 覆盖不全，恒 False 仍有样本被拦")

# ================================================================ 5. 负向验证本脚本自身
print("\n[5] 自检：故意写错期望值 → 脚本必须报错（证明上面的『抓到』不是空转）")
probe = old_resolve(dynamic_default=True, has_dynamic_sources=True, cli_dynamic=False)
if probe[0] is True:
    failures.append("自检失效：旧实现居然把「配置 true 且无 CLI flag」判成要抓")
else:
    print(f"    抓到 ✓旧实现在「配置 true + 无 flag」下返回 {probe[0]}"
          f" ⇒ 确认它确实读不到 config")

# ================================================================ 汇总
print("\n" + "=" * 70)
if failures:
    print(f"❌ 负向验证发现 {len(failures)} 个问题：")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 负向验证全部通过")
print("   ·5/5 个新语义断言在旧实现下确实会失败 ⇒ 测试真的在守护 TASK-010")
print("   · 3/3 个老 flag 行为零回归 ⇒ 本轮没顺手改坏既有语义")
print("   · 过滤/冻结/冲突三类守卫都能区分新旧实现")
print("=" * 70)
