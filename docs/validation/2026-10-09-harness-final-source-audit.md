# Harness 源码对照与最终验收（2026-10-09）

行为基准为 Codex revision [`822e58cc3d666166c7446c5b1ea2e52f5d09594c`](https://github.com/openai/codex/tree/822e58cc3d666166c7446c5b1ea2e52f5d09594c)。视觉以本机 Codex 图册与实际操作记录为依据；公开 checkout 不包含桌面端全部 React 页面。

## 完整链路修复

- 会话恢复与同步统一读取 80 条历史页，保留真实总数与游标；先显示消息，再加载完整执行上下文。同步期间 owner 变化时重取当前 owner，不拼接两个会话的快照。
- 列表用一次目录 inventory 获取摘要与修改时间；读取移出 WebSocket loop。删除已有 manifest 的会话只读 metadata revision，先发布 tombstone，再清理不可达历史文件。
- 回复增量在 query 所属 loop 归并，首收据与 120ms 收据再持久化；真实工具使用记录仍在执行前落盘。断流保留已执行工具和具体失败，恢复从结果继续。
- final 结算检查已经到达的 mailbox/子任务输入；压缩恢复仅将明确的 no-op 视为正常路径，真实错误交既有失败 owner。独立 read 不再受未配置的默认 10 个并发限制。
- 对照 Codex 修正默认流重试为 5 次、200ms 倍增及 ±10% jitter；连接阶段独立为 5s→60s。服务端 Retry-After 只应用一次，容量错误不再用 foreground/连续三次 529 推断终止。WS 耗尽预算后再切 HTTPS，保留唯一的实际 attempt 记录。
- Skill 显式链接按真实路径选择；空目录不反复扫描。Deferred 名称目录不构造 BM25，实际搜索才建索引；详细 schema、boolean/const/allOf/nullable 声明及完整权限 cache 指纹保持同一来源。
- MCP 保留完整说明与 initialize instructions，稳定处理重名/超长名称。原始、归一化与实际历史名称贯穿规则、toolset、schema、执行及 hook；持久规则保存稳定原始身份。
- MCP manager、配置、工具与资源桥按实际 workspace scope 同步；无项目会话使用 global/plugin scope，显式 None 不继承旧项目。
- 会话切换与虚拟行卸载保留用户展开、历史窗口及滚动选择。多 agent 关系一次索引，每行只订阅自身真实成员；长回复不重复投影已完成过程。
- 工具历史按快照游标合并实时尾部；同步旧页保留新回复。未知动作保留真实短行。实际子任务工作记录不使用主用户气泡或聊天框。
- 已批准的工作区不重复重写 Windows trust ledger，新增批准在持久化成功后生效；真实 IPC 失败在 UI 请求边界显示。健康 sidecar 重启在仅有错误窗时恢复主窗口。
- 完整上下文 ready 后自动请求现有真实用量接口；不借旧会话上限。上下文恢复完成与统计响应到达分别处理。

11 个模型基础 prompt 与对应 Codex 模板逐字一致，unknown-model fallback 同样一致，保持原文。

## 同机性能

会话样本为 1,600 条消息、约 5.7 MB provider snapshot 与 61 个会话。冷恢复数值指第一份可见历史页，完整上下文随后仍真实加载。

| 项目 | 修复前中位数 | 修复后中位数 |
| --- | ---: | ---: |
| 冷恢复历史页 | 692.571ms | 32.014ms |
| 热恢复历史页 | 121.364ms | 23.430ms |
| 热会话列表 | 41.741ms | 4.332ms |
| 5,000 个 4 字符增量的 journal 热路径 | 861.028ms | 16.734ms |
| 1,000 个 deferred tools 的名称目录 | 1199.468ms | 23.497ms |

后两项为局部微基准，不能等同于模型网络或整个 Codex 桌面的端到端回复速度。

## 验证与真实任务

- 唯一完整本地后端回归初次结果：6,450 通过、3 失败、102 跳过。两个旧 MCP 截断/说明断言和一个 Journal 测试替身缺 async 契约，修正后对应最终模块通过；中间失败证据保留。
- 最终受影响模块分批验证：provider/kernel/recovery/Skill 290、MCP/bootstrap/WS/interaction 167、权限规则 10、hook 身份/准备 52、会话历史/同步 5 项，均 0 失败、0 跳过。各批有重叠，不相加作为独立总数。
- CI 提交预审发现另一个旧 runtime protocol bootstrap 替身，补齐 projectless scope 契约后，与 MCP hot reload、provider composition、hooks、scope 生命周期统一 46 项通过。
- 前端必要链路首次 354 个唯一用例均取得通过结果；追加信任链 78、用量/恢复相关 215 项通过，批次存在重叠。TypeScript、生产构建、bundle budget 与 UI debt budget 通过。
- 桌面完整单元测试 112 项通过、0 失败、0 跳过。Python 编译、kernel 边界、117 个 server events/122 个 client commands 的协议同步及 64 个静态工具无重复检查通过。
- 真实 Responses 模型任务使用已配置 gpt-6.1-sol，在独立 profile/workspace 中启动两个只读子 agent，主任务执行真实 Python。各方确认有效订单 4 笔、767.00 CNY，写入并重新读取 audit_result.json 核对。
- 最终源码的原生 Electron 集成退出码 0、pageErrors=0：实际彩色子任务身份、真实工作记录、无子任务 textarea、最终收起、主动展开与草稿跨会话保留、重启恢复、临时空会话的改名/归档/恢复/删除全部通过。
- 无项目 MCP 实测 workspace=""、serverCount=0，实际空 inventory 正常返回。恢复用量仅自动发出一次请求；真实响应 used=35101、limit=1000000，UI 显示 4%。此次 inspect used 是后端当前估算，未当作旧 provider 实测值。

远端 CI 以该分支对应提交的 [GitHub Actions](https://github.com/khalil-wu/minicode/actions/workflows/ci.yml?query=branch%3Acodex%2Frelease-readiness-20260927) 为准，本地通过不代替远端结果。

## 验证边界与发布

102 个本地跳过项为：49 个符号链接权限、22 个沙箱/ACL 环境、23 个可选语言 grammar、5 个 Windows 打开句柄替换限制、3 个 Linux 特定行为。跳过未计入通过。真实模型任务使用明确的完全访问权限，不代表这些受限沙箱场景通过。

尚无与 Codex 桌面的同机端到端 A/B。Codex 专用远程 executor/Apps 接入和完整外部 schema 资源等能力，不能凭本次本地链路验证宣称实现。

用户选择本次提交源码并完成 CI，保留签名发布要求。Windows CD 仍要求 WINDOWS_CERTIFICATE_BASE64、WINDOWS_CERTIFICATE_PASSWORD 两项 GitHub Secrets，以及 HTTPS 的 MINICODE_UPDATE_FEED_URL 变量；本次不创建发布标签或发布未签名安装包。

详细审计、JUnit、微基准、原生截图与中间失败记录保存在本地 audit/ 与 output/harness-final-20261009/。这些目录受 Git 排除；用户日志、设置、密钥、会话数据、参考 checkout 和构建二进制不随源码提交。用户原有 design.md 修改保持在本地。
