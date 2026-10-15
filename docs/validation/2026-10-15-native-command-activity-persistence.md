# 原生命令 planned / started / terminal 的持久化消费

Root 补齐本批后端 consumer，接口/producer消费范围按节点最新71/71通过、0失败/错误/跳过：`output/native-command-activity-persistence-20261015-latest-results.json`。后续独立审阅发现direct工具回调仍绕过journal入口；因此这71不能证明正常QueryEngine的执行中冷恢复已经闭合。具体缺口及最终修复验证在下文继续记录。桌面原profile实机伴随另记，不把自动检查当成图像验收。

## 依据与实际缺口

Desktop 已核 pinned Source 与实际安装版 app-server：冻结的 commandExecution planned item 先于审批，request 单独归底部，允许后同 id 开始真实执行；拒绝/取消可没有 exit/output/duration。provider 的空参数流式 announcement 不等同于这个 planned item。具体消息证据见 `source-native-command-events-run01` 与 native 后端交叉审阅报告。

新增的 `streaming_announcement`、`command_presentation`、`execution_started_at` 原先只进入 WS snapshot 与部分 producer receipt。AgentTurnState 与 journal 白名单会丢它们；QueryJournalRecorder 又忽略所有 pending tool_call，因此只等待审批而未执行的 native request 在冷恢复中可能被补成 args={} 的合成 use。直接按 WS 通过不能证明 canonical/SDK/cold 一致。

## 补齐的生产链

- AgentTurnState call/result/resume 保留这三项实际事实，明确 false 和时间0不丢失；同一 item 重投时保留已观察的执行事实。原冻结显示时间与实际执行开始独立。
- 真实 `runtime.span:tool.started` 更新已有 native record，并沿原 AgentRunner partial 保存。没有对应 native planned 记录时不造记录；已经终态的记录不被迟到 started 降回 running。没有另起 viewer timer。
- 真实 stdout/stderr preview、exit code（包括0）、command actions 同样完整进入 canonical result，避免 live delta 可见而冷记录缺失。
- QueryJournalRecorder 将 provider announcement 保存为原始、transcript-only lifecycle，不作为 execution use。真正带 frozen presentation 的 pending request 保存原 args/id/digest。
- ExecutionJournal use/result 白名单保留三字段；同 id 请求重复时只有实际阶段事实变化才写更新。真实 tool.started 更新原 open use，冷模型历史仍只有同一个 call，不制造第二次命令执行。
- 没有拿到结果的真实中断，沿既有 explicit synthetic terminator 保留已有 presentation/start，缺失 exit/duration 继续缺失，不编造0或 elapsed。原 timer/journal terminal、模型借用与资源清理所有权未重写。

## 验证

新增5项覆盖 canonical call/resume/result、provider announcement 与 frozen planned 的 raw/use 区分、真正 started 的幂等冷读取与缺结果中断，以及实际 native producer 的批准/PowerShell执行和用户 abort 两条完整链。实际两条都经过 QueryJournalRecorder → append/read/reopen/reconstruct，并核同 id、真实 argv、输出和计时；abort 保留原取消传播且没有 execution start/exit/duration。

必要旧范围包含 AgentTurnState、context journal delta、canonical controls、admission/resume、原运行时长 owner 和同批 native producer。首统一67节点63通过/4失败：2个新fixture漏传必填 user_message，另2个旧fixture尚未跟上实际manual compact参数和command_type关键字。失败XML保留。

仅纠正fixture再窄验，新5项全过；旧恢复测试还暴露其 drain 只等待transport任务，提前关掉了仍在跑的真实execution任务。现按dispatcher两个真实任务集合排空。旧compact测试改为在已提交的compacted事件发送失败，而不按脆弱的“第4次发送”计数；原错签名此前根本没有到达提交。最后一个被共享helper覆盖的旧fixture补明确preview.refresh参数契约。没有修改生产来适配旧fixture，没有隐藏pending task警告。各次latest按classname/name去重71全过，不相加重跑数。

本批首frontend冻结构建 TSC/Vite、UI debt、bundle budget 均通过，身份在 `output/native-command-activity-final-build-identity-20261015.json`：entry `assets/index-Cz0Kkr_F.js`，index SHA `792253278a0bb46649779d6d3681c76bdf5be0b6373f7f198c1d20ebb27f0dbf`。随后直接读取Source输出组件确认✓成功有依据，且无exit/未interrupted应显示展开详情中的“退出码 未知”，先前隐藏unknown的推测已撤回；native未执行的用户拒绝不强造Stopped。最小修复与19项必要unit完成后只重构建这一次，最新entry为 `assets/index-BqCYZOvL.js`、index SHA `958917700b4f83c97b82857274fc2ea2a464229eb59de70b5314f18ed993a746`，见 `native-command-activity-footer-final-build-identity-20261015.json`。CSS仍为 `index-CpG3V_Lq.css`，SHA `1c87eb577857c9aa7a7bbd66a71660172858449eaa7b12a6714529ec7011d621`，两次均与此前BT3逐字节一致。

首identity collector曾选到theme-boot/monaco-base，已保留为collector-initial文件并改为实际index entry匹配；该错误身份未用于实机或打包验收。新后端wheel与Node其它源仍等待整批最终冻结，不将这份frontend身份误称整个产品完成。

## Direct callback 的后续交叉审阅

独立审阅查明：`record_event_async` 没有接普通runtime.span，且真实direct `ToolExecutionContext.emit_event` 本身会直接调用宿主，绕过QueryEngine stream的journal分流；只补异步白名单仍不够。前述producer测试的consume手动调用`record_event`，证明了consumer但没有证明这条真正入口。CodeMode nested仍有自己的原owner路径，不能用其通过替代direct。

最初候选wheel已暂停交付，原build/install与hash在`source-internal-evidence-delivery-initial-status-20261015.json`保留，没有给它计冷验通过。随后真正QueryEngine direct callback已接同一journal owner，SDK无宿主callback也在实际入口先写启动事实；nested沿原路径，不创建第二套owner/queue/clock。late callback必须匹配原turn/iteration，不会因provider call id复用覆盖新回合时钟。原unresolved/synthetic路径还保留真实command_actions。

这条入口经真实PowerShell写entered并阻塞、执行中从新journal实例读取、放行后核输出；direct/nested/CodeMode及有无host共6种组合，加真实两turn旧闭包回放，共新增7项全通过。连同必要旧范围按节点最新67全过，原5 consumer也在该最终字节下复验；详见`2026-10-15-native-query-start-admission.md`。首frozen context赋值与后续fixture失败完整保留，没有通过兼容层绕过。

最后按Source实际空aggregatedOutput，将未执行native终态的model-facing receipt从Shell stdout回退中移除；raw summary/error保留，typed stdout/stderr和实际执行过的异常仍显示。前端必要范围最终21/21。最新构建为 `assets/index-2l4V0mq0.js`，index SHA `b8195e87598a308abe3073b63ac9ac948df585f06ed6b00fb81f688454fad142`，TSC/Vite、UI debt、bundle budget和protocol sync均通过，CSS仍是上述相同SHA。`production-native-command-activity-companion04`以已有abort只读核对，无新provider/命令，最终09图Shell为空、unknown footer与raw receipt分别保留。

Root独立逐张查看旧allow的01/02/04/05、最终03的5张以及最后04的09图，未把中间动画帧当settled图。原profile已有8条transcript、draft/rules都保持；执行start、duration3253ms和exit0在真正cold后保持，原abort无执行clock/exit/duration。最终wheel与安装冷验见`2026-10-15-source-internal-evidence-delivery.md`，这不把整个Codex产品等价算作完成。
