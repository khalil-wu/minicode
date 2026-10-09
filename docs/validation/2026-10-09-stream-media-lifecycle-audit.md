# 流式输出、图片归属与会话生命周期补充审计

行为基准仍为 Codex revision [`822e58cc3d666166c7446c5b1ea2e52f5d09594c`](https://github.com/openai/codex/tree/822e58cc3d666166c7446c5b1ea2e52f5d09594c)。视觉基准为用户指定的本机参考图册 `output/playwright/ui-reference-20261004-v2/`；公开源码不包含桌面端全部 React 页面，不据此声称视觉完全一致。

## 真实问题与修复

| 用户可见问题 | 根因与完整链路修复 |
| --- | --- |
| 折叠浏览过程后仍有大图 | `artifact.preview` 没有输出来源和工具身份，工具截图被投影成最终答案图片。补齐 producer、Code Mode 原始字节来源、协议、持久化、公共投影、恢复及前端归属；普通工具图片属于实际调用详情，生成图片及显式 Markdown 图片保留各自呈现。 |
| 多图在工具完成后丢失 | 流式工具快路径替换整个 cell，丢掉当前消息的第二张图片。含图片的更新重新走已有来源投影与过程缓存，实际删除的图片也会移除。 |
| 切换或同步后旧图重新漂回答案 | 首屏、后台 hydrate、REST 历史页与 session.sync 统一恢复已有图片来源。旧数据仅按精确 artifact ID 或 owner 范围内的真实元数据读取，不猜标题、不修改用户历史；分页保留原工具身份。同步的异步元数据读取位于 owner 检查之前。 |
| Evaluate JavaScript 有上下两个滚动框 | 同一 call 的代码和结果使用独立限高容器。现在一个详情表面和滚动区，清楚区分 JavaScript、操作结果及真实错误，保留完整内容。 |
| 子任务长期只显示目录读取 | 子任务实际发生供应商超时，但后台过滤了真实 provider progress；持久化和 hydrate 又漏掉当前工具、等待阶段、迭代与更新时间。接通 callback、Query、journal、SQLite、公共 schema、WebSocket 和前端；工具结束及下一模型请求清除旧工具，真实失败和重试保持可读，正常握手继续隐藏。 |
| 有数据帧时仍被当作供应商静默 | Codex SSE 在收到事件后才解析，MiniCode 的等待边界位于语义输出之后。被忽略的 reasoning、被动进度、未知或允许范围内的畸形帧现在产生无内容的内部 transport activity，重置既有等待；不产生模型文本、工具行或虚假进度。既有取消、绝对截止时间和必需终态保持生效。 |
| 损坏历史的会话删不掉、删除阻塞其他事件 | WebSocket 删除入口仍先读整份历史，实际磁盘删除也在事件循环中。入口只读必要摘要，读盘及原子 tombstone 删除进入已有工作线程；保留运行结束栅栏与资源清理顺序。后台删除结束后的 fallback 使用现有会话生命周期锁，避免覆盖用户新选中的会话。 |
| 冷归档读取和复制完整上下文 | 归档只需元数据却调用完整 record 读取。健康 manifest 直接提交归档元数据，保留历史、context、消息数和 projection pointers；完整缓存失效，返回契约明确为摘要。旧存储沿原有 checkpoint 升级路径。 |

图片归属覆盖普通工具、MCP、多图、Code Mode 选择输出和 `generatedImage()`；来源不能由标题或工具包装层伪造。本批图片来源恢复及切换 payload 的磁盘读取移出事件循环，runtime snapshot 在调用线程时先取得。

## 子任务现场证据

2026-10-09 的真实鹈鹕页面任务中，子任务 19:16:50 启动，19:16:56 完成目录读取，19:22:03 执行 `tool_search`。供应商记录有两次 300 秒流超时和一次 `request_timeout / stream closed before response.completed`；19:31:31 记录用户中断，未写入 `scene.js`。

因此卡住并非仅动画或前端问题。供应商确实失败，后台又隐藏了相关事件；已有日志不能证明本次超时由被忽略的原始帧造成。旧 journal 没有保存的重试信息不能凭空重建，修复后新事件才会完整转发和持久化。

源代码依据为 `codex-api/src/sse/responses.rs` 的 SSE 接收超时边界、`app-server/src/bespoke_event_handling.rs` 的 Error/StreamError 及 will_retry，以及 `core/src/agent/status.rs` 的真实生命周期。删除和归档对照 app-server 的 thread_delete/thread_archive 行为。

## 复查与验证

- Skill 显式身份、注入时序和压缩保留链路，以及 deferred tools 下一次 prepare 激活和完整 schema 来源，复查未再发现确定差异。本批仅为实际 `generatedImage()` 补工具说明，没有改模型基础 prompt。
- 首轮本机完整前端结果为 285 个文件通过、1 个 Monaco 初始化 hook 超时；3,518 项通过、21 项未运行。首次原生模块转换不应计入 10 秒初始化钩子，现移至 suite 加载阶段；保留真实生产初始化和全部断言，两份 Monaco 测试 30 项通过，未增加超时。
- 图片来源、浏览详情、子任务事件、恢复、provider liveness、取消及截止时间的必要回归均通过。本批后端 producer/projection 集中 209 项、前端受影响链路集中 150 项通过；测试批次有重叠，不累加成独立总数，中间失败证据保留。
- 新增实际 WebSocket 删除测试，覆盖损坏 transcript 和 snapshot、tombstone 及另一个会话内容保留；工作线程 barrier 验证事件循环响应和用户选择的会话身份。最后删除/切换影响模块 34 项、实际 WebSocket 7 项通过；加强后的普通及超时 fallback 两项复验通过。归档影响模块 88 项通过，覆盖冷损坏历史、热缓存、无 inline metadata、projection 日志、后续 journal 续写及旧格式升级。批次存在重叠。
- 使用实际历史数据的独立浏览器回放确认：截图详情展开时存在一个图片证据卡，收起后卡片为零，最终答案无该工具图片；子任务呈现已中断，无主用户气泡和输入框。修复后的真实错误事件 fixture 可显示 retry/error，正常握手不显示。独立回放没有用户 transport session，未把截图字节加载当作通过。
- Evaluate 真实浏览器组件验证只有一个滚动容器，完整代码和结果末行可达。生产 TypeScript、构建、bundle/UI debt budgets 和协议/内核检查通过。

同机既有样本为 1,600 条消息、5,703,221 字节 context checkpoint。三个冷归档样本中，原 `_mutate_meta` 路径中位 615.037ms，元数据路径中位 14.441ms；逐项核对 transcript、context 和消息数保持一致。这是局部存储性能比较，不等同于 Codex 桌面的端到端 A/B。

远端全套以本提交的 [GitHub Actions](https://github.com/khalil-wu/minicode/actions/workflows/ci.yml?query=branch%3Acodex%2Frelease-readiness-20260927) 为准。详细日志与回放保存在忽略目录 `output/harness-ui-chain-20261009/`、`output/playwright/`。

保留用户原有 `design.md`、实际会话和未保存内容。未重启或取消用户应用；正在运行的 Electron/后端进程仍需正常重启才能加载本批源码。签名发布要求不变，本批不发布旧版或未签名 EXE。
