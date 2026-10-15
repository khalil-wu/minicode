# 原生命令投影与内部证据链：最终交付

后续状态（2026-10-15）：用户随后报告的 CI、新会话和项目动作问题已收尾。冻结基线 `18e8bd8d` 的 GitHub run `38749202449` 七项全部通过，见 `2026-10-15-ci-lifecycle-and-linux-mount-repairs.md`。现在继续下列已确认的 heartbeat、Memory Phase 1 WS 与 remote 剩余范围，尚不把只读审计或施工中的代码计为完成。本文件下方冻结输入、前端和 wheel 身份是此前交付批的历史记录，不代表后续 CI 修复及新功能后的当前源码或安装包。

后续实现检查点：heartbeat H1–H3（重复版本、按接受顺序恢复、完整 transcript 引用及运行时 Skill 证据）相关节点最新去重 391/391；H4（真实 busy gate、start-or-steer、durable run 与等待寿命）最新去重 130/130；Memory Phase 1 独立 WS 及原运输回归最终一次五文件 83/83；前端 renderer observer 123/123，均无跳过。范围相互有重叠，不能相加；初败及修复后窄复验均保留。详见 `2026-10-15-source-heartbeat-retained-context.md`、`2026-10-15-source-heartbeat-host-admission.md` 和 `2026-10-15-memory-phase1-websocket-transport.md`。这批正在提交完整 CI，本地专项结果不等于新提交已全绿；remote 仍另行实施。

本批生产修改、必要验证、前端构建与独立安装冷验已完成。它闭合下列已定位的问题，不代表整款 MiniCode 已与 Codex 完全等价。官方账号登录、授权及依赖该账号的 hosted 服务按用户要求排除；排除项不记通过。

行为依据为 `.tmp/codex-src` 的 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。视觉和活动投影另核实际安装版 desktop ASAR 与隔离 app-server 消息，不能把 Rust 源码版本当作安装版版本。

机器可核对的最终索引：`output/source-internal-evidence-delivery-final-20261015.json`。最后一次核对的 1144 个冻结输入没有变化，前端身份和安装包身份与验收记录相同。本轮收尾没有重跑已闭合的测试或修改生产代码。

## 实际修复

- 原生命令区分 provider 空参数 announcement、真实冻结 planned item、底部审批 request、实际开始和终态。Source 在审批前确实会发布 planned item；MiniCode 保留这份真实记录，不把等待审批当作进程已经执行，也不添加准备命令、等待批准或 shield 等自创状态。
- 命令真实开始时间由原执行入口产生，经 canonical、原 journal、持久恢复和前端消费。实际 direct callback 曾绕过 QueryEngine 的 stream journal；现在先等待同一个 journal owner 写入再调用宿主。SDK 无宿主也使用同一入口，nested/CodeMode 保留原路径；旧 turn/iteration 回调不能覆盖新回合复用 call ID 的时钟。
- 只有真实 native `abort` 表示用户取消。Guardian denied/timed_out、宿主超时或 digest 拒绝不会被错误升级成用户中止。未执行的取消保留 command、原始原因和终态，继续没有 execution start、exit 或 duration。
- 按实际 Source 输出组件修正详情：真实 exit 0 显示成功；没有 exit 且非真实 interrupted 时显示“退出码 未知”。未执行的模型取消回执留在原始记录，不再冒充 Shell stdout；真实 stdout/stderr 与实际执行异常继续保留，不新增“无输出”。
- 成功且实际 accepted 的 raw `node_repl`/`cua_repl` MCP CodeMode 结果，进入原 conversation 的 Guardian 证据存储。冻结私有执行身份、实际历史准入游标、顺序、预算和异步图片选择均沿 Source；普通 MCP、名称相似工具、wire metadata 不能伪造该身份。本批没有创建新的 Node/CUA 执行器。
- 修复 Responses 图片编码把显式 `original` 覆盖成 `auto` 的真实 wire 丢失；Responses、Chat、Anthropic 消费同一份图片，不重复包装或计数。正式声明并安装 Pillow native decoder。
- heartbeat 输入来源经真实调度 claim、durable run、逐条输入 origin、QueryEngine/Guardian 和冷 snapshot 保留；同回合真人 steer 仍来自 User。Memory Phase 1 请求归属真实启动会话 A，不借用被分析的 B/C 身份。Guardian reviewer 路由按 Source 实际 caller gate 修正，当前 custom/API-key/override 不发送不适用的 reviewer/parent response 投影，不新增官方认证子系统。

## 验证范围

各行是各自范围的最新结果，存在重叠，不能相加作全产品通过数。初始失败、错误期望与必要窄复验均保留。

| 范围 | 最新结果 | 证据与边界 |
| --- | --- | --- |
| 原生命令前端与 ExecCell | 21/21 | `native-command-activity-frontend-batch04.log`；最终输出投影字节 |
| canonical/journal consumer 与 producer | 71/71 | `native-command-activity-persistence-20261015-latest-results.json`；该批本身未证明 direct callback 入口，后续补下一行 |
| 实际 QueryEngine 启动准入及必要回归 | 67/67，新增 7 项 | `native-query-start-admission-delivery-20261015.json`；真实 PowerShell 阻塞期间从新 journal 读取，direct/nested/CodeMode × host/无 host，以及两 turn 旧闭包回放 |
| Node/CUA accepted evidence 与原 Guardian 消费者 | 349/349，新增 33 项 | `source-node-repl-evidence-delivery-20261015.json`；真实 stdio MCP、CodeMode、本地 HTTP 与三 API 图片请求；实际 stdio/HTTP 另在最终 QueryEngine 字节下复验 |
| heartbeat 输入来源 | 176/176 | `source-heartbeat-input-origin-20261015-latest-results.json`；不等于完整 heartbeat 合并/注入已实现 |
| Guardian 路由及不可伪造来源 | 23/23 | `source-guardian-response-route-host-boundary-final-20261015.xml`；早期 47 项中两个错误 Source 肯定断言已撤回，不与本行相加 |
| Memory Phase 1 启动归因 | 42/42 | 两份 `source-memory-phase1-provenance-*-20261015.xml`；实际独立 Responses HTTP，不声称 Phase 1 WS 验收 |

上述必要范围均无跳过。开发测试使用 Python 3.12；安装冷验与 decoder smoke 使用项目 bundled Python 3.11.9，不把两种运行环境混算。

详细调用链分别记录在 `2026-10-15-native-command-activity-persistence.md`、`2026-10-15-native-query-start-admission.md`、`2026-10-15-source-node-cua-evidence.md`、`2026-10-15-source-heartbeat-input-origin.md`、`2026-10-15-guardian-response-routing-correction.md` 和 `2026-10-15-memory-phase1-request-provenance.md`。

## 最终前端与真实桌面

最终 entry 为 `assets/index-2l4V0mq0.js`，index SHA256 为 `b8195e87598a308abe3073b63ac9ac948df585f06ed6b00fb81f688454fad142`，entry SHA256 为 `1f11fd9cc96c514cbef80d95d609d1bdd447cf9a634bfc1d73b96c3a4b4decf3`。TSC、Vite、UI debt、bundle budget 与 protocol sync 已通过，记录在最终索引。

CSS 为 `assets/index-CpG3V_Lq.css`，SHA256 `1c87eb577857c9aa7a7bbd66a71660172858449eaa7b12a6714529ec7011d621`，与此前 BT3 逐字节一致，保持既有字体、字号和字重。

真实 Electron/WS/native policy 验收按构建分开保留，目录均在 `output/playwright/codex-control-audit-20261013/`：

| 实机阶段 | 构建 | 可计入的结果 |
| --- | --- | --- |
| `production-native-command-activity-companion01` | Cz0Kkr_F | allow 完整链通过；整个 run 因 QA 把实际 cancelled 误预期为 interrupted 而为 false，不宣称整场通过 |
| `companion02` | BqCYZOvL | 0 个功能场景；QA 使用本地 store switch 而非实际 WS 请求，失败原证据保留 |
| `companion03` | BqCYZOvL | 剩余实际 abort、真正 cold restart 两项通过，errors=[]；其中截图暴露取消回执冒充 Shell，随后独立修复，不把该输出显示计为通过 |
| `companion04` | 2l4V0mq0 | 最终受影响显示只读验收 1/1，errors=[]；0 次新 provider 请求、0 条新命令，不重复旧功能场景 |

合计仅为 **3 个唯一真实功能范围**，加 **1 个最终只读显示范围**，不称为在最终前端重跑了前三项。最终 backend 在 companion03/04 前后 16 个相关生产文件哈希相同。

Root 独立逐张查看 companion01 的 01/02/04/05、companion03 的最终五图及 companion04 的 09 图；中间展开动画帧不作为 settled 验收。最终图：

`output/playwright/codex-control-audit-20261013/production-native-command-activity-companion04/09-current-build-native-preexecution-raw-receipt-and-empty-shell.png`

真实批准命令在冷启动后保留原 start `1792053612540`、duration `3253ms`、exit `0` 和实际输出；原 abort 没有伪造执行时间/exit/duration。原隔离 profile 的 8 条 transcript、`NATIVE_POLICY_VISUAL_DRAFT_KEEP` 与原规则保留。没有操作用户普通窗口，所有自有 QA app/provider 已退出。

## 最终 wheel 与独立安装冷验

交付 wheel：`output/source-internal-evidence-final-wheel-delivery-20261015/minicode-0.2.0-py3-none-win_amd64.whl`，大小 **7,543,989 bytes**，SHA256 **`5fa1d189f3ddaa48d503f41f97854c6a972f12806bc0bc3b7c5204aa17b30ace`**。这是 Windows x64 backend wheel；不据此宣称重新构建了完整桌面安装器。

冻结清单 `source-internal-evidence-delivery-final-inputs-20261015.json` 含 1144 个输入，SHA256 `36739741def78226e04f37861ae1b1b5a6c8fb6fa6effc756d94aae9b1db32ab`。最终核对未变；wheel 的 1129 个 backend 文件与冻结源码、全新安装目录逐字节一致。三个新增模块 `guardian/node_repl.py`、`guardian/images.py`、`backend/ids.py` 均实际打包。

全新安装目录为 `C:/Users/ago/AppData/Local/Temp/minicode-source-evidence-final-a6e94a11d8634318863a455ab08fb92a`。使用 bundled Python `-I`、Temp cwd、仅 System32 PATH，实际安装包 native consumer 冷验 **14/14**，覆盖 native identity、资源、冻结 argv/审批、实际 PowerShell、prefix 落盘及冷 owner 复用；详见 `source-internal-evidence-final-installed-cold-20261015.json`。

`pyproject.toml` 正式声明 `Pillow>=11,<13`，sidecar lock 固定 `Pillow==12.2.0`，wheel metadata 为 `Pillow<13,>=11`。同一全新安装目录的 backend 经 bundled `_imaging.cp311-win_amd64.pyd` 实际解码 PNG、capture/store/render，并保留 `original`，见 `source-internal-evidence-final-installed-node-decoder-20261015.json`。这只是安装后的 native decoder 边界，不另算一个 MCP/server 场景。没有重建/清空整个 runtime，也没有伪造 runtime build stamp。

早期候选 wheel `94e8d3d1…` 在独立审阅发现 direct callback 漏写后停止交付，仅保留原 build/install 历史；未给它记冷验通过。当前 `5fa1d189…` 是修复后的最终包。

## 仍需后续对齐的范围

此前交付批没有未通过的必要项，但不代表全产品等价。heartbeat 重复触发合并、完整 transcript 引用、busy 自动注入及相关 gate 已在上方后续检查点接通，分别保留其新验证；不追写成此前批次已实现。本地可配置 remote executor 的 registry/connection、thread selection、远程 filesystem/tools 与生命周期链仍在后续实施，且不因排除官方账号而自动排除。远程能力须先核实际桌面入口，不能因 Source 有 API 就自创按钮。

Memory Phase 1 的归因及 HTTP 链由此前批完成。随后确认的可选 WS 差异现已另批修复：Source `memories/write/src/runtime.rs::stream_stage_one_prompt` 调用 `ModelClientSession::stream`，后者在 `core/src/client.rs:1030、2237` 按 provider WebSocket 能力及 session fallback 选择运输，没有官方账号门槛。MiniCode 现为实际 side context 建立独立 pool/request，保留主回合连接和 fallback，具体真实 HTTP/WS 与取消、partial usage、计费隔离证据见上方新报告，不借主回合或 Phase 2 测试替代。

Node/CUA 本批是已注册工具的证据消费链，不包含新执行器。官方账号相关 hosted connectors 不做。旧“高级 review”笼统待办不能直接沿用：实际桌面 Markdown/code-comments 工作流与 Rust 可选 ReviewTask 应分别核对。更多桌面区域也不能借本批测试数被宣布完全一致；已证实范围与后续只读范围分别保留在 active-work 和 `2026-10-15-local-remote-environment-scope-audit.md`。
