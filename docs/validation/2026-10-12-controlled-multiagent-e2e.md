# 多 agent 桌面端受控验收

本用例使用本地确定性 Chat Completions Provider。Electron、MiniCode 后端、WebSocket、QueryEngine、TaskTool、read_file、send_message、持久化日志与恢复均使用项目真实实现。它不向前端 store 填充消息或子 agent，不伪造 WebSocket 服务，也不证明任何外部 Provider 已通过。

用例：`frontend/tests/e2e/electron-controlled-multiagent.spec.ts`。

## 运行前提与命令

等待这批产品修改统一完成 TypeScript 检查与构建后运行。从主目录 `C:\Desktop\MiniCode` 执行当前已集成源码（worktree 的依赖 junction 会被 Vite 文件服务限制）；不要把已有用户 profile 或 workspace 复制到测试环境。profile 位于用户 Temp 新目录；工作区位于 Windows Temp 新目录，包含两个事实文件，并在启动前用真实 git rev-parse 确认不存在上级 Git 仓库。配置只指向 loopback Provider 和测试占位 key。

```powershell
$env:MINICODE_CONTROLLED_E2E_EVIDENCE_ROOT = 'C:\Desktop\MiniCode\output\playwright\codex-rebuild-20261012\controlled-multiagent'
node frontend/scripts/run-electron-e2e.mjs tests/e2e/electron-controlled-multiagent.spec.ts
```

Runner 只启动当前 frontend Vite；Electron 自己启动真实后端 sidecar。不会运行构建。测试沿用已有端口分配设施，后端和 Provider 使用独立 loopback 端口；主应用窗口与原 profile 保留。

## 实际验证顺序

1. 通过生产工作区/创建会话/权限动作创建真实会话，再从 textarea 发送审计请求。Provider 只决定模型响应，不替换工具实现。
2. 主 agent 调用一次真实 TaskTool，并行创建两个只读子任务。Provider 暂停两个子请求，等待真实 `subagent.start` 与 `started_at > 0` 到达；拍摄主聊天启动行、汇总和运行列表。
3. 放行 Provider。两个子任务真实读取各自文件，真实向父代理发送消息，并返回最终结果。父代理读取 TaskTool 返回的实际子 id，再发送后续任务；验证原子身份保留、mailbox epoch 更新、第二轮真实读取、再次向父代理发消息和最终结果。恢复只改变 delivery，保留原 profile 的明确能力；原受限 background 不额外获权。
4. 请求非 Git 工作区中的隔离 worktree，触发真实 preflight 拒绝。验证存在失败 done 与真实原因，公开 started_at 为 0，没有同 id 的 start，Provider 没收到该子 prompt。
5. 请求一个初始化成功的子任务，Provider 真实返回 HTTP 400。验证有真实 start、非零 started_at、随后真实 failed 和错误证据；与未启动拒绝分别记录。
6. 关闭该测试 Electron/后端，再用同一个新建测试 profile 重启。对照会话、子 id、名称、路径、mailbox epoch、模型、真实开始和完成时间；重新打开工作记录，核对后续最终结果。

## 证据与结果口径

证据目录生成 `controlled-evidence.json` 与 8 张 Electron compositor 截图。JSON 包含实际发出的协议命令、实际收到的 runtime/tool/subagent 事件、Provider 请求的摘要和每个完成场景的断言；不记录 HTTP Authorization、环境凭据或完整 Provider 请求正文。失败会使 Playwright 非零退出；没有跑到的场景不会写 `passed:true`。

截图按场景命名：实际启动主聊天与汇总、运行列表、子记录与后续、未启动拒绝、实际 Provider 失败、恢复后的终态列表、恢复后的子记录、真实项目的新聊天选择器。打开运行列表通过生产摘要按钮，恢复后通过工作区菜单；等待右侧 panel 与首行可见，再等待两帧与 250ms，由 Electron compositor 采集。测试窗后台操作并禁用原生鼠标输入，采集时 showInactive 后隐藏；Playwright CDP 仍驱动生产 textarea、按钮和键盘处理。仅测试窗口关闭后台节流，不改变产品动画，不操作用户正常窗口。

最终状态：第 13 轮通过，`controlled-run-13.log` 退出 0。当前证据 JSON 有 5 个 passed 场景，fixtureFailures 为空；8 张 native compositor 截图逐一保留。运行中子任务详情已核对不再显示用户指出的顶部“运行中”；真实计时和错误保持。

| 场景 | 实际证据 |
|---|---|
| 两个真实子任务和续传 | 实际 read_file、向父代理 send_message、同一子 id 的 mailbox epoch 2、新一次实际读取/发送/最终结果；工作记录有两个真实回合，主窗口新 turn 不清历史 |
| 未启动拒绝 | 真非 Git 工作区 isolation=worktree 拒绝，done failed、started_at=0、同 id 无 start、Provider 未收到该子任务 prompt |
| HTTP 400 | 子任务实际 start/started_at>0 后收到本地服务 HTTP 400 并 failed；正常错误文字和展开后的真实 HTTP/code/type 同时可读，原始响应正文不混入短行 |
| 重启恢复 | 同 profile 关闭并重新启动桌面和后端；4 个子 id、真实名称/路径/epoch/model/开始和完成时间一致；已开启 0、完成 2、需要处理 2，续传最终记录仍可打开 |
| 非空项目菜单 | 后端实际登记的 workspace 从生产新聊天动作进入项目选择，勾选和本地范围真实，无人工 seed 最近项目 |

前序失败保留，不算通过：

- 01：恢复 profile 缩减原通信能力，已修原有权限保留。
- 02：后续主 turn 清空前后端会话子任务快照，已修持久化、retry、父任务结束后回调和 epoch 链。
- 03、04：fixture 错把过程的真实折叠/阅读状态固定为单一状态；改成实际按每个回合 disclosure 阅读，没有改产品来满足测试。
- 05：测试窗口被查看操作改变；只有本轮新 JSON 和 failure 截图，目录的 capture-status 标明旧图不能算本轮证据。
- 06：初始工作区标准 Git 探测会继承 HOME 仓库，后来改为真实无 Git 的 Windows Temp。没有创建 HOME 子工作树，相关进程已结束。
- 07–10：资源准备实际卡在 Git 子进程。临时线程/Popen 诊断限于测试 backend，确定其继承桌面生命周期 stdin；资源 Git 和记忆 Git 按 Codex 的 null stdin 调用修复。诊断 helper 未进入产品，最终运行不加载它。
- 11：fixture 要求默认错误携带原 Provider body 标记；改为验证真实 HTTP/code/type 和错误显示。
- 12：上述诊断字段在后端转录中存在，但 frontend 中间 cell 模型漏传 detail。修完整投影和现有展开证据，补对应回归；13 完整通过。

此结果证明本地受控环境中的真实实现衔接，不证明任何外部 Provider、完整 PR 工作流、已安装外部插件或 OAuth 服务已通过。
