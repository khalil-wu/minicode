# ContextCompaction 与独立手动任务的前端链

新增实机发现：模型切换截图显示自写“已安排压缩上下文(85%)”toast。Source协议公开的是`thread/tokenUsage/updated`和真实`ContextCompaction` item；已核实桌面xS/SS负责实际压缩生命周期，所查assets没有此自写调度文案。MiniCode现在保留真实`budget.warning`事件、Inspector中的bucket/percent/will_compact/replayed、当前用量与后台调度，只移除自写预算/调度弹窗。真正压缩开始/结束/中断继续原投影。`root-budget-projection-run01`两文件94项通过，包含live/replay与will_compact两种值；原范围有重叠，不称新增94项。

Source `core/src/compact_token_budget.rs` 发真正 `ContextCompaction` started/completed；`app-server-protocol/src/protocol/v2/item.rs` 的公共 item只有实际id。普通 `new_context/get_context_remaining` FunctionCall/Output 本身不构成普通活动。桌面 `sites-end-resource::xS/SS` 显示独立状态，运行十秒后加原“可能需要几分钟”，区分Work优化、manual、自动压缩和中断。中文来自安装版zh-CN，英文来自同一Source默认文案。原 glyph是 `app-shared` 实际导出 `S2i as vu`，对应4条原path；不是按名称另找类似图标。

MiniCode transport仍使用自己的事件名。Root补齐 `item.started/completed` 的 context_compaction类型，前端不再只接受agent_message。auto关联实际assistant；manual是独立操作，没有assistant_message_id时以实际id保存唯一system/process记录，不借当前回复、不创造助手消息。空content是这种真实状态的合法形状；live、public历史和重开均保留原开始/结束毫秒。过程区用专门的Source行，并在独立任务中省去额外“已处理”头。

预算兼容事件携带同一 `compaction_item_id`，只更新实际usage/Inspector；conversation.compaction.updated继续更新会话元数据，不再另造重复notice。内部模型工具成功的 debug可见性由实际producer给出，前端按现有visibility投影；原JSON、文本及错误保留原记录，不按未知工具名隐藏。

没有改字体或字号。正在处理的文字复用已有过程扫描，增加了对应reduced-motion选择器；完成和中断停止扫描。手动任务独立呈现，不能被造为主用户消息或新的模型最终回复。

实际组件/协议证据位于 `output/playwright/codex-control-audit-20261013/`：

- `root-context-projection-run01`：7文件214项通过、0跳过。包括真正normalize→stream reducer→process projection→Source行、十秒、原时间/冷恢复、真实内部记录保留和foreign owner；旧validator/runtime/AgentTimeline/回复行验证有重叠，不能当全控件数量。
- `root-context-projection-run02`：实际收集2文件36项通过；新增独立manual任务无fake assistant/额外过程头及普通agent_message缺owner仍拒绝。命令中第三个不存在的AgentTurn.test未被收集，不计入通过数。
- Source压缩 glyph提取和hash清单：`source/context-compaction-icon-manifest.json`。全链生产截图使用后续最新构建；组件通过不冒充桌面全场通过。
- `root-context-projection-run03`：两文件56项通过，有旧49项重复。真正桌面诊断捕获旧validator拒绝Source合法空summary，客户端随即关闭WS，ContextCompaction完成及后续答案没有到达；重连replay再次拒绝。不是延长等待或后端nested互等。修正只有携带实际compaction_item_id的无总结状态允许空summary，缺真实id的旧空摘要/空失败仍拒绝。新测试实际经过normalize→notice/runtime→完成item，原测试只直接cast runtime不足以覆盖该入口，证据如实保留。

Header归档另沿正式null/null switched处理：原前端忽略它，后端payload contract也不接受它，均已修。home标记跟随实际command id/type与list，较晚选择胜出；正常sidebar入口仍按原行为。`root-archive-navigation-run01` 的1项真实前端blank消费失败保留；生产消费者修复后，run02三文件128项通过。源draft先snapshot，当前cowork mode保持，未保存文件仍归原workspace。

公网模型、外部Notes/MCP与全部hooks不在这些前端检查中。系统clipboard、普通用户窗口和外部应用启动没有被这批QA操作；生产端到端证据与对应受控模型/目的地拦截范围单列。

生产 `production-context-diagnostic-run01/desktop-output.log` 与 `renderer-warnings.json` 保存实际断线原因；最终 `frontend-build-run08` 成功，修后仅Window诊断另记。后端同时用原有真实usage_reported修正未报告用量被默认UsageInfo(0)覆盖的问题；`token-budget-usage-run01` 14项通过，明确reported0没有被当成缺失，原有Nested/Eventstream没有为前端问题加兜底。

最终生产 `production-control-run18` 使用build-run10，完整11场景通过，其中真实VM读取文件和剩余预算→无总结new_context→新initial模型回复，以及独立 `/compact`→真实操作item→无假assistant→Header归档Home。`10/11/13` PNG已经人工查看，原用量、窗口和操作事件存于wire；多聊天inventory没有覆盖正式Home。仅Window `production-context-diagnostic-run02` 的3场景也已通过；这两轮含重叠不累加。最终完整轮pageErrors为空、native窗口不可见，renderer有一条资源404，未称所有日志零警告。
