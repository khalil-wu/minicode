# Remote MCP、Skill 与原生模型输出消费链

2026-10-16。行为依据仍为 Codex Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。这是未提交工作区的 R3 组件交付记录，不借用 `ae278f9` 的历史 CI，也不声明整款桌面或所有 remote 工作已经完成。

机器索引：`output/source-remote-mcp-skills-delivery-20261016/index.json`。其中按完整 testcase ID 取最新结果：**181 个唯一节点，180 passed、1 skipped、0 failed/error**。前端 ConnectorsTab **28/28**。各批有重叠，不能直接相加。

## 实际接通的调用链

- 每个 MCP server 独立消费捕获的 environment authority；缺失 local 可用 ambient HTTP，缺失 remote 只准真实 selected plugin；Pending/Failed 不可用，FromThread 不额外收紧。连接和 authority 是不同事实。远程 stdio、HTTP 沿原 environment 的进程、HTTP 和 lease 执行，不重新读取窗口当前环境。
- ExecutorOnly 来源不读取宿主 token store、env credential 或 OAuth；真实 host-owned HostFallbackAllowed 保留 Source 旧 peer 的显式分支。`env_vars` 的 `source` 全链只有 `local` / `remote`，不再当作另一个变量名。配置、状态、后端保存和设置页编辑均保留 `environment_id`、`enabled`、bearer env 名及 env header 声明。
- 受限 FS 强制 high-level capability discovery；每 root 带其捕获 sandbox，缺 `capabilityDiscoverySandbox` 保留真实错误。扫描失败或 partial warning 不缓存为下一轮成功。普通注册不虚构 capability root；OwnerReady 只用实际提供的 root。
- executor Skill catalog 保留真实 authority、package、environment URI、enabled、namespace、metadata 与完整 UTF-8。ContextBuilder 再绑定时保留同一个 turn catalog，不再丢 executor catalog 后改扫宿主目录。Required Skill 的 exact name / enabled / source gate 保留 Failed、Starting 要求，真实 QueryEngine 在 provider 请求前拒绝缺失项；专用 domain error 将 Source 原文直接送到用户错误事件。
- `skill_mcp_dependency_install` 是 Stable/default true 的内部开关，没有新增 UI 开关。MiniCode 的真实 originator 是第三方 `minicode`，不伪造 Source first-party 身份来打开自动提示安装。已有 MCP 和显式用户安装入口保留。
- implicit Skill invocation 在实际 ToolBatch dispatch、原命令执行前记录同 turn 的 canonical user-owned Skill。实际脚本 exit 7 仍有这份证据；冷 snapshot 不恢复 runtime trust，remote Skill 也不能变为宿主 user-owned 证据。
- 三个资源 core 工具保留 Source JSON 形状、原 resource/template 字段、单 server cursor、contents 与 metadata。它们消费实际 captured ModelDefinition 已有的 `model_messages.tools.mcp_resources`；描述和完整 schema 覆盖按 Source 支持的 JSON Schema subset 解析，非法参数覆盖发 warning 后保留 bundled 参数。参数覆盖不改变执行参数契约。
- host-produced `native_tool_output` 经 Context、checkpoint、journal、Responses、Chat、Anthropic 和 Guardian 消费；不伪造 MCP accepted identity 或 Skill 来源。Code Mode 的公共结果不泄露这个内部字段。
- MCP proxy 的审批和 server callback 绑定其 server 自己的捕获 permission scope，不借 primary/host profile。旧 proxy 的 callback manager 也保持原 owner。重绑先移除旧目录代理，再通过独立 retirement owner 等待原 Code Mode cell、真实工具 cleanup 和 RPC request 结束后关闭旧连接，避免目录刷新取消已有请求。实际 Source stdio、真正 yield 的 Code Mode cell 在下一 step 重绑后再次调用原 proxy 已验证。
- remote stdio 的非法 JSON frame 沿官方 MCP SDK 的约定把原异常送入 read stream，保留日志，后续合法 frame 继续由 SDK 消费；不再 debug 后直接丢弃。

## 验证与限制

正式运行使用 bundled Python 3.11.9 和 `output/ci-repair-20261015/pytest-python311.py`。实际安装版 Source executor 是 `C:/Program Files/WindowsApps/OpenAI.Codex_26.1007.9446.0_x64__2p2nqsd0c76g0/app/resources/codex.exe`；自有进程使用隔离 CODEX_HOME/cwd，没有模型或官方账号请求。

实际 Source stdio 证明原进程 framing、最终无换行 Unicode、非法 frame 原异常、Skill walk/high-level/read、原 lease 与 child lazy MCP。独立 R1 WebSocket peer + 真实 loopback HTTP MCP 服务证明 checkout pin 的 `environment/config/read` 消费、HTTP headers/session/cookies、原字段和分页；这部分是 Source wire 契约验证，不能冒充安装版原生 API 支持。

唯一 skip 是安装版 26.1007 的 `environmentConfigRead` 宣告为 true，实际 `environment/config/read` 返回 `stub does not implement ... yet`。原失败和探针跳过理由保留，未将其计为通过。官方账号登录、授权和依赖账号的线上服务按用户要求不做，不计阻塞或通过。本批没有部署外部机器，也没有证明 R4 desktop remote host 的 tunnel/bridge/UI 全链。

| 记录 | 实际结果 |
| --- | --- |
| `remote-mcp-skills-complete-batch02r1.xml` | 历史初败：55 节点，46 pass、9 fail；保留，不计最新总数 |
| `remote-mcp-skills-complete-batch03.log` | 中断，无最终 XML/总数；停止时输出还有缓冲，原先 40% 不能当作完成位置 |
| `remote-mcp-skills-runtime-batch03r1.xml` | 42 节点，36 pass、5 fail、1 skip；测试夹具缺参、mock 签名和 Windows 换行预期先失败 |
| `remote-mcp-skills-runtime-batch03r2.xml` | 5 节点，2 pass、3 fail；继续暴露测试夹具缺 api_key、AgentSession 错误入参、测试服务 GBK 输出 |
| `remote-mcp-skills-runtime-batch03r3.xml` | 3 节点，1 pass、2 fail；测试尚未真正 yield，且把真实 exit 7 误预期为工具 API error |
| `remote-mcp-skills-runtime-batch03r4.xml` | 两个修正后的实际执行节点均通过 |
| `remote-mcp-skills-regression-batch04.log` | 中断；原 SDK 测试把 `python` 交互进程当 MCP 服务，新 catalog 重建后会等 initialize；改为实际 stdio MCP fixture |
| `remote-mcp-skills-admission-batch05.xml` | maxfail=3，三项失败；测试的 workspace-write fixture 缺 Source 默认 root-read，真实 Windows Git context 无法运行该读受限配置 |
| `remote-mcp-skills-admission-batch05r1.xml` | 10/10：8 个实际 stdio SDK 审批 + 2 个 required Skill 的真实 QueryEngine 零 provider 请求及 UI 原文 |
| `remote-mcp-skills-regression-batch06.xml` | 131/131；另 8 个节点因已在上行通过而 deselected，不把 deselected 算通过或 skip |
| `remote-mcp-connectors-batch03.json` | 28/28，设置页编辑保存、环境/凭证声明与草稿回归 |

后续 R0 实际 stubborn tool 验证发现 terminal 清理仍可能等待用户才释放的工具：Root 将 current/old capabilities 的 retirement 交给原 lifecycle owner，用户 terminal 不硬等仍在运行的真实 cleanup。本组件在创建 retirement 时固定原 cleanup borrower tuple，避免移交后发现退休任务自身；真实 Source 请求期间把 retirement 放回 cleanup owner 的无自等验证保留在 `remote-mcp-skills-retirement-batch07r1.xml`。此前 `batch07.xml` 的测试服务 GBK 读入中文路径初败也保留，未记为通过。

最终真实 SDK 验收进一步发现 registry drain 后 wrapper 会晚交接实际 invoke child。只等待上述 tuple 会让非 MCP child 提前失去原 Prepared snapshot。`PreparedEnvironmentCapabilities.retire` 现在在调用边界捕获原 `cleanup_tasks_by_call` 字典对象：先等原 consumers/cells，再跟随该字典里实际 wrapper/child 的实时值，最后等待原 MCP client 的 pending requests，才关闭原能力。没有每轮重读当前 builder，没有动态扫包含自身的 lifecycle 集合，没有加入新的 owner 状态；registry 现有 `context.llm` 模型租约与 SDK 原会话锁继续沿真实 child 保留。

`remote-mcp-retirement-handoff-batch01.xml` 与补显式 Source executor 的 `batch02.xml` 合并后为 **37 个唯一节点：36 pass、1 skip、0 fail/error**。第一批因未设置显式 Source executor 有 6 skip；第二批只重验这 6 项，5 pass、1 个安装版 stub skip。该 37 项包含 6 个 step capability 回归，未另加到上述 181 个 R3 唯一节点。真实 Source selected-plugin、Code Mode cell、原 lease 与 retirement 无自等场景均通过。remote agent 的 `sdk-mcp-final-owner-handoff-after.xml` 另记录完整 **16/16**：绑定／未绑定 × 提前／延后交接，直接证明没有 binding waiter 的非 MCP child 也能保留原 Prepared snapshot，并在 source 完成后的第二阶段 capability 关闭期间继续持有会话锁。这些批次不能相加作唯一总数。

本组件最终生产文件 `backend/mcp/environment_runtime.py` SHA-256 为 `6c446f0bddff6a4d4e7ba98bbc35f9a3bab53ba922fb1a1479e9462033403052`。R0 的 terminal/claim 与全部 backend 唯一节点仍由对应总体验收记录说明，本报告不替代那份验收。

最终组件字节和证据文件 SHA256 在机器索引。它记录验证后的当前组件身份；共享文件还含其它 agent 的改动，不宣称每一批运行期间冻结了整个 checkout。没有 stage、commit、push、删除 data/、清理用户草稿或操作用户普通窗口。字体、字号和字重没有修改。
