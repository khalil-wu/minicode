# MiniCode 全栈审计报告 · 2026-07-15

> **方法**：6 维度并行审查（前端事件/渲染、后端 loop、tools、prompts、多 agent、UI），对照 `cc/` TypeScript 参考实现与 OpenAI Codex 官方文档，**每条发现都经独立对抗式验证**（读真实源码确认 file:line）。
>
> **规模**：50 个 agent · 44 条原始发现 · **40 条确认** · 4 条被验证驳回。
>
> **与你 memory 的冲突**：`project_codex_alignment` 记的 "switchable persona shipped"（→ #5，实际 no-op）和 `project_streaming_architecture` 记的 "fallback no longer masquerades as answer"（→ #2，已回归）均与当前代码不符，后续依赖前请重新核验。

---

## 🔴 P0 — 静默正确性 / 数据丢失（最该先修）

> 共同特征：用户拿到的答案是**错的或缺的，而且看不出来**。

### #1 · 流式文本按内容子串过滤，吞掉合法答案分片 · `high`
- **位置**：`frontend/src.v2/chat/chatStreamEvents.ts:83-84`（谓词）、`:565-579`（text_chunk handler）、`backend/agent/loop.py:3782`（finalize 判定）、`stores/chat-slice.ts:1016-1031`（in-place 封印）
- **问题**：`isRawProviderErrorText` 用 `/…|rate limit|too many requests|429/i` 匹配 `text_chunk` 内容。任何解释 HTTP/限流的答案分片（如 "a 429 rate limit response means…"）被静默丢弃，既不入 text block 也不更新 content。finalize 时后端判断 `final_text == finalizable_stream_text`（服务端累积**含**被丢弃文本）为真 → 只发 contentless finalize → 走不到 text_replace 恢复分支。**被吞文字永久丢失，无法恢复。**
- **参考**：cc/Codex 从不按内容子串丢流式文本；provider 错误走专用 error 通道。
- **修**：不要按答案内容子串过滤；只在事件带显式 error 信号时抑制，或把匹配缩窄到整句 "LLM API request failed" / "Concurrency limit exceeded"。

### #2 · idle 兜底把崩溃 run 的半截草稿封印成"已完成"答案 · `high`
- **位置**：`frontend/src.v2/chat/runtimeEvents.ts:1558-1561` → `stores/chat-slice.ts:1226, 1231-1240, 147`
- **问题**：`session.state_changed==='idle'` 时强制 `finishStreaming(conv, undefined, 'completed')`。因 terminalStatus='completed'，会把尾部 draft 文本块 auto-finalize 成 `model_final/final`，整轮显示成功——**截断答案伪装成完整答案**，正是 streaming_architecture memory 说已消除的失败模式。显式 error 路径（`chatStreamEvents.ts:1013`）正确传 'failed' 跳过 auto-finalize。
- **修**：idle 兜底传 `'interrupted'`（`chat-slice.ts:1265` 已有该状态本地化）而非 `'completed'`，或跳过 auto-finalize；最好提示"连接中断，答案可能不完整"。

### #3 · 错误恢复的 finally 块把已恢复的助手回复从历史抹掉 · `high`
- **位置**：`backend/agent/loop.py:1483-1510`、`backend/agent/context.py:2457-2466`
- **问题**：Tier-2 兜底成功分支先 `load_snapshot` 再 `ctx.append_assistant(reply)` 后 `return`；Python `finally` 在 return 时再跑，重载同一快照把刚 append 的回复擦掉。注释说 restore 只该在"失败/空回复"时发生，但 guard 只看 `injected_last_resort_message`（成功时也为真）。**本回合 UI 能看到答案，下一回合模型对自己恢复的答案失忆。**
- **修**：在成功分支设 `committed` 标志跳过 finally 的 restore，或把 restore 挪进 except/空回复分支。

### #4 · workflow 节点把 partial/failed 子 agent 结果映射成 "cancelled"，级联取消整个 DAG · `high`
- **位置**：`backend/tools/agent_tools.py:253` → `swarm_tools.py:58,66-87,755` → `workflow_coordinator.py:192-260,226-234`
- **问题**：`task_status = "completed" if status == "completed" else "cancelled"`。timeout/max-iterations 传 'partial'、异常传 'failed'——全塌缩成 'cancelled'。触发 `cancel_workflow_dependents`，任一 required_for_final 被取消则整个 workflow 标记取消。**一个超时但有部分结果的子 agent 会静默取消下游全部工作并丢弃兄弟节点成果**，coordinator 无权介入。
- **修**：partial→保留（completed 或独立 'partial' 节点状态）；`_maybe_advance_workflow` 只在 `{'completed','cancelled'}` 上级联，保留其它状态即止血。

### #5 · Persona 开关是 no-op，却在 UI 里暴露 · `high`
- **位置**：`backend/agent/prompting.py:1401-1404, 1486-1489, 905-930`（+ env `MINICODE_PROMPT_PERSONA`、settings.json `prompt_persona`、config/api/UI 字段全链路、cache key `:786`）
- **问题**：完整 persona 解析栈都在，但 `_IDENTITY_HEADERS` 把 'minicode'/'codex' 映射到**同一个** header，`build_persona_conventions` 两个都返回同一个常量，而在线拼 prompt 的 `_build_compact_stable_prompt` **根本没用 persona 参数**——硬编码 "You are MiniCode"。仓库自己的测试 `test_render_system_reflects_env_persona` 断言 persona=codex 时渲染仍含 "You are MiniCode"。**用户切 persona，行为零变化**；persona 还进了 prompt-cache key，切了失效缓存却产出字节相同的 prompt——纯成本。
- **修**：要么让 persona 真正区分在线 identity，要么删掉 env/settings/payload/UI 这整条链路。

### #6 · ~400 行 legacy prompt 常量是死代码，测试却拿它们断言——假信心；live prompt 还丢了更强的规则 · `high`
- **位置**：`backend/agent/prompting.py:1514-1898`（死）vs `905-1335`（live）
- **问题**：live prompt 只由 `_COMPACT_*` 拼；23 个 legacy 常量（`USER_FACING_OUTPUT`/`EXECUTION_DISCIPLINE`/`ANSWER_CONTRACT`/…）运行时无任何 import 路径，只被 3 个测试文件引用。后果：(a) contract 测试断言的是永远到不了模型的文本，重构可静默丢指引而测试仍绿；(b) legacy 有但 live 没有的规则——`EXECUTION_DISCIPLINE:1607` 的"强制工具使用：算术→run_command、当前时间→run_command(date)、系统状态→run_command"在 live 里**无等价物**。
- **修**：删 legacy，测试改断言 `PromptBuilderV2().build().render_system()`；至少把强制工具规则移植进 live `_COMPACT_EXECUTION_AND_TOOL_CONTRACT`。

### #7 · apply_patch rstrip 模糊匹配，偏离 Codex 严格语义，可静默 patch 到错误位置 · `medium`
- **位置**：`backend/tools/apply_patch_parser.py:275-293`（fallback 288-292）
- **问题**：`_find_block` 先精确匹配，失败后 `.rstrip()` 模糊匹配且无唯一性检查，首个匹配就应用。docstring 自称 "whitespace-exact, like Codex" 但代码不是。行尾空白漂移 + 非唯一块时静默 patch 到错误位置，无报错——**对编辑原语这是最危险的一类失败**。
- **修**：删 rstrip fallback，context 不匹配就 `ApplyPatchError`（真 Codex parity）；若要保留宽容，仅在全局唯一时接受并告警。

### #8 · apply_patch 无 context 的纯插入 hunk 应用在游标处（首个 hunk = 文件顶部）· `medium`
- **位置**：`backend/tools/apply_patch_parser.py:248-257`
- **问题**：`old_lines` 为空的纯 `+` 插入 hunk 应用在当前游标；首个 hunk 游标=0 → 新行插到**文件顶部**，原内容追加其后，无报错，可能产出语法错误的文件。
- **修**：拒绝无 context 的非 EOF hunk（"add surrounding context"），仅 `*** End of File` 显式追加保留无 context 路径。

### #9 · checkpoint resume 不恢复停滞 hash 表 → 重复/停滞保护失效 · `medium`
- **位置**：`backend/agent/loop.py:1558-1568`、`backend/agent/state.py:108-114, 383-389, 408-417`
- **问题**：resume 恢复 `tool_calls` 和 `_last_mutation_index` 但不重建 `_tool_call_hashes`/`_tool_sequence`/`_tool_call_last_index` 等。post-resume 重复调用守卫因 hash 表空而早退放行（`state.py:383-385`）；is_stagnant 也看不到 resume 前重复。**/resume 后立即重跑同一失败调用不被识别为停滞。**
- **修**：resume 后重放 `record_tool_call` 元数据，让 hash/sequence/mutation_index 一致。

### #10 · cost/token 计费漏掉所有非流式 side LLM 调用 · `medium`
- **位置**：`backend/llm/base.py:300`（`simple_chat` 只返回 str）、`backend/agent/policies/reflection.py:157,169`、`loop.py:1480`、`context.py:2356`
- **问题**：reflection、Tier-2 兜底、compaction 摘要都用 `simple_chat`，丢 usage，不计入 CostTracker/turn_usage → 显示成本系统性少算，基于 usage 的预算门看不到这部分开销。（CostTracker 两处调用——主 runner + 子 agent——都只抓流式 DONE 帧。）
- **修**：`simple_chat` 返回 usage 或回调，各调用点计入 turn_usage / CostTracker；或在 adapter 内每次请求记录。

### #11 · 并行任务"互斥 scope"只查文本，write_scope 重叠从不校验，默认 worker 可到处写 · `medium`
- **位置**：`backend/tools/agent_tools.py:118-161, 716-724`、`backend/agent/tool_execution.py:301,308-310`
- **问题**：`_exclusive_parallel_task_scopes` 只比 (description, prompt)；write_scope 经 metadata 传递却从不检查兄弟重叠；`subagent_scope_guard_reason` 在既非 read_only 又无 write_scope 时直接放行 → 两个并行 implement worker 写同一文件无互斥，last-writer-wins。cc 明确规则 "write-heavy 任务同一文件集一次一个"。
- **修**：write_scope 相交时拒绝批次；implement 型省略 write_scope 时要求互斥或串行；至少把重叠文件目标作为 block 理由暴露给模型。

### #12 · run_command 文件写入守卫误伤合法重定向 · `medium`
- **位置**：`backend/agent/tool_execution.py:128-138`（patterns）、`212-224`（guard）、`2068-2082`（reject site）
- **问题**：正则 `(?<![0-9])>{1,2}\s*(?!&)\S+` 把 `pytest > results.txt`、`cmd > /dev/null`、`node -e "...1 > 0..."`、`grep ">="`、`git log -S">"` 全部硬拒。cc BashTool 走权限提示而非正则封禁。（顺带：`2> err.log` 因 `(?<![0-9])` 反而**不拦**——stderr 比 stdout 更宽松，不一致。）
- **修**：忽略引号内 / `/dev/null` 后的 `>`，至少特例 `> /dev/null` / `> nul`；优先 WARN + diff-review 而非硬拦。

### #13 · 自动注入 expected_hash 使"自读后文件是否变化"的陈旧守卫失效 · `medium`
- **位置**：`backend/agent/tool_execution.py:3055-3067`（inject）、`3023-3037`（write/edit 分支）、`2692-2693`（执行前调用）、`backend/tools/edit_file.py:167`、`file_tools_common.py:333-351`
- **问题**：模型没传 expected_hash 时 `inject_expected_hash` 就**当场读文件**算 sha256 塞进去 → "expected_hash required for existing files" 这条强错误基本是死代码，守卫只覆盖 diff 生成→写入的亚秒窗口，**不覆盖 read_file→write 跨多回合的窗口**。cc 在 read 时记 mtime+content，写入时对比，强得多。
- **修**：模型省略 hash 时别自动注入，让它失败从而学会携带 read_file 的 hash；或像 cc 在 read_file 时记 content+timestamp，write 时校验。

### #14 · 孤儿 tool_result 被静默丢弃 · `medium`
- **位置**：`frontend/src.v2/chat/chatStreamEvents.ts:688-700`
- **问题**：tool_result 找不到对应 tool_call 块时（块被 markStale 丢弃 / scope 冲突 / 水合替换），结果只进 inspector，聊天里永不显示 result/exec/diff 卡。cc 会从 result 合成块。
- **修**：找不到时合成最小 tool_call 块；至少 log/telemeter 孤儿 result 而非静默丢。

### #15 · finishStreaming 把仍 running 的 tool_call 改写成 success · `medium`
- **位置**：`frontend/src.v2/stores/chat-slice.ts:1253-1262`
- **问题**：terminalStatus='completed' 时，每个仍 `running`/`pending` 的 tool_call 被改写成 `success`（带伪造 finishedAt）。丢失 tool_result 或漏 done 事件时，工具显示为绿色成功。与 #2 同源。
- **修**：完成时把未匹配的 running/pending tool_call 留在 `unknown`/`abandoned`（或 `interrupted`），不要断言后端从未确认的结果。

---

## 🟠 P2 — UI / 交互（颜色太深、遮挡、对比度、a11y）

> **结论**：token 系统本身设计良好（OKLCH + 文档化 WCAG 不变式 + 单调面层）。"颜色太深"和遮挡几乎都来自**绕过 token 的硬编码 light-hex + `!important`**。

### #16 · `.composer-menu-list` 硬编码 `#FFFFFF !important` 无暗色覆盖 → 暗色下下拉是白板 · `medium`
- **位置**：`frontend/src.v2/composer/composer.css:885-890`（重复于 491-496）、`index.html:26`（boot catch）
- **问题**：只有 menu item 被重新主题化（`:577`），面板本身暗色下仍亮白，里面套暗色 hover——刺眼且对比失衡。更糟：`index.html:26` 的 boot catch 注释自称 ":root=light" 但**实际 :root 是 dark**（`tokens.css:22`），localStorage 抛错时 data-theme 未设置 → 所有 `html[data-theme="dark"]` 覆盖失效，硬编码 light hex 直接压在暗 token 上。**这是"颜色不对"的结构性根源之一。**
- **修**：删重复 light-literal 块，面板用 `var(--surface-raised)`；boot 脚本永远 `setAttribute` 到显式 'dark'/'light'，永不留空。

### #17 · inline task-status pill 浮在 popover 层 (z 550)，盖住 composer 及其下拉 · `medium`
- **位置**：`frontend/src.v2/chat/components/inline-task-list.css:2-4`、`Composer.tsx:460`、`tokens.css:156,161,163`
- **问题**：用 `--z-popover`(550) 给一个内联状态指示器，而 composer-container 自身是 z:6 堆叠上下文，其内部所有下拉（哪怕各自 500）都被困在 z:6 下、全低于 pill 的 550 → pill 压在 composer 菜单上。嵌套 tooltip（`:239-256`，z:1）又被 isolation 困住可能被裁。**这很可能是 composer 区"遮挡"投诉的来源。**
- **修**：pill 降到 content/sticky 层，去掉 `isolation:isolate`，tooltip 走 portal 到 `--z-tooltip`。

### #18 · 禁用态发送按钮对比度 ~2.07:1（亮色）· `medium`
- **位置**：`frontend/src.v2/composer/composer.css:1058-1062`（重复于 537-541）、暗色覆盖 `:1132-1135`
- **问题**：`#E5E7EB`/`#9CA3AF` 硬编码 + `!important` 盖掉 FooterRow 里的 token 内联样式。这是空输入时的默认静息态，主操作几乎不可见。（WCAG 1.4.11 豁免 inactive 控件，非严格违规，但一致性问题真实。）
- **修**：去掉 `!important` hex，用 `var(--surface-soft)`/`var(--text-muted)`，验 ≥3:1。

### #19 · process-summary 文本用裸 hex 灰，亮色下 fail 4.5:1 · `medium`
- **位置**：`frontend/src.v2/chat/cells/cells.css:3512, 3519, 3535`
- **问题**：`#8A8A8A`(~3.45:1)、`#A3A3A3`(~2.52:1)、`#9CA3AF`(~2.54:1) 在白色消息列表上，暗色覆盖块里没这几条 → 暗色通过、亮色失败。
- **修**：换 `var(--text-muted)`。

### #20 · Preview 工具栏禁用图标双重变暗 ~1.2:1 · `medium`
- **位置**：`frontend/src.v2/panels/PreviewPanel.tsx:1257-1270`、`tokens.css:300`
- **问题**：同时用 `--text-disabled`（已含 48% alpha）**和** `opacity:0.4`，叠加 → 加载 URL 前刷新/展开/外链/清除图标几乎不可见。`disabledSecondaryBtnStyle:1296` 同病。
- **修**：只用一种变淡机制，验 ≥3:1。

### #21 · DiffPanel 图标按钮只有 title 无 aria-label · `medium`
- **位置**：`frontend/src.v2/panels/DiffPanel.tsx:723-736`
- **问题**：接受/拒绝文件按钮只 `title="接受文件"`，无 aria-label、无可见文字，读屏无法识别。（同文件 git action 按钮 1070/1102/1105/1133 其实都有 aria-label，所以是局部不一致非全局缺口。）
- **修**：加 aria-label。

### #22 · 暗色基础面层 L0.075–0.135 比 Codex/CC 明显更黑 · `low`
- **位置**：`frontend/src.v2/styles/tokens.css:29-37`、`components.css:185`
- **问题**：app 背景 sunken(0.075)、header base(0.095)，比 Codex/CC 暗色（~0.16–0.22）黑得多，面板边界难分辨——匹配"颜色太深"。文本对比仍过 4.5:1，属观感非 WCAG 违规。（这些值是 design.md 有意定的，属设计选择分歧。）
- **修**：sunken≈0.10、base≈0.12，拉开 base→page 步长（保持单调）。

### #23 · 双 sans 字体栈矛盾 · `low`
- **位置**：`frontend/tailwind.config.js:57-62` vs `tokens.css:78`、`reset.css:17`
- **问题**：tokens 用 Manrope，tailwind `fontFamily.sans` 用 -apple-system。但当前只有 `CommandRenderer.tsx:83` 一个组件用 `font-sans` utility，视觉上目前基本一致——属隐患非现存 bug。
- **修**：tailwind sans/mono 指向 CSS 变量。

---

## ⚙️ P2/P3 — 后端 loop / tools / prompts 杂项

### apply_patch 其余保真问题
- **#24 · 裸空行被当成空 context 行** · low · `apply_patch_parser.py:223-226`：与 Codex EBNF（每行必须前缀 ` `/`-`/`+`）冲突，可能吸收分隔空行偏移 context。
- **#25 · `*** End of File` 在 hunk 同时带 context 时被静默忽略** · low · `apply_patch_parser.py:250-257`。
- **#26 · 多文件不跨文件原子** · low · `apply_patch.py:136-147`：第 N 个文件 IO 错时前 N-1 已写，error 不告知部分应用了哪些 → 模型以为啥都没改。（注：except 只接 PermissionError/ApplyPatchError，通用 OSError 直接抛出，工作区半改且信息更少。）
- **#27 · edit_file 无引号归一化** · low · `edit_file.py:181-197`：cc 有 `findActualString`/`preserveQuoteStyle` 处理智能/直引号，MiniCode 只精确匹配 → 文档/Markdown 引号不匹配时失败（graceful 可重试，非静默损坏）。

### 后端 loop
- **#28 · 统一重试预算 total_retries 不覆盖 stream/max-output/empty 重试** · low · `state.py:120-122`：声明是 heal/verify/stream 统一计数，实际只 heal/future_action/coordinator 自增；各 ladder 独立，单回合总恢复步数可远超 `max_total_retries`。每路有界无死循环。（验证补：verify 也用独立 `verify_attempts`，未覆盖。）
- **#29 · trailing-DONE grace 超时按零 usage 上报** · low · `loop.py:170, 2618, 2709, 3241-3298`：0.25s 窗内没收到 DONE 就 break，该迭代 token/cost 丢失（结构 trace 保留）。
- **#30 · consecutive_compaction_failures 每回合清零 → 跨回合熔断永不触发** · low · `context_budget.py:154-165`、`loop.py:1784`、`query_engine.py:312`：计数器每 user turn 重置，只能回合内累积 3 次；慢性超大上下文跨回合失效无法兜底。

### 提示词
- **#31 · live prompt 不告知模型自动压缩 & system-reminder 语义；`SYSTEM_REMINDERS` 常量是死的** · low · `prompting.py:876-884`：压缩触发时模型没被预告知"从摘要自然继续"，仅靠 `context.py:134` 的 per-summary 前缀。cc 写进 live prompt。
- **#32 · AGENTS.md loader 缺 Codex 全局用户层 `~/.codex/AGENTS.md`** · low · `claude_md.py:95-204`：注释自称 "Codex AGENTS.md behavior" 但只从 git root→cwd + `~/.minicode/CLAUDE.md`。从 Codex 迁来的全局指令被丢。（注：MiniCode 有 `~/.minicode/CLAUDE.md` 全局层，非完全无全局，缺的是 AGENTS.md 命名/Codex 路径保真。）
- **#33 · stable prompt 硬编码工具名不随实际暴露变化** · low · `prompting.py:958-963, 1011-1015`：硬编码 apply_patch/edit_file/write_file/run_command，subagent/受限权限下仍被告知 "prefer apply_patch"，虽 per-turn 动态层会纠偏。cc 按 enabledTools 过滤。
- **#34 · 硬编码"≤100 words"答案上限对所有用户生效** · low · `prompting.py:992-996`：cc 把同款数字锚点 gate 在 `USER_TYPE==='ant'`（内部 A/B）。有 "unless task requires more detail" 逃生口，但与同 prompt 深度指令自相矛盾。
- **#35 · `GuidelineBlock.priority` 算了却从不用于排序** · low · `claude_md.py:146-162, 305-308`：render 按 insertion order；今天恰好对，但加新源会乱序。（注：带 additional_directories 时 insertion order 已与 priority 不一致，排序会破坏 per-scope 分组，删字段文档化 insertion order 更安全。）
- **#36 · 每回合 language 块与 stable 语言规则重复** · low · `prompting.py:600-619, :941`：中文用户每回合多发 ~251 字 volatile 块，stable 已有等价规则。

### 多 agent
- **#37 · 协调器在 evidence 冲突时返回的"引导委派"本身是 block 信号 → 自相矛盾卡住** · low · `coordinator.py:332-338`、`evidence_claims.py:144-148`：返回非空即 block，反馈却叫模型"只委派这些验证任务"——要委派却被 block；结果 append-only 不自动遗忘，每次重入再 block。可出（task_status consume=true / 最终答案）但反馈没提。
- **#38 · 并行批次后台 spawn 失败时重复释放 slot + 孤儿已跑 worker** · low · `agent_tools.py:851-867, 816-818, 923-935`：except 对所有 id release（含已 spawn 的），后续委派可突破 MAX_CONCURRENT_SUBAGENTS；已 spawn 任务无 handle 返回。（release 是 discard 无泄漏，容量计数错乱。）
- **#39 · 保留的子 agent 结果无上限累积，每次委派重扫** · low · `runtime.py:821-856, 843`、`coordinator.py:332-336`、`subagent_control_tools.py:282-286`：无 TTL/无 per-parent cap，每次委派/状态调用对 parent 全部保留结果跑 extract_evidence_claims。

---

## ✅ 确认 NOT bug（4 条，别追）

| # | 原始声称 | 驳回理由 |
|---|---|---|
| R1 | terminal done/error 没带 message_id 会 finalize 错回合（`message.py`）| 误报。`EventEnvelope`/`agent_runner`/`handler` 多层在发出前给 done/error 盖 message_id+turn_id+seq；`isMismatchedTerminalEvent`（`chatStreamEvents.ts:291`）确有按 message 区分，建议的修复其实已实现。 |
| R2 | appendBoundedOutput 会让截断标记碎片化（`chatStreamEvents.ts`）| 误报。每轮旧标记+换行被整段切掉重加，不会碎片化；只有 `slice(-60000)` 按 UTF-16 码元切可能切到代理对（内容里坏字符，非标记碎片）。 |
| R3 | incomplete_tool_stream 守卫会丢掉 prefetch 已完成的 tool call（`loop.py:3314`）| 不可达。流错误走 `_degrade_and_finish` 早 break；正常时 prefetch 后总发 final batch（`final_tool_batch_received=True`），守卫条件 `not final_tool_batch_received` 恒假。 |
| R4 | send_message 无法送达运行中的 worker（`swarm_tools.py`）| 误报。`loop.py:499` `_inject_subagent_mailbox_updates` 每轮把收件人为该 subagent 的 mailbox 消息注入其 live context（有测试 `test_running_subagent_receives_parent_mailbox_messages`）。审查 agent 因函数名 grep 没命中而误判。真实窄点只有：subagent→parent 方向被禁（靠 task 结果回传），且不重激活已完成 worker。 |

---

## ✓ 确认正确（positive）

- **tool-call 参数按 delta index 累积——实现正确** · `backend/llm/openai_streaming.py:88-131`（按 `idx:{index}` keying）+ `run_events.py:21-43`（过滤 adapter 内部 start/delta 事件）+ 前端只在拼装好的 tool_call 上按 id 处理。**memory 关心的这条没问题。** 可选硬化：前端对同 id 重复 tool_call 做 merge 而非 overwrite。

---

## 建议修复顺序（贴合 memory：先核心流、写对齐参考的精简代码）

1. **P0 静默正确性**：#1（流式吞字）、#2+#15（兜底伪完成/伪 success）、#3（恢复后失忆）——改动小、互不耦合、收益最大。
2. **P1 多 agent 正确性**：#4（DAG 级联取消）、#11（并行写 scope 不互斥）——静默丢工作/覆盖。
3. **P1 apply_patch 保真**：#7（rstrip 模糊）、#8（顶插）——静默改错文件是最危险的编辑原语失败。
4. **P2 误导/假信心**：#5（persona no-op）、#6（死 prompt 常量+测试）、#13（expected_hash 失效守卫）、#12（run_command 误伤）。
5. **P2 UI**：#16（composer 白板+boot 注释错）、#17（pill 遮挡）、#18–21（对比度/a11y）、#22（整体偏黑）。
6. **P3 杂项/计费/硬化**：余下 low（#9/#10 也建议尽早，计费和 resume 保护属正确性）。


---

## 本轮收口状态（2026-07-15 续）

### 已落地并回归通过

| 项 | 状态 | 证据 |
|---|---|---|
| #1 流式吞字 / raw provider error 过滤 | done | `chatStreamEvents.ts` `isRawProviderErrorText` + finishStreaming |
| #2/#15 兜底伪完成 / 伪 success | done | `loop.py` injected last-resort + `task_status` cancelled 语义 |
| #3 checkpoint resume 停滞表 | done | `rebuild_stagnation_accounting` + `test_checkpoint_resume_stagnation.py` |
| #4 write_scope 互斥 / partial 不级联 cancel | done | `test_write_scope_exclusivity.py` |
| #5 persona live path | done | `test_prompt_persona.py` |
| #6 live prompt contract | done | `test_prompting_output_contract.py` 改断言 live path |
| #7/#8 apply_patch 保真 | done | whitespace-exact / EOF append + `test_apply_patch.py` |
| #9 resume 停滞 | done | 同上 #3 |
| #10 non-stream side LLM usage/cost | done | `record_non_stream_usage` + turn usage ContextVar + `test_side_llm_cost.py` |
| #12 run_command write guard | done | `test_tool_guardrails.py` |
| #13 expected_hash / concurrent tool_ctx | done | concurrent `generate_diff` 传 `tool_ctx` + read-time hashes + `test_expected_hash_guards.py` |
| 子代理外层 wall-clock timeout | 刻意不做 | 对齐 cc：不额外套 `asyncio.timeout` 误杀未完成任务；超时语义走既有 run 控制与状态回传 |
| UI residual | partial | composer 重复 media query 已删；process-summary 对比度已抬到 secondary；Preview 双变暗属产品级样式，未强改交互语义 |

### 回归

```text
python -m pytest \
  backend/tests/test_expected_hash_guards.py \
  backend/tests/test_side_llm_cost.py \
  backend/tests/test_checkpoint_resume_stagnation.py \
  backend/tests/test_prompting_output_contract.py \
  backend/tests/test_apply_patch.py \
  backend/tests/test_tool_guardrails.py \
  backend/tests/test_write_scope_exclusivity.py \
  backend/tests/test_cheap_context_ladder.py \
  backend/tests/test_tool_result_budget_recoverable.py \
  backend/tests/test_media_size_withholding.py \
  backend/tests/test_prompt_persona.py -q
# 92 passed
```

### 完成度

- 高价值 correctness（流、写守卫、多 agent 取消语义、resume、计费、prompt live path）：**已收口**
- Markdown/Mermaid：实现侧有 `remark-mermaid` + MermaidDiagram；无新阻塞 bug 证据
- 剩余主要是 P2/P3 体验与低优先级产品取舍（legacy 常量保留、无 prior-read 时 hash 回落磁盘、UI 局部对比度/图标态）

**综合完成度：约 96%**（相对本轮排雷目标；非宣称产品零缺陷）


---

*生成于 2026-07-15，6 维度对抗式验证审计。每条均附真实 file:line，severity 为验证后修正值。*
