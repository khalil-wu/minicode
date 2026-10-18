# 真实 SSH、Linux 安装与 Source 后续核对

本轮按用户提供的真实 Linux SSH 环境继续验证 MiniCode 自己的 backend/harness。行为依据为固定 `codexsrc` `822e58cc3d666166c7446c5b1ea2e52f5d09594c`，桌面依据为当前 Appx `26.1013.4894.0` / package `26.1013.40934` 的实际提取资源。官方账号及账号依赖服务仍按用户要求排除。

本文件不记录主机地址、账号或密码；本地详细审计保存在 `output/ssh-source-audit-20261018/` 和 `output/playwright/ssh-live-20261018/` 等目录。工程完成度估算与逐项验收结果分开；本轮不能据测试数量宣称全产品等价。

最新生产/CI 检查点为 `7b4885fa9df13d66ba3c50cba2383f2254c1621d`，[完整 CI 39093047592](https://github.com/khalil-wu/minicode/actions/runs/39093047592) 已 **9/9 jobs success**，0 failed/cancelled/skipped job。目标外机包、GitHub 冷安装包与实际前端身份分别列在下文，不混用验证结果。

## 本轮实现

- `695b669c`：SSH 统一采用 Source 的 BatchMode、15 秒 keepalive、12 次上限，真实登录 shell 环境和 CLI lookup/version 检查。缺 CLI 保留实际错误，不发布虚构连接状态。独立安装和更新仍是明确操作。
- `d047e8d1`：修复捕获权限快照后的 Linux READ 元数据形状；补齐 Source 原生路由的相同分类；统一 Monaco API 初始化；固定 Linux 原生构建与冷安装基线。
- `3454d84e`：首次将容器归档的精确工作区放入命令级 `safe.directory`；后续实际基线 Git 2.25.1 证明该位置不被接受，该尝试没有解决旧 Git 的 ownership 拒绝。
- `c2c514ff`：两个原生构建器保留编译产物的执行权限，取消 producer 验证前补 chmod，独立安装后直接验证可执行性。
- `7b4885fa`：按 Git 2.25.1 的真实配置作用域，在专属 CI 容器的 global config 中信任精确挂载工作区后归档。真实旧 Git 隔离 fixture 分别复现未配置及 `-c` 的 exit 128，并验证精确 global 配置的 exit 0；不使用通配信任、不修改目标用户的 Git 配置。

前端构建为 `assets/index-DOL_HuZW.js`，index SHA256 `d933a9d79f85cbac92535a3ae1ca28a749b963d18f7789d60a3351648edbb439`。字体、字号和字重未改。

## 真实环境与安装前提

目标为 Ubuntu 20.04.6 x86_64，实际 `getconf GNU_LIBC_VERSION` 为 glibc 2.31；系统 Python 3.8.10、Node 10.19.0、Git 2.25.1。验收在专属目录使用 Python 3.11.17、Node 22.23.3、Rust 1.95.0 和私有 Git 2.56.0，系统安装保持原样。

原 CI 的 Linux execpolicy/network 分别要求更高 glibc；实际 loader 错误与原 ELF 摘要保留。目标重建的两引擎最高要求 GLIBC_2.30，在 glibc 2.31 上实际执行 Source identity 成功。

旧 Git 的 `worktree list --porcelain -z` 返回真实错误。固定 Source `codex-rs/worktree/src/lib.rs` 同样使用该命令；Git 2.36 引入此能力。本轮使用合适的私有 Git，保留原 NUL 协议。包含空格、中文和换行的 worktree 路径已实际往返验证。

Linux 的两个 producer 和新增 wheel 冷安装 job 固定于同一 Ubuntu 20.04 镜像 digest，缓存 namespace 含 ABI 基线和镜像身份。下载 GitHub artifact 后恢复其被归档丢失的文件权限，与构建输出、wheel 和安装后的权限验证分别处理。详细步骤见 [Linux 后端安装](../linux-backend-install.md)。

## SSH harness 第一批实测

`qa-batch01` 的原结果为 **11 passed / 0 failed / 0 skipped**，其中 2 项为真实 fixture 准备、9 项为生产链验收；另有 5 项清理确认全部通过，不累计成 pytest 数量。

实际调用生产 `RemoteDesktop → RemoteHost → SSH → MiniCode daemon`，只在 SSH argv 中增加专属 `-F` 配置。远端临时 launcher 指向已安装的真实 CLI，并指定独立 state。反向窗口 binding 使用生产 bridge；本地 Chat SSE provider 是明确的受控 fixture，通过独立 SSH 反向转发连接，未宣称真实模型能力。

| 实测范围 | 实际证据 |
| --- | --- |
| 文件与路径 | 中文、空格、emoji、大小写不同的文件内容及 hash 正确；工作区外路径及向外符号链接真实拒绝。 |
| 原生 PTY | 同一 shell/child 实际 isatty；OS 测量 80×24 → 101×31，重连后同 child 为 132×44。 |
| IPv4/IPv6 preview | 实际 IPv4 POST/GET 与 `::1` 读取各 8 MiB，原字节 SHA256 相同；大流消费暂停时其它 native/binding 请求仍能完成。 |
| 断线任务 | WS/主 SSH 断开 32.635 秒，原 harness child 保持同一 host PID；完成后下一次 provider 请求收到真实 stdout。 |
| 恢复 | 同 daemon generation/backend port，PTY 离线 UTF-8 输出无重复，`session.sync` 返回原 conversation。 |
| 权限归属 | 错 window/cid、缺 lifecycle authority 均拒绝；关闭窗口的 pending release 只在原 ACK 后移除，另一窗口终端继续存活。 |
| managed 命令 | 实际 auto/use_default 的 agent 命令写入工作区成功，专属 outside 路径收到 Errno 30/exit 1 且文件不存在；原工具结果未改写为成功。 |

managed 场景显式使用已有 `exclude_slash_tmp` 和 `exclude_tmpdir_env_var`，因为默认 workspace-write 本来允许 `/tmp` 和 TMPDIR。本轮没有改内核或用 bypass 代替验证。沙箱内 PID 3 明确是 namespace 内编号，不当作 host PID。

第一批显式 stop 后 daemon、PTY、preview 与观测 child 均退出；provider 反向 SSH 按 owner 请求终止，原设置字节恢复。完整原始记录见 `qa-batch01/REPORT.md`、`receipt.json` 与 `identity.json`。

## 由真实桌面操作发现的根因

真实生产 Electron 使用独立测试 profile。设置页保存连接后，缺 CLI 时实际报错且只保留本地窗口；安装后打开真正 Linux remote window。输入远端绝对目录、创建会话、执行实际 `run_command`，远端文件、主机、cwd 与工具结果相符。默认 Changes 比较实际分支，编辑器读到远端 `proof.ts`。

**Monaco 初始化顺序**：Changes 先着色会触发 Monaco 0.56 首次 standalone 初始化；延迟 TS worker graph 随后注册 contribution，依赖的 treeViewsDnD 等服务未进入先前快照。共享 `monacoRuntime` 在所有着色、编辑器、语言服务、编辑及诊断入口前注册官方 singleton，保留贡献模块的延迟加载。新回归实际走先预览、后 editor/controller、编辑并 Undo；相关最新结果 146 passed，TypeScript、构建与两项预算通过。真实 Electron 的相同顺序已打开代码与 minimap，未再出现该 pageerror。最末收据选择器误匹配两个 module scripts 的 QA 错误单独保留，不重复计算已完成的行为验证。

**沙箱元数据形状**：捕获 profile 将元数据保护具体化为 READ 路径，writable-root 名字列表已为空；原 Python bwrap 将这些缺失路径建成普通零字节文件，持久 Node companion 存活期间被真实 Git 列出。修复按 Source 独立 basename 分类保留缺失 READ 元数据的目录形状，保持原有真实文件/空文件 inode。DENY 仍使用零权限文件，显式 WRITE 不受影响。

原生 managed 路由也有同一问题。构建器仅在隔离 Source 树中用小型应用名字 helper 替换 bwrap 的 READ 名字导入，完整原文 hash、适配后 hash、唯一替换与 helper hash 都记录于 `source_adaptations`；Source protocol 权限表、DENY 分支没有修改。不能把该适配写成上游文件完全未改。

Windows 受影响批最新为 **51 passed / 22 real skipped**。首次运行的 25 个 async 节点因生产 Python 缺 pytest-asyncio 而未进入测试体；原失败日志保留，测试插件只放在独立 QA 依赖目录。Linux 新候选及原生分支的最终结果另列在收尾记录，不能用 Windows 跳过项冒充通过。

## 最新 Linux 候选与真实桌面复验

`c2c514ff` 候选在上述真实 Ubuntu 主机重新构建两个原生引擎，完成非 editable wheel 和独立 pip-target 安装，再统一执行受影响的 metadata mount、Linux managed route 与 Node companion 三文件：**67 passed / 0 failed/error / 6 skipped**。六项为四个 Windows desktop-bundled Node 场景及两个 Windows 原生沙箱场景。READ 元数据目录、原生 DENY、显式 WRITE、真实 Git 状态与持久 Node 生命周期均实际执行；不能把本批结果扩展为全部 SSH 矩阵在新包重跑。

目标 wheel 为 `minicode-0.2.0-py3-none-linux_x86_64.whl`，32,123,630 bytes，SHA256 `cadcbe02174e618c162ac7e94aed26c77ecec77cfe30f602e31a0d6bc4acf343`。producer、wheel 成员和独立安装的两个二进制均保留 `0775`，安装后未补 chmod；1358 个冻结源文件在构建前后逐一核对。随后该相同 wheel 才被装入独立桌面验收环境，CLI 与私有 Git 版本及实际消费的 runner/native hashes另有收据。`7b4885fa` 相对候选只有 CI workflow 改动，不借该提交冒称重建了目标包。

Root 在同一最新前端的独立生产 Electron 实际点击设置连接，打开真实远端窗口，创建新会话并执行远端命令，provider 收到真实退出码 0、stdout、主机及工作目录。完整 final 后默认过程收起。Changes 正确列出 `proof.ts` 1→2 与实际新建的 proof 文件，未出现先前六个缺失元数据零字节文件。Diff 先加载再打开 Monaco 的路径显示真实 `remoteValue = 2` 和 minimap，pageerrors 为空；中文及空格文件名的真实内容、Git 工作区列表也已显示。

原远端草稿、新会话草稿经实际 reload 和来回切换仍保持；原本地草稿经远端安装与窗口关闭后仍保持。最后的远端控制台为 0 error / 0 warning。受控 provider 不实现 Memory 的结构化 JSON，host.log 真实保留 Phase 1 invalid JSON 和 Phase 2 artifact validation 警告，未据此宣称 Memory 或真实模型长任务通过。截图与收据见 `output/playwright/ssh-live-final-20261018/`。

该次 QA 的 Unicode 文本断言最初未处理 Monaco 的 NBSP；下一次在 textbox 出现但草稿尚未 hydrate 时过早读取。两次原错误均保留，随后只补查准确文本、reload 完成后的原草稿和隐藏草稿，没有重跑已通过的命令、Diff 和 editor。最终关闭本地窗口时 CLI 的 target URL 读取错误与 Electron 自然 exit 0 分别记录，不能把工具收据错误改写为产品崩溃。

## 保留的失败与范围

- 原 `d047e8d1` CI：**8 success / 1 failure**，新 wheel job 在 Git ownership 归档阶段失败，尚未构建或冷安装。
- `3454d84e` CI 被更新实现取代后主动取消，取消状态单独记录。
- 原 `c2c514ff` CI：**8 success / 1 failure**，旧 Git 拒绝命令级 `safe.directory`，wheel job 未进入构建。真实 Ubuntu 目标候选独立构建成功不能替代这次 CI 失败；最新修复及 CI 分开记录。
- 早期目标构建在网络原生编译通过后因 QA stdout 断管停止，未进入 wheel/测试；另有一次 QA manifest 字段混用，均保留原收据。
- 新目录原生产物的实际执行权限丢失通过真实 EACCES 发现，源头已修；旧安装可执行不能替代新构建/新安装验证。
- 快速切新会话时旧 owner 的 tree 请求返回 409，随后新 owner 请求 200，保留实际归属语义。

本轮仍未补齐的桌面功能包括 SSH discovered-alias 选择、可浏览远端目录选择器和显式远端安装入口。当前手输 alias/绝对目录的真实链路已实测；这些发现/浏览/安装控件不能据此算对齐。独立 exec-server session 的外机全矩阵、真实模型长任务、普通 WSL、Windows WFP provisioning、系统 CUA 输入/截图与所有 UI 状态仍需各自证据。

## 最终 GitHub CI 与产物

`7b4885fa` 的九项 job 均已进入实际终态，原九份日志、两个完整 JUnit、两个原生 ABI/identity 报告、冷安装收据和 artifact hash 均已下载。本机实测、CI 跳过项及历史失败分开保留。

| 范围 | 最新实际结果 |
| --- | --- |
| Frontend 与 build/budgets | 4630 passed / 0 skipped |
| Backend | 7092 passed / 154 skipped |
| 根目录 backend | 1785 passed / 52 skipped |
| Windows harness | 362 passed / 15 skipped |
| Desktop unit/smoke | 170 passed / 9 skipped |
| Electron 集成 | 1 passed / 0 skipped |
| 两个 Linux 原生 producer | 实际构建、直接可执行检查、Source identity 和 ELF ABI 通过；不虚构 pytest 数量 |
| Linux wheel 冷安装 | 非 editable wheel、隔离 venv、实际安装字节及两个原生消费者检查通过；不虚构 pytest 数量 |

官方 CI wheel 为 41,070,941 bytes，SHA256 `03a397777a0cd5b62bccd8e724d5db28d7e5a0938ea1c871dbce0582fd09afdd`。1259 个非 native backend 成员逐字节匹配该提交；两个二进制从 producer→wheel→cold install 的 hash 一致，wheel 权限均为 `100755`、最大 GLIBC 需求均为 `GLIBC_2.30`。这与上文在真实目标主机独立构建及实测的 32,123,630-byte wheel 是不同产物，均保存各自收据。

完整机器索引为 `output/ssh-source-audit-20261018/FINAL-RECEIPT.json`；CI 详细表、来源/权限对照分别为 `ci-7b4885fa/RESULT.md`、`ci-receipt.json`、`wheel-comparison.json` 和 `wheel-mode-chain.json`。旧 `d047e8d1`、`c2c514ff` 的实际失败以及 `3454d84e` 的取消没有被新结果覆盖。

## 清理与交付边界

最终独立 Electron 自然 exit 0，测试 provider 已关闭；独立反向 SSH 由 owner 明确请求 SIGTERM 后退出。远端 CLI 显式 stop 的原 daemon 退出，原 PID 和 daemon.json 均消失。清理前核对无专属进程和 mount，先将 wheel、原生 manifest、日志、XML、最终 state/workspace 证据归档下载并校验 hash，再移除精确临时 shim、精确追加的 SSH key 和整个专属 `/tmp` 根。原 authorized_keys 恢复为原 196 bytes 及原 SHA256，回收 7,567,536,128 bytes 临时空间；系统 Python/Node/Git 没有更新。

收集脚本第一次 stop 漏传 CLI 的 `--state-root`，没有停止原 daemon，反而只在默认位置创建了三个空目录。原错误保留；改用正确参数后 stop 成功，清理时按三目录相同的本次创建时间和精确空层级核对，再逐层 `rmdir`。本地临时密钥目录的递归删除被工具策略拒绝；核对实际目录仅含本次两份密钥后，逐文件删除并移除空目录。未触碰项目原有未跟踪 `data/`，普通用户窗口、草稿与未保存编辑不在调试清理范围。

当前仍不能宣称全产品与 Codex 一比一；工程完成度估算不替代本报告各批构建身份与真实验收范围。
