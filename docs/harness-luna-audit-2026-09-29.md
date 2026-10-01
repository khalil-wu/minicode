# MiniCode harness 独立审计：2026-09-29 / gpt-6-luna

本轮重新启动 Python 后端、Vite 和 Electron；新建状态目录、会话、任务仓库和模型请求记录。使用用户指定的本地网关与 `gpt-6-luna`，凭据只进入进程环境。保留开始工作时已有的未提交改动，旧审计报告和旧成绩不作为本轮验收证据。

对照源码：`.tmp/codex-src`，提交 `588b781ab4924ce7352488394028e63d74cf807f`。已对照工具注册、工具暴露、代码模式、审批/沙箱编排、Windows 网络过滤和事件协议。另核对了[官方审批与沙箱说明](https://developers.openai.com/codex/agent-approvals-security)。

**结论：本轮发现的控制面、工具契约、执行隔离和投影问题已直接修复，并有真实任务和外部验收证据；不能据此认定整个项目已达到 Codex 的完整成熟度。** 全量生产源码清单有 782 个文件、302,873 行。清单生成读取了这些文件，人工深读覆盖下述调用链及相关实现；这不等于逐行人工复核全部源码。三个历史仓库难题的本轮模型运行也没有全部通过，详见验收表。

## 架构边界

| 层次 | MiniCode 实现 | 对照与审计结论 |
|---|---|---|
| 控制面 | `QueryEngine` → `TurnKernel` → `TurnIterationRuntime` → 模型流 → `ToolBatchRunner` → 下一轮模型 | 与 Codex 工具 router / orchestrator 的职责一致：控制模型、工具、审批、预算、取消和终态，不把它等同于沙箱。 |
| 工具契约 | `ToolRegistry`、`ToolsetPolicy`、`DeferredToolCatalog`、代码执行运行时 | 对照 `core/src/tools/spec_plan.rs`、`registry.rs`、`code_mode/`：注册、模型直接暴露、按需发现、脚本内部调用分别处理。 |
| 执行面 | `SandboxRunner`；文件路径与网络工具各自的边界实现 | 对照 `core/src/tools/orchestrator.rs` 和 `windows-sandbox-rs/src/wfp.rs`。受管 shell 必须落实声明的文件/网络策略。Windows 当前采用已验证的 Docker 后端；旧 Low integrity 不能作为隔离边界。 |
| 持久化 | 会话 repository、ExecutionJournal、checkpoint、WebSocket outbox | 会话目标、历史、工具轨迹和前端投影分别持久化；进程结束不等于任务验收通过。 |
| 前端 | 协议验证 → 事件 reducer → 会话 store → 普通消息/活动/审批/diff/子 Agent 面板 | 同一事实用一份状态表示，提议的修改不能被投影为已经落盘的修改；子 Agent 文本增量不应附带整个历史。 |

文件和网页工具仍在后端宿主进程内通过各自路径/网络策略执行，尚未统一成一个独立执行服务。Docker 是当前 Windows 受管命令的可用执行器；本轮没有实现或安装 Codex 式专用 Windows 账户/WFP 后端。

## 已修复的根因

| 问题与实际影响 | 修复位置与行为 |
|---|---|
| 长工具结果被包成单行 JSON，再按整行截断，模型只能看到 `First line exceeds…`，反复读取源码 | `backend/tools/code_execution.py`：截断实际选定的文本，保留开头、末尾、错误、有效 JSON、cell 标识与完整 artifact；计入 JSON 转义长度。也保留重启后 unavailable cell 的恢复凭据。 |
| 内部工具签名看起来像位置参数，且任意截掉第 5 个之后的可选参数；Task 的 anyOf 必填分支丢失 | `backend/tools/schema.py`、`registry.py`：从规范 schema 渲染对象参数、枚举、数组和必填分支，完整参数仍可在 `ALL_TOOLS` 查询。真实任务曾出现缺少 prompt、错误 agent_type 和字符串参数调用。 |
| 代码模式外层 schema 不变，内部工具替换/增删后 exec 目录仍命中旧缓存 | 将内部可见工具指纹纳入 exec 目录缓存键；直接模式同时移除没有执行入口的 tool_wait。 |
| 能力快照重复生成完整 schema；UI 和用量检查使用默认工具策略而非本轮实际策略 | 元数据投影不再重复物化 schema；能力与用量检查采用当前执行策略或按下一轮配置派生的策略。明确区分直接、脚本内、按需、隐藏工具。 |
| 脚本内委派把 cell 的临时限制继承为子 Agent 的会话上限，真实 explore Agent 没有可调用的读取入口 | `loop_components.py`：始终向执行上下文发布独立的会话权限上限；后台和常驻子 Agent 的允许列表包含代码执行入口，叶子权限仍逐项执行。 |
| 只读委派进入并行批次后仍占用写锁，阻塞另一调查与父 Agent 读取 | `TaskTool.is_read_only` 与实际只读委派规则一致；回归测试用两个必须同时进入的任务验证并发，不靠耗时猜测。 |
| 长 cell 返回 running 却不说明在等什么，模型把慢任务误当作工具不可用 | cell receipt 返回 `pending_tools`；说明如何逐个输出独立长任务结果。保留 running / failed / cancelled 的真实含义。 |
| 每个子 Agent 文本片段都重发完整子会话，真实父会话日志超过 100 MB | `agent_tools.py`、`execution_journal.py`、前端协议/reducer：首次文本建立快照，随后传增量、持久化序号和字符偏移；边界仍发完整快照。处理 Unicode、重复增量、缺失偏移、空格和换行。 |
| 成功的脚本轮询、内部工具重复展示；委派被标成“发送消息”，失败编辑被标成“已编辑” | 只折叠有可见叶子证据的成功编排收据，调试历史和失败保留；委派、消息、取消、部分完成分开显示，读取/搜索等常用文案统一。 |
| 被拒绝写入的预览 diff 被累计进最终“已修改”统计 | 工具失败结果不继承提议 diff，历史修改汇总只计算成功执行记录。真实任务中被拒绝的清空文件提议不再虚增 52 行删除。 |
| Low integrity 可写另一个被标记 Low 的工作区，且代理环境变量不能阻止 socket | 从受管沙箱候选中撤下此后端；没有能落实策略的执行器就不启动命令，显式提权继续使用既有审批。旧设计文档加上撤回说明。 |
| Docker 内 pwsh 把原生退出码 7 变成 1；Windows 映射路径后残留 `\`，声明可读文件读不到 | 宿主和容器共用 PowerShell 输出/退出码脚本；映射已声明根及路径分隔符。镜像补齐 Python 命令和 pytest，实际重跑执行边界验收。 |

## 工具提供与冗余

没有把 Git、shell、文件修改、AST 和 LSP 等职责不同的工具简单删成一个入口，也没有以改名模拟对齐。

独立构建默认注册表，移除仅供宿主使用的私有 schema 字段后测得：

| 模式 | 直接提供的工具数 | 工具请求 JSON 字符数 |
|---|---:|---:|
| direct | 49 | 44,399 |
| code | 51 | 47,344 |
| code_only | 7 | 25,267 |

这说明工具数量减少并不意味着 schema 成本消失，内部目录仍占上下文。当前桌面会话叠加模型、工作区和权限后实际显示：72 个注册工具，6 个直接工具、48 个脚本内工具、16 个按需工具、2 个隐藏工具。源码工具名检查通过，没有跨文件重复注册名称。上述数字是本轮注册表与会话的实测，不是所有配置的常量。

## 本轮验证

### 真实任务、运行与恢复

- **结账服务修复**：通过 Electron 输入任务；两个 explore Agent 读取不同职责代码，父 Agent 修改、展示审批 diff、执行测试、查看 diff、交付。独立外部 oracle **10/10 通过**，模型运行的可见测试 **7/7 通过**。原有 `tests/test_checkout.py` 的 Git diff 为空。验收依据是外部运行与文件内容，不是模型的完成表述。
- **审批**：通过实际页面允许有依据的修改和 pytest；拒绝一条清空 checkout.py 的写入提议，文件保留且外部验收通过。拒绝提议同时揭示并推动修复了最终 diff 统计污染。
- **进程重启恢复**：关停并重新启动 Electron/后端，恢复相同会话 ID、用户目标、最终答复、工作区、历史和完成状态，hydration 结束。
- **流式子 Agent**：真实双 Agent 只读任务完成；该会话约 3.27 MB wire 日志，520 条文本增量，完整快照只在边界发送。随后单 Agent 任务 107 条增量、约 431 KB 日志，空格/换行修复后无新增协议告警。这些任务长度不同，不能把前后字节数当作等负载性能倍数。
- **上下文压力**：全新 24k 窗口任务触发压缩、达到 1800 秒预算并保留 checkpoint，未被伪装为成功。其实际修复质量未通过外部 oracle。

本轮启动的历史仓库任务都使用新 workspace/state，请求与结果保留如下。该组是连续诊断过程：后启动的进程可能包含中途修复，因此**不是最终统一版本的对比跑分**。

| 任务 | 模型轮数 | 用时 | 外部验收 |
|---|---:|---:|---|
| real_scheduler | 114 | 817.55 秒 | 失败；模型 completed 不计通过 |
| real_file_snapshots | 42 | 314.16 秒 | 失败；仍有快照一致性/非文本大小边界未修好 |
| real_compaction | 250 | 1807.36 秒 | 失败；1800 秒预算终止并保存检查点 |

这些是模型对历史损坏仓库的解题结果，不是当前 MiniCode 对应模块回归测试的结果；本轮没有手动改答案或修改 oracle 来取得通过。

### 执行面

新增 `scripts/check_execution_boundary.py` 可直接复现，不需要模型或密钥。已构建 `minicode-agent-sandbox:latest` 并通过 SandboxRunner 实际运行，**9/9 通过**：

1. 工作区内写入；2. cwd；3. 原生非零退出码原样返回；4. 声明的外部根可读；5. 相邻工作区不能写；6. 直接连接 IP 被禁网边界阻止；7. 沙箱内运行 pytest；8. 超时清理；9. 取消清理。没有遗留本轮容器。

```powershell
docker build -f backend/sandbox/Dockerfile -t minicode-agent-sandbox:latest backend/sandbox
python scripts/check_execution_boundary.py --out .tmp/execution-check-unique
```

### 自动回归与界面

- 后端工具/上下文/审批/取消/检查点/沙箱相关集中回归：197 通过、19 跳过；之后执行面与子 Agent 改动的集中回归：104 通过、2 跳过；子 Agent 流式相关 71 通过。包含重叠用例，不把这些数字相加。
- 前端全量运行覆盖 184 个文件、2,159 项测试。首次仅有子 Agent 面板仍断言旧英文文案的一项失败，更新断言后对其所属文件和协议测试复核通过；相关工具、diff、流式、能力投影回归也分别通过。
- TypeScript 检查、生产构建、协议枚举同步、Agent kernel 边界检查、工具名称重复检查通过。
- 实际检查桌面空态、流式活动、计划进度、子 Agent、审批、diff、拒绝状态、最终交付和重启恢复。390×844 下无横向溢出；可见图标按钮均有可访问名称。这不等于完整的 WCAG 合规认证，也不代表逐页视觉验收所有产品页面。

## 证据与边界

本轮工作目录：`.tmp/harness-luna-20260929-1610/`。

- `evidence/source-inventory.json`：生产源码清单。
- `tasks/*/{trace.jsonl,requests/,result.json,result.log,changes.diff}`：真实模型过程及外部验收。
- `evidence/checkout-oracle.log`：独立 10 项结账验收。
- `evidence/cross-workspace-probe.json`：旧低完整性后端跨工作区写入的实际复现。
- `container-acceptance-final/result.json`：修复后的 9 项受管执行验收。
- `evidence/tool-surface.json`、`stream-wire-metrics.json`：工具成本与流式投影观测。
- `evidence/*tests*.log`、`*acceptance.log`、`frontend-suite*.log`、`frontend-build-final.log`：验证输出。
- `output/playwright/luna-{approval,verified-delivery,mobile}.png`：实际页面截图。

原生 Windows 专用账户/WFP 后端、文件与网页工具统一执行面、全部生产文件逐行人工审计、三个历史难题在最终统一版本上的全部通过，以及完整产品无障碍/逐页验收，仍不能在本轮证据下标记为完成。
