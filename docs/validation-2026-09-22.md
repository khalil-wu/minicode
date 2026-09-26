# 审批、小窗口与桌面验证（2026-09-22）

本轮使用 Windows 本机、`glm-5.3-flash`、现有 supertoken 网关。
凭据只通过环境传入，未写入新文档或测试 fixture。

## 已实现

- 审批抓包回放：保留旧会话 fixture，新增四连接审批 fixture；直接操作
  `InlineAgentPrompt`，验证挂起断连、重连重发不重复、允许、拒绝及说明、卡片清除、
  实际响应与抓包一致、工具状态与回合终态。两组均随前端 CI 执行。
- 自动输出预留：未设置 `response_reserve` 时使用窗口的 10%，上限 16384。
  36K 默认输入边界从 19616 提升至 32400。显式配置（包括 0）保留；自动模式
  在主会话、子 agent、SDK/eval 和模型双向切换中保持一致。
- Electron 启动：预加载阶段 URL 尚未提交，不能用非空 URL 作为 IPC 身份条件。
  按已拥有的 WebContents 与主框架验证调用者；跨源导航仍由窗口管理器约束。
  回归验证空 URL 的合法预加载、其他窗口、子框架和已销毁窗口。
- Windows 沙箱：辅助进程使用当前解释器的 `-I` 与脚本绝对路径启动，不再依赖
  用户工作目录能导入 `backend`，也不会导入工作区或 PYTHONPATH 中的同名模块。
- diff：`turn.diff.updated.diff=null` 表示当前汇总不可确认，空字符串仍表示
  精确的零修改。命令执行后保留文件工具的历史编辑证据，显示「编辑记录」，
  不提供基于旧 patch 的直接撤销。精确撤销到零仍会清除卡片。
- 更新桌面真机测试的菜单入口、子任务记录展开、回复区 diff 位置与中断恢复
  等待条件；截图使用 Electron 的原生捕获 API。
- 中断恢复后的新回合：恢复快照可能先清除本地占位消息的 streaming 标志，旧
  runtime 事件入口据此把真正的新 `agent.run.started`、正文和完成事件都当成迟到
  事件。现在由同会话、同消息的 run-start 激活尚未结束且 turn id 匹配的记录；
  已结束记录仍不能被旧 start 复活。
- 即使中断时只有瞬态推理、刷新后没有正文或工具记录，界面仍显示「已停止」。
- 子 agent 邮箱通知：原事件桥只转发 tool_call/agent.progress，漏掉子 agent
  已提交的 subagent.event。现通过现有 incarnation fence 转发协调事件；子任务
  结束后的旧回调仍被拒绝。

## 36K 真机结果

修复后使用新建的 inventory 工作区，全部源文件修好后统一跑业务测试：

| 指标 | 结果 |
|---|---|
| 终态 | completed |
| 模型迭代 | 11 |
| 工具调用 | 24，全部 success |
| 自动压缩 | 0 |
| 业务测试 | 16 passed |
| 测试源文件 | 与独立生成的基线逐文件一致 |
| 修改 | pricing、store、validate、csvio 各一处 |
| CHANGELOG | 指定首行与四条函数修复说明齐全 |

证据：`.tmp/longrun/adaptive36k-final.trace.jsonl`、对应 `.log` 和
`ws-adaptive36k-final/`。首轮验证受到此前沙箱启动故障干扰，已停止并保留
`adaptive36k.*`，未计为成功。第二轮是在沙箱修复后重新创建的工作区运行。

这证明该 36K 编码场景不再因默认预留过大而反复压缩；不代表所有小窗口都能容纳
系统提示和工具，也不代表本轮再次验证了压缩期间的全部约束保持。
36K 是本轮显式设置的本地 TokenBudget，用来复现调度问题；没有把它当作
glm 服务实际最大上下文窗口的认证。

## 自动回归

- 预算与上下文相关：82 项通过。
- 前端初次全量：179 个文件、2071 项通过；后续 diff 改动的 4 个相关文件、71 项通过。
  最终全量 2073 通过、1 项 SettingsCenter 等待超时；该文件单独复测 62 项通过。
- 桌面单测：56 项通过。
- root 全量：1532 通过、36 跳过、15 失败；15 项均为沙箱辅助进程无法导入
  backend。修复后相关三个文件共 132 项通过，包含新增模块归属回归。
- backend 全量：2991 通过、17 跳过、47 失败。当前虚拟环境缺少已在项目声明的
  tree-sitter、tree-sitter-bash、tree-sitter-powershell，已补装；另有沙箱启动
  和重连测试的未固定时序。四个受影响文件复测：127 通过、1 跳过。
- 后续 diff 的 Python 回归：65 项通过。
- 工具执行相关回归：26 项通过。
- 恢复/流事件/审批回放相关：177 项通过，包含恢复快照之后的新 run-start 与旧
  start 不能复活已完成消息的回归。
- TaskTool/邮箱相关：72 项通过；停止状态显示与回合投影：34 项通过。
- TypeScript 编译、协议同步、agent kernel 边界检查通过。

以上区分首次全量与修复后的定向复测，没有把两者写成一次全量全绿。

## Responses 的真实限制

本轮对现有网关再次发起真实请求：

- `/responses`：HTTP 500，`not implemented` / `convert_request_failed`。
- `/responses/compact`：HTTP 503，`model_not_found`，没有对应 compact channel。

因此 Responses 及 native compaction 的当前网关真机验证未通过。没有替换供应商、
改用模拟端点或把其他 wire 的成功算作 Responses 成功。原有 native compaction
协议测试属于自动测试覆盖，不能替代供应商端到端证据。

## Electron 真机

使用 `frontend/tests/e2e/electron-real-provider.spec.ts`，每条用例建立独立
state root 与工作区，并启动真实 Electron、后端 sidecar 和模型。

- 多文件编码通过：修复源文件、补空列表用例、命令最终成功；回复区保留编辑记录。
- 审批拒绝通过：被拒绝的 `write_file` 没有创建目标文件。
- 完整桌面重启后，会话恢复通过。
- 中断、刷新页面、继续发送通过：前一条助手记录保持 interrupted，后一条
  为 completed，正文恰好为 `CANCEL_RECOVERY_E2E_OK`。
- 三个 general-purpose 子 agent 并行读取与前台邮箱回传通过，桌面保留每个
  子 agent 的唯一报告。随后用 send_message 唤醒同一个已完成子任务，mailbox
  epoch 增加，并通过后台结果交付读到后来创建的文件内容，不能用旧结果过关。
  该完整用例最终 1 passed（3.2 分钟）。

邮箱测试明确使用 `read_only=false` 以允许协调消息，任务仍禁止文件编辑。
已完成子任务的唤醒使用受限后台工具集，不要求它调用该模式未开放的 send_message。
这两个模式分别验证，未为测试放宽产品权限。

本轮顺序复测了失败项，成功项未反复调用模型。过程中发现的旧菜单、折叠组、
截图 API 和终态等待假设都已同步到当前实现。证据位于
`artifacts/real-provider-e2e/coding-task.json`、`coding-task.png`、
`interrupt-recovery.json`、`last-run.json`、`mailbox-resume.json`，分轮结果在
`.tmp/approval-budget-electron*.log`。五类桌面真机场景均已分轮通过。
