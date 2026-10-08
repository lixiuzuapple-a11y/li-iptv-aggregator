# TASK-012 · DYNAMIC EVENT PREFLIGHT 报告

> §17 / §18 / §28 产出。实现：`liptv/preflight.py`（新增模块）。
> CLI：`python3 -m liptv dynamic-preflight`（只读）。

---

## 一句话

当前轮动态赛事在**进入播放列表之前**先做一次轻量可达性预检，
结果只有三档 `PRECHECK_PASS` / `PRECHECK_FAIL` / `PRECHECK_UNKNOWN`；
**UNKNOWN 永不排除任何条目**；KORICE 恒为 advisory。

---

## 预检做什么（§17 逐条）

按顺序，命中即停：

| 步骤 | 检查 | 失败归类 |
|---|---|---|
| 1 | URL scheme 合法 | `FAIL` |
| 2 | DNS / TCP connect | `UNKNOWN`（不可判定） |
| 3 | HTTP 状态码 | 4xx / 5xx / geo |
| 4 | content-type / playlist 头 | `HTML_FAKE` |
| 5 | HLS → 最多 1 个 media playlist | `INVALID_PLAYLIST` |
| 6 | HLS → 最多 1 个 segment（读 188 字节首包） | `SEGMENT_UNREACHABLE` / `EMPTY_MEDIA` / `INVALID_MEDIA` |

**非 HLS 直流**（`rtmp://` 等）只做步骤 1～4，**不深探**（§17 明令）。

---

## 冻结边界（§17 逐条落地）

| 禁止项 | 实现方式 |
|---|---|
| 不写 `probe_result` | `precheck_entry()` 只返回对象，**不碰 DB**；测试 `test_r2_precheck_writes_nothing_to_db` 固定 |
| 不进 fixed selector | 预检结果**不写 stream 表**，selector 看不到它 |
| 不保存 signed URL | 结果对象**只存 host**，不存 URL；测试 `test_r2_result_object_holds_no_url` 固定 |
| 不长深 probe | 最多 1 个 media playlist + 1 个 segment，预算由 `budget` 参数封顶 |
| 不转码 / 不代理 | 只发 HEAD/GET 读少量字节，**从不转发媒体流** |
| 总预算可控 | `precheck_entries(urls, budget=N)` 硬上限；测试 `test_r2_budget_caps_work` |

---

## §18 policy：authoritative vs advisory

```python
authoritative=True   → FAIL 排除该条目
authoritative=False  → FAIL 只记 advisory_fail，条目保留
authoritative=None   → 按 per-result 的 authoritative 字段；缺省视为 False
```

**KORICE 全程 advisory**（`authoritative=False`）。

测试固定：
- `test_r2_authoritative_excludes_fail` — JSNZKPG FAIL 被排除
- `test_r2_advisory_never_excludes` — advisory FAIL 不排除
- `test_r2_korice_cloud_fail_still_keeps_all` — KORICE 全挂也保留全部
- `test_r2_per_result_authoritative_false_also_protects`

---

## 🚨 本轮修掉的最危险缺陷：代理伪造状态码

**这是 §17 最贵的误杀场景，实测才发现。**

本机开着 FlClash 代理时，一个**根本不存在的端口**返回的是：

```
HTTP 502 Bad Gateway
```

而不是 `ConnectionRefused`。

如果照字面判 `FAIL`，**authoritative 源（JSNZKPG）会被整批删掉赛事** ——
用户看到的是「今天一场都没有」，而根因是本机有个代理。

修正：

```python
# 网关类状态码不能作为「源站失败」的依据
GATEWAY_STATUSES = frozenset({502, 503, 504})
# 命中 ⇒ PRECHECK_UNKNOWN（不可判定），永不排除
```

测试固定：
- `test_r2_gateway_status_is_unknown_not_fail` — 502/503/504 ⇒ UNKNOWN
- `test_r2_gateway_never_excluded` — 即使 UNKNOWN 也不排除
- `test_r2_404_is_still_authoritative_fail` — **对照组**：404 是源站自己的明确答复，不受代理降级影响
- `test_r2_negative_gateway_set_is_exactly_502_503_504` — 网关集合精确锁定
- `test_r2_negative_gateway_set_excludes_4xx` — 4xx 不得被误归网关

生产机上**没有代理**，所以这条防护在生产不会触发；但本机有，任何在
本机跑的验证都会踩到。

---

## §28 动态输出冻结：三条禁令

| 禁止项 | 保障 |
|---|---|
| 复活上一轮 signed URL | `preflight` 结果**不持久化**；失败源在下一轮重新走完整流程，绝不复用上轮 URL |
| preflight FAIL 后偷偷用 stale URL | `apply_policy()` 排除后**没有 fallback 分支**；测试 `test_r2_authoritative_excludes_fail` + 代码结构双重保证 |
| KORICE advisory FAIL 被误当 authoritative | policy 判定只看 `authoritative` 字段，不看来源名；测试 `test_r2_per_result_authoritative_false_also_protects` |

---

## 生产实测（2026-10-07）

最近一轮 publish 摘要：

```
SRC jsnzkpg-sports  ok=True  status=ok  cat=None
SRC korice-ppv      ok=True  status=ok  cat=None
```

- 两个源最近一轮都 **ok**；
- `precheck_fail` / `precheck_unknown` 均为空 ⇒ 没有条目被预检拦下。

⚠️ **诚实限制**：生产尚未出现过「precheck FAIL」的实例，所以
**FAIL 路径只有测试覆盖，没有生产实证**。这是「没出过事所以没证据」，
不是「验证过所以安全」。

---

## 测试覆盖（21 项，全部在 `tests/test_task012b.py`）

`PASS` 路径 / `HTML_FAKE` / 空 HLS / segment 404 / segment HTML /
segment 空 / 410 / 403 geo / **网关降级** / 不可达 UNKNOWN /
UNSUPPORTED_SCHEME / 非 HLS 直流 / authoritative 排除 /
advisory 不排除 / KORICE 全挂仍发布 / per-result 保护 /
计数正确 / 不写库 / 结果不含 URL / 脱敏 / 预算封顶 / 空输入 /
标签覆盖。
