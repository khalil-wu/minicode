# Heartbeat / Memory 检查点的 CI

功能检查点为 `98dd6e49d1a2fd2578f043fec2982223e34c7914`，对应 GitHub run `38760788042`。原基线 `18e8bd8d` 的七项绿灯不代表这个新检查点的状态。

最终 CI 修正提交 **`2c3cae04ca0a121d74d91cc26b40d68689a6d417`** 的 run **`38764385600` 七项全部成功**。Backend 为 **6579 passed / 27 skipped / 0 failed / 0 errors**，6606 个节点；Windows、Root、原生策略构建、前端及两项桌面任务均 success。实际状态及完整 Backend XML/log 保存在 `output/ci-repair-20261015/github-2c3cae04-state.json` 与 `github-2c3cae04-backend/`。27 个 skip 保留，不算通过。

首次 Windows job 在原认证租约测试中失败：`test_changed_request_auth_replaces_adapter_and_preserves_inflight_lease[api_key]` 尚未进入 provider，就在 `_bind_config_model_request_policy` 中产生 `TypeError: isinstance() arg 2 must be a type`。其 fixture 将 `factory.OpenAIAdapter` 类替换成一个返回 adapter 的函数；新增的真实 adapter feature gate 因此在测试替身上失败。外层 `entered.wait()` 的超时是这个前置异常的结果，不是认证服务慢。

只修两处实际注入点：`test_auth_request_ownership.py` 与 `test_step_auth_refresh.py` 使用真实 OpenAIAdapter 子类注入原 `httpx` client，保留类身份、所有原认证/计费/租约断言及原时间预算。生产没有添加 `getattr`、吞异常或为测试函数增加兼容层。

bundled Python 3.11.9 的必要相邻统一复验为 **66 passed / 0 failed / 0 skipped**，59.98 秒，覆盖上述两文件、`test_source_accepted_responses_input.py` 和 `test_source_model_request_policy.py`。原始日志/XML 为 `output/ci-repair-20261015/auth-fixture-typed-adapter-fix.*`；首次 GitHub 原日志保留为 `github-98dd6e49-windows-job.log`。后续提交仍须以它自己的 GitHub 全量结果为准。

## 全量后端的关联失败

98dd 首次 Backend 的真实结果为 6606 tests：6477 passed、94 failed、8 errors、27 skipped。认证 fixture 修正提交 `6bb751dd270ec19e79106576fd20e90cd454ba4c` 的 run `38762214824` 已有六个任务成功，其中包括 Windows；Backend 为 6492 passed、79 failed、8 errors、27 skipped，没有新增的失败节点。两次完整 XML/log 均下载保存在 `output/ci-repair-20261015/github-{98dd6e49,6bb751dd}-backend/`。

以下修复都保留原业务断言，未修改生产逻辑，没有新增 skip：

- 三个 startup fixture 的 Bootstrap 缺少真实 `begin_drain`，在 lifespan 的第一条退出操作处抛异常，跳过服务关闭及 `_state.bootstrap` 清除。这个 fake 因直接赋值而不受 monkeypatch undo 跟踪，持续污染之后的 56 个 Bootstrap 缺字段节点。只在原两处 fake 复用真实 `AppBootstrap.begin_drain`，并断言 session shutdown 前已关闭真实 admission。没有在 file_memory、MCP、registry 等下游加字段兜底。只读故障链见 `bootstrap-fixture-pollution-audit.md`。
- 原 native tail fixture 用 `object.__new__` 跳过 adapter 初始化。改用正式构造及真实 `aclose`，保留 journal/native reasoning/opaque/custom call/result 配对断言；未给生产添加属性缺失 fallback。
- H4 调度 fixture 没有配置真实模型，而本机 `.env` 经 config import 注入的模型变量掩盖了这个缺口。原测试隔离 tuple 增加 `OPENAI_MODEL` / `ANTHROPIC_MODEL`；仅改变测试进程环境、按 monkeypatch 恢复，不修改 `.env`。隔离后原 18 个节点 18/18 失败，复现 CI 边界，再为实际隔离 settings / archived conversation 配置真实模型。终态和队列 fixture 分别使用正式 AgentState、AppConfig、ScheduledTask/Run、SessionRunManager 构造及解绑，不以 SimpleNamespace 缺字段冒充真实对象。

## 修复后必要验证

| 真实范围 | 最新结果 | 原始证据 |
| --- | --- | --- |
| 认证与配置 gate 四文件 | 66/66 | `auth-fixture-typed-adapter-fix.xml` |
| startup 起点及 23 个受影响文件 | 225/225 | `bootstrap-fixture-lifecycle-chain.xml` |
| 同 startup 起点 → 原导航尾段 | 10/10 | `bootstrap-fixture-navigation-tail.xml` |
| native tail 全文件 | 9/9 | `native-tail-formal-constructor-20261015.xml` |
| H4 独立失败组及相邻八文件 | 133/133 | `heartbeat-independent-fixtures-after.xml` |

以上均使用 bundled Python 3.11.9，无失败/错误/跳过；各批次有重叠，不能相加。它们使原 102 个 failure/error 节点逐一都有最新通过结果；节点映射和原始失败分别保留于 `github-98dd6e49-failed-nodes.json` 与后续修复索引。完整新提交 CI 仍需独立通过，本地分批结果不能替代。

完整 CI 已由本页顶部 2c3cae04 的七项结果确认；上述末句是提交前验证边界的历史记录。之后另核设置消费，发现 Stable `content_item_kinds` 没有 Source display_name，却会经 MiniCode 通用 flag 列表新增开关。仅补既有内部 flag 排除集合，保留真实配置/effective/override 和模型 wire gate；既有 feature-flags 全文件 10/10 通过，见 `content-item-kinds-settings-final.xml`。这项后续小修不追写为 2c3cae04 的代码。
