# 窗口外观、Diff 与 agent 收尾交接

本批从 `2140e4ad` 继续。后端对照 `.tmp/codex-src` 的 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`；Diff 对照早先安装的 26.1007.9446.0（ASAR 26.1007.71326）冻结资源与原参考图，核完整 caller、worker、CSS 覆盖及条件分支。期间桌面应用自动更新到 26.1013.4894.0，冻结证据不重标为新安装版本的全界面复验。官方账号与依赖账号的服务仍排除。

## 已完成的界面与 Git 链

- 真实双窗口复现并修复了明暗配置互相覆盖与其他窗口不更新：每种模式独立保存配套颜色和代码主题，监听实际 KV 变化同步 store/CSS，迁移保留旧配置。异步旧加载不能覆盖较新的本地或其他窗口编辑。
- 根据实际单主题 Diff caller 区分 chrome 背景、原始代码前景/背景、增删行、行号栏和词级颜色。两条 HTML 路径删除正文整体增删染色和重复背景；默认路径也覆盖。Monaco 只统一相应 Diff 颜色，保留编辑器背景、语义、模型及字体。
- GUI 与提示词共用固定 Git 元数据读取链，保留原 executor、URI、读取权限和清理归属。完整磁盘读取下不错误要求模型命令沙箱；真实受限读取仍拒绝。模型 Git 和写操作继续原权限链。
- 真实本地沙箱不可用返回结构化 503，Source RPC 保留原错误契约。前端 API 与工作区消费者共用原始错误信息解析，避免 `[object Object]`，不加新的工具失败标记。

## Agent 邮箱与工作树完整交接

真实探针先后复现了两个丢任务入口：普通 child 已结束模型回合但尚在清理时，followup 返回成功后未进入下一轮；隔离工作树的同一窗口会先删除 clean 原目录，导致已接受任务随后报原目录不存在。

现在关闭旧邮箱、消息准入和持久化路由共用原 owner/path/epoch 决策。未消费的 trigger/queue-only 消息保持原身份与正文；trigger 在旧 session、适配器和资源退出后真实启动，queue-only 保留到明确下一轮。成功 resumed 回执等待实际 start；启动失败保留原错误和邮箱，不自动反复尝试同一消息。

工作树按真实资源状态交接：已接受 mail 持有原 path 和 base HEAD 的 continuation，Git 分支按实际状态推导；无待办时进入 retiring，后来的请求在写入前明确拒绝。下一轮沿用原工作树，实际无待办时才清理。合法 `git switch -c` 可以继续，清理只删除 MiniCode 创建的临时 ref，保留新的真实分支。持有状态不冒充用户修改，也不重建已经不存在的工作树。

物理清理复用现有按路径跨线程/跨进程锁，SQLite 只执行短 CAS 和凭据写入。原执行器即使失租也必须关闭自己拥有的 session/适配器；对工作树则必须先取得真实资源所有权，不能在 owner/epoch 被替换后继续删除。失败清理恢复、超过普通 terminal 历史上限的 continuation、会话退出、live/cold 会话删除和用户改动保留都接入同一原资源生命周期。新资源状态没有直接加到界面。

原全库 SQL guard 的独立 recipient 测试真实报 database locked，已保留为失败；最终 path guard 探针证明同一资源仍在清理门控时，另一个 runtime 已把消息发给独立 recipient。不是通过隐藏真实错误或加重试来通过。

最终定向门控 **30/30**，资源消费者与既有 recovery/purge **21/21**，均 0 skip；未发布工作树取得与 owner 检查 **9/9**，实际切换分支及清理 **5/5**，持久 recipient 恢复 **2/2**。这些分组有重叠，不相加。旧 18 模块完整尝试的 56 passed/1 个测试 3 秒等待失配仍保留，改为等待真实 worker 后通过。剩余相关节点按实际未执行身份续跑，最后一段 **39 passed / 446 deselected**。其中一轮在旧会话切换测试顺序相关挂起，该测试单独及紧邻两节点分别通过，原挂起仍记录为未完成；没有把本地分段结果冒充一次完整套件全过。最终提交 CI 和安装候选分别留证。

## 验证身份与范围

外观和 Diff 实机检查构建：`assets/index-CEpr6Wko.js`，index SHA256 `c32ef2009aab294275fcd5fcbd6e88e9a0057e3521cff9e485f49c0951661892`。两次真实私有 profile 冷启动、自然重建的第二窗口、各自草稿、隐藏工作区草稿、脏编辑器、原磁盘字节与 Diff 的颜色来源共 26 项观测全部满足。实际增删内容与行号经过真实 Git/WS 链，不注入 store。

最终构建为 **`assets/index-C9rIzayf.js`**，index SHA256 **`4e2bedf3caaf5f3dbe919c9b52b2a51314134ac52e830c375bc2e1c80ce8f622`**。TSC、Vite、bundle 与 UI debt 检查通过；最终构建的真实双窗口另验证保存配色在第二窗口启动时即生效、模式/配色同步和草稿保留，GitPanel 使用明确的受控 HTTP 503 验证原消息、草稿与权限保留。消费者验证不替代后端实际拒绝测试。早先构建的实机记录没有重标为最终构建全场重放。前一错误消费者检查点为 `assets/index-8hTCLCgY.js` / `884c5960…f50ae`，其记录独立保留。

定向前端分组分别为外观/API 30、Diff 78、工作区错误/UI 44、原始启动 1 项通过；分组有重叠，不相加冒充独立总数。Git 后端 8 个相关文件覆盖 147 项：首轮 143 通过/4 个新断言错误，修正实际拒绝时机的测试预期后受影响两文件 21/21，未改的 126 项沿用原通过。没有把未执行或跳过算通过。

本地前端完整回归实际为 4414 通过/1 失败/0 跳过。失败定位到主题显示函数耦合存储读取；已让初始化只读取一次，显示/系统模式切换使用已有 renderer 状态。受影响 7 文件随后 27/27，通过结果和原失败分别保留，不宣称重跑过完整本地套件。

Git 的独立 fresh-profile Electron 验证使用原 Wz 构建，实际 status/worktree/delivery HTTP 200、staged/unstaged 展开及工作树正确，草稿和 confirm 保留，未创建沙箱账号。它不证明最终错误解析器。所有本批自建窗口与服务已退出，普通用户窗口未重启。

实现已冻结并推送为 `4826a0c12f856e17d6806f22807c6668b3e28b14`。该提交的[完整 GitHub CI 39005641653](https://github.com/khalil-wu/minicode/actions/runs/39005641653) **8/8 作业成功**，两个 pinned Source 原生构建、前端构建和两项预算检查全部通过，CI 前端入口也为 `index-C9rIzayf.js`。实际测试结果如下，各范围独立记录，不合并成产品完成率：

| 范围 | 通过 | 跳过 |
| --- | ---: | ---: |
| 前端单元与链路，374 文件 | 4415 | 0 |
| backend 完整套件 | 6997 | 118 |
| 根目录后端完整套件 | 1768 | 51 |
| Windows harness 与进程合同 | 362 | 15 |
| Desktop 单元 | 161 | 9 |
| Desktop Electron 冒烟 | 6 | 0 |
| Electron Playwright | 1 | 0 |

上述测试失败、错误均为 0。Linux CI 中的 Windows 专属合同、未指定自有 Source peer、可选文档依赖等跳过理由完整保留在 `output/ci-window-mailbox-20261017/ci-receipt.json`，跳过不计通过。该前端完整 CI 包含本地 4414/1 后修复的显示/存储调用链；不需要把原本地失败或分段结果重写成完整通过。

Windows 后端候选 r16 从这一提交重新构建，并在独立临时安装实际通过 **112/112**：基础冷启动 41、MCP 媒体 7、目录 6、Git 12、消息与原工作树生命周期 46，失败、错误和跳过均为 0，启动健康检查 `errors=[]`。1,274 个冻结输入与 1,253 个 backend 包成员通过源码、wheel 和安装字节核对；模块实际来源位于新安装，明确包括 `backend.agent.worktree`。169 处 checkout 差异仅为 CRLF，非换行内容差异和未提交输入均为 0。

r16 wheel 为 `minicode-0.2.0-py3-none-win_amd64.whl`，16,119,603 字节，SHA256 `2dde048872868745a04b721b9692219fe9428ceae7dee532f6e7169ee896cbb2`。Git 与目录组实际使用新安装的外部 Source peer `896a0166b774037a/codex.exe`，版本 `0.163.0-alpha.10.2`，SHA256 `f6dbf48cb70f62649c645f26c87dc9bf84cc36986202dfad8e12fb5b8ca70312`；它与固定源码 checkout `822e58cc` 分别记录。Source 使用自有 stdio 连接，不代表外机 HTTP、Linux 安装、原生 ACL 或全桌面安装程序已经验收。测试依赖只安装到新候选的 QA 目录。SDK 原进程和 fork 已退出，普通用户窗口、服务和生产 runtime 未改动。

本地完整证据：

- `output/appearance-multiwindow-20261017/report.md`：配置迁移、异步竞争、分组测试及构建日志。
- `output/playwright/appearance-multiwindow-20261017/validation.json`：26 项生产窗口观测、截图及冷启动记录。
- `output/playwright/workspace-error-consumer-20261017/final/receipt.json`：最终构建双窗口与错误消费者；上级目录保留前一构建记录。
- `output/diff-theme-caller-20261017/report.md`：实际 Source 文件 SHA、UTF-16 偏移、worker/非 worker 链及独立 CSS 计算；9 文件/78 项回归。
- `output/git-readonly-sandbox-audit-20261017/REPORT.md`：Source/local HTTP、原权限与沙箱错误边界。
- `output/playwright/git-fresh-profile-20261017/REPORT.md`：独立 Git 界面与服务退出证据。
- `output/harness-followup-20261017/mailbox-final-review.md` / `mailbox-final-receipt.json`：普通与工作树消息交接、原始失败、最终门控和实现 hash。
- `output/harness-followup-20261017/global-guard-probe-60a41623/receipt.json`：绑定最终 runtime/store hash，按原路径清理期间独立 recipient 仍可真实收消息；旧探针另保留。
- `output/source-executor-remote-final-wheel-r16-20261017/REPORT.md` / `installed-scope-receipt.json` / `delivery-receipt.json`：本次新构建、独立安装、112 项实际结果、包身份和来源/字节核对。
- `output/ci-window-mailbox-20261017/ci-receipt.md` / `ci-receipt.json`：实现提交完整 8 项 CI、全部原始 job 日志及两组 backend JUnit 证据。

首次类型错误、QA 定位错误、错误的测试预期和未完成的诊断均保留在各自目录，不重写为通过。

## 继续保留的具体差项

HTML Diff 尚未生成 Source 默认开启且受大小限制的 changed-word spans，也未实现普通文件审阅的独立会话评论及范围选择。只读原 parser/feedback 探针已确认审批评论把 patch index 当文件行号：hunk 的旧第 100 行/新第 200 行会被发送成第 1/2 行。需要保留 side/range、真实坐标和独立会话评论生命周期。Source 的词内差异默认开启，增删总行数严格超过 2000 或关闭偏好时停用，配对行去 EOL 后各不超过 1000 字符才参与词级计算；不能只加颜色或翻开评论按钮就宣称该链完成。精确 caller、SHA、UTF-16 偏移和探针见 `output/diff-theme-caller-20261017/next-render-chain/report.md`。Source app 本身未启用库 hover paint，本批没有增加该效果。

Windows restricted-token 的命令 readiness 仍与 dedicated sandbox account discovery 耦合；Source 的 unelevated 路径从当前 token 创建限制 token，不要求 elevated setup 账号。现有 native helper 已有 legacy 分支，但还需正确处理应用自有 home 和实际 ACL/capability 生命周期，不能只删 Python 账号检查。只读证据在 `output/windows-unelevated-readiness-audit-20261017/REPORT.md`；本批未执行该 native 路径。它是与已修 GUI 元数据不同的能力差项。外部 SSH/正常 WSL 发行版、WFP 实际流量、大型真实模型长任务及其他平台整体验收仍见[当前差距](2026-10-16-current-desktop-gaps.md)。
