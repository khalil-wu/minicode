# Typed provider error classification

本轮继续审计时，活源码显示 `backend/llm/errors.py` 的 `LLMErrorClassification` 在改动前把 `error_type` 与 `provider_error_type` 标成裸 `str`。改前静态探针命中 `error_type: str` 和 `provider_error_type: str = "unknown"`；运行时即使 `classify_llm_error("HTTP 429 rate limit exceeded")` 能返回正确的 `api/rate_limit`，调用方仍只能依赖字符串拼写，无法把错误类别当成协议契约检查。

现已增加 `LLMErrorType` 与 `ProviderErrorType` 两个 `StrEnum`。分类器的所有出口在 `LLMErrorClassification.__post_init__` 收敛为枚举，枚举值仍是原来的 wire 字符串，因此已有 JSON、事件和字符串比较保持兼容。回归探针同时验证身份是枚举成员，且与原 wire 值相等。

沿消费链实测又发现一个实际后果：适配器已经识别 `HTTP 503 + model_not_found` 为 fatal/model，但 `provider_stream_error_event.py` 只把事件正文、状态码和 provider code 拼回文字再分类；`503` 的优先级会把它改成 retryable/network。现在该边界优先读取事件中已验证的枚举 wire 字段，再保留旧文字分类作为兼容回退；相同请求经过事件投影后仍是 `model/model/fatal`，`context_length_exceeded` 仍是 `prompt_too_long/retryable`。

这对应 codex 的活源码：`codex-rs/protocol/src/protocol.rs:1851` 用 `CodexErrorInfo` 表达公开错误类别，`codex-rs/protocol/src/error.rs:82` 用 `CodexErrorDetails` 穷举内部语义，并在 `protocol.rs:2063` 的 `ErrorEvent` 上承载分类。MiniCode 之前只有分类逻辑，没有同等级的类型边界。

验证：`backend/tests/test_media_size_withholding.py` 的分类回归和 `backend/tests/test_provider_boundary_projection.py` 的结构化 503 投影回归通过；枚举继承 `StrEnum`，所以既可由 `is` 做类型断言，也可按旧 wire 值序列化和比较。
