# Heartbeat / Memory 检查点的 CI

功能检查点为 `98dd6e49d1a2fd2578f043fec2982223e34c7914`，对应 GitHub run `38760788042`。原基线 `18e8bd8d` 的七项绿灯不代表这个新检查点的状态。

首次 Windows job 在原认证租约测试中失败：`test_changed_request_auth_replaces_adapter_and_preserves_inflight_lease[api_key]` 尚未进入 provider，就在 `_bind_config_model_request_policy` 中产生 `TypeError: isinstance() arg 2 must be a type`。其 fixture 将 `factory.OpenAIAdapter` 类替换成一个返回 adapter 的函数；新增的真实 adapter feature gate 因此在测试替身上失败。外层 `entered.wait()` 的超时是这个前置异常的结果，不是认证服务慢。

只修两处实际注入点：`test_auth_request_ownership.py` 与 `test_step_auth_refresh.py` 使用真实 OpenAIAdapter 子类注入原 `httpx` client，保留类身份、所有原认证/计费/租约断言及原时间预算。生产没有添加 `getattr`、吞异常或为测试函数增加兼容层。

bundled Python 3.11.9 的必要相邻统一复验为 **66 passed / 0 failed / 0 skipped**，59.98 秒，覆盖上述两文件、`test_source_accepted_responses_input.py` 和 `test_source_model_request_policy.py`。原始日志/XML 为 `output/ci-repair-20261015/auth-fixture-typed-adapter-fix.*`；首次 GitHub 原日志保留为 `github-98dd6e49-windows-job.log`。后续提交仍须以它自己的 GitHub 全量结果为准。
