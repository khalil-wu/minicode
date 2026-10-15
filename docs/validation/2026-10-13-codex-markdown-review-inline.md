# 桌面 Markdown Review 的完整 inline 链

本批承接 `2026-10-13-codex-review-explore-source-audit.md` 的源码审计，完成 inline review。没有引入 Rust `ReviewTask`、JSON rubric、Never 审批或未实机确认的 author-review gate，也没有增加未接通的 detached 按钮。持久化 agent_mode 保留原值；review 只在当前普通回合使用 Default collaboration。没有操作正常用户窗口。

## Source 消费依据

安装包和哈希见原审计报告。`app-shared-737655e1fb23.js` 的 `owr`（UTF-16 偏移 4979859）先识别 `## Code review guidelines:`，随后使用 `zp(g)` 的最后 `## My request:` 段作为 `user-message.message`；`zp` 定义位于 `Bp` 初始化约 308725。完整指南仍存在原始模型 input。本批使用现有显式 `display_content` 与 `backendContent` 契约贯通真实请求，不在前端按未知用户文本猜测裁剪。

模型批注实际链：`Bii`（5664706）从完成回合的 lastAgentMessage 提取行首 directive；`zii`（5664237）要求非空 title/body/file，数字属性解析为整数，start 至少 1，end 至少 start，用 right/end 定位；`Vii` 按真实位置和内容去重，`Hii` 按 conversation 存储；`app-initial::Mmc`（9783205）消费该完成事件，directive Markdown render handler `Gol/y9`（约 10992654）返回 null。原始文本不删除。

路径显示遵循真实 Source：`zii → Aii`（5663204）根据真实 workspace roots 找最长匹配、否则使用 cwd；`a1`（4957837）先去完整 root 前缀，再允许包含 workspace 最后文件夹段的路径别名。后一规则确实会接受外部绝对路径中同名文件夹段；本批按 root 明确要求保留这一源码显示语义，没有把它当成工具权限边界，也没有另加猜测的安全/兼容层。

Review target 行的 Source glyph 也已核验：registration `F` 的 uncommitted/base-branch item 均未绑定 Icon/leftAccessory，只有条件 RightIcon spinner；`app-initial::epl`（11127809）没有默认或父 glyph 继承。因此删除了新菜单起初误加的 Lucide GitBranch。中文目标、请求与错误文案直接核对安装包 `zh-CN-b05cfd55c534.js` 的对应 message id。

## 完成的调用链

- `/review` 选择未提交或基础分支；真实读取当前/default/recent-order 分支。菜单保留读取错误/retry，empty-composer 条件来自 Source，键盘选择/Escape 可用，提交中/权限 pending/模型未就绪禁用目标。
- `review.options` / `review.prepare` 使用真实 owner execution cwd 和 Git root。uncommitted 范围为 staged、unstaged、untracked，包含 unborn HEAD 和中文文件路径；base branch 使用真实 merge-base，不以 HEAD 伪装。冷 review 保留已接受输入的 mergeBaseSha，即使原分支已删除。
- `desktop-review-guidelines.md` 从已安装 ASAR 原样解码保存；普通用户输入按完整指南、真实目标说明、真实请求拼接。可见请求只显示 `requestMessage`。CLI 既有 `/review <补充说明>` 仍保留实际说明，默认审 uncommitted；`/review base-branch <branch>` 准备真实分支目标。
- 随后调用原有 `sendChatMessage` / `user_message`，等待真实准入。`code_review` scope 经输入解析、运行上下文、准入 echo、公开 transcript 和 cold hydrate 保留；没有写回 durable agent_mode，没有消耗未提交的附件、mentions 或 skills。
- 内置 raw slash 的 prepared receipt scope 通过显式 `capture_command_results(include_data=True)` 进入普通回合，其他 native completion 摘要维持原契约。
- 最终 directive 成为现有只读 diff 的真实文件/右侧行批注，同一行多条缺陷保留独立 cards。未在当前 patch 内的已解析评论仍能查看；不会伪装成持续加载。原始回答保留，未识别普通 Markdown 的换行/语义不变。
- 已有 workbench/draft 归属机制处理迟到回执；换会话不会覆盖新草稿。真实 review 输入准入后打开当前 owner 的 split diff；冷打开 diff 可从真实输入 scope 恢复比较。

## 必要验证与失败记录

先完成整批功能和必要测试，再统一运行。验证未打开用户窗口；真实桌面/provider 端到端由 root 独立执行，不用组件通过冒充桌面通过。

- `npm exec tsc -- -b --pretty false` 通过。
- `python scripts/check-protocol-sync.py` 通过：118 server events、126 client commands；registered commands、TypedDict 等检查也通过。
- 前端首批 5 文件 134 项：132 通过，2 个已有 Markdown CRLF 语义用例失败。根因是 directive 隐藏函数无条件 split/join 改掉普通文本 CRLF；已保留原始行结束符和非 directive 原文。修后仅重验受影响 parser/Markdown 两文件 57 项，全部通过；57 与原批重叠，不累加。最终 134 个唯一相关用例通过，0 跳过。
- 后端首批 4 文件 29 项：24 通过、5 失败。4 个 native receipt/capture 用例揭示公开摘要被不必要扩大为完整 data；已改为仅 builtin review 显式要求 data，默认摘要恢复原合同。旧 review fixture 原来没有 Git owner，现按真实 Git repository 和实际 WS 反序列化/dispatch 核对 options/prepare/普通模型输入。
- 重验 native 文件后仅旧 compact 200 fixture 的 `after.transcript == []` 断言失败：root 前批已实现真实独立 manual compaction system/process 记录。按 root 指定改 fixture，小段核对真实 producer id、owner、turn、manual/context_compaction、开始结束时间、唯一 system/process，无额外用户或假 assistant；初次新 fixture 错把 block.type 写成 context_compaction，实际为 process/itemKind=context_compaction，按源码修正。最后只重验该失败参数通过。最终后端 29 个唯一相关用例通过，0 跳过。未修改 compaction 生产实现。
- 新增测试覆盖真实 Git 三类更改/merge-base/pinned SHA、真实 scope/public 投影、旧模式独立权限、完整指南；前端覆盖同一行独立批注、冷 hydrate、迟到 owner、菜单目标与真实准入前草稿/附件保护。

Root 的真实桌面 `production-review-run01` 在首个目标菜单场景发现 UI 阻断：保留的 `/review` 草稿触发旧 autocomplete 再开，覆盖 review target 点击，provider 请求为 0。没有 force click 或抬 z-index。已修输入菜单生命周期：open/dismissed review snapshot 接管同稿的 selection effect，打开时关闭其他输入菜单；取消保留原稿并阻止同稿 autocomplete 自动重开，改稿/切 owner 释放，pending switch 不显示旧目标；渲染层与真实菜单状态共同互斥。新增同一 effect regression，局部选中 3 项通过、其余 42 项未选中，未计为通过；与原批重叠，唯一新增前端用例为 1（原 134 + 生命周期 1）。Root 将用更新构建重跑实际桌面场景，首次失败证据保留。

在已核验安装版中文值补齐后，仅重验该新生命周期用例 1 项通过、44 项未选中；不把未选中或重复用例累加。真实 Git 未提交测试还覆盖含前导空格的文件名，使用原 NUL inventory 而非 trim 路径；该受影响用例 1 项通过，不重复已过整套。

Root `production-review-run02` 的两个真实场景已通过：uncommitted、实际 merge-base、真实 VM read、最终 directive/dedup、provider Default 输入且 durable Plan 不变。冷恢复时右栏按原选择关闭，draft/原请求/最终答复恢复；由真实 UI 打开变更后再验，没有自创强制打开面板。该轮截图发现新inline card继承diff代码字体：Source `use-diff-annotations-2b4370d1c219.js` 的model分支（约33542）经 `Qt`（27432）→`Vt`（12767），Vt外容器（23194）及初始化 `Zt`（26976）定义的只读ProseMirror `Xt`（27179/27333）均显式 `font-sans`。只在新卡 `[data-code-review-comment]` 设置 `font-family: var(--font-ui)`，不改全局字体字号或源码区。最后桌面构建/截图验证由root单列。

Review 的 authored payload 已按实际 send 链核对：`startCodeReview` 直接调用 `sendChatMessage`，不经过 `buildOutgoingContext`；其 defaults 是 attachments/attachmentRefs/contextRefs 空数组、quotedMessage null，transport/display附件只从传入数组计算，skills/plugins只从传入contextRefs计算，不回读composer待发内容。Source `fpl`（11134257）的 document selector 按 `t7s(controller.getText())`→`e7s` 实现 requiresEmptyComposer，没有按附件/skill数量过滤；registration 的 isRefreshingAttachments 来自真实 attached-worktree刷新。改稿释放旧目标，同稿加附件仍不进入review payload，若上传造成真实owner变化则关闭旧菜单；没有增加想象的附件限制。

## Root 桌面 QA 接口

菜单：输入 `/review` 后发送，或 slash 菜单选择内置 `/review`。

```css
[data-code-review-menu]
[data-review-target="uncommitted"]
[data-review-target="base-branch"][data-review-branch="main"]
[data-code-review-comment][data-code-review-file="bug.py"][data-code-review-line="2"]
```

WS 操作与真实回执：

```text
review.options { execution_cwd, conversation_id?, client_command_id }
  → command.result data { cwd, gitRoot, currentBranch, branches, conversation_id }

review.prepare { mode: uncommitted|base-branch, execution_cwd, conversation_id?,
                 base_branch?, request_message?, merge_base_sha?, client_command_id }
  → command.result data { scope: { mode,cwd,gitRoot,baseBranch?,mergeBaseSha? },
                          prompt, requestMessage, diff, conversation_id }

user_message { content:完整prompt, display_content:实际request, code_review:scope,
               conversation_id?, user_message_id, assistant_message_id, client_command_id }
  → command.result command:user_message data.admitted:true
     data.input { content, display_content, code_review }
```

受控模型可以只执行真实 `read_file`，随后发送包含准确绝对路径/行号的最终 directive。完整 Source guidelines、目标、真实 cwd 应在 provider raw evidence 中核验；用户气泡、准入 echo、冷恢复和 Markdown copy 使用同一真实 request metadata。Root 的 offscreen QA 结果在其独立生产证据中记录，本报告未声明它已通过。
# 最终桌面 QA 补证

`production-review-run03` 在统一build-run12：一个实际 Electron 测试、3个场景全部通过，退出0，4张PNG。真实临时Git仓库/merge-base、普通WS准入、完整Source提示词、真实CodeMode读取、最终directive、同一批注去重与完整冷恢复贯通；仅模型HTTP受控。用户气泡显示真实中文请求，指南保留模型输入和原记录；持久Plan模式不变，本review回合使用真实Default指令。右栏按照正常UI打开“添加面板→变更”后从冷历史重建scope和批注，草稿保留，未新增模型审阅请求或修改磁盘文件。

失败证据保留：run01实机捕获旧slash autocomplete遮挡review目标，修复菜单互斥/取消/改稿生命周期并删除Source没有的目标glyph；run02前两个场景通过，最后测试错误假设冷启动自动打开右栏。未为测试新增自动开栏，修正为真实UI操作，run03完整通过。pageErrors为空、renderer warnings为空、native.visible=[false]；没有操作正常用户窗口。

随后仅修新批注容器继承的代码字体，采用Source `Vt/Xt font-sans`；`production-review-visual-run01` 在build-run13是字体视觉补证，一测试/一个已有场景通过、2张PNG，不重复计入3个功能场景。计算样式确认card.fontFamily与body.fontFamily同为MiniCode原界面字体，源码仍SFMono/Consolas等原代码字体。原字体和字号未改。完整原始wire、provider请求、partial checks、PNG、verdict位于各自目录。

