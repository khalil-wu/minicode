# Executor 编辑器、HTTP 与 checkpoint WS 消费链

本批是此前 R2 工具消费链之后的 workbench 入口接线。代码仍属于未提交 remote WIP，不等于 `ae278f910148f9c731a0dfaa51453431d66179c7` 的绿色 CI。索引为 `output/executor-editor-api-delivery-20261016/manifest.json`；Root 的 desktop host/tunnel、最终前端 build 和实机截图另行冻结，不把它们与本批的测试相加。

## 对照契约与实现

Source checkout `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 的 `app-server/src/request_processors/fs_processor.rs` 明确选择 `try_local_environment()`，并调用 executor filesystem；这描述的是该 app-server 宿主自己的文件系统。其 `exec-server/src/local_file_system.rs` 提供 URI、metadata、walk、copy/remove 与文件 handle。独立注册 executor 的 MiniCode 编辑器是现有 workbench 的扩展，不宣称 Source app-server 已提供同名远端编辑器或 LSP API。账号登录、官方授权和依赖官方账号的线上服务不在本批内。

- HTTP workspace 请求从真实 session/conversation owner 开始，调用 R0 `capture_conversation_environments`，复用同一 store instance + conversation 的 thread owner；再绑定本次接受的环境和 permission scope。URI 请求、显式环境选择与 configured empty 不能进入 controller Path 的旧接口。没有模拟 WS host，也不从 metadata 取得 Ready 对象或注册权限。
- tree、file、compare-write、目录创建、rename/delete、raw、preview、名称/内容搜索、项目索引和 Git status/diff/worktree inventory 均直接使用所选 executor。响应保留 environment ID 和真实 path URI；本机宿主保留既有 native path 字段，独立远端用 URI。Editor 的 compare-write 使用真实内容 hash；搜索中的 dirty buffers 只作为搜索输入，不保存到磁盘。
- GUI rename 由 Source 支持的 copy/remove 完成；后段失败保留已复制的目标并返回真实部分状态，不回滚掩盖字节。递归删除使用 Source 的常规 remove 语义，不强加其不支持的 recursive no-follow 操作。raw 文件 handle 和响应生命周期保留原环境 lease。
- Electron 的文件读取、文件树和搜索在已有对话时走同一个 HTTP owner 链；请求失败不再转 native FS。没有对话的原 native 行为保留，所选 remote desktop host 的原 native RPC 仍由 Root 的宿主连接层转发到该宿主。
- `EditorTab` 保存读取时的 conversation/environment/path URI，后续保存保持这份语义所有者；重连更新 transport session ID，切换到另一对话不把旧草稿改写到新目标。hidden workspace 和持久草稿沿原保存机制保留。只有包含真实 `actual_hash` 的 409 才显示为文件版本冲突；owner/empty 的实际错误继续作为保存失败呈现。
- 预览 URL 的重新签名保留来源对话和环境；workspace raw v3、browser raw v2 将该所有者纳入 HMAC。旧无 owner 的本机 URL 契约保持。编辑器 AI completion 使用接收宿主配置与 UI 传入的未保存内容，文件身份来自本次 accepted scope，不加载 remote cwd 下的 controller 配置。
- checkpoint.list/rewind 的真正 WS dispatcher 入口捕获本次环境，传到 R2 checkpoint service/manager；UI command workspace 和 executor cwd URI 分别核对。恢复继续使用当前权限，不能借历史高权限 scope。
- `workspace/__init__.py` 的公开 `create_workspace_router()` 在应用组装时才导入 HTTP consumer，消除了权限模块导入 PathUri 时反向进入 editor API 的真实冷启动循环；没有 try/except 或本机 fallback。
- 按 Root R4 的接口补充了 runtime connections/remoteHost 类型。存在 remoteHost 时，目录选择使用现有输入对话框，默认该宿主 home，工作区路径交给该宿主 trust 操作；不打开控制端目录选择器。字体、字号、字重和 CSS 未改。

## 实际验证与初败

后端使用 bundled Python 3.11.9；独立 Source peer 为安装版 `OpenAI.Codex_26.1007.9446.0` 的 `codex.exe exec-server --listen stdio --concurrent-requests 4`，每个 fixture 使用独立 Temp cwd/CODEX_HOME，不调用模型或账号。HTTP/WS 使用真实 Uvicorn TCP listener、真实 `WebSocketSession`、真实 dispatcher 和 R0 snapshot。测试同时创建 controller shadow 同名文件，逐步确认它没有被读取结果冒充、保存、移动或删除。

| 批次 | 结果与意义 |
| --- | --- |
| backend first | 46 项：42 pass / 4 fail / 0 skip。两条实际消费链已走到递归删除，暴露强送 no-follow 不被 local/Source 支持；另两条为 empty snapshot 仍显式请求已删除 selector 时，测试误期望 No usable，而 Source 正确返回 unknown ID。WS checkpoint、本进程之外的 cold HTTP、raw 签名以及 38 项既有回归已通过。 |
| backend repair | 4/4。按 Source 常规 remove 修复消费者；无 selector 的 empty 断言验证 No usable，不改生产的 unknown ID 语义。 |
| backend identity-final | 3/3。local/Source 证明缺 manager 的显式环境请求不落回本机；独立 Source executor 的语言请求明确 503，并验证没有进入 controller LSP。 |
| backend local-path-final / structured-identity-final | 各 2/2，范围重叠。保持本机 worktree native path 与现有本机生命周期调用的契约，远端继续 URI；结构化 worktree/tree/index 元数据没有在 response model 中丢失环境身份。 |
| backend completion-final / launch-tree-final | 3/3 与 2/2，部分重叠。completion 用受控 adapter 验证本次宿主配置、未保存输入和实际 environment/path 身份进入既有 adapter 边界，并验证关闭；没有把它当成真实模型运输。既有 completion 取消/无 tools 回归通过。目录 inventory 与文件内容沿既有 GUI 规则区分，`.minicode/launch.json` 仍可从树中打开。 |
| frontend first | 84 个真实 test：65 pass / 19 fail，另 1 个 collection failure 占位（XML 85 case / 20 failure）。新协议模块 eager 导入 store，使 Node consumer 访问 window；新 browser runtime 测试缺 jsdom；PreviewPanel 的旧 spy 尚未包含新增 owner 第四参。原失败全部保留。 |
| frontend repair | 32/32。协议模块改为纯 owner 数据接口，由真实 useWebSocket 注册 live getter；旧 Node 协议测试恢复。原先通过的其它文件没有重复运行。 |
| frontend preview-owner-final | 5/5。更新调用 owner 的实际期望，保留 dirty tab、未发送主草稿及来源工作区导航的原断言。 |
| frontend save-owner-final | 10/10。真实 owner 身份冻结、重连、HTTP 错误保留、native FS 不作为失败兜底，以及 remote path picker/trust。 |

最新唯一范围：后端 **48/48**（新入口 9 项 + 既有范围 39 项），前端 **90/90**。这是保留初败后的逐项最新结果组合，不宣称一次重跑了所有项。无跳过；Uvicorn/websockets/Starlette 的 DeprecationWarnings 和 Browserslist 提示保留，不能写成零 warning。Root 的 TSC/build/实机验收另有范围与构建身份。

还有两次实际冷启动初败必须保留：Root 的 remote daemon 首跑记录了 Python 循环 import，路径在 manifest；随后独立 bundled Python + Temp state/cwd 的 `import backend.main` 通过，真正子进程冷 HTTP `/health` 与正常 shutdown 通过。Root 的旧 QA 前端白屏来自构建和编辑交叠：asset `websocket-runtime-DnDEQq1S.js` 写于 UTC 05:01:20.805，缺失 import 补入源码写于 UTC 05:01:30。旧 asset 中只有未绑定的全局函数调用；它早于当前源码，不归因于 Rollup，也不能冒充最新 TS 的通过记录。Root 保留旧现场，后续基于冻结源码另建 QA 产物。

## LSP 和生命周期边界

remote desktop daemon 上运行的 MiniCode 后端把该宿主视为 local executor，继续调用该宿主自己的语言服务。独立 Source executor-only 当前没有目标 language-server 配置与 LSP 运输，已明确返回 `editor.executor_language_server_unconfigured`；这是一项真实未实现能力，不是安装版 Source 不支持 filesystem，也不把 error response 计为 LSP 功能通过。其余实际配置在独立 executor 上的 worktree lifecycle、环境安装及 Git delivery service 也不转 controller；当前明确提示该目标尚未配置服务。本机及 remote desktop 宿主的原实现保留。

若继续实现独立 executor LSP，需要完整处理：

1. 从实际目标配置选择 language-server executable/argv 和 C/C++ compiler 配置；不能借控制端 Node、clangd 或 bundled language-services 路径。
2. 以环境/真实 registration/窗口/client 为服务 owner，复用原 document_id、model_version、snapshot_revision 准入与取消规则。
3. LSP JSON-RPC 帧通过所选 executor 的真实 process handle/stdin/output 运输；启动、didOpen/change/close、取消和 cleanup 始终保留原 lease，不在 registry 替换后迁移。
4. LSP URI 转换按目标 convention 处理，移除 `LSPClient` 和 editor language manager 内 controller `Path.resolve()` 对远端路径的解释。
5. 用实际目标语言服务验证 dirty 文档的 completion/hover/definition/references/rename/diagnostics/format，确认目标磁盘原字节不变，并验证关闭文档、重连和替换 registration 时的资源释放。

此列表是下一项具体实现范围，不以“已有远端桌面 host”代替独立 executor LSP 的证据。
