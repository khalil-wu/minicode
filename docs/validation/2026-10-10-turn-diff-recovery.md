# Turn Diff 归属与旧会话恢复修复 · 2026-10-10

本轮修复由真实旧会话恢复失败触发：浏览器配置目录被全工作区快照加入本轮 Diff，4 MiB 的历史 Diff 加上消息其他字段超过恢复预算，`session.restored` / `conversation.switched` 被静默丢弃，前端一直 connecting。

## 修复

- 删除 QueryEngine 开始与 QueryTerminal 结束的全工作区快照及其覆盖逻辑。`write_file`、`edit_file`、`apply_patch` 已提交的编辑仍由现有 tracker 生成净 Diff；浏览器、shell、外部修改不混入或清除已提交编辑。原有文件读取、目录及搜索缓存失效职责保留。对照 Codex `core/src/turn_diff_tracker.rs:47` 与 `tools/events.rs:621`。
- 存储保留完整的可公开 Diff；恢复页面对大 Diff 只传文件摘要，点击查看才通过现有会话索引读取原文。复用索引和 HTTP 会话身份，不引入独立产物缓存或扩大消息预算。[投影](../../backend/conversations/public_projection.py#L559)、[索引读取](../../backend/conversations/repository.py#L331)、[HTTP 入口](../../backend/api/routes_chat.py#L153)。
- 旧 `workspace_snapshot` 显示为“工作区比较”，不能冒充助手代码修改；旧版本已经截断的记录明确显示“不完整历史Diff”并禁止撤销。读取失败可见，切会话、删除、版本更新及迟到响应不安装旧结果。[按需加载](../../frontend/src.v2/chat/loadMessageTurnDiff.ts#L16)。
- 关键恢复投影错误传到现有命令错误边界，前端明确 failed；草稿、附件、排队消息和未完成任务保留，显式重连真正成功后才释放队列。[发送边界](../../backend/ws/event_outbox.py#L258)、[恢复失败](../../frontend/src.v2/hooks/useWebSocket.ts#L1163)。

未修改用户的页面、浏览器 profile 或历史 Diff 正文；保留 `design.md` 的已有修改。实际窗口已恢复连接，并保持打开。

## 验证

分批去重后的当前结果：后端 **237 项通过，16 模块**；前端 **400 项通过，19 文件**；0 失败/错误/跳过。去重按用例身份取最新结果，同名参数用例保留其序号，不累加重复批次。收据位于本地 `output/full-chain-fixes-20261009/diff-recovery-validation-status.json`，不入库。

覆盖了默认 QueryEngine 的真实编辑工具、浏览器成功/失败、2.8 MB 配置 JSON、子进程备份、并发外部编辑和后续编辑组合；无编辑任务不虚构 Diff。历史 5 MiB patch 完整存储/读取、旧 4 MiB 截断记录、索引读取不加载私有 checkpoint、错误消息/轮次/版本、删除后的读取、按需加载与迟到响应均有必要回归。

真实 unpacked Windows 构建使用原 profile，已观察 `isConnected=true`、`connectionPhase=connected`；用户指定旧会话恢复 4 条消息、13 个文件摘要，正文仍 deferred。认证 HTTP 请求返回 200，保留 **4,194,304 字符**和原版本，实测读取 **765.2 ms**。该旧记录已被旧版本截断，不能宣称丢失尾部已恢复。冷读取公开页面及校验实测 **1,176.74 ms**，页面 **1,019,008 bytes**，Diff 原文不再随恢复发送；此耗时不是整个桌面启动时间。收据为 `diff-recovery-real-instance.json`、`diff-recovery-real-http.json`。

`compileall`、kernel boundaries、protocol sync、最终 `tsc -b` / desktop-release build、`git diff --check` 均通过。最终发行 staging 保持独立，活跃 `frontend/dist` 未清理；当前窗口的读取选择不因替换资源而强制重载。

## 失败记录与边界

- 后端首批 155 通过 / 2 失败：旧测试期待非精确 mutation 清除已有编辑 Diff；改为核对已提交事实和真实失败、缓存失效后复验 73 通过。原失败收据保留。
- 前端首批 345 通过 / 28 失败：一个真实缺口为仅含 deferred Diff 的助手消息被过滤，已修复；其余是测试清理误删 fake IndexedDB 的 setImmediate 回调。随后 181 通过 / 2 失败，定位为新 fixture 漏传结果等待器必需参数；补齐后 lifecycle 42 通过。最后版本交错与归属批次 37 通过。所有失败保留，不算通过。
- 上一提交 `df33456b` 的 [GitHub CI](https://github.com/khalil-wu/minicode/actions/runs/38013920939) 有测试失败：补齐直接 handler fixtures 的真实锁与空 command claim；WebSocket smoke 按命令 ID 消费初始/完成两个切会话事件；QuickOpen 正确收尾 IndexedDB；归档断言核对用户数据而非数组地址。相应后端 80、前端 68 项本地复验通过，没有修改生产逻辑来迁就这些 fixtures。新提交远端 CI 结果另行记录。
- 两次只读调试端口探针使用了旧 `DevToolsActivePort`，分别失败于 HTTP 400 和 ECONNREFUSED；没有据此判产品故障或算作通过。最终验证使用真实新实例和认证 HTTP。
- 本轮没有重跑该页面的浏览器点击测试或模型生成，也未删除用户项目中的旧配置资料。模型供应商此前的 HTTP 500、实际账单及长期运行不在本次修复结论内。
