# 扩展 exec：子会话权限与规范执行事实

本轮仅修正 `backend/tests/test_extension_execution_actions.py` 的 fixture 与断言，未改生产权限链、扩展 exec 或 compaction 实现。最终整文件实际 **13 passed、0 failed、0 error、0 skipped，进程 exit 0**。不将 Root 的 manual compact 50 或 Guardian WS 112 再次累计。

## 真实根因

原 fixture 无条件设置 `PermissionSettings(require_confirm=["run_command"])`。按配置契约，这是 host 明确提供的 static ask：`builtin_approval_routing` 为空，`PermissionChecker` 在 bypass 也保留这个 restriction，随后没有审批入口的命令被正确拒绝。原 `False-body` 失败证据保留于 [extension-facts.log](../../output/manual-compact-short-window-20261015-extension-facts.log)。

fixture 还直接创建 `PermissionContext(mode="bypass")`，其默认 `approval_policy` 是 `on-request`，没有经过实际 host 的 `PermissionChecker.build_context(mode)` 生成 Never。现将 confirm 分支的显式 ask 与 bypass 分支的 no-ask 分开，并使用真实 checker producer 构建子会话权限。旧 body/observer 四个正向节点保留全部文件位置、实际 run_context、仅 wrapper 入模型历史、原父历史不变与审批次数断言，另核对子命令的实际 mode 和 approval policy。

新增 body/observer 两个否定节点继续明确提供 ask，并验证 bypass 仍不能执行：无实际 Command 调用、子目录未产生文件、扩展收到的完整 canonical ToolResult 精确包含 `approval_policy:never`，其 request_digest 与真实前端结果相同，且未生成 approval_request。父历史、规范 journal 与 unresolved tool use 断言保留。

## 实际批次

| 批次 | passed | failed | error / skipped | exit | 原始证据 |
| --- | ---: | ---: | --- | ---: | --- |
| batch01 | 11 | 2 | 0 / 0 | 1 | [JUnit](../../output/extension-exec-child-permissions-20261015-batch01.xml)、[log](../../output/extension-exec-child-permissions-20261015-batch01.log) |
| batch02 | 11 | 2 | 0 / 0 | 1 | [JUnit](../../output/extension-exec-child-permissions-20261015-batch02.xml)、[log](../../output/extension-exec-child-permissions-20261015-batch02.log) |
| batch03 | 13 | 0 | 0 / 0 | 0 | [JUnit](../../output/extension-exec-child-permissions-20261015-batch03.xml)、[log](../../output/extension-exec-child-permissions-20261015-batch03.log) |

batch01 两个失败来自新增否定用例误读前端 `tool_result.content`：该投影只有 summary 等显示字段，并不暴露完整 ToolResult。batch02 改为读取内部 permission_decision 后，因 canonical 早拒绝不发布该前端事件而失败。两次执行均已证明实际命令未运行、无文件且结果为 error，旧四个正向节点也均通过；没有把这些错误断言作为权限通过证据。

最终使用扩展 API 实际返回的完整 ToolResult 验证规范拒绝原因与 request_digest，不往前端补内部事件，不放松权限或原有断言。batch03 全文件 13 个节点全部通过，保留前两批原始失败记录。
