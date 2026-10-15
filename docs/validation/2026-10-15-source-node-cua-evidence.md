# Source Node/CUA REPL 审批证据链

Source 固定为 `.tmp/codex-src` 的 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。这是 `2026-10-15-source-node-repl-evidence-audit.md` 之后独立实施和验证的批次，不重算原生命令策略、旧安装包或旧桌面截图的通过项。

当前状态：本批生产链及必要验证已闭合。新增 33 个场景全部通过；去重合并本批各次必要运行后，349 个场景的最新结果均为通过，0 失败、0 跳过。主 agent 随后已独立完成最终 wheel、安装冷验及 bundled native decoder 验收，见 `2026-10-15-source-internal-evidence-delivery.md`；不把 decoder smoke 加成新的 MCP/server 场景。

## 实施边界

实际 `MCPToolProxy.execute` 在私有 runtime metadata 中产生类型化 `MCPAcceptedResultIdentity`，冻结这次执行的 raw server/tool。结果在原 `store_result` 成功交给真实 CodeMode result sink 或实际历史后，才允许原 conversation 的 `GuardianHostState` 捕获。工具参数、MCP wire JSON、provider metadata、同名工具及 `TrustedTool` 文字不能产生这份身份。

只接收已注册 raw server `node_repl`、`cua_repl` 的成功 CodeMode 嵌套结果；普通 MCP 仍是原 MCP 动作，不增加权限、不变成 TrustedTool。本批没有创建 Node/CUA 执行器，也不包含账号相关 hosted connectors。

`node_repl.py` 参考 Source 的 `core/src/context/node_repl_review_evidence.rs`、`core/src/tools/handlers/mcp.rs` 和 `guardian-context/src/node_repl.rs`：

- thread owner 独立于主模型历史压缩保存有序 immutable snapshot；记录序号、8 MiB live store 和 x64 64/56-byte 结构记账沿用 Source 的淘汰语义，优先丢图片，再丢旧文本。
- provenance 每字段限制 128 UTF-8 bytes；每结果 6000-token 文本预算为后续文本保留空间，转义关闭标签后截断。渲染选择最近完整结果，32,000 UTF-8 bytes 限制与资源省略标记的位置沿用 Source，不向更旧的小结果回填。
- 跳过 encrypted blocks；只有无文本且整份结果没有 encrypted block 时采用真实 structuredContent JSON。图片按实际 base64 解码和 Pillow decoder 读取，保留有效 detail，捕获 reference 预算 8 MiB。
- admitted parent `ModelExecutionSnapshot` 决定 mode；真实 Source catalog provenance 决定 required computer-use review，任意 `.extra` 不具有这份 host 权威。

同步审批将这份 snapshot 放入原 Guardian prompt，作为不可信 user 文本和图片，保留文本/图片/文本顺序。完整 action 和必要 Node 文本保留；总输入预算不足时先移除 Source 允许移除的工具说明、可选历史及图片，并留下实际省略说明。

实际 HTTP 发现原 Responses user image 编码器把显式 `original` 覆盖为 `auto`。已将该处改为消费 `img.detail`，没有显式 detail 时仍为原 `auto`。Guardian 保留原来的单份 `LLMMessage.images` 表示；不增加第二份 provider item、图片或计数，Chat 和 Anthropic 继续消费同一份图片。实际 Responses/Chat 请求保留 `original`，Anthropic 请求携带原 base64 图片；三条 HTTP 请求都仅有一张图片，文本/图片/文本顺序不变。

`GuardianContextBuilder` 的 reviewed sequence 在原 `TurnIterationRuntime` 成功历史准入事务的 `commit_turn_admission` 推进；核对真实 child turn 与新准入的 transcript marker。准备 prompt、短暂调用 `start_turn`、网络响应或首 token 都不会提前推进这份游标。准入失败由原事务恢复历史。

异步 `Snapshot`、retained `Conversation` 和 `Decisions` 继续使用原 service/owner。`images.py` 对原 canonical history、native compaction windows 和 Node store 作一次共享选择，保留最近 4 张及 8 MiB reference bytes；Node 图片在历史图片后加入，而且仅在 `tool_outputs` source 启用时加入。实际工具开始时冻结图片快照，retained request 去重已交付图片，Luna classifier 去掉 detail；store 和同步审批仍保留 detail。

## 打包依赖

`pyproject.toml` 增加 `Pillow>=11,<13`。桌面正式 lock 的 `Pillow==12.2.0`、bundled Python 3.11.9 的 native decoder 安装和最终 wheel 由主 agent 单独验收；本批不重建 runtime、不改 runtime stamp，也不把开发 Python 的通过当 bundled runtime 的通过。

## 验证记录

运行使用开发 Python 3.12，仅在临时目录、隔离 runtime、真实本地 HTTP sockets 和自有 stdio 子进程中验证。没有使用用户模型账号或修改用户 profile。

| 运行证据 | 通过 | 失败 | 跳过 | 含义 |
| --- | ---: | ---: | ---: | --- |
| `output/source-node-repl-evidence-batch-initial-20261015.xml` | 320 | 7 | 0 | 首次完整必要 Guardian/MCP/CodeMode 批，包含主 agent 要求的 3 个 heartbeat admission 场景 |
| `output/source-node-repl-evidence-correction-20261015.xml` | 34 | 1 | 0 | 修正夹具；保留真实 4 次不同动作审批与原错误 2 次期望的失败证据 |
| `output/source-node-repl-http-admission-final-20261015.xml` | 1 | 0 | 0 | 4 个真实动作、顺序与实际 admission cursor；发生在最终 detail 消费修正前 |
| `output/source-node-repl-evidence-final-20261015.xml` | 85 | 2 | 0 | 最终生产字节：新增 33 场景全过，含 stdio/实际 HTTP 与三 API 图片消费；另两处旧媒体夹具的固定下标失败 |
| `output/source-node-repl-media-fixture-correction-20261015.xml` | 2 | 0 | 0 | 两处旧媒体测试按真实 user role 选消息，检查原媒体保留和省略行为通过 |

以上运行不相加作独立通过数。`output/source-node-repl-evidence-delivery-20261015.json` 按场景 ID 合并最新结果，记录 349 个独立场景、每项证据来源、测试重命名映射，以及本批 19 个生产/测试文件的完整 SHA-256。历史失败全部保留，均被后续相关场景覆盖通过。

初始 7 处失败分别为：2 个新测试漏传 `AgentState.user_message`；1 个新测试错误预期只有命令审批；4 个旧场景只在 `LLMSettings` 声明 search/original 能力，未给 helper 提供已准入的 `ModelDefinition`。旧 helper 增加显式 `model_info` 参数，相关场景传入 fixture 模型事实，没有放宽生产能力 gate。最终额外两处旧媒体测试原本假定消息固定在下标 1；当前真实 developer 协作模式消息在前，故改为按 user role 查找。没有为测试改变生产提示词或删消息。

真实 stdio peer 只提供 `observe` 方法，返回有效 PNG、可见文本和 encrypted block；没有提供解释器。实际 CodeMode 脚本调用它两次，并真实执行两条 PowerShell `Write-Output` 命令。默认策略对四个不同动作依次审批：`node_repl.observe`、第一条命令、`node_repl.observe`、第二条命令。四个 target/review ID 均不同，每项只有一对 `in_progress → approved`。首 MCP 审批前没有尚未返回的 Node 证据；后续 reviewer 原会话仅在实际用户历史准入后推进序号，新 delta 不重送旧 response，未发送 encrypted block。原主模型历史只含脚本选择的输出。

`output/source-node-repl-http-final-20261015.json` 保存最终完整 HTTP fixture 请求、原审批事件，以及 Responses/Chat/Anthropic 图片请求；`output/source-node-repl-http-diagnostic-20261015.json` 独立保留早期 4 动作诊断及被覆盖为 `auto` 的实际 wire 证据。

同步 admission 另实际触发历史准入失败，验证原事务回滚历史且 sequence 保持 0，再次真实准入后变为 1。异步 Snapshot、retained Conversation 和 Decisions 均经过实际本地 HTTP；retained 请求中旧图片只保留一次，Luna 路径按 Source 去除 detail，Node sync transcript 不进入 async classifier。

## 剩余范围

本批没有未通过的必要项。Node/CUA 实际执行仍依赖用户真实注册的相应 MCP server；本批不是内置解释器，也没有伪造 hosted connector 或 TrustedTool 权威。桌面正式 wheel、原 profile 实机回归及 runtime 整体锁定仍是主 agent 的独立交付步骤，不能用这些源码测试代替。
