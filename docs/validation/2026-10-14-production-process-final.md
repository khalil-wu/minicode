# 2026-10-14 最新生产构建：主对话与子 agent 过程投影

本批最终结果为 **119 项相关前端检查通过，TypeScript/Vite 构建通过，生产 Electron 完整任务通过**。最终任务为 `output/playwright/codex-control-audit-20261013/production-projection-final-run05/`：1 个长场景、8 项结果全部通过、14 张截图已逐张查看。它验证真实后端、JavaScript VM、内层工具、两个子 agent、WebSocket、持久化与实际冷启动；模型 HTTP 服务受控，不向前端注入工具、消息或子任务记录。

## 分组文案的实际依据与改动

重新查看 `alignment-audit-20261011/site/images/projection-codex-expanded.png` 与 `projection-codex-edits.png`，Codex 中文原图分别显示“已读取文件运行了命令”和“编辑了文件运行了命令”。因此最初仅凭可读性提出的“中文缺分隔符”判断已撤回，不能据此加自创连接词。

保存的 `codex-rebuild-20261012/process-parity/source/sites-end-resource-11d1a7be5e09.readable.js::VO` 将 `summaryParts.map(...)` 交给 `<Er type="unit" value={parts}>`；`Er` 的实际导入为 `app-shared::Zdn`，导出回溯到 `VHe = uS("formatList")`。这是 React Intl 的单位列表，中文没有分隔符，英文使用逗号。`::mv/hv` 用真实文件与命令 count 选择 singular/plural，`inline-followup-markdown::ke/Ve` 根据真实路径集合生成文件统计。

保存的 `sidebar-width-source/zh-CN-b05cfd55c534.js` 明确给出：

| Source 消息 | 原始中文 |
| --- | --- |
| `summary.readFiles.leading` | 已读取文件 |
| `summary.readFiles` | 读取文件 |
| `summary.editedFiles.leading` | one：编辑了一个文件；other：编辑了文件 |
| `summary.editedFiles` | one：编辑了一个文件；other：编辑了多个文件 |
| `summary.commands` / `.leading` | one / other 均为：运行了命令 |

本批只修对应分组标题：使用 `Intl.ListFormat(locale, {type: "unit"})`，修正后续读取文案；根据真实 `diff.files` / 已有输入目标去重选择单文件表达；英文命令根据真实调用 ID 区分一条和多条。主任务实际一文件修改的标题现在为“编辑了一个文件读取文件运行了命令”，生产 consumer 精确断言通过。没有改变字体、字号、分组边界、事件语义、扫描动画或披露状态。

## 最终真实任务结果

| 核对项 | 实际结果 |
| --- | --- |
| 读取、目录、读取命令、编辑 | 实际文件与命令执行；`Get-Content` 的真实 `command_actions.type=read` 投影成读取；磁盘首行真实修改 |
| 三条普通命令间距 | 三个摘要头均高 30 CSS px；相邻间距分别为 2 / 2 CSS px，包括 stderr 与退出码 7 |
| 组合折叠与高光 | 主与子运行组默认组合折叠；开组后组头仍有实际活动扫描，内部命令正文静态；真实执行结束后停止扫描 |
| VM 与原始证据 | 受 token 限制的模型观察仍收到 `computed 42`；原始完整回执存为真实 artifact 并留在历史；常规过程不重复显示 VM 包装或计算输出 |
| 独立 VM 错误 | `PROJECTION_SCRIPT_FAILURE` 的真实堆栈可展开阅读；不靠隐藏错误取得通过 |
| 普通命令非零退出 | 原命令、stderr 和退出码 7 保留；主/子摘要均无“失败”标记或红色；冷启动后的子详情也真实可见 |
| 子 agent | TaskTool 真正启动两个 `default` agent；真实名字/彩色头像出现；子工作记录独立于主用户气泡；运行中可收起，完成保留其手动选择 |
| 完成与阅读选择 | 主用户已展开的过程在最终回答后保持；另一个未经展开的真实主回合在完整最终回答后默认收起；子 B 完成后首次打开默认收起，主动展开后跨返回保留 |
| 实际修改 Diff | 等待真实新增文本、第 1 行号、`+` 标记及增删底色渲染完成后截图，磁盘修改为 `+1/-1` |
| 冷启动 | 真正关闭 Electron/backend 后重新启动；完成子任务默认折叠，原读取和 stderr 可展开；主第二回合仍是完成状态，侧栏没有运行旋转，输入区恢复发送按钮 |

`code-projection-evidence.json` 保留实际 provider 请求、WS 事件、原始 artifact、几何和 8 项 verdict。`playwright-result.json` 记录 1 passed / 0 failed / 0 skipped / 0 flaky，运行约 78 秒；两次主回合 `done.status=completed`，fixture failures 与全局 error 均为 0。不能将多个重跑的相同场景相加。

最终运行加载的生产 `index.html` 修改时间为 `2026-10-14T11:07:45.946Z`，SHA256 为 `0546f35d4a8b3b5acdc654a1716b8bce4ddf920198a3537c895beb28159967dd`，对应 `index-DAAdef6e.js`。构建日志为 `process-group-caption-build01.log`。

## 截图核对

最终 14 张 PNG 已全部查看。`00a/00b/00c` 是真实主/子执行中的状态；`01` 保留独立脚本错误，`01b` 将主命令 stderr 滚入视口；`02/03` 是子任务手动收起及完成保留；`03b` 是已渲染 Diff；`04/05` 是完成子任务默认折叠及主动阅读；`05b` 等到实际第二次 done 和 `isStreaming=false`，最终答案、折叠、侧栏无旋转及发送按钮同时可见；`06/07` 为实际冷恢复后的读取、stderr 和退出码。

生产真实任务的截图与 WS 相互核对；没有将静态 PNG 当成完整动画性能验收，也没有宣称所有桌面按钮都已等同。

## 保留的失败与修正

- `production-projection-final-run01`：真实产品异常 `'StreamEvent' object has no attribute 'tool_call'`，在内层操作前终止。Root 已修实际 `tool_calls` 契约，未加兼容字段。本轮明确使用冷启动；原失败 PNG、WS 与临时 profile 的 traceback 保留，不能算通过。
- `run02`：内层操作与子任务已经真正运行，QA 却按旧假设读取 `tool_result.name`。实际结果只携带调用 ID；改为用真实 `tool_call.id/name` 关联，实际原始 artifact 未丢失。该轮是 fixture 失败。
- `run03`：完整断言通过，但 `05b` 截图早于第二回合外层 done，仍显示真实停止按钮；它不作为 settled 最终状态依据。
- `run04`：补实际 done 等待及报错详情滚入视口后通过；该轮仍是文案修复前的构建，不冒称包含后续产品改动。
- `process-group-caption-unit01.json`：118 / 119；旧压缩 consumer fixture 只渲染 `cell.id`，却期待已删除的泛化 region。改为渲染真实 `StatusNoticeCell`，断言真实失败标题与原报错，并保留不冒称成功的检查。`unit02.json` 对应同一范围最终 **119 / 119，0 failed / 0 skipped**。这些范围与前批投影检查重叠，不累加。

所有桌面运行只使用独立临时 QA profile/workspace 和 offscreen 窗口；没有重启普通用户窗口、替换其模型设置、清除草稿、保存编辑器文件或操作系统剪贴板。本批不覆盖公网 Provider 质量、所有 MCP/Skill/审批场景，也不代表整个 Codex app-server 或桌面全部服务已经等同；这些需要各自真实调用链验收。
