# Request permissions：Source 契约与本轮实现

参考 checkout：`.tmp/codex-src`，commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。

## Source 调用链

- `codex-rs/core/src/tools/handlers/shell_spec.rs:161`：模型 schema 为 `reason? / environment_id? / permissions.network? / permissions.file_system.read?,write?`。该工具默认 feature `RequestPermissionsTool=false`。
- `codex-rs/core/src/tools/handlers/request_permissions.rs:70`：选择真实 environment，将相对路径按该环境 cwd 解析，规范化后要求至少一项权限。返回真实客户端批准的 profile。
- `codex-rs/protocol/src/request_permissions.rs`：turn/session scope 与 `strict_auto_review`；profile 复用网络和文件系统权限数据结构。
- `codex-rs/protocol/src/models.rs:92`：普通路径序列化为 read/write；带 deny、glob 或 special 的 profile 用 canonical entries。`permissions.rs:135` 的 special value 是 `{kind:...,subpath?}`。
- `codex-rs/sandboxing/src/policy_transforms.rs:19,147,582`：去重／空嵌套项；请求与客户端批准取交集；不能把 read 提升为 write、不能批准更宽路径；保留约束已接受 grant 的 deny；符号 project roots 和相对 glob 在授权环境里固化；网络合并只增加已有允许；glob scan depth 按真实约束合并。
- `codex-rs/core/src/session/mod.rs:2900,3155`：Never／不允许 request_permissions 的 granular 返回空 profile，取消不产生 grant；以 pending 请求捕获的环境／turn 为准；strict + session 归为空 turn response；turn 与 session 保存权限，按 environment 分区。
- `codex-rs/core/src/session/turn_context.rs:467`：执行时合并 session 与当前 turn grants；strict 状态由 turn 持有。
- `codex-rs/core/src/tools/handlers/mod.rs:291` 与 `apply_patch.rs:496`：shell-like command 和 apply_patch 消费实际 grants，预先批准仅覆盖已授权权限。
- `codex-rs/core/src/tools/orchestrator.rs:139`：strict 模式即便原 requirement 是 Skip 也发起实际 approval action，apply_patch 同样进入该 orchestrator；`session/session.rs:1741` 的新 child services 使用新的 grant map。

## MiniCode 实现

- 新 `backend/tools/request_permissions.py`：按 Source schema 注册；feature `request_permissions_tool` 默认 false。事件复用现有 `approval_request` owner/waiter，subtype 为 `request_permissions`。冻结的请求 profile 与客户端可变 transport payload 分开。等待复用现有 `await_preflight` 的 turn deadline、cancel 与 lifecycle drain。
- 新 `backend/permissions/grants.py`：规范化／响应子集／序列化；`PermissionGrantStore` 保存 session 和当前 turn 的 environment grants，turn 结束移除 turn authority 和 strict 状态。
- `ToolExecutionContext` 传递 grant store、真实环境／turn 与最终授权 digest；`PermissionContext` 只携带当前授权快照。
- `command_tool.py` 合并实际 grants 后启动 subprocess，baseline 与实际 launch policy 分别留存以便 stdin 检查。显式升级沙箱仍走独立审批，普通工具批准不冒充路径权限。
- `apply_patch.py` 仅以真实 write grant 允许工作区外路径，修改／移动每个目标都检查；已授权目标无需重复默认 diff 审批，真实 diff 与应用证据仍保留。
- `checker.py` 只将此 Source authority 用于 `run_command` 与 `apply_patch`，不据此扩大 MiniCode 的其它 typed 工具。用户显式静态 ask／deny、content rules 与现有受保护文件规则保留。
- `config.py` 区分缺省工具审批 routing 与实际配置的静态规则。来源由有效 raw config 是否提供列表决定，不按列表内容猜；直接构造 `PermissionSettings` 的宿主规则视为显式，宿主要表示 builtin routing 必须传 `builtin_approval_routing`。
- `sandbox/policy.py` 修正 additional profile 合并的重复 entries 与 glob scan depth，以 Source max／unbounded 语义合并。
- subset 检查按 Source `PathUri` lexical authority，不将请求的符号链接拼写替换为磁盘实际目标；显式非 root deny 路径优先于其下更具体的 read/write entry。deny glob 使用 Source `GlobBuilder` 的单路径分量 `*`、递归 `**`、alternatives、unclosed class 和 invalid-pattern 行为；不继续使用可跨目录的普通 fnmatch。tmpdir 特殊权限用请求捕获的实际 environment 临时目录集合，不新增系统目录兜底。

## 验证与实际边界

新增 `backend/tests/test_codex_request_permissions.py`，覆盖扩大批准、read/write 子集、deny/glob、符号 roots 固化、turn/session/environment 生命周期、Never／granular禁问、strict/session禁止组合、取消等待、真实工作区外 patch I/O、授权结束后拒绝、默认与显式同名审批规则、沙箱升级与 typed 工具边界。结果由本轮统一测试运行填写，不能把尚未运行当通过。

后续批次已补实际 Guardian 自动复核、retained-context/VerifiedAnswer、strict 新鲜审查、真实手动批准，以及 ThreadOwned/检查点复用与冷恢复。最新相应326项通过，见 `2026-10-14-source-guardian-checkpoints.md`、`2026-10-14-source-mcp-skill-prompt-integration.md`。Async Guardian V2、增强工具和官方账号下的解密验收仍不在通过范围；前端没有新增 strict 开关。

当前实际 executor 仍为本地 primary environment。显式未附加 environment 请求会真实拒绝；不能据环境分区数据结构声称 remote exec-server／URI 环境已实现。后续 Source granular 配置/managed约束/SDK与WS producer已接入并验证，见 `2026-10-14-codex-source-granular-approval.md`，不是仅手工 PermissionContext 禁问夹具。

Source feature 默认关闭，正常产品配置未替用户打开。用户现有会话、附件、草稿与编辑器未保存内容不作为测试数据。
