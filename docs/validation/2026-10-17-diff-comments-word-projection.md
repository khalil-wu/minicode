# Diff 词内高亮与普通会话评论

本批从 `4b8468d8` 继续。后端行为参考 `.tmp/codex-src` 的固定提交 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`；桌面评论、菜单与格式参考 26.1007 冻结资源和原截图，不把它们重标为 26.1013 全界面复验。官方账号及其线上服务排除。

## 实际改动

- HTML Diff 的统一、分栏与聊天内联显示共用真实 changed-word decorations。公开 `diff@8.0.4` 的 tokenizer、Myers 处理及 Source join 规则已逐项核对；只配对真实删除/新增行。UTF-16、CRLF、1000 字符和 2000 条变更的严格阈值、纯新增/删除及高亮尚未就绪的 plain fallback 分别处理，不用简单前后缀比较冒充词 diff。
- decoration 合并到现有语法 token 的 DOM 文本节点，保留转义、嵌套颜色与 Monaco 的 tab 映射。普通、增删行和词内颜色仍沿上一批色板，字体设置没有调整。公开 BSD 许可证随前端资源分发。
- “变更”选项中的真实“词级差异”checkbox 使用 Source `wordDiffsEnabled.2`，默认启用，保存后同窗及跨窗同步，选择后关闭菜单；不添加没有实现的菜单占位项。
- 普通审阅、Git 与历史 Diff 通过行号栏按钮或实际拖选添加评论，保留左右侧、真实文件行号、范围、工作区和选定的原始 partial hunk。正文保持正常阅读和文本选择。旧代码把 patch 第 1/2 条记录当成文件第 1/2 行的问题已从坐标、事件、状态到发送链修复；无末尾换行标记也不再清掉下一条新增行的计数。
- 用户评论与模型 finding 分开。普通评论保存后自动成为输入区数量附件；可预览、编辑、删除，只有评论也能正常发送。它不触发审批拒绝。原有审批反馈继续走其原 request/turn owner，并携带真实位置。
- 正常用户消息的 `content` 采用 Source `# Diff comments:` / File / Workspace / Side / Lines / hunk 格式；可见正文保留用户自己的文字。既有 `context_refs` 新增 `diff_comment` 类型以保存结构化上下文，未臆造 app-server RPC 字段。首轮、活动回合追加、排队、历史恢复和召回使用同一数据。
- 已保存评论按会话持久化；客户端工作区草稿移交正式会话 ID 时去重转移。发送清除本次不可变快照，未接受失败按真实草稿身份恢复，后写的新文字、新评论和隐藏会话内容保持归属。

## 未保存批注的阅读归属

原第一版 hook 会在 DiffBody 切换会话或来源卸载时删除尚未保存的批注。审查确认这是实际丢稿入口，未把它留成已知限制。

Source 的局部 `rn` 清理会清正文 Map 和 `St` 的 unsaved 标记，但进一步追到共享对象及原生内存仓库后，现有冻结资料仍未证明前层导航确认流程。因此不能凭局部 cleanup 推导 MiniCode 可以无提示丢失隐藏内容。

现在直接复用现有内存 draft 表：阅读 owner 绑定已有窗口与“变更”面板，来源键分别使用真实 review request、Git 暂存范围或历史 revision，再带工作区和文件。切会话、来源、视图或关闭再打开页签只隐藏对应草稿，返回时重接；保存、取消及删除会话按真实身份清理。没有新增磁盘草稿文本、导航确认弹框、租约或兼容层。用户未保存内容的保留要求优先，未声称复制了一个未经证实的 Source 确认流程。

## 分范围验证

各分组有重叠，不相加作独立总数，也不换算产品完成率。

| 范围 | 结果与证据 |
| --- | --- |
| 词 diff、DOM 合并、Inline 接入 | 初批 4 文件 32 通过；plaintext caller 修正后仅复验受影响 Inline 13 通过；偏好接入后的两文件 17 通过。`output/diff-theme-caller-20261017/next-render-chain/word-tokenizer/` |
| 评论模型、格式、持久化 | 2 文件 21 通过；saved 与 transient、真实位置、提交快照清理/恢复及客户端作用域移交分别覆盖。 |
| 输入、发送、历史与排队 | 最终覆盖 199 个唯一前端用例。初批缺少 i18n 键的失败保留，补实际中英文后受影响分组通过，未重跑的未改分组保留原结果。`output/diff-comments-send-20261017/REPORT.md` |
| 后端实际消费者 | 9/9，覆盖真实 QueryEngine、WS、provider 输入、持久历史、queue 与已消费 steer。后端生产修改只有现有 context_refs 类型准入。 |
| Root Diff 界面 | 首批 59 passed / 2 failed；旧测试的 Range 单文本节点和整句 gettext 假设被真实词级节点拆分，修正实际 DOM 选择构造和读取后该文件 42/42。后续阅读 owner 与 storage 两文件 54/54，含 A/B、来源、页签重开和独立阅读实例。原失败保留。 |
| Shell 与构建 | SidebarRight 定向检查通过；最终构建包含 TSC、Vite，两项预算通过。首次 UI budget 真实报新增直接图标导入 88 > 86，改走已有 icons 入口后通过，未提高预算。 |

最终前端为 `assets/index-CfrFa9WJ.js`，index SHA256 `076d2b095c4b891f50a1f58b056f302474588ce7e7b3f9bd309a9c7e3ae9b1e7`。最终完整 GitHub CI 和 r17 独立安装结果在收尾后另记，不借上一批 4415 / r16 的结果证明本批。

## 真实桌面与边界

Playwright CLI 接入自有 offscreen Electron 的独立 CDP、profile、backend 和 Git 工作区；只让原生文件夹选择器返回自有目录，未注入 renderer store。真实 Git fixture 在旧第 200 / 新第 300 行产生变化。

首个 ZOs 构建实际验证点击、Enter 保存、统一/分栏切换保留输入、L200→R301 拖选、数量附件预览及词级开关。实际模型请求收到单侧 R300、跨侧 L200-R301、原工作区、原 hunk 的 CRLF 和两条原评论。聊天区只显示评论附件，没有铺开内部格式文本。

最终 Cfr 构建重新加载后，从服务端恢复的历史保留两条结构化评论，可见用户正文仍为空；再次实测未保存的 `UNSAVED_A_READER_KEEP` 经真实新建 B、返回 A，以及关闭再打开“变更”页签后仍存在。CLI console 为 0 error / 0 warning。两次构建身份和截图分别保留，不将旧截图冒称最终构建重放。原字体与代码字体的实际值在 receipt 中。

该自有 fresh confirm profile 的后台 Memory Phase 2 另外触发一次模型请求，并真实出现两条 `node_repl` 原生沙箱账号状态不可用错误。这是此前已列出的 Windows readiness 差项，本批没有隐藏、转成通过或以 bypass 重跑抹掉。普通评论主请求的验证与这条辅助能力分别记录，不宣称整款 harness 无错误。

自有窗口通过实际关闭按钮退出 0，provider、debug 和 backend 三端口均关闭。CLI 随后读取已关闭页 URL 的 TypeError、第一次 provider 检查错误假定总请求数和 content 类型、stdin 状态探针不可用等 QA 诊断均保留。没有操作用户普通窗口；最终只读发现先前 PID 9064 已不存在，未观测其退出时刻或原因，不把它写成仍然打开。正常构建保留旧活动资源，用户 `data/` 没有暂存或删除。

完整实机证据：`output/playwright/diff-comments-20261017/receipt.json`、`provider-verification.json`、原始快照与截图；自有退出为 `cleanup.json`。集中构建与 root 结果在 `output/diff-comments-integration-20261017/`。

## 仍保留的差项

“变更”菜单其余 Source 控件，例如完整文件加载、富预览、隐藏空白和导入等，本批未接成完整菜单与行为，不能用词级开关代替全部 Diff 能力对齐。未留存的私有 comment-card 叶 chunk 也未凭印象补造内部样式。

Windows 未提权 readiness/home/ACL、系统 Computer Use、私有 MCP App relay、未收录主题、真实外机/WSL/WFP、长模型与长多 agent 任务，以及全区域全部交互状态的验收继续保留。官方账号相关服务仍按用户范围排除。
