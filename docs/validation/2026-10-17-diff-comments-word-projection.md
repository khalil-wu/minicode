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

最终前端为 `assets/index-CfrFa9WJ.js`，index SHA256 `076d2b095c4b891f50a1f58b056f302474588ce7e7b3f9bd309a9c7e3ae9b1e7`。生产实现冻结为 `bbc52231ca240667e9b01a01fdc9ff775b73c011`，后续 `bf14f6f0f2d0881708cb2b63924b1d912bc51178` 仅修正下面两处测试断言，未改生产源码。

首次完整 [CI 39012403115](https://github.com/khalil-wu/minicode/actions/runs/39012403115) 为 7 项成功、前端 1 项失败；其前端为 4492 passed / 2 failed。两项分别是 ActivityCell 和 transcript remount 对完整 `new value` / `new-a` 的单文本节点查找；实际词级 DOM 已拆分文本，逐行 `textContent` 才是其完整正文。修正后两文件 60/60，通过内容和独立文件展开选择的断言，未关闭词高亮或放宽为任意文字命中。新完整 [CI 39012950337](https://github.com/khalil-wu/minicode/actions/runs/39012950337) 精确绑定 `bf14f6f0`，**8/8 成功**；不借上一批 4415 / r16 的结果证明本批。原始失败和新结果分别在 `output/ci-diff-comments-20261017/` 与 `output/ci-diff-comments-final-20261017/`，本地修正为 `ci-text-assertions.json`。

| 最新完整 CI 范围 | Passed | Skipped |
| --- | ---: | ---: |
| Frontend，380 文件 | 4494 | 0 |
| Backend，backend/tests | 6999 | 118 |
| Backend，根目录 tests | 1768 | 51 |
| Windows harness | 362 | 15 |
| Desktop unit | 161 | 9 |
| Desktop Electron smoke | 6 | 0 |
| Electron Playwright | 1 | 0 |

前端 TSC/Vite、bundle/UI debt 预算和两项 pinned Source 原生构建均通过。跳过不计通过；实际跳过原因在 `ci-receipt.json`，包括平台、未配置 owned Source peer 等边界，未把它们算成实际 Source 外机互通。CI API、完整日志、JUnit 和 artifact 都绑定本次 head。

收尾文档提交仅修改本记录及当前差距清单；其 backend、frontend、desktop 和 scripts 树与 `bf14f6f0` 保持一致。`bbc52231 → bf14f6f0` 仅有上述两个测试文件差异，backend/desktop/scripts 树亦相同。具体提交及树身份、推送状态和安装候选仍 held 的事实另存 `output/ci-diff-comments-final-20261017/delivery-receipt.json`，不将文档提交冒称另跑一轮 CI。

## r17 独立安装：身份通过，完整验收未通过

新 backend wheel 绑定 `bbc52231`，SHA256 `37226bb371625b68f8c36233129ff6f65052bc2ffd15362ef6eff0ea8a1e49f2`，16,120,035 bytes。1,274 个冻结输入、1,253 个 backend 成员以及两次 presentation 各 511 个模块的来源和字节核对通过；169 处 checkout 差别仅为 CRLF，零其他差异。后续测试提交没有修改 backend，未重复打包。

新安装的基础 cold 41 项通过且进程 exit 0。首次 presentation 的 9 项断言均通过，但承载 Python 在结束阶段以 `3221225477 / 0xC0000005` 退出，因此**不能记为正式 50/50，也不交付该候选为验收通过**。原执行器在非零退出处停止，manifest 明确 `acceptance_complete=false`、`delivery_hold=true`，未生成成功 delivery receipt。

Windows Application 1000/1001 对应 PID 4684、report `7bc3344a-2be1-4f12-9599-56f8fd48d5a9`，故障模块为 PyAV 自带 `libwinpthread-1-26b1e143f68b103d25cd0ba0a146f5b4.dll`，偏移 `0x5e8c`。实际正常 WS 导入通过 `backend.mcp.result_projection` 的顶层 `import av` 加载该 DLL。r16/r17 对应模块及依赖锁相同，均为 av 17.1.0，本批未新增原生输入；没有证据把崩溃归因为本批评论改动。

进一步只读核对本机对应 `python.exe.4684.dmp`（SHA256 `ef4df71b644b40078c0b6a7950c6266133a6379544e051f57669f2d4aff12476`）：独立 AMD64 PE unwind 与现有 System32 DbgHelp 的 24 帧 PC/RSP 完全一致，所用镜像时间戳匹配 dump。已确认 `Py_Exit → ExitProcess → LdrShutdownProcess → libstdc++ 析构/ostream::flush → __emutls_get_address → pthread_once → libwinpthread 内部函数`，故障指令向 RAX=0 写入，前一调用为 `calloc(1,32)`。这定位了原生退出路径，尚未证明分配为何返回空或可复现触发条件，不认定 OOM，也不把它归为推测的 PyAV logging 回调；单线程 dump 不排除更早线程活动。具体栈、指令与限制见同目录 `NATIVE-EXIT.md`，未附加活进程或安装新诊断工具。

开启 faulthandler 的一次诊断复跑 9 项 exit 0、纯 import 三次以及 r16/r17 正常 WS 导入各两次均 exit 0，故障未稳定复现，不能据此宣称根因已修。当前 DLL 哈希匹配其 RECORD；r16 没有独立保留历史 DLL 字节快照，不从当前对比推断历史字节。没有换依赖、延迟 import、吞错或修改生产 runtime 来消除报告。`startup-health.json` 的空错误仅证明独立 cold MCP logger 范围，与下文真实桌面 Memory Phase 2 原生账号错误分开。

完整报告和原始 Windows 事件、XML、进程退出、来源及字节记录在 `output/source-executor-remote-final-wheel-r17-20261017/REPORT.md`。自有测试进程均已退出，私有 staging 留存审查；用户数据与普通进程未受影响。

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
