# MCP 永久批准：真实配置与桌面闭链

Source 依据为 `.tmp/codex-src` commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 中 `mcp_tool_call.rs` 的 `session_mcp_tool_approval_key`、`persistent_mcp_tool_approval_key`、`normalize_approval_decision_for_mode` 与 `apply_mcp_tool_approval_decision`。Auto 可记住实际工具；Prompt/Writes 归一化为单次，不把其表面按钮当作永久权限。

## 真实根因与修复归属

验证前发现本轮永久批准 target 仅接受 `local/project`，而真实 `MCPServerManager._load_local_configs()` 发布 `source="user"`。默认 dataclass `local` 不能代替真实 producer。Root 已将目标解析改为实际 `user/project`；因此用户 `data/.mcp.json` 的 Auto 请求能够真实发布 `allow_persistent_approval=true`。验证 agent 未修改产品代码，仅报告此根因并补验证。

永久选择由底部 composer 的范围菜单发送 `remember_for_future=true`，保留原 request/conversation/turn/message 身份。后端写实际来源中 `tools.<raw-tool-name>.approval_mode=approve`，reload manager；当前准确批准的冻结调用保留自己的 digest receipt，能跨越自身造成的 policy 改变后执行，不依赖页面伪造结果。Plugin 写 user settings 中原 plugin ID、原 server namespace 和原 raw tool 名，包内 manifest 不被改写。

MiniCode 实际项目 producer 的配置路径是受信工作区 `.minicode/mcp.json`，不是工作区根的 `.mcp.json`。验证使用真实路径和实际 parser；项目 trust 输入在单元测试中控制。普通用户 trust ledger 未修改。

## 生产桌面验证

脚本：`output/playwright/codex-control-audit-20261013/mcp-persistent-authority-desktop-qa.cjs`。

最终证据：同目录 `production-mcp-persistent-run01/`，`verdict.json` **passed=true**，四项检查通过，`errors=[]`，七张 settled PNG 已逐张查看。Electron、backend、WS、真实 stdio JSON-RPC MCP 和文件 I/O 均为生产实现；仅模型 HTTP 回复受控。

1. 首次 Auto 请求提供“允许一次 / 允许此次对话 / 始终允许此工具”。真正点击永久选择后，`wire.json` 捕获原请求 `persistent-0` 的 `control_response`，嵌套 response 为 `action=approve, remember_for_future=true`。实际 `data/.mcp.json` 写入 `tools.operation.approval_mode=approve`；其他 Prompt/Writes 配置保留。
2. 当前冻结调用 `persistent-0` 与变参第二调用 `persistent-1` 均到真实 peer 的 `calls.jsonl`，只发一次审批。原 unsent draft `ROOT_OWNER_UNSENT_PERSISTENT` 恢复；真实切到第二 owner 后 `OTHER_OWNER_UNSENT_PERSISTENT` 也保留，再切回第一 owner 仍保留原草稿。
3. 关闭独立 QA Electron/backend 后，用相同独立 profile 重启真实进程。两个 owner 的草稿再核对一次。新 turn 的 `after_restart-0/1` 都实际执行，零审批；这是冷进程重新读取持久配置，不是复用内存 session cache。
4. Prompt 与 Writes 各两次不同参数请求，每次都提供单次允许；没有“选择允许范围”箭头或永久选项。四次 approval snapshot 真实声明 `allow_persistent_approval=false, allow_session_remember=false`，原配置仍为 Prompt/Writes。

实际八个 MCP tool result 的 `status=success, is_error=false` 且各有真实 request digest。`calls.json` 保留八个原 tool/arguments；没有将 skipped、失败或未运行调用算作成功。

## Producer 批次

新增 `backend/tests/test_mcp_persistent_approval_chain.py` 六项涵盖：实际用户 parser、实际项目 parser、Plugin manifest→user override 写入和冷读取、Prompt/Writes 不写永久权限，以及 Source 明确的持久写入失败只保留已批准 thread authority。Plugin 的 enabled snapshot 和项目 trust 输入受控；manifest、配置 parser、目标选择和真实写盘不是 mock。该组中 manager reload 回调受控；真实 reload/冻结调用与重新连接由上面的生产桌面验证覆盖。

第一次合并批次 **58 passed / 2 failed**，原 `mcp-persistent-producer-run01.xml` 保留。两处为新增 fixture 问题：预期未含现有 writer 规范化的 `env={}`，以及误用项目根 `.mcp.json`。按真实 producer 修 fixture 后，六项复跑全部通过，`mcp-persistent-producer-run02.xml`。按测试 classname/name 覆盖最新结果为 **60 passed / 0 failed / 0 errors / 0 skipped**，见 `mcp-persistent-latest-results.json`，不将两轮数字相加。

本次无需重新构建 frontend；生产桌面直接加载前一批已通过 TypeScript/Vite 的当前 dist，该 dist 实际包含永久批准文案与范围选项。未操作普通用户 profile、窗口、历史、附件或未保存文件。QA profile/workspace 与失败记录均保留用于追查。

Hosted Apps account/connector 权限、所有 plugin 安装路径的原生 UI、OS 受限 ConPTY 和公网 MCP/OAuth 不属于本批运行范围；没有据本批声明这些功能等同。
