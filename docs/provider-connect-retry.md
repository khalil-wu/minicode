# Provider 连接阶段重试轨

日期：2026-09-18
范围：`backend/llm/errors.py`、`backend/agent/policies/stream_retry.py`、
`backend/agent/provider_stream_transport.py`、`backend/config.py`

---

## 1. 问题：分类器把"连不上"和"流断了"判成同一件事

`backend/llm/errors.py` 的 `classify_llm_error` 对下列**语义完全不同**的失败返回
**完全相同**的结果。实测：

```
从未连上/DNS失败    retryable=True  type=api  provider_type=network
从未连上/拒绝       retryable=True  type=api  provider_type=network
从未连上/网络不可达 retryable=True  type=api  provider_type=network
流中途断开          retryable=True  type=api  provider_type=network
流中途断开2         retryable=True  type=api  provider_type=network
超时                retryable=True  type=api  provider_type=network
```

于是重试层没有依据区分两种情况，两者共用 `stream_max_attempts`(=10) 预算，
曲线 `min(500ms·2^n, 32s)`，约 160 秒后**回合失败**。

### 1.1 根因：类型信息被销毁后才做分类

区分所需的信息本来就在。`_error_text_fragments`（`errors.py:545`）
**已经把 `type(item).__name__` 拼进待匹配文本**：

```python
_append_text(fragments, type(item).__name__)
```

这正是 `_NETWORK_KEYWORDS` 里出现 `"connecterror"`、`"remoteprotocolerror"`、
`"readtimeout"` 这些**异常类名小写形式**的原因——分类器先把类型渲染成字符串，
再用子串匹配把它找回来。类型在异常对象上一直都在，只是被降级了。

实测异常链确实到达具体类型（`raise ProviderStreamFailure(exc) from exc`，
`agent/first_byte_waiter.py:52` 保留了原始对象）：

```
connect/DNS        chain=['APIConnectionError', 'ConnectError']
connect/timeout    chain=['APIConnectionError', 'ConnectTimeout']
mid-stream         chain=['APIConnectionError', 'RemoteProtocolError']
read timeout       chain=['APIConnectionError', 'ReadTimeout']
```

### 1.2 影响

用户合上笔记本 / 掉 Wi-Fi 是真实场景。"连不上"意味着**请求根本没发出去**，
重发同一个请求不可能中毒；"流断了"意味着请求已经在飞，可能已被处理。
前者应当等网络回来，后者应当限额。现在两者同命。

---

## 2. 参考实现：codex 的三段式

**① 在传输层定型**：`codex-rs/http-client/src/transport.rs:80-82`

```rust
fn map_error(err: reqwest::Error) -> TransportError {
    if err.is_connect() {
        TransportError::Connection(err.without_url())
```

`reqwest::Error::is_connect()` 是 HTTP 客户端自带的**谓词**，回答"连接阶段是否失败"。
不是字符串匹配，也不是类型清单——是出错点自带的分类。

**② 类型化传递**：`TransportError::Connection(HttpError)`
（`http-client/src/error.rs:21-22`）→ `CodexErrorDetails::ConnectionFailed`
（`codex-api/src/api_bridge.rs:191`、`protocol/src/error.rs:147`），
并在 `is_retryable()` 中显式列出（`protocol/src/error.rs:403`）。

**③ 独立且无界的重试轨**：`codex-rs/core/src/responses_retry.rs:17-18, 58-83`

```rust
const INITIAL_CONNECTION_RETRY_DELAY: Duration = Duration::from_secs(5);
const MAX_CONNECTION_RETRY_DELAY: Duration = Duration::from_secs(60);
...
if features.enabled(UnboundedConnectionRetries)
    && matches!(request, Sampling)
    && matches!(err.details(), CodexErrorDetails::ConnectionFailed(_))
    && !session_source.is_internal()
    && !provider.info().is_amazon_bedrock()
{
    sess.notify_stream_error(turn_context, "Reconnecting... waiting for network", err).await;
    retry_state.connection_retries += 1;          // 不碰 retries
    tokio::time::sleep(retry_delay).await;
    retry_state.connection_retry_delay = retry_delay.saturating_mul(2)
        .min(MAX_CONNECTION_RETRY_DELAY);
    return Ok(());
}
```

要点：**5s 起、倍增、60s 封顶、不消耗 `retries` 预算、不设上限**，
且这条分支排在常规 `max_retries` 判定**之前**，所以优先级更高。

---

## 3. 改动

### 3.1 按异常类判定连接阶段（`backend/llm/errors.py`）

新增与 `is_connect()` 对等的谓词：

```python
_CONNECT_PHASE_ERROR_TYPES = frozenset({
    "ConnectError", "ConnectTimeout", "ConnectionRefusedError",
    "NetworkUnreachableError", "NewConnectionError", "gaierror",
})
_IN_FLIGHT_ERROR_TYPES = frozenset({
    "RemoteProtocolError", "ReadError", "ReadTimeout",
    "WriteError", "WriteTimeout", "IncompleteReadError",
})

def is_connection_not_established(message) -> bool:
    names = {type(item).__name__ for item in _error_chain(message)
             if isinstance(item, BaseException)}
    if names & _IN_FLIGHT_ERROR_TYPES:
        return False
    return bool(names & _CONNECT_PHASE_ERROR_TYPES)
```

- 走**异常链**而不是只看向量最外层：SDK 的 `APIConnectionError` 是包装，
  真正的判据在其 `__context__` 上。
- `_IN_FLIGHT_ERROR_TYPES` 是否决项：一个链里同时出现两族时，以"请求已发出"为准。
- 只有字符串时返回 `False`（保守）——类型是唯一可信判据。

### 3.2 独立的重试日程（`backend/agent/policies/stream_retry.py`）

`StreamRetryState` 增加 `connection_retries` / `connection_retry_delay_seconds`，
新增 `plan_connection_retry()`，常量对等 codex：

```python
CONNECTION_RETRY_INITIAL_DELAY_SECONDS = 5.0
CONNECTION_RETRY_MAX_DELAY_SECONDS = 60.0
```

它只负责日程，**不返回重试预算**——这正是与 `decide_retry` 的差别。

### 3.3 分支优先级（`backend/agent/provider_stream_transport.py`）

```python
connection_retry = bool(
    safe_to_replay
    and not is_timeout
    and getattr(settings, "stream_connection_retries_enabled", True)
    and is_connection_not_established(cause)
)
if connection_retry:
    retry_delay = plan_connection_retry(connection_retry_state)
    new_attempt = stream_attempt          # 不推进请求重试序号
else:
    new_attempt, retry_delay = plan_stream_retry(...) if safe_to_replay else (stream_attempt, None)
```

守卫与 codex 一致：

| codex 条件 | 本实现 |
|---|---|
| `Sampling` 请求 | 只作用于流式采样路径（本函数仅被该路径调用） |
| `ConnectionFailed(_)` | `is_connection_not_established(cause)` |
| 非 internal session | `safe_to_replay`（未提交工具效果才等） |
| 非 Bedrock | 由 `stream_connection_retries_enabled` 关闭 |
| feature flag | `settings.stream_connection_retries_enabled`，默认 **True** |

同时把 WS→HTTPS 的传输降级（`not connection_retry`）排除在连接轨之外——
与 codex 相同：连接轨在降级判定之前返回。

**用户可见语义**（对应 codex 的 "Reconnecting... waiting for network"）：

- 文案：`无法连接提供商，正在等待网络恢复`
- `retry_attempt` / `max_retries` 传 `None`——**不显示 N/M**，
  因为这个预算没有被消耗，显示了就是在撒谎
- span id 用连接计数（`recovery:<span>:net3`）而非未推进的序号，
  否则重复尝试会在 Inspector 里塌缩成一条
- `recovery.retry.started` 的 data 增加 `connection_retry` / `connection_retries`

### 3.4 有界性

无界只针对"连不上"。回合自身的绝对截止时间仍然生效：
等待走 `budget_runtime.bounded_provider_timeout(retry_delay)`，
被截止时间截断时抛 `PhaseDeadlineExceeded`（`turn_budget_runtime.py:107-114`），
且 `sleep_or_cancel` 可被 `cancel_event` 打断。codex 靠用户中断，本实现两者都有。

---

## 4. 验证

新增 `backend/tests/test_stream_connection_retry.py`（14 例）：

| 测试 | 锁定的不变量 |
|---|---|
| `test_connect_phase_failures_are_recognized_by_type`（3 参数） | `ConnectError`/`ConnectTimeout`/`ConnectionRefusedError`（经 SDK 包装）判为连接阶段 |
| `test_in_flight_failures_are_never_read_as_connect_phase`（3 参数） | `RemoteProtocolError`/`ReadTimeout`/`ReadError` 一律**不**判为连接阶段 |
| `test_a_bare_string_cannot_claim_the_connect_phase` | 无异常对象时保守返回 False |
| `test_plan_connection_retry_doubles_to_a_cap` | 5→10→20→40→60→60，封顶后不再增长 |
| `test_unreachable_provider_does_not_consume_the_request_retry_budget` | `stream_attempt=3` 而 `stream_max_attempts=2`（**请求预算确已耗尽**）仍重试；序号不变；`retry_attempt`/`max_retries` 为 `None`；文案含"等待网络恢复" |
| `test_connection_track_attempts_keep_distinct_spans_and_back_off` | 连续三次延迟 5/10/20，span id 各自可辨 |
| `test_mid_stream_failure_still_spends_the_request_budget` | 流中断仍走请求预算：序号 1→2，延迟 1.0–1.25s（500ms 基数 ×2 ±25%） |
| `test_an_exhausted_request_budget_still_finishes_a_broken_stream` | 同样耗尽的预算下，**流中断**照旧收尾（对照项） |
| `test_the_same_exhausted_budget_finishes_when_the_connect_track_is_off` | 失败与预算完全相同，仅关掉连接轨 → 收尾。**证明上面那条断言判别的是连接轨，而不是输入** |
| `test_connection_track_falls_back_to_the_request_budget_when_disabled` | 开关关闭后退回常规预算路径 |

其中"预算确已耗尽仍重试"是核心断言：旧实现在该输入下
`plan_stream_retry` 返回 `(attempt, None)`（`loop_runtime_helpers.py:110-111`），
直接走 `recover_provider_failure` 收尾、回合结束；最后两条测试用
"同输入、只切开关"的方式把这一点变成了可证伪的断言。

**回归**：`compileall` 通过；四门禁通过（`check-agent-kernel-boundaries` /
`check-protocol-sync` / `check-no-duplicate-tools`，`check-large-files` 为告警）；
`test_stream_connection_retry` / `test_provider_stream_control` /
`test_report_regressions_20260801` / `test_provider_contracts` /
`test_llm_provider_service` / `test_provider_stream_resource_boundary` /
`test_provider_boundary_projection` / `test_provider_settings_snapshot` /
`test_live_text_streaming` 全部通过。

**预存失败（非本次引入，已 stash 到 HEAD 逐条隔离确认）**：
`test_extension_execution_actions.py` 在 HEAD 下同样挂死（Windows IOCP socket 等待）；
`test_model_execution_ownership.py`、`test_skill_read_roots_live_refresh.py` 等
在 HEAD 下同样失败，与本次改动无关。

---

## 5. 未做与理由

1. **把 `classify_llm_error` 整体改成类型驱动**：本次只把"连接阶段"这一个
   需要进入*控制流*的判据类型化。其余关键词（内容过滤、计费、模型名等）
   目前只影响文案与重试与否，且大量证据来自 provider 自定义的错误体文本，
   整体迁移需要先按 provider 逐个建立类型映射，属独立工作。
2. **Bedrock 等 provider 的例外**：codex 显式排除 Bedrock。本实现没有 provider
   级开关，统一由 `stream_connection_retries_enabled` 控制；若某个 provider
   需要例外，再加该维度的判据。
3. **连接轨也触发传输降级**：codex 不在连接轨里降级（先返回）。保持忠实。
