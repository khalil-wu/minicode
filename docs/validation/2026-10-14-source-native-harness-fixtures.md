# Source 原生工具链与测试夹具收尾

参考 checkout：`822e58cc3d666166c7446c5b1ea2e52f5d09594c`。

当前按测试节点取最新结果：**363 passed，0 failed，0 error，19 skipped**，共 382 个节点。机器记录为 `output/native-harness-fixtures-20261014-results.json`；这些测试与其他验证批次有重叠，不能直接相加。

19 项跳过均来自当前环境缺少可选 JavaScript、TypeScript、TSX、Go、Java、Rust 的 tree-sitter grammar，没有计作通过。

后续资源分发批已补齐这五个运行依赖，并在真实 bundled Python 中复验完整既有工具文件37/37、0跳过，见[代码导航语法包分发](2026-10-15-code-navigation-grammar-distribution.md)。上面382节点数字保留为原冻结范围的历史事实，不与后续重叠结果相加，也不把这些grammar当作Source execpolicy解析器。

## 已验证的真实调用链

- 受控 HTTP SSE 经实际 OpenAI adapter / SDK 解析，执行原生 `tool_search_call`，返回 `tool_search_output` namespace 定义，随后调用真实 MCP proxy；核对 namespace、原始工具名称、会话所有权和一次执行。私有 journal 与冷恢复保持原生工具体和调用身份。
- 真实 stdio MCP 进程经过 MCPClient、MCPToolProxy 和 Code VM。完整 `content`、`structuredContent`、`isError` 和块内 metadata 可供 JavaScript 使用；顶层私有 `_meta` 不进入公共结果，未选输出不进入模型历史。
- Code Mode 的文本、图片和音频按原始顺序进入实际 Responses request；图片 detail、微短音频处理、原生预算与冷恢复分别核对。`tool_exec.max_output_tokens` 和 `tool_wait.max_tokens` 保持 Source 名称。
- 启动、yield、wait、旧 cell 注册表与实时权限边界、模型切换、审批、原生 patch 截断恢复均保留原有行为断言。测试夹具从真实私有执行 receipt 观察运行状态；模型请求独立核对 `LLMMessage.code_mode_output`，没有把显示 JSON 重新放回模型 body。
- 会话切换先发布分页页面，再异步读取私有 context；不可读记录仍保留真实 hydration 错误。浏览阶段没有将未知历史 cwd 改成当前目录。

## 验证中发现并修复的生产根因

原生 MCP discovery 将 wire name 混入授权策略，导致下一轮 schema 校验拒绝。现在 inventory 保留 wire name，激活和 availability filter 消费真实 `policy_identity`，授权边界的严格校验保留。

Code Mode 在建立目录时直接重复调用 runtime 的 `model_schema().output_schema`，使合法 extension wrapper 的空返回触发崩溃。现在 Code Mode、目录及嵌套搜索共同消费实际已准入的 canonical schema；本地输出 schema 不进入 Chat API 参数。

会话切换在发布初始页面时同步读取完整 conversation，阻塞私有 context hydration。浏览阶段现使用 summary 的真实 provenance；完整记录恢复留在既有 hydration 和实际执行边界。

此外，native search 的 SDK object arguments 经既有 JSON detach 路径进入生产 consumer，直接及嵌套搜索用真实 `source_for_call` 判断调用来源。

## 验证记录

| 批次 | 通过 | 失败 | 跳过 | 记录 |
|---|---:|---:|---:|---|
| 01 | 136 | 18 | 0 | `output/native-harness-fixtures-20261014.xml` |
| 02 | 340 | 23 | 19 | `output/native-harness-fixtures-20261014-batch02.xml` |
| 03：失败节点及必要原生链 | 83 | 4 | 0 | `output/native-harness-fixtures-20261014-batch03.xml` |
| 04：剩余四节点 | 4 | 0 | 0 | `output/native-harness-fixtures-20261014-batch04.xml` |

历史失败记录均保留。最后四项为夹具 producer 描述缺失、合法 pending/running 事件计数和消息类型遍历问题；修正后仍核对实际执行、原生 body、namespace、journal 与冷恢复，没有削弱这些契约。

这些结果确认本批工具链及对应回归，不代表全部 Codex 服务已经一比一完成。受控模型响应也没有替代真实模型风险判断或 Windows 原生沙箱隔离验证。
