# Desktop harness integration — 2026-10-16

本批以 Codex Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 的实际调用链及已提取桌面资源为依据。不是全产品等价声明；没有修改 MiniCode 既有字体、字号或字重，也没有进行官方账号接入。

## 已补齐的调用链

- **MCP App**：仅接受注册工具的 UI descriptor；真实结果进入事件、持久记录、恢复和前端。原 MCP client 与 executor snapshot 分别持 lease，回答结束及同 ID 注册替换不会把 App 改接新服务器；App 请求不占用模型轮次的清理等待。完成后的 server elicitation 仍通过原 invocation/store/conversation 找到真实用户会话。
- **App 展示**：展开过程时保留原调用顺序；收起后应用常驻。成功 App 不再附带重复的原始工具行，资源或初始化报错时可展开原始记录。稳定 portal 容器与 Chromium `moveBefore` 保留同一 iframe；实时消息转历史、虚拟行卸载、过程展开收起、真实右侧栏开关均已实测。子记录只读，确认后的后续消息走原发送入口，保留输入草稿。
- **独立 executor 配套**：语言服务、环境安装、Git 交付和工作树操作使用原目标文件系统、程序、路径域及权限。状态与写请求绑定原 registration；不在控制端执行目标命令，不重借同 ID 替身。环境流断开后取消原安装，并解除已无人消费的输出队列背压后等原 handle、pump、lease 关闭。
- **目标命令**：直接传原 argv 给 Source。真实 `.cmd` 探针验证了空格与单引号路径、中文参数、`& | >` 字面参数和退出码 37；删去了造成二次引号解释的手工 shell 包装。受限 PowerShell 的程序发现使用原目标 cmdlet 与环境变量。
- **工作树**：新会话保留规范 file URI、独立 thread owner 和原 attachment lease；恢复下一次真实运行的 cwd。快照保存原始 LF、CRLF、二进制及 ignored 文件；结构化 Git 输出不使用工具展示的尾部截断结果。删除前先保存 ref，恢复不经 Git filter 改写原字节。外部 Git metadata 的本次操作权限按 Source `git-utils/src/worktree.rs` 与 `trust.rs` 的双向注册关系核对，不扩大原线程或整个主仓库的写权限。
- **Windows PTY**：真实 ConPTY 原进程 resize、断线重连和按窗口释放。已在确认正式 native runtime 没有运行进程后激活验证过的候选；实际文件哈希与 native 源输入校验通过，原 runtime 备份保留。

## 最终集成验证

以下按独立批次列出，不重复累计此前的 focused 结果：

| 批次 | 实际结果 | 原始记录 |
| --- | --- | --- |
| 环境、Git、LSP、文件、admission、环境生命周期，7 文件 | 112 passed / 0 failed / 0 skipped | `output/desktop-harness-final-20261016/backend-batch01-results.json`、同目录 XML/log |
| 全部 MCP 文件、remote Skill runtime、PTY、remote host | 209 passed / 1 skipped / 0 failed | `output/desktop-harness-final-20261016/backend-mcp-pty.xml`、`.log` |
| 桌面 unit suite | 170 passed / 0 failed / 0 skipped | `output/desktop-harness-final-20261016/desktop-unit.log` |
| 前端全量，360 文件 | 4333 passed / 0 failed / 0 skipped | `output/desktop-harness-final-20261016/frontend-full.xml`、`.log` |
| App、主/子过程折叠与右侧栏等前端 6 文件 | 最新唯一 145 passed | `output/mcp-apps-20261016/root/frontend-02.xml`，其中 SidebarRight 用 `frontend-sidebar-03.xml` 的 40 项替换原 39 项；不是又一次六文件全跑 |

MCP 批次的唯一 skip 是 installed Source `config/read` interoperability probe，原结果没有算通过。第三方弃用警告保留在日志中。

Chromium 验收使用真实 ChatTurn、AgentTurn、SidebarRight 及生产 App 宿主，MCP server 为自有测试 peer；这不是普通用户窗口录屏。证据为 `output/playwright/mcp-apps-20261016/chat-turn-lifecycle-evidence.json`、`actual-sidebar-evidence.json` 及对应 PNG。iframe 初始化次数始终为 1，原 server nonce、PID、计数及测试 MessagePort 连续；草稿 `UNSAVED_HUMAN_DRAFT` 保留。

生产 MCP Apps 协议采用官方 ext-apps SDK 的 postMessage，严格核对 iframe Window 与独立 origin。上述额外 MessagePort 是浏览器生命周期探针，不冒称 Codex 私有 sandbox 的 initId/MessageChannel relay 已实现。Chromium 的初始 about:blank sandbox 提示原文保留，没有隐去或称作零 warning。

## Windows 受限权限的未闭合边界

真实 installed Source 的 Root READ + 目标 WRITE，在含中文的 pytest 祖先路径中出现 `fs/writeFile` Windows `os error 5`；工作树 Git 操作还出现真实 cwd 读取错误。原两个权限验收失败保留在 `output/executor-worktree-20261016/validation-permission-batch.xml`。

诊断把同一 wire、同一 Source peer、同一权限形状分别用于普通目录和工作树目录，两者均失败；不受限读写均成功。另一个原 Source probe 在自有 ASCII 路径下受限写入成功，见 `restricted-scope-probe.json` 与 `restricted-scope-ascii-probe.json`。因此不能把它报告成 `.minicode` 独有问题或“所有 Windows 受限操作都不支持”，也不能以 ASCII 结果抹掉非 ASCII 失败。

原两个正式权限测试在 ASCII `--basetemp` 下仍然失败，未修改断言：文件写入已通过，linked checkout 的 Git stage 仍被拒绝写入 `index.lock`；含只读 carveout 的工作树动作被 Source 明确以 `cannot enforce split writable root sets directly; refusing to run unsandboxed` 拒绝。见 `validation-permission-ascii.xml` 与 `permission-ascii-original-wire.json`。对应 Source 拒绝逻辑为 `codex-rs/sandboxing/src/windows.rs:109–179`。这两项不是通过，也没有把它们转成 skip/xfail。仅双向注册 metadata 校验已用原受限文件系统验证正例与坏 backlink 拒绝，见 `registered-metadata-restricted-probe.json`。

没有改 ACL、账号、provisioning、用户项目路径或权限模式来制造通过，也没有使用目录别名、控制端写入或另一执行器回退。原始 UTF-8 文件内容、错误与原 owner 继续保留。这个边界未闭合前，不把候选包称为全部能力已验收的发布包。

## 构建、数据与已有窗口

最终 TSC、Vite、bundle budget 和 UI debt budget 均通过。entry 为 `assets/index-Cq3J2rPH.js`，index SHA-256 为 `3103f03f7d0cc45e2d1277f521178908a98d6f8a84ca8f4c9d52f76a49f01bd1`。全量前端运行后仅将新图标改从项目统一出口导入同一符号，修复实际 UI debt 87 > 86；随后重新检查类型、构建和两项预算，没有抬高预算。

当前独立候选为 `output/source-executor-remote-final-wheel-r8-20261016/minicode-0.2.0-py3-none-win_amd64.whl`，SHA-256 `ecfdaae1841b6fe5fa46e36a6118be041e523c8c97e9d904368a6a30b7016593`，16,055,405 bytes。1264 个冻结输入、1244 个 backend 包成员与全新安装目录逐字节一致，`changed_inputs=[]`；bundled Python 3.11、`-I`、私有 Temp cwd/state 和限定 PATH 下冷验 **28/28**。它验证新模块可独立导入、MCP bridge 字节、原生策略与既有执行链；不能替代上文失败的受限工作树能力，所以保留 `candidate_only=true`。

r6 的 wheel 构建和安装完成，但冷验脚本缺少历史 native producer 来源副本而中断，没有记成通过。之后校正最后一处工作树按钮可用条件，用新的 r7 冻结树及新的安装目录完整重验。提交检查再发现一个新文件尾部的额外空行；仅移除该空行后，从新的 r8 树及新的安装目录重新完成 28 项冷验，未借旧包结果。原生网络和 execpolicy 二进制没有因无关修改重编，也没有修改其 manifest 去迁就字节。全部记录在上述候选目录与 `output/desktop-harness-final-20261016/`。

没有重载或关闭普通 MiniCode 窗口，没有读取、归档、stage 或删除用户 `data/`，也没有清除隐藏工作区草稿、附件或未保存编辑器内容。普通窗口原 backend 进程不会自动加载本批 Python 修改；新进程才会使用新后端。

仍独立保留 Node/CUA 配套服务、Linux OS managed 网络路由、外部 SSH/真实 WSL、WFP provisioning/流量矩阵及完整同构建 UI 状态矩阵等未验收范围，详见 [当前差距](2026-10-16-current-desktop-gaps.md)。
