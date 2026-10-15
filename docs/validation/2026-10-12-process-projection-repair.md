# 2026-10-12 过程投影纠正

用户实机反馈与本轮截图指出：子 agent 完成后过程仍展开，列表出现重复“输出”，组合折叠与活动文字扫描不符，且存在 Codex 参考中没有的“显示更早”入口。

## 依据

- 实际查看正常 MiniCode 的成都天气子任务：完整最终回答上方仍展开过程，含多条“输出”。该窗口中的任务与草稿未被重载或清空。
- 重新逐张查看相关旧 Codex 参考，并结合用户新增的收起、展开图。关键依据是 `final-app-audit-20261006/codex-live-diff.png`、`codex-ui-replica-20261005/reference-user-latest.png`、`alignment-audit-20261011/codex-live-agent-detail.png` 和用户 `ca6235…`、`c261d7…` 图。
- Codex 源码：`core/src/tools/code_mode/execute_handler.rs`、`delegate.rs`、`output.rs` 区分模型输出与内层工具；`app-server/src/bespoke_event_handling.rs` 的规范 item 开始/完成及命令完成链；真实 FileChange、patchUpdated 和已存 item 分页游标。
- 公开 checkout 不包含 Codex 桌面前端动画源码。活动文字、组形态与展开外观以实际图和当前只读观察为准，不把参考截图当成完整动态测试。

## 修复

- 正常 `tool_exec`、`tool_wait`、`text()` 回执属于模型上下文，不再生成独立过程“输出”条目。内层读取、搜索、命令、编辑保持真实顺序；独立脚本失败及其截断诊断可展开，完整原始回执仍在历史和 artifact 中。
- 主与子过程共用折叠链。完成的子详情首次进入默认收起；运行、失败、中断可手动折叠，阅读选择跨返回保留。
- 组合摘要采用真实读取/运行/编辑动作，展开后连续短行、内部滚动。扫描只跟随实际执行或流式 reasoning；折叠时落在活动组合摘要，展开时落在具体活动文字，完成、等待批准和计时保持静态。
- 实际命令保留 Shell、原命令、stdout/stderr、退出状态与完整结果。收起与展开按参考切换标题；不足 1 秒不显示耗时。
- 删除 40 组、40 项等人为过程窗口和“显示更早”按钮；后端真实分页改为展开和滚动触发，保留错误与阅读位置。完成子任务列表不再手工裁剪 10 项，会话历史同样通过上滚加载。
- 每文件 Diff 的已携带上下文独立折叠，显示真实行数和行号，复用现有语法着色。补丁没有携带的正文不伪造展开内容。
- 保留 MiniCode 的字体、字号和主题配置。

## 验证范围

受影响单元和链路批次已运行。各批次存在重叠，不相加作独立用例总数；首轮旧断言失败与后续修正日志保留。TypeScript 与 Vite 最新生产构建通过，日志为 `projection-repair-production-build-final.log`。首轮构建的旧 ToolCallCard 字段错误另保留于 `projection-repair-production-build.log`，已按真实 `diff.files` 契约修正。

真实生产构建 Electron 全链路第 4 轮通过：4 个场景及 9 张过程截图，实际后端、JavaScript VM、read_file、list_files、run_command、edit_file、TaskTool、WebSocket、持久化与重启均参与。观察命令执行中的组合摘要，点击后扫描交给具体命令，完成后停止；实际文件修改核对磁盘。主用户资料不用于测试，测试不向前端注入消息、工具或子任务。

前 3 轮失败均保留，不能计为通过：第 1 轮模型 fixture 没有识别截断预览中的计算输出；第 2 轮测试点击了随展开而变化的筛选索引；第 3 轮把独立 Memory Phase 2 agent 误归入主任务。第 4 轮依据真实内部 prompt 和专用 8-tool registry 识别该后台调用，主与子任务的 code-only 契约仍单独验证。原始回执、artifact、请求与 UI 证据在 `output/playwright/codex-rebuild-20261012/code-projection-folding/`。

第 4 轮打开右侧 Diff 时的首张采图捕到了 Suspense 加载占位，因此该图本身不算 Diff 已完成渲染的证据。另用第 4 轮保存的独立 QA profile 回放真实任务，等待实际新增行、真实第 1 行号、`+` 标记与增删底色完成后补拍：`03b-real-edit-file-patch-rendered-replay.png`、`diff-rendered-replay-evidence.json`。这是额外的真实历史回放，不能声称最初那张图已经就绪。

受控模型用于稳定驱动实际后端、JavaScript VM、工具和子任务；它不代表外部 Provider 的生产验收。右侧完整 Monaco Diff 是另一条渲染链，本轮 InlineDiff 改动不等于它已全面复刻。

## `/compact` 与最终构建

正常用户日志在 17:30:22、17:39:09 明确记录 `Manual compact failed: Responses HTTP client is not initialized`。会话模型缓存切换后旧 adapter 被关闭，而空闲共享 ContextBuilder 仍持有它；手动压缩直接借用旧引用导致失败。

现在主动和后台会话压缩都捕获各自当前模型、适配器和预算，使用独立 ContextBuilder 与操作 lease，直到压缩和 CAS 提交完成才关闭。成功后只回投仍属于原会话的共享上下文；失败、实际取消与切换会话不改旧历史或另一会话。没有重建已关闭的 HTTP client，也没有吞掉真实 Provider 错误。压缩相关最终 84 项通过，其中 8 条包含真实 OpenAIAdapter、本地 HTTP、cache retire、原生加密窗口、模型切换、HTTP 400、取消与历史保留。

另外真任务暴露 301 行重复文本的一行编辑被 `SequenceMatcher.autojunk` 扩大成 `+301/-301`。共享 Diff 生成器现在把真实相同前后缀作为 equal 锚点，只匹配变化区，保持标准可应用 patch。最终受影响 135 项通过、4 项平台跳过，跳过不算通过；真 Git 正反应用与审批/工具/回合统计链均通过，10 万重复行小改用时 33–42 毫秒。

最终第 5 轮以新前端和新后端完整执行，5 个场景全部通过、fixtureFailures 为空。真实新增行、行号与符号到达后才采 Diff 图，实际首行编辑为 `+1/-1`。压缩提示不再重复套分类头，其 28 项相关前端回归通过；最新构建日志是 `compact-notice-production-build.log`。本机正常用户 MiniCode 已以新后端和该构建重新启动，未注入测试任务或修改其档案。
