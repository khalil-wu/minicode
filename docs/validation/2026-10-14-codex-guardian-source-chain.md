# Guardian、retained context 与 VerifiedAnswer

参考 checkout：`.tmp/codex-src`，commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。

## 生产调用链依据

- `core/src/guardian/decision.rs` 捕获 action 与 issuing step，取消覆盖 extension 结果；没有 contributor 不等于批准。`ext/guardian-reviewer/src/routing.rs` 将 on-request/granular + auto_review 路由到实际 reviewer，mandatory/fresh/strict/retry 不冒充普通 human approval。
- `core/src/guardian/review_request.rs` 每次 attempt 捕获最新授权版本；允许结果必须复核 root lifetime、review context 与用户授权版本。授权变化是可重试 stale_authorization，历史 reset 或已确认上下文变化是取消。
- `ext/guardian-reviewer/src/settings.rs` 只允许 `exec_command/write_stdin/view_image/exec/wait`，要求 managed read-only sandbox，不暴露追加权限。`protocol/src/models.rs:495` 将 read/write entries 收紧为 read，保留 deny，network restricted。
- `ext/guardian-v2/src/sync_reviewer/reviewer_config.rs` 在 private config 禁用 Skill/Memory/notify/MCP/agent delegation/hooks/web search 等，不篡改 parent config；`core/src/session/session.rs` 的 reviewer 身份来自可信的内存创建，不信任恢复数据中的名字。
- `core/src/guardian/reviewer_config.rs` 与 `ext/guardian-reviewer/src/model.rs` 使用实际 catalog，优先 advertised reviewer model + low effort；不支持 low 时使用其 default。没有 catalog reviewer 时按 Source 保留 parent 模型或明确 override。
- `model-provider/src/provider.rs` 普通 reviewer id 为 `codex-auto-review`，OpenAI API-key auth 为 `gpt-5.6-luna`。认证类型必须来自实际 runtime，不按供应商显示名称或 token 字符串猜。
- `prompts/src/guardian_instructions.rs` 只替换模板中的两个 placeholder；替换进来的 policy 原文不能再解释。原文资源保存在 `backend/agent/guardian/resources/`，有来源和 LICENSE。
- `ext/guardian-reviewer/src/assessment.rs` 为真实结构化 output schema（只要求 outcome），接受 Source 明确支持的 JSON 外围 prose；缺省 risk/user_authorization/rationale 按 Source 赋值，非 JSON 是 review failure。
- `codex-api/src/common.rs:403` schema wire name 为 `codex_output_schema`。`core/src/session/turn.rs:1595` 对 private Guardian 的 **output_schema_strict=false**，普通 final schema 为 true；不能擅自把 Guardian optional fields 改为全 required。
- `core/src/guardian/review_session.rs:284` cache key 为 `guardian:{parent_thread_id}`，不改 private session 的真实身份。
- `ext/guardian-reviewer/src/pool.rs` 在 idle trunk 上续审，繁忙时只从上次已提交的 history/cursor fork；失败、取消和未排空的 session 不成为可复用完成记录。
- `ext/guardian-reviewer/src/lib.rs/retry.rs` 总 deadline 90 秒、最多 3 attempts；parse、stale authorization 和明确 transient provider errors 才重试。`completion.rs` 区分 genuine allow/deny 与 timeout/cancel/review failure，错误不赋虚构 risk。
- `ext/guardian-reviewer/src/circuit_breaker.rs` 真实拒绝按 turn 计数，普通连续 3 次或最近 50 个 review 中 10 次中断；cyber specialty 一次中断。review failure 不是 policy denial。
- `history/src/retained_context.rs` 独立保存 user/assistant/verified-answer 三个 family，每组最多 8 条、每条 16KiB、每组 64KiB；按接受序裁剪，丢失记录显式不完整。
- `core/src/tools/handlers/request_user_input.rs:112` 用 frozen 原问题与真实返回答案配对，加入选中 option 的原 description；不从助手的“是否允许”文本推测 VerifiedAnswer。
- `guardian-context/src/verified_answers.rs` 在 900-token 近似预算内保留完整 question+answer，否则整条省略并注明不完整，不能截掉拒绝条件后保留 grant 前缀。
- `core/src/context/contextual_user_message.rs:52` 用 host 内容来源鉴别真实用户授权；`core/src/context/inter_agent_message.rs` 和 `guardian-context/src/transcript.rs` 将现代委派/通信作为 assistant evidence。Source origin enum 是 User/Heartbeat，agent delivery 不能变成 Root human。
- `guardian-context/src/history.rs` 提供 Source Independent review history：user/nonuser 分区，各 128 items/4MiB，非 user 溢出裁最旧半；generation 由 eviction/reset 改变，parent 模型 compaction 不能重写其 originals。

## MiniCode 本轮模块与 host 接口

独立代码为 `backend/agent/guardian/{contract,retained,action,prompt,config,runtime,pool,service,truncation}.py`。

`GuardianEvidenceOwner` 必须在真实 host 接受、持久和验证成功的边界调用，不从 model history 的角色名推断授权。提供 `reserve_order`、`record_accepted_message`、`record_verified_response`、`record_manual_approval`、`snapshot`、`checkpoint/from_checkpoint` 和 `reset_history`。agent origin 记录为 assistant，不增加用户授权 revision。checkpoint 的原事实在 compaction 后保留；rollback/reset 要使用真实存活 checkpoint，并改变 lifetime。

`GuardianReviewService(evidence_source, runtime_factory, interruption_handler)` 提供 `decide(action, context, required, strict_auto_review, retry_reason, approval_reason, approvals_reviewer)`。`None` 才进入现有 human 流程；其它返回 `GuardianReceipt`，其 `approval_response()` 绑定原 digest/policy identity。request_permissions 的自动批准由 host 按 Source 转换为完整 requested profile + turn scope；stdin 绑定旧 exec target 与本次 input approval identity。

`snapshot.root_versions` 是 `(root_thread_id, history_reset_version, user_message_revision, review_context_revision, verified_answers_complete)`。Root host 提供每次最新值，不能只在 child 启动时截取一次后永久复用。

private runtime 复用现有 `QueryEngine` 和实际 subprocess/sandbox owner。MiniCode 的内部工具名 `run_command/monitor/view_image/tool_exec/tool_wait` 对应 Source 的五种允许能力；不把任意 typed file/MCP/Skill 工具混进 reviewer。parent config、用户草稿、用户工作区内容和主窗口不用于测试数据。

## 验证与尚未覆盖的 Source 路径

新增 `backend/tests/test_codex_guardian.py`，覆盖 Source parser/schema、完整 Q/A scope、省略与 checkpoint、不可变 snapshot、agent provenance、Source full/delta 区段、完整 action budget、catalog/readonly ceiling、stale allow 重审、历史 reset、取消排空、三次 parse retry、genuine-denial circuit breaker，以及受控模型下的真实 QueryEngine private reviewer 调用链。由本轮统一测试填写实际结果，未运行不算通过。

当前模块采用 Source **Independent** original review history。`ThreadOwned` 可解密 parent checkpoint 的复用/一轮 checkpoint recovery、retained delivery source 的完整去重、optional conversation-history tools、增强 Node REPL/图片策略和 Guardian V2 async scorer 仍需单独衔接，不能把本批 synchronous service 等同于这些能力全部完成。

完整模型/工具/schema/prefix开销预算还需要与标准 provider 请求的实际 serialized frame 合并审计；private snapshot 的 evidence 不能通过裁掉完整 action 或限制条件来“解决”超预算。optional review 超预算可按 Source 返回 human，mandatory review 必须明确失败。

## 桌面投影（本阶段）

本阶段使用已留存安装包 `OpenAI.Codex_26.1007.9446.0` 的 `app.asar`，保留提取件在 `output/playwright/codex-control-audit-20261013/codex-open-source/`。依据为 `agent-activity-item-ae3ad6b8cd78.js` 的 `dr/pr`（approved 隐藏、inProgress 可组合、其余 standalone）、`split-items-into-render-groups-6abd45f3fafa.js` 的 `S`（exec/patch/MCP target 附加与去重，denied MCP 独立）、`automatic-approval-review-details-27e4ad610245.js` 的 `D/A/j`（初始折叠、标题扫描、风险 amber、真实理由和明确授权文本），以及 `sites-end-resource-873a414616c6.js` 的 `aS/tS/sC/OC/aC`（短行、真实 breaker divider、denied shell 单行与原理由）。图标直接取自 `app-shared-30671c57ec82.js` 的 `Dst/xsr terminal-shield-light-16`，中文文案取自同安装包 `zh-CN-3727db380fd4.js`。

两事件由 typed protocol 到 immutable message block、stream snapshot、public cold transcript、hydration、TurnProjection、cells 和 Timeline 接通。普通 Guardian warning 原文保留，只有真实 breaker 的 `interruption=true` 投影为 Source divider 与说明 tooltip；private reviewer 不冒充子 agent。工具真实 `errorKind=approval_rejected` 且 assessment denied 才进入 Guardian declined 显示，不能把普通 failed 或 stdin 旧 exec 误判为 declined。declined shell 不把模型安全指导当 stdout，不虚构未知 exit code，也不把未运行的多个 parsed command 展成多条已执行事实。

新增前端 `chat/guardianProjection.test.tsx` 与后端 `test_codex_guardian_projection.py` 供全局统一批量验证。单次前端 tsc 检出四条新类型衔接错误（dispatch boolean、record target identity、markdown exhaustive cell），均已修；修复后的最终 build/测试与真实 isolated-profile UI 验证由主线程统一填写。未运行的验证不算通过。

## `/approve` 与拒绝后的明确授权（下一批）

继续读取当前 ASAR `sites-end-resource::tC/sC/OC/aC` 和 `app-primary::Xrt/nit/rit`，核对了 inline 按钮、整个 thread 的单个 pending 操作、已记录 approval IDs、newest-first 十条选择范围、失败后恢复、Source 原文说明与 toast。后端依据为 `core/session/handlers.rs::approve_guardian_denied_action`、`core/context/guardian_approved_action.rs`、`guardian-context/transcript.rs` 和 app-server `thread/approveGuardianDeniedAction`。

新增 `manual_approval.py` 从 host-owned denial ledger 的真实 `assessment.action` 生成 `{action, outcome: allowed}` 与完整 Source developer body。客户端只选择真实事件；conversation、review、turn、target、时间与 action 都须匹配 host 记录，任意 `/approve` 字符串、assistant/tool 文本或客户端另造 action 不构成授权。审批只记录明确授权，绝不执行操作或新开 turn；用户随后真正提交重试时仍经过完整 Guardian 审核。

`backend/ws/handlers/guardian.py` 接 Source wire `thread/approveGuardianDeniedAction` 的 `{threadId,event}`；现有 MiniCode conversation_id 仅作路由 envelope。在 owner projection lock 内恢复对应 thread 的真实 snapshot/ledger，clone builder 写 Source developer body，再使用 repository CAS 先持久化，成功才 apply 到 live host。持久化失败不产生 live 授权；现有 `await_task_despite_cancellation` 保持已派发 commit 和 live apply 一致，不遗留“落盘成功但 live 没记录”的取消窗口。重复成功请求幂等。

前端 inline control 仅在主对话真实 declined command 展开后出现，子 agent 只读过程不添审批控件；`/approve` 打开 Source denial selector。pending 时该 thread 其它批准按钮停用，成功隐藏已批准 review，失败恢复原控件；草稿、附件、工作区和当前 active thread 由原 owner 检查保留，不自动发送 Retry。新增 `guardianManualApproval.test.tsx`（4 项）和 `test_codex_guardian_manual_approval.py`（6 项）覆盖真实事件选择、精确契约、持久化、cold inactive 恢复、failure/cancel barrier、幂等与下一审的原始授权证据。未运行 suite，仍由主线程统一验证；此项不能代替所有 Source 桌面状态均已验证。
