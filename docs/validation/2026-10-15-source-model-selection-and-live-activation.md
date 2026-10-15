# 会话下回合模型选择与显式运行步骤准入

本批修正了一个实际行为误判：Codex 桌面的普通模型/强度按钮更新下一回合默认选择，不能无条件切换已经在运行的模型。MiniCode 现在保留这个边界；显式 `turn.settings.update` 和运行中 extension 步骤选择才走 Source step activation。未新增前端开关或界面状态，未调整字体、字号、原生窗口或前端构建。官方账号不做。

## Source caller 更正

[保存的真实桌面调用链](../../output/playwright/codex-control-audit-20261013/source/thread-settings-next-turn-vs-live.json) 来自 `app-shared-737655e1fb23.js`，SHA256 为 `a1daa891e89a395b8db399ff6eaa365f12a86880c3d8cf891caf4ccbcb9eebf5`。

`Ddn.updateThreadSettingsForNextTurn` 显式传入空 activeTurnId，`applyThreadSettings` 写 `thread/settings/update`。该类只有在 **approvalsReviewer** 不为空且有 activeTurnId 时才发 `turn/settings/update`；模型与 effort 不走这个 live 分支。`runThreadModelSettingsUpdate` 维护 latest model/collaboration/effort 与线程设置。

此前只读审计正确发现了 MiniCode 原无条件 publish 的调用链，但没有先区别桌面按钮与显式 live API；不能把它解释成“普通模型按钮应加上 live feature gate”。历史审计已保留，并增加此 caller 更正链接。

Core 依据仍是 `.tmp/codex-src` commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`：`core/src/session/step_activation.rs`、`step_settings.rs`，以及 `protocol/src/permissions.rs` 的真实 full-disk-write 判定。没有把 CC 内容 glob 或 command-prompt 意图当作 Source PrefixRule。

## 实际生产链

普通 `llm.model.set`、任务 effort 和恢复默认仍解析真实 catalog/config、保存该会话的 model_selection，并投影下一回合的选择。它们不创建/改写正在执行的 adapter，不改 RunContext 中的 admitted/current snapshot，也不因 `step_model_switching=false` 或未知模型的 fallback metadata 拒绝普通偏好选择。下一次新用户 turn 的实际 admission 才读取持久默认；steer、当前 provider 后续工具回合与既有请求保留原 snapshot。

显式 backend facade `turn.settings.update` 仅完整支持 `conversation_id`、`turn_id`、model/effort。它定位原任务和 RunContext，在串行更新锁中捕获 admitted/current snapshot、原 model owner task、原取消 token 与 turn identity。异步 catalog/auth 解析后重读真实工作区 config/requirements，复核同一原任务与 context，再检查：

- Source 默认关闭且在现有内部清单隐藏的 `step_model_switching`；没有新增桌面开关。
- 当前和 admitted approval policy、managed reviewer 约束、required-model authority、Cyber/ignore_rules 的 prefix-ignore 分类。
- host catalog metadata 来源，Cyber rejection class、computer-use/node REPL review requirement、Guardian 覆盖、node 可用性和 reviewer override。
- Guardian V2 模型分类配置，规范化 Source 默认空 transcript；没有显式 reviewer override 时，比较 parent fallback node policy、resolved policy 与 template。
- Source model-required authority 的实际 full-disk-write 条件：保留 Root/Minimal、平台 SlashTmp、同目标 Write 覆盖 Read、不可覆盖的 Deny 与 glob 限制语义。

成功只发布该原 turn 的下一 step；已经发出的请求和工具仍用旧 snapshot。显式 turn 更新不覆盖 thread 下一回合默认。原任务完成、取消或被替换时返回 `targetUnavailable`，不把新选择转投后来出现的任务。拒绝返回实际 command error，不发伪成功。原来按 conversation 当前 task 无条件赋 snapshot 的低层 publish 已移除。

运行中 extension 的 `set_model` 与 thinking-level 更新使用同一 `ModelActivationState.commit`。异步 set_model 持有同一更新锁，原 execution/generation 失效时不能提交；同步 effort 调用不在异步更新进行中伪报应用。idle extension 仍能设置下回合默认。`TurnIterationRuntime` 在真实下一 provider iteration 才激活新 snapshot、预算和工具面；必要的小窗口压缩保留既有实现。

## 模型 metadata 的真实生产来源

`backend/llm/source-model-admission-metadata.json` 从上述 Source `models-manager/models.json` 提取 **11 个精确 slug** 的 Guardian、node、specialty 和 reviewer 字段，记录 commit 与源文件；缺省采用 `ModelInfo` 自身 serde 默认值，不按模型名家族或预算推断。

`ModelDefinition.source_model_metadata` 是 host-only typed provenance，实际 `_base_model`、models.json 及 extension 组合构造都会设置或继承；普通 raw model 字典与 extra 不能伪造，公开 `to_dict` 不透传这个字段或伪造的 used_fallback 标记。未知模型没有可信来源时，显式 live 更新按 Source fallback 限制拒绝，普通下一回合选择仍可用。

实际 ModelRuntime 构造、注册、反伪造和公开 DTO 均有生产路径测试，成功分支不只依赖测试手填 marker。Source commit/license 见 `backend/llm/SOURCE_MODEL_METADATA_NOTICE.md`；新增 node policy 原文带原 NOTICE/Apache license。Root 另外完成了 wheel 资源包含与包内导入验证，本报告不把它换算成模型行为测试数。

## 批量验证与真实失败记录

[统一批 XML](../../output/source-model-activation-batch04.xml)：六文件 **62 passed / 0 failed / 0 errors / 0 skipped**。覆盖 UI 默认/effort、显式 WS applied、extension 下一 step、原请求/工具 snapshot、SDK 与 provider contract、Source 比较矩阵及内部 flag。

之后仅补两个必要窄范围：

- [真实 task replacement](../../output/source-model-activation-task-replacement-final.xml) **6/6**：异步 auth 等待期间，原 execution task 真正完成，经真实 RunManager.register 注册仍在运行的新任务；WS 与 extension 均不能把更新转投替代任务。还覆盖取消与最终阶段读取到的新 requirements。
- [Source filesystem authority](../../output/source-model-activation-filesystem-final.xml) **11/11**：实际 full-disk-write 含 Root read/deny、Minimal、Windows SlashTmp、同 target 的 read/write、path/glob 限制及外部/不限文件系统。

按 classname/test name 保留最新结果去重后为 **73 passed / 0 failed**，见 [合并索引](../../output/source-model-activation-latest-results.json)。重复的六项不与 62 相加。

失败和未完成记录保留：首次批的新 fixture 缺实际 ToolExecutionContext 字段，barrier 未到达，停止后改用真实 context；不是一次通过。`targeted02` 揭出本批真实 task 捕获错误——bootstrap 在内部 stream 拉取 task 执行，不能把它当 QueryEngine 已确定的 model owner task；已改为捕获原 `run_context.model_owner_task`。`targeted03` 的 requirements fixture 在 await 中替换了已被局部 import 捕获的函数，且从错误的回执出口取结果；改为同一个真实配置读取函数在 await 后返回更新的 requirements，并检查实际 error event。最终上述完整批与窄范围均通过。

本批静态核对还修正了最初过简的 full-disk-write predicate，避免把 shadowed Read 或 Minimal 错当有效写限制；修复后的 Source 精确分支由上述 11 项验证。

## 真实桌面、HTTP 和 WS

[production-model-selection-next-turn-run02](../../output/playwright/codex-control-audit-20261013/production-model-selection-next-turn-run02/verdict.json) **3/3，errors=[]，exit 0**。独立 offscreen Electron/profile/workspace，实际桌面、后端、repository、HTTP 与 WS；模型回复由本地 fixture server 控制。

1. 原 A HTTP 请求打开时，调用**真实前端 setModelSelection action**，经实际 model.set ACK 将 thread 默认存为 B；Footer 选择为 B，原任务仍 streaming。此项不冒称点击过菜单。
2. 使用**原窗口实际 Composer 键盘入口**提交 steer，收到真实 `turn_mode=steer` 回执；当前 provider continuation、实际 tool_exec 与结果继续使用 A。
3. 当前 turn 完成后，通过实际输入框发送下一条用户请求，新 turn 的真实 HTTP model 为 B。原 wire 中有两组不同的真实 agent.run.started/completed：steer 所属原 run 与后续新 run 分开。额外 notes 辅助请求也保留原 A；不把它当成第四个主 provider step。

请求事实和当时加载的实际前端 bundle 见 `actual-model-selection-facts.json`。三张 PNG 已逐图查看。

[production-model-selection-button-companion01](../../output/playwright/codex-control-audit-20261013/production-model-selection-button-companion01/verdict.json) **1/1，errors=[]，exit 0**。这是另一个有限按钮 companion，不重跑完整 steer/follow-up 套件：A 正在请求时，实际鼠标点击模型/强度合一按钮、进入“选择模型”、点击 B。检查真实 llm.model.set 与成功 ACK、repository 默认 B、原 A HTTP 完成仍 A，以及原未发送草稿在菜单操作和任务结束后都保持。其三张 PNG 同样已逐图查看。Fixture 的 B 未声明强度档位时，前端按其真实 metadata 显示模型入口，未借用 A 的强度能力。

首次 `production-model-selection-next-turn-run01` 的第一项默认选择通过，但另开 fresh WS 发送 steer 暴露跨窗口 follower/owner 路由缺口，回执为 `Agent run exited before the user turn was durably admitted`。原 A 仍在执行，只有原 A provider 请求，不能把它算作模型切换失败或重复任务通过。失败证据已交负责 follower 链路的 agent；本批没有通过隐藏错误或修改跨窗产品来凑通过。

所有 QA 均未操控、重启或保存普通用户窗口、草稿、附件或未保存编辑器；未写前端/native 或重建 dist。

## 保留范围

本批闭合普通下一回合模型选择、显式 model/effort step activation 和所需 host provenance。原生 `.rules` 解析/PrefixRule producer、命令执行阶段的 ignore_rules Allow PrefixRule 过滤以及持久 amendment 仍未实现；本批只在 live admission 核对 prefix-ignore 分类，不能据此宣称整个 ignore_rules/execpolicy 已完成。跨窗 follower 新任务另有实际失败和独立修复范围；不以本批局部通过宣称整个 Codex 桌面或 harness 全等。
