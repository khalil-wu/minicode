# 公开 Realtime 的原会话 harness 与关闭链

本批依据公开 Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c`、已提取 Desktop `26.1013.4894.0` 消费代码以及已有官方文档收据。它按一个 Realtime 功能组记录，六个命令和十一个通知不是六/十一个完成率分母。Bedrock 由 root 独立接入，不将其算作 Realtime。

## 实际消费链

`thread/realtime/start`、`appendAudio`、`appendText`、`appendSpeech`、`stop`、`listVoices` 进入既有 WebSocket dispatcher；`threadId` 与 MiniCode conversation owner 必须相同。原执行 host 持有服务、provider 选择、API Key/自定义 header、端点与 proxy 捕获。切换到 B 后，A 的媒体、backing query、持久 speech 与关闭仍属于 A。连接重附着复用同一服务；媒体及控制请求不会作为离线工作重新执行。

公开 Source 的 V1/quicksilver、V2、V3/frameless 保持显式版本。冻结 Source 的 V3 WebSocket 发 `session.update` 并等待 `session.started`；新官方 guide 的不同 wire 不能静默替换它。V1/V3 WebRTC 使用真正的 multipart SDP/session HTTP consumer，再按原 `rtc` call 进入 sideband。existing-call 不初始化或改写现有会话配置。当前版本不支持的 transport 不出现在当前 capability 的可用列表；另列显式版本的支持范围。

config prompt、请求 prompt、显式 `null` 与未提供参数保留 Source 优先级。默认语音表、`realtime.type`、voice/version 限制、initial items、BEM channel/prefix、response item 模式、UTF-8 500-byte append 和输出预算都有真实 consumer。V2 通过 `conversation.item.done` 的 `background_agent` 调用进入原 `user_message` admission；忙回合走既有 steer。它沿用真实 QueryEngine、权限、工具、context 和持久化，没有另一套模型循环。

普通 model `item.completed` 仍可能是待完成的候选。Realtime 只在原 turn `done` 的持久完成回执后发送最终 handoff 并发布 `bemItemPromoted`。不同回合、进程短行及失败回合不会被认成成功最终答案。V2 的 response-create 队列尊重实际 active response 和结束事件；媒体错误通过专用 realtime error/closed 呈现，不把已关闭 sideband 的错误反向变成普通 backing query 的失败。

## 原始事实与尾部记录

speech/timeline 使用原 item ID、`realtimeSessionId` 和真实 user/assistant role；先写入 repository，后发布 `item/completed`。原用户语音被 handoff 接收时，canonical admission 保留原 realtime block。尚未提交给模型的后续 speech 不冒充后来的普通 typed input，也不会误拒先前 backing turn 的最终提交。原音频不写入会话重放窗口。

关闭时遵循 Source `realtime_conversation.rs:1792–1808` 的最终 consumer：`transcript_tail::record` 明确只记录 history，**不启动推理**。尾部是 `realtime_conversation.delegation` 的私有上下文片段，`is_user_input=false`；已有真实 speech 仍在 timeline，内部包装不会变成新的“我”气泡。若 backing query 尚在准备，原 RunContext 将同一稳定片段带入初始化后的 ContextBuilder；冷恢复后只保留一次。Source 起停 developer fragments 按实际 world-state 变化进入 history，带有 contextual provenance 的前置 developer 消息不能被旧的 legacy-prefix 过滤误删。物理媒体 active 与 Source world-state context_active 分开，只有尾部真正写入 history 后才让 realtime_end 生效；独立 gate 验证了这一关闭顺序。

真正 handoff admission 使用既有 execution admission owner，停止语音只等待媒体服务关闭；后台已接收的任务继续运行。会话卸载、归档、清理和进程关闭复用原 thread runtime 的关闭消费者。Source receiver 本身没有被放入永远无法清空的 pending-cleanup 集合。

## 模型结束语音工具

已注册真实的 deferred model-only 工具 `end_realtime_voice_call`，描述逐字沿用 Desktop：「End the current voice chat. Only call this tool if the user explicitly asks to end the voice chat.」它没有参数，不出现在 Code Mode 的嵌套工具表。结束意图通过同一模型指令表达，没有另造一个自然语言关键词判定器。

初始和每个 receiving step 从原 thread runtime 取得实际 active/started、未 closing 的 service；schema、目录、执行使用同一份临时能力。它不进入持久 policy。不同 conversation、store、子 agent、已关闭服务及旧 sampled request 都不能结束另一条通话。只有 voice 为可发现工具时，先注入当前 service 再判断是否保留 `tool_search`；结束后，同一个搜索实例也会按真实 active 状态使缓存失效。

执行调用原 service 的 `aclose()`，等待真实尾部落盘与关闭后返回 ended=true。下一次模型调用不再收到该工具，同时收到原 realtime_end developer fragment。测试通过真正 WS handoff→WebSocketManager→QueryEngine→工具目录/执行→媒体关闭→后续模型步骤→持久最终答案，覆盖直接工具模式与先搜索再调用模式；切换后的 B 会话保持原归属。

增量验证还发现了真实历史问题：end 工具尚在运行时，终端 tail 会落在 assistant call 与随后真实 result 之间，旧整理逻辑因而把 result 当 orphan 丢弃。现在结果写入时只在确实插入了非 tool context 的情况下复用既有排序 consumer，让真实结果与原 call 成对；保留 tail 的内容、时间戳、同步持久化和下一步上下文，普通工具不触发额外的全历史 token-cache 重建。

## 集中验证

当前冻结收据 `output/full-parity-20261021/realtime-final-freeze.xml` 是一次集中运行：**57 passed、0 failed、0 errors、0 skipped**，耗时 21.46s。此前 40 项底座收据保留，不与本次重复计数。

| 范围 | 当前通过用例 |
|---|---:|
| 本批真实 Source Realtime consumer | 13 |
| 结束语音工具、动态目录、真实模型往返及原 owner gate | 11 |
| cell/thread 生命周期 | 4 |
| 原 owner 清理 | 4 |
| canonical checkpoint/history | 19 |
| 历史性能与缓存边界 | 6 |

真实 loopback HTTP/WS 验证了三个 WebSocket wire、V1/V3 multipart WebRTC、existing-call、原 header/session/URL、UTF-8、配置 precedence/能力 gate、原 provider 捕获、BEM/前缀/预算。真实 WebSocketManager、Session、QueryEngine 和 ScriptModel 验证了 A→B、断线→重连、原 backing task、final 持久提交后回传、后台任务进行时继续说话并停止语音、后台继续完成、尾部只记录不推理、cold read 与 teardown。

前面的失败收据保留：一次命令包含不存在的测试文件而未执行；Session fixture 最初未提供自己的 tool registry；SDP body 验证发现 mode-instructions 循环覆盖 startup instructions 的实际 bug；并发 final/tail 验证发现早发候选、错误推理及历史过滤问题。end 增量第一批为 83 pass / 3 fail；后续完整结果断言发现真实 result 被 orphan 整理丢弃，修复后直接/deferred 模型节点 2/2 与原 stop 节点 1/1 分别通过，再集中完成当前 57/57。测试显式关闭 A/B 的 Memory 生成，避免后台 consolidation 与被测模型共用等待门。没有把失败、deselected 或跳过算作通过。当前收据覆盖修复后的生产代码；compileall 和定向 diff --check 通过。

## 验收边界

本批未在本机操作 GUI、麦克风、扬声器或 CDP。loopback 与 ScriptModel 证明本地 wire 和 harness，不能证明实际供应商的 Realtime 模型可用、真实音频质量、系统设备或桌面全部视觉状态。外部 provider/设备记为 `VO-RT-API1`，最终前端构建、冷包与 CI 由 root 在全部生产改动冻结后统一验收。

相关目录：`backend/realtime/`、`backend/ws/handlers/realtime.py`、`frontend/src.v2/composer/RealtimeComposerActions.tsx`。完整全产品边界见 `2026-10-21-full-parity-ledger.md`。

Desktop 的 `capture_screen_context` 与 `transfer_voice_call` 是另有客户端门控和实际消费者的能力，不能用浏览器截图或更换后端 owner 冒充。本次仅审计，不新增按钮或实现；准确 Source 路径与当前余项见 `2026-10-21-voice-session-tools-source-gates.md`。
