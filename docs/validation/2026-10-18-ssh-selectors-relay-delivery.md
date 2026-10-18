# SSH 发现、远端目录选择与生命周期核对

本批以固定 `codexsrc` `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 和实际安装的 Codex Appx `26.1013.4894.0` / package `26.1013.40934` 为依据。连接、目录、流关闭、内部回环及窗口请求归属的真实根因已修复；最新统一前端、专项回归和最终新进程的实际桌面操作均分别留证。新 GitHub CI 按本批冻结提交取证，旧 `7b4885fa` 的九项成功不能替代本批结果。

## 实现与依据

| 范围 | 本批行为 |
| --- | --- |
| SSH 发现 | 按当前 Source 扫描配置的 Include、glob、已访问文件、每个 Host 的第一个具体别名，排除模式及 colima，并按 locale 排序。发现阶段不运行 ssh-G。 |
| 实际 catalog | 已保存别名由真实 `ssh -G -F` 解析 Host、Port、Identity；实际非零退出、缺 hostname 和 github.com 按 Source 过滤并留下日志。保存保留别名身份，连接继续交给 SSH 配置解析，不冻结实际解析值为显式覆盖。 |
| 批量保存 | 整批检查后一次持久化，再刷新实际 catalog；前端只连接成功返回的可用 id。过滤后的单项结果为 null，不虚构连接。 |
| 连接详情 | 菜单进入 Details，header Edit；值本身可复制，无额外复制图标。Alias、Host、Port、Identity 来自当前记录；Version 只显示当前已连接 host 的实际 ready.version。 |
| 列表与切换 | SSH 与 WSL 按当前显示名排序；WSL 已保存连接和新发现发行版的呈现行合并排序，仍执行各自真实动作。WSL 发现结果不会泄漏到 SSH 页或压住其空状态。 |
| 连接生命周期 | 名称变化保留原 transport；实际目标变化关闭原 proxy 再连接，不停止或接管另一 daemon 的任务。旧 generation 的待释放窗口不能向新 generation 发送释放。仅名称更新保留 transport 是 MiniCode 实现选择，不冒称 Source 内部对象比较逐字相同。 |
| 远端目录 | 从当前窗口绑定的 host 枚举目录，只有最终确认才 trust 选中目录。输入前缀、方向键、Enter、Up、点击浏览、取消、空/错误状态均按当前 picker 契约接通。键盘 Enter 只选中建议并保持父列表；点击目录行才同时进入目录。 |
| 目录链接 | 当前桌面 producer 实际走 ExecutionHost → fs/readDirectory，按链接目标分类。有效目录 junction/symlink 可选择，无法读取目标元数据的单项按 Source 略过；整个目录读取错误继续报告。普通 Node Dirent 不是这个远端消费者的对照依据，先前据其排除 junction 的判断已撤回。Windows Python 的 DirEntry.stat 缓存也不能代替实际目标查询，因此使用 os.stat(path)。 |
| Git 子目录 | 使用已有 rev-parse --show-toplevel 结果识别当前 checkout，保留任务的真实 cwd、scope、owner 和 path_uri。主仓库与 linked worktree 的当前/可移除判断都使用 checkout root，不用 next 默认值或隐藏原 500。 |
| Relay | 单个 socket 的读写或 connection_lost 通知只结束对应 stream，保留实际 error frame 和 traceback；物理 relay 写失败及真正的非 I/O 关闭错误继续传播。没有重试、降级或成功状态替代。 |
| FIFO 与关闭归属 | data/end/close 保留最后字节与 EOF；closing stream 在真实关闭前保留 owner，物理断开和任务取消中断其 pending I/O。Node 接收端等待原 received 队列和 socket.end 完成，frame reader 不等待其它 stream 的慢收尾。 |
| 正常断开 | 已连接且没有实际错误时，只发送 EOF 并等待所拥有的代理正常退出；启动取消和真实错误仍终止该连接进程。正常收尾的非零退出保留真实 code/signal/stderr，没有定时降级或重试。 |
| 内部窗口绑定 | RemoteNative 的私有回环 HTTP client 使用 trust_env=False，直接访问已绑定本机 bridge；不改变模型和其它业务请求的代理策略。 |
| 窗口与请求归属 | 同 host 只有一个释放 owner，等待该窗口已接受的普通 native 请求及 native/editor 两路生命周期 ACK；disconnect、目标 refresh、app close 均不能越过它。正常 transport close 等全部已接受 native 请求（含完整 JSON body），真实 I/O error 仍立即中断。失败交原调用方；既有显式 retry 意图沿 main 传递，只在明确再次清理时退休已失败的 owner，活动 owner 不替换，closed host 不重放旧请求。 |

完整桌面函数、资源哈希与消费者位置集中在本地 `output/ssh-selectors-20261018/source-contract.md`、`source-resource-receipt.json` 和 `independent-selector-review-20261018.md`。目录类型依据为 `codex-rs/app-server/src/request_processors/fs_processor.rs:134–153` 和 `codex-rs/exec-server/src/local_file_system.rs:678–708`；Git 依据为 `codex-rs/worktree/src/lib.rs` 的 repository_root/relative_cwd 分离；FIFO/关闭依据为 `exec-server/src/forward.rs:117–170` 的写任务归属、Close 前消息排空和真实 disconnect 中断。Source CLI 的 stdio-to-uds 在 stdin EOF 后半关闭 socket 并等待反向输出完成；实际桌面 Duplex 的 final/end 与 destroy/kill 是不同操作，dispose 确实使用 destroy，不能冒称所有 Source 断开都等待 EOF。本批修正的是 MiniCode 持久 daemon 已约定的正常 EOF-detach 链路。

**撤回旧安装入口缺口**：当前桌面连接行的实际 consumer 明确 `case install-codex: return null`。后端安装 API 的存在不代表当前设置有安装按钮，本批不添加该控件，也不把它继续列为缺少的 UI。

## 专项验证

| 范围 | 各节点最新实际结果 |
| --- | --- |
| Connections 与 picker | 两个文件统一 **33 passed**：26 项连接、7 项目录选择，包含独立审查发现的四处修正。 |
| Host 与目录分类 | 最新完整文件 **5 passed**：实际 CLI/窗口归属/PTY/harness、实际 Source fs/readDirectory，以及真实 loopback bridge＋环境 HTTP proxy trap。0 skip。 |
| Git editor API | 本地与实际 Source 的 21 个唯一节点最新均通过，0 skip；主/linked 子目录、native/URI、projectless 仓库/非仓库及原 owner/scope 均有覆盖。 |
| Relay | 同一桌面 Python 3.11.9 / Windows Proactor / -X dev 的完整文件 **19 passed**，包含真实 TCP、半关闭、credit、最后字节、堵塞 drain、取消 owner、单流错误及物理失败传播。 |
| Desktop host/连接/窗口生命周期 | 最新 remote-host、remote-desktop、window-lifecycle 三文件 **42 passed**，包含正常 EOF/非零退出、两路 ACK、并发窗口、失败/显式清理、实际反向 binding、完整 JSON body 及真实错误中断；security 既有受影响批次 **18 passed**。不累计重跑。 |

前端证据为 `frontend-source-review-batch05.log/.xml`；host 最新为 `backend-host-final06.log/.xml`；Git 为 `worktree-cwd-receipt.json`；relay 为 `proactor-runtime-boundary/close-order-repair.md/.json`，正常断开另见同目录后续回执。最初 npx 命令未进入测试体、host final05 未加载 asyncio 插件、Git linked owner/CRLF 夹具差异、错误目录对照及 Windows 缓存差异均保留。没有把未运行测试体或历史失败计为通过。

默认 CI 未设置 MINICODE_SOURCE_EXECUTOR，实际 Source 对照在 CI 会真实跳过，本机显式 Source 的通过另列。新 host/relay/store 测试进入 Linux 全量后端；Windows 精选 job 未包含所有这些文件，不能把九项 CI 解读为每项新增测试均跨平台通过。

## 实际生产 Electron 与模型

验收只操作独立 profile。Electron、renderer、IPC、RemoteDesktop/RemoteHost、RemoteNative、反向窗口绑定和 backend 都是生产实现；只有名为 WSL 的测试 transport 被替换为实际本机 Python CLI proxy。这证明 host-bound UI/协议链，不冒称真实 WSL 或本批重新做过外机 SSH。

真实 SSH 发现及 ssh-G 使用专属配置；连接到明确关闭的 loopback 端口得到实际拒绝，没有发布已连接或多余远端窗口。实际 Details、发现多选/刷新、已连接 version、中文目录列表、浏览取消及最终确认均有截图。浏览和取消没有写 trust；首次确认时仅选中的中文目录进入信任表。后续新建的两个 projectless 任务目录按既有任务流程进入信任表，不能将它们误判为 picker 浏览扩大了信任。

真实 `gpt-6.1-sol / high / Responses` 通过生产 backend 和修复后的 relay 执行 read_file。返回的 content_hash 与独立读取 proof.txt 的 SHA-256 相同，最终回答包含实际中文内容。完整 final 后过程默认收起；历史重开后发送处于空闲状态，没有停止按钮。选定文件也从真实文件树打开到 Monaco，内容和 minimap 正常，未编辑文件。

本地原草稿和远端原会话草稿经实际切回及 reload 保持；隐藏的 workspace-empty 原草稿由只读 IndexedDB 核实存在，不冒称曾在界面中重新显示。字体、字号和字重未修改。

原 relay 失败、第一次真实模型请求的测试环境鉴权失败均保留：后者是继承的 owned-qa 环境变量覆盖了新配置，修正独立 daemon 的环境后实际请求为 200，没有生产鉴权兜底。详细回执、原始 wire 和截图在 `output/playwright/ssh-selectors-20261018/`。

最新前端为 `assets/index-rC2EnfFC.js`，index SHA-256 `caf6a30f75c8099b66f3f5fa15c137447b695a998ed160c56f5f191380981290`；TypeScript、构建、bundle/UI debt 两项预算通过。此前 CxYf 构建实测 Details/Edit 重命名排序、只读别名及 WSL/SSH 行隔离，回执 `latest-build-acceptance.json`、截图 15–20。rC2 新进程再次实测有效 junction、不可读目标省略、Enter 保持父列表、Up 返回父层、取消保持 generation，以及实际历史 final 默认折叠和空闲输入状态；证据为 `fresh-current-picker-receipt.txt`、`fresh-current-resting-receipt03.txt`、截图 23–27。未重复发起已通过的模型任务。

关闭链的原失败完整保留。十个纯 asyncio 探针最初没有复现 Win10054，未算解决。随后实际 -X dev source_traceback 指向 Relay.run → virtual close → writer.close；真实同包 data/end/close 和背压回归证明两端提前关闭会丢字节，取消 closing owner 也会留下真实 pending I/O。修复后 19 项、两端完整 direct 链和新进程重放分别验证。

第一轮新进程没有再出现 Win10054/121，但真实 UI Disconnect 仍出现 client_connected_cb / Win64；该失败见 `fresh-current-host.log:190`。完整独立链再次复现，根因是 stdin.end 后立即 kill。修正正常 EOF 顺序后，第二轮新 Electron/daemon 经实际连接、目录浏览/取消、远端窗口关闭、UI Disconnect 和显式 daemon stop，原始日志为 `post-eof-host.log`：callback、client_connected_cb、asyncio ERROR、Win64/10054/121、traceback、ResourceWarning、unclosed 均为 0，四条 -X dev 慢 task 诊断原样保留。回执为 `post-eof-ui-receipt.txt`、`post-eof-runtime-log-verdict.json`，截图 28–29。没有修改默认 Proactor、异常处理器或用 catch 隐藏该错误；这一结果只证明所列实际场景未复发。

重载时的早期草稿验收曾错误地把全部目录元数据要求为瞬时相同；会话 catalog 尚在恢复时 workspaceRoot 可暂为空。原失败和前后状态保留。按照生产 draftKey 的 windowOwnerId/kind/id 核对，完整 authored draft 未变，目录元数据恢复后也有独立读取证据；没有修改生产草稿代码或注入恢复值。QA profile、原始本地/远端草稿和历史记录保留。Playwright 在关闭最后页面后读取 URL 的工具异常不等于应用退出失败，须另外核实实际进程结果。

第二轮连接关闭通过后，整个应用退出仍发现 releaseWindow 尚未收到 ACK、UI Disconnect 就关闭 transport 的竞态，实际进入退出清理恢复页。`post-eof-full-desktop.log:139`、`post-eof-startup-recovery.yml`、截图 30 保留失败；随后点击恢复页 Quit 的第二次退出不算首次通过。两路 ACK、唯一 owner 和既有显式再次清理的真实 HTTP 红绿回归分别保留，三条关闭路径已统一修复。

第三轮首次应用退出正常，当前 generation 的释放全部确认，但普通 pty:list 请求还在反向 _binding 时被剪断。`post-release-asgi-failure.json` 精确记录 5 个 ASGI / HTTPX 协议错误、5 个未关闭 MemoryObjectReceiveStream 和 10 个 traceback 块。没有把它们归为正常 403。Source 当前 scheduler 只放弃未 dispatch 的请求；sentToTransport 的请求不能冒称取消成功。本批据此保留已发 native HTTP 的真实 owner，让窗口释放及正常 transport 关闭等待它完成，完整正文与真实错误中断都有门控回归；不 catch HTTPX 错误或修改 middleware。

最终第四轮新进程使用同一 rC2 前端及最新 main/remote-host/remote-desktop，实际快速执行打开远端窗口、目录浏览/取消、关闭远端窗口、UI Disconnect，再显式停止自有 daemon、首次关闭本地应用。`post-native-owner-runtime-log-verdict.json` 的 6058 字节日志中 callback、ASGI、HTTPX、Win64/10054/121、ResourceWarning、未关闭流及 traceback 均为 0，四条 -X dev 慢 task 诊断保留；`post-native-owner-desktop-verdict.json` 核实首次 Electron exit 0、signal null、0 fetch/cleanup/shutdown 失败。窗口销毁后的 5 个真实 HTTP403 已完整返回原调用方并保留，不用假结果替代它们。当前 generation 待释放为 0，旧失败 generation 的一条持久记录保持原样，未跨 generation 重放。机器回执、截图 33–34 及七个自有应用/daemon 进程全部退出的记录在 `post-native-owner-*`；profile、草稿与历史保留。未操作普通用户窗口或原 data。

共享临时模型输入及唯一自有 QA 配置中的凭据在两会话结束后单独清理，正式配置与 profile 不清空。变更文件和自有证据/profile 的 UTF-8 / UTF-16LE 精确匹配结果为 `output/ssh-selectors-20261018/credential-audit-final.json`，清理回执为 `private-config-cleanup.json`。字节扫描不代替图片视觉脱敏，相关 provider 截图由并行 UI 任务单独记录。

## 其它并行范围

真实 tools、Skill、MCP、Node 跨 compact、委派、审批和其它界面区域见[并行 UI 与 agent 链报告](2026-10-18-real-ui-agent-chain-delivery.md)。SDK/REST/Memory/WS/Eval 的产物 store 所有权和关闭链已完成：等真实 borrower、后续 child 和磁盘扫描结束，借用 store 不关闭，延后关闭错误继续留证。20 项新生命周期测试通过，212 个唯一节点的各自最新结果为 211 passed / 1 real skipped，原始失败和窄复核分别保留，未冒称重跑一整轮全绿。完整本地回执为 `artifact-owner-lifecycle.md/.json`。

普通 WSL、其它设备/平台矩阵、系统 CUA 的完整输入/截图和全部界面像素状态不能借本批专项结果宣布等价。官方账号及依赖其账号的线上服务仍按用户要求排除。原未跟踪 data、普通用户窗口、草稿和未保存编辑不属于清理范围。

## 本批 CI 身份

首轮生产提交 `7f20e5665254ded97c8ec14ee98639c955055095` 的 [CI 39108669632](https://github.com/khalil-wu/minicode/actions/runs/39108669632) 为 6 success / 3 failure；9 份完整日志、8 类原始产物和 84 项 Source/ABI/wheel 校验齐全。7 个失败用例对应旧 prompt/750ms 去重断言、漏传桌面策略，以及未迁移 awaited store close 的夹具。后续仅更新测试合同，未恢复旧生产策略或加入兼容兜底。统一前端五文件 **211 passed**；后端四文件 **106 passed**、0 skip。扩展本机验证另发现两项既有测试混合了冷准备/命令排队与实际完成，现按真实准备信号和已登记的 delete owner 等待，保留原 1s/2s 行为时限与上下文隔离/不启动索引和 MCP 断言。原调试批 104 pass / 2 fail 和普通模式 4 pass / 2 fail 均保留，不将重跑累计。

本报告与生产修改一同冻结提交，九项新 CI 和八类产物只接受该完整 SHA 的实际运行。最终 run、原始日志/JUnit、真实 skip、ELF ABI/Source 身份、wheel RECORD/可执行权限和冷安装回执保存在 `output/ssh-selectors-20261018/ci-current/`；GitHub [本分支 CI](https://github.com/khalil-wu/minicode/actions/workflows/ci.yml?query=branch%3Acodex%2Frelease-readiness-20260927) 可核对提交与结果。各批次数量存在重叠，不相加为全产品覆盖率。
