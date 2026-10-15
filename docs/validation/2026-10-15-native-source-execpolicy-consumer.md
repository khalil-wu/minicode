# 固定 Source 执行策略完整消费链

本记录覆盖 MiniCode 现有后端消费固定 Source 执行策略及其审批投影，
不代表整款产品与 Codex 完全一致。Source 固定为
`822e58cc3d666166c7446c5b1ea2e52f5d09594c`；独立原生 helper 的 180 节点和
release 冷验 9 场景见同日 helper 报告，不重复计为消费链测试。

## 实际调用链

- trusted config stack 的 system/user/project `rules/*.rules` 按 Source 顺序读取；
  仅不存在的可选目录被忽略，真实 IO/UTF-8 错误保留。
- 原生 Starlark parser 载入会话策略 owner。无效 user rules 使用 Source 的
  `load_exec_policy_with_warning` 语义，丢弃整组 user policy、保留 managed rules
  及真实 warning。managed `[rules]` 使用原始 Source producer，Allow 不被接受。
- 模型过滤来自已准入的 `ModelExecutionSnapshot`，同一 filtered base 供 native
  requirement 和 permission instructions 使用；environment overlay 不进入模型提示词。
- 最终工具参数、实际执行 argv、policy argv、shell/cwd、权限 profile、
  additional grants 被同时冻结到执行 digest。审批后执行原 capture，不再发现 shell
  或靠字符串危险词重判。旧 `with_escalated_permissions` 只在准入归一化，
  对模型公开 Source 的 `sandbox_permissions`。
- 原生 Skip / NeedsApproval / Forbidden 决定 run_command 的实际授权；
  非 shell/MCP 能力边界继续使用各自真实 owner。
- 普通命令 available decisions 为 Approved、真实提议的 ExecpolicyAmendment、
  Abort；没有凭空增加 conversation/session approval。inline additional grant
  只提供 Approved/Abort。WS 和直接 SDK 回调都核对同一冻结决策、前缀和 digest。
- accepted amendment 用原 Source advisory-lock writer 写入，成功后更新原 owner；
  真实写失败会发 warning，当前明确批准的调用仍运行一次，不产生后续可复用授权。
- 模型 permission instructions 使用七份原 Source 模板与原文字段；保留显式空文本、
  Source 路径预算、Never/UnlessTrusted/OnRequest/Granular 与 reviewer 组合规则。

## 原生审批表面

Source ASAR 依据为
`output/playwright/codex-control-audit-20261013/source/native-command-approval-body-audit.json`，
含 qr/Xr/Aml 原函数、中文词典、三行预览常量及源文件 SHA256。

- MiniCode 采用已核实的 Source 默认 `STEPS_COMMANDS`：终端身份、真实 reason
  作标题、仅公开命令预览。reason 按 Source approval requirement reason 优先、
  其次冻结 justification 的顺序生产。
- `display_command` 从 `FinalExecutableToolRequest` 的公开 command 生成，经
  实际 approval event → WS payload → pending store → replay 原样传递。
  权限用 policy argv 不重新拼到 UI，内部 UTF-8/PowerShell 包装不进入命令预览。
- native 专属 body 没有 raw argument/cwd 表、login false、额外沙箱红条、
  来源内部标签或泛化队列状态。MCP 和非 native 审批保持各自现有表面。
- 预览按实际渲染测量，超过三行才显示展开/收起；默认 `3lh` / line-clamp 3，
  展开容器 `20rem` 内滚动，完整公开命令不丢失。
- prefix 使用 split button 的普通动作菜单，`允许一次` / `允许类似命令`，
  不出现已选值 check。右侧信息图标复用 HTML Tooltip，原 prefix 放在真实提示中；
  不修改设置 SelectMenu。保留现有 MiniCode 字体、字号和字重。
- qr → Yr 对真实前缀的 LF/CR 返回 null，不显示 scoped action；shared
  V6/WSr → GSr/xWn 原算法只格式化 Tooltip 中空格、shell 字符及引号，审批仍提交
  原 token array。原函数与 SHA 见 `source/native-command-prefix-source-audit.json`。
- native 终端 FE/Wki 和右侧 Info bC/lzi 使用真实 Source SVG 路径，保持原
  18/14 显示尺寸，不修改 generic/MCP 图标。正式 SVG 与提取哈希见
  `output/native-approval-source-icons-20261015.json`。
- native 提交状态复用 `Button.loading`，保留 `允许一次`，等待回执期间禁用
  approve/deny/scope、隐藏快捷键提示；回执拒绝后恢复可提交状态。普通/MCP
  继续各自原有行为。这一已核 Aml 过渡、原 SVG 和 prefix 展示规则均已进入
  当前统一 BT3XeGQn 构建。

真实未实现边界：MiniCode 的 legacy `viewMode=normal|verbose|summary` 不是
Source 的 `conversationDetailMode`。没有证据可以强行映射，因此没有据此模拟
`STEPS_PROSE` 的 Hand/reason-only 分支；该设置和分支仍属于独立待实现能力。
Source shared `wq/cjn` 对真实 `unsupported-auth` 本身强制 coding/COMMANDS；
此事实不能反过来声称已实现完整 account/preference 模式。

## 已完成的独立验证

当前 backend 去重 124 节点通过、0 failed/error/skipped。原始批次 118 节点中
36 个失败及 82 个通过全部保留；修复真实 RunCommandTool 导入、fixture 必填项
及旧字符串分类断言后，仅重跑受影响 73 节点，未修改节点保留原结果。
审批 UI 字段后重跑 native 消费链 21 节点全部通过。

其中实际 native parser / PowerShell / 临时 profile 覆盖：

- 执行参数与执行 digest；已准入模型过滤及同一提示词；真实 managed rule 生产。
- 串行工具 → 实际 approval payload builder → pending replay → 原 resolver/handler →
  原生持久化 → 实际 PowerShell → 下一次 Skip → 冷 owner 复用。
- 伪造 prefix、修改执行参数、写失败、真实 Abort、denied-read sandbox split。
- actual production `run_agent_loop` bootstrap、capture、真实命令和 final。

日志：`output/native-execpolicy-consumer-20261015-backend-batch01.xml`、
`...backend-batch02.xml`、`...backend-batch03.xml`。

审批投影受影响四文件当前 98 节点通过：canonical 22、InlineAgentPrompt 51、
controlEvents 18、ContextMenu 7。原先未修改 approvalPolicy 的 2 节点保留，
消费链 frontend 当前合计 100。当前统一 TSC/Vite 已 exit 0，
`assets/index-BT3XeGQn.js`；UI debt 和 bundle budget 均 exit 0，日志为审计目录的
`source-clock-interleave-reading-final-*-batch03.log`。原始 TSC 的可选 prefix label
错误保留在 `...final-build-batch01-retained.log`；此处不沿用修改前的 tsc 结果。
新长预览 fixture 的首轮 computed-style mock 缺少真实 DOM
方法，1 个失败保留在 frontend batch03；修正 fixture 后 batch04 的 19 节点通过。
真实 available-decision 类型收窄后 batch05 的 19 节点通过；最后同批核对原 SVG、
提交过渡和 Source prefix 规则后，batch06 的 22 节点通过。

Root 独立桌面 `production-native-execpolicy-ui-run01/verdict.json` 已有三场景通过：
实际 prefix 菜单 → WS 透明数组/digest → PowerShell → 下一调用 Skip → 同 profile
冷复用，并保留原草稿。四张截图、wire、provider 与进程日志保留。它验证的是
UI 改动前的真实功能链；原截图发现的额外 UI 已修，不能算新 UI 的截图验收。

## 包交付与后续冻结

正式原生资源已放入 `backend/permissions/resources/execpolicy/x86_64-pc-windows-msvc`，
wheel package-data 和 Electron backend extraResources 均包含它。包只声明实际验证的
`py3-none-win_amd64`，没有宣称其它平台原生资产可用。exe SHA256 为
`82448962cf8c535b126defa5c20791a9ae0ca92af0dffe492096b47754c21ecc`。

core wheel 原产物
`output/native-execpolicy-consumer-wheel-final-20261015/minicode-0.2.0-py3-none-win_amd64.whl`
已在独立 target 安装，Python `-I`、checkout 外 cwd、PATH 仅 System32 下完成
12 项真实检查：已安装模块和原生资源路径、完整哈希、Source identity/提示词文件、
实际最终准入/透明审批/PowerShell/落盘/提示词/冷复用及无 system warning。
结果 `output/native-execpolicy-consumer-20261015-installed-cold.json`；批次01误把
runtime.span 当作 warning 的失败保留，批次02通过。

上述 core wheel 早于 native 新 UI DTO 和并行 clock/owner-close 修正，仅保留历史
证据。各 owner 最终冻结后已统一交付新包：

`output/native-execpolicy-consumer-wheel-delivery-20261015/minicode-0.2.0-py3-none-win_amd64.whl`
为 7,499,736 bytes，SHA256
`a9151f5fd822d03c88bc36e666ee0e4497f32291714dabe703bab9cec9b9fe89`，
实际 build/install 均 exit 0。最终冻结输入 1,143 文件，manifest SHA256
`1409e912a5b46c8175ce18dedb488e5ebe125ba51932252138ce369bbb678e5d`；
构建前后及冷验后 changed=[]。

该新包在 fresh target 完成独立 `-I` / checkout 外 cwd / PATH 仅 System32 的
14 项真实冷验，结果 `output/native-execpolicy-consumer-20261015-installed-delivery-cold.json`。
wheel 的全部 1,119 个 backend 成员逐一符合安装文件和冻结 Source 哈希，包含最终
runtime/timing、manager 和 session_lifecycle；实际公开 display/reason 的
build → reemit → resolve、PowerShell、落盘、提示词与冷 owner 复用通过。
没有重跑原 native 180 节点或 release 9 场景来充数。

最终交付元数据为 `output/native-execpolicy-consumer-20261015-delivery.json`。

Root 独立当前 BT3 `production-native-execpolicy-visual-companion03/verdict.json`
已 1/1 通过、errors=[]、exit 0，四张图逐图查看：真实 Source terminal/Info、
reason 标题、四行公开命令默认三行 → 实际展开/收起、普通动作菜单无 check，
真实点击后 PowerShell 四行输出、原 `VISUAL_DRAFT` 恢复；原规则 bytes 前缀保留后
追加 Write-Host，canonical completed/done duration 数值一致。

仍用原 `XNe46y` profile，没有清空历史、草稿或规则。companion01 因原自有空
临时工作目录已消失失败；仅重建该空目录和本次 marker。companion02 因真实
missing-workspace 清理已移除 active_workspace 指针失败，fixture 改从原 run01
ledger 取同一个原目录，两个失败均保留。03 的新视觉场景与 CC81 的既过功能三场景
分别记录，不混为四个新功能通过。整款产品其它区域的对齐不在本页结论内。
