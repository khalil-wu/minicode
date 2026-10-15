# 当前 Source 对齐工作记录（总任务尚未完成）

## 当前优先项：CI 已通过，继续剩余 Source 行为

最新完整检查点为提交 `2c3cae04ca0a121d74d91cc26b40d68689a6d417`，GitHub run `38764385600` 七项全部通过，Backend **6579 passed / 27 skipped / 0 failed/error**。它包含 heartbeat、Memory WS 以及全量回归揭示的测试生命周期/配置隔离修正，详见 `2026-10-15-heartbeat-memory-ci-checkpoint.md`。原 CI 修复基线 18e8bd8d 的七项绿灯保留于 `2026-10-15-ci-lifecycle-and-linux-mount-repairs.md`；新会话及文件夹动作已修。当前继续 remote 完整消费链，不把独立客户端基础层或只读审计计为整项完成。历史窗口和 wheel 身份不能代指当前源码。

heartbeat/Memory 本地专项及顶部完整 CI 均已通过：H1–H3 最新节点去重 391/391、H4 最新节点去重 130/130、Memory WS 最终一次 83/83、前端 observer 123/123，专项无跳过。真实 `/compact` 请求携带 host native item ID/turn，并按原接受顺序恢复；不以文本匹配或公开 metadata 推断信任。各范围重叠不相加，原始失败保留。稳定报告为 `2026-10-15-source-heartbeat-retained-context.md`、`2026-10-15-source-heartbeat-host-admission.md`、`2026-10-15-memory-phase1-websocket-transport.md`；本地 remote 完整消费链仍未闭合。

此前 CI 修复阶段记录见 `2026-10-15-ci-new-chat-repair.md`、`2026-10-15-ci-source-contract-round03.md`：bundled Python 3.11 新会话的 `is_junction` 根因与文件夹旁的菜单/新聊天按钮已修复、实机核对，前端 `index-srAOTZkG.js` 曾在普通 MiniCode 中运行且 `/health` ready。后续已闭合 REST 私有包装记录、LSP 正常退出、未知模型提示词字节来源和冻结审批生命周期；各范围按冻结清单和真实 XML 分别记录。旧 8311 snapshot 明确中止、不计通过；当时未执行的 Root 尾段已完成，最新 GitHub 状态以上一段的 18e8 七项绿灯为准。下方“最终前端/最终 wheel/冻结输入”描述均属于此前交付批，不代表后续修改后的当前源码或安装包。

## 最新状态：原生命令活动投影与内部证据链本批已交付

范围更新（2026-10-15）：用户明确“官方账号不做”。官方账号登录、授权、接入，以及依赖该账号的 hosted 服务线上验收均移出本轮范围，不再计作待办或阻塞。历史未运行证据保留，不把范围排除记为通过；第三方/自定义模型接口、本地权限、工具、Skill、MCP、多 agent 与桌面交互继续对齐。

本次续做更新（优先于下面历史阶段描述）：

- 本次续做的生产与必要验收已闭合，最新前端为 `index-2l4V0mq0.js`，index SHA `b8195e87598a308abe3073b63ac9ac948df585f06ed6b00fb81f688454fad142`。TSC/Vite、UI debt、bundle budget和protocol sync通过；CSS与此前BT3逐字节一致，保留字体、字号和字重。最终汇总见 `2026-10-15-source-internal-evidence-delivery.md`，机器索引为 `output/source-internal-evidence-delivery-final-20261015.json`。本批闭合不代表整款产品等价。
- 原生命令策略已完成产品完整消费：真正Source Starlark/AST→可信层规则/managed requirements/原模型Allow过滤→最终argv/profile/digest→Source可用审批决策/原prefix→实际PowerShell→规则落盘及后续/冷owner复用→相同权限提示词。相关backend最新124、frontend100通过；原helper180与release9是不同旧范围，不相加。该阶段历史wheel `a9151f5f…`、安装冷验14/14及1119个backend成员/1143个输入保留；当前交付已由下述新包取代。范围是已验证Windows x64，见 `2026-10-15-native-source-execpolicy-consumer.md`。
- native审批真实功能 `production-native-execpolicy-ui-run01` 三场通过（CC81冻结构建）；逐图发现的raw login、红色升级条、选择勾及CLI式prefix文案已按当前ASAR qr/Aml修完整native分支，Source实际SVG、三行真实测量/展开、动作菜单/Info tooltip及loading原label均接入。最终BT3的原profile视觉伴随 `production-native-execpolicy-visual-companion03` 一场通过、4图Root逐张查看；真实命令执行、原草稿、原规则追加、canonical耗时一并核实。01临时空workspace消失、02已清除active-workspace指针的fixture失败保留，只重建自有空目录/本次marker，未重置原profile历史或规则；不重复计旧三功能。
- 物理回合/阅读批已完整验收：首clock/interleave 331范围及后续必要窄批分别留证，最终受影响3文件91/91；不累加重叠节点。run02前两项窄通过，最终BT3 run05仅补剩余阅读/final一项通过；同一正文anchor `132.546875→132.546875`、偏移0px，原上传/草稿/dirty Monaco/磁盘和旧partial均保留。根因是旧无assistant用户被global busy误造处理区，以及native/TanStack双补偿与冗余auto120占位，均已修生产链。见 `2026-10-15-source-physical-turn-clock-projection.md`。
- 原执行时长另已修正真正producer：run04实图56秒→55秒暴露WS后设timer漏1162ms准备时间；现原Runtime持有monotonic+epoch起点，partial、持久runtime/journal、canonical done、startup failure和WS都用同一实测duration，未知旧clock不猜。该scope最新69/69含新7项和必要相关回归，首失败/窄复验保留；run05实机三层duration均44687ms，界面44秒。见 `2026-10-15-source-run-timing-owner.md`。
- 执行/运输与资源生命周期已闭合：Source无订阅且无实际执行的60秒卸载、原window instance唯一writer重连、Home真实admission、原owner关闭超过32秒继续真实读取/命令/同一final、新物理窗新SID观察、原Window/PTY/WebContents销毁，及真实独立memory HTTP在app.close前取消/排空均验收。相关最新统一86/86、历史去重190不同case最新全过；实机6个唯一场景（CC81的4个life、BT3的2个aux），失败尝试保留且不重复计数。见 `2026-10-15-source-thread-execution-lifecycle-audit.md`。
- 新增heartbeat来源批已闭合：真实due/manual claim→durable run trigger→本次输入origin→QueryEngine/Guardian→snapshot cold，并按同一物理回合每条steer自身来源处理。真实HTTP两场核对scheduled Source blob/header、manual无伪造scheduled及无memory标头；新增10项连同受影响旧范围按节点最新176/176。首bundled无async插件与四项public metadata错误期望、必要窄复验均留证，未操作用户窗口；此批不等同于重复heartbeat合并/busy自动注入已实现。见`2026-10-15-source-heartbeat-input-origin.md`。
- 内部memory/同步Guardian归因已接实际producer→Responses HTTP/WS与独立usage/User origin。首47的两个Guardian肯定断言因误读Source caller已撤回；真正Source仅在selected-model/backend-auth/routes gate允许时发reviewer头，parent_response属于client_metadata。当前custom/API-key/override均不满足，已删除错误无条件投影，不为排除的官方正向服务新建子系统。后续真实HTTP/WS与本批新增host-only override边界共23/23通过；内层llm_request_metadata不能伪造internal_session_source/parent response/Sourceblob。历史与修后范围不相加，见`2026-10-15-guardian-response-routing-correction.md`。
- 原生命令活动投影批已闭合：真实Source审批前发planned item，request另归底部；provider空参数announcement只留raw，不冒充命令开始。去掉准备/等待/shield自创投影，只有真实native abort才取消原owner；Guardian拒绝/超时不再误作用户abort。canonical/journal保留同id presentation与真实execution clock；独立审阅又补实际QueryEngine direct callback先journal再host、SDK无host与旧turn闭包隔离。consumer初批71、实际入口最终67（新增7）、前端最终21各自通过，不累加。Source真实“✓成功”和展开详情“退出码 未知”保留；未执行取消回执仅留raw，不再冒充Shell stdout。实机allow在Cz0通过，abort/cold在Bq通过，最终2l4仅补1项受影响只读显示；共3个唯一真实功能及1个只读显示，失败尝试与构建边界保留。Root逐张看最终图，原8条消息/草稿/规则保留，见 `2026-10-15-source-native-command-activity.md`、`2026-10-15-native-query-start-admission.md`。
- 增强REPL证据批已闭合：真实MCP accepted-result→原thread/Guardian store→实际history admission游标→Snapshot/retained/Decisions共用图片选择，Node/CUA仅消费成功且真正接收的raw结果，不伪造新执行器。修Responses显式original detail丢失，三API实际HTTP核同一图片和顺序；真实stdio/CodeMode/HTTP四个不同动作各一次审批，未误当重复审批。新增33/相关最新349全过无跳过，最终QueryEngine字节另复验真实stdio/HTTP，不相加。见 `2026-10-15-source-node-cua-evidence.md`。Source unsupported-auth强制COMMANDS的路径不凭Mini viewMode发明prose模式。
- Memory Phase1归因已闭合：实际启动A持有请求身份，被分析B/C只保留各自durable输出；真实启动caller、协调/提取、重试稳定UUID7、独立usage与未知来源均核对。42个不同case全过无跳过，实际standalone Responses为HTTP，不借其它链声称Phase1 WS通过。见 `2026-10-15-memory-phase1-request-provenance.md`。
- 本批最终wheel `py3-none-win_amd64` SHA `5fa1d189f3ddaa48d503f41f97854c6a972f12806bc0bc3b7c5204aa17b30ace`，大小7543989，1144个冻结输入未变，1129个backend成员与源码及全新安装字节一致；新node_repl/images/ids模块实际入包。bundled Python -I/Temp cwd/System32 PATH真实consumer冷验14/14；Pillow正式声明/lock12.2.0已安装，独立安装backend的native decoder/capture/render smoke通过并保留original。此smoke不冒充新增MCP或完整runtime重建；早期候选94e8…因direct回调缺口停止交付，没有记冷验通过。见最终交付报告与JSON。

此前原 input_origin 批明确留下的 heartbeat重复合并/完整transcript引用/busy自动注入及相关gate，现由本页顶部的新检查点补齐，不归入旧批。当前明确剩余边界仍是本地可配置远程executor的完整registry/connection/thread selection/filesystem/tools与生命周期，不因官方账号排除而划掉，见 `2026-10-15-local-remote-environment-scope-audit.md`。先核实际桌面入口再实现，不能因Source API存在自创按钮；旧“高级review”笼统待办须区分实际桌面Markdown/code-comments与可选Rust ReviewTask，不继续盲派。其它旧待办以下均为历史记录，须用最新报告确认，不能按测试数推测整款完成率。

此前只读终审确认的 Phase1 可选 WS 差异已在顶部的新检查点独立接通并实测，完整证据见 `2026-10-15-memory-phase1-websocket-transport.md`。原归因/HTTP批结果保持历史身份，不借主回合或 Phase2 WS 通过替代。

此前本次续做的阶段记录（以上述最终生产/实机结果为准）：

- follower 真实五类功能已分别完成：run05 的运行中恢复→真实读取→同一最终回复及默认收起，run06 的带附件 steer、真实 follow-up queue、A 后台/B 或 Home 阅读和原 turn Stop。run06 四项 errors=[] / exit0；相关前端 current-turn recovery 按最新 scope 239/239。不能因此把仍在修的 steer 插入顺序、最终回复可见位置和恢复秒表算作完成；见 `2026-10-15-follower-current-turn-recovery.md`。
- 新补真实物理回合用户位置：依据 Source 用户 item started/completed，在实际 consumed admission 原子保存 assistant marker + 原 user 行，再发布完整已提交 public user，stream / history / frozen resume 保持同一边界；pending reasoning 先于用户 item 发布，不按时间猜位置，不重排 canonical 数组。后端必要 scope 按节点最新57/57，原失败及窄复验保留；前端与实机 companion 正在接。见 `2026-10-15-source-physical-turn-input-boundary.md`。
- 原运行起点 `started_at` 已从实际 main run-start 保留到本地及 follower 单页/分页 resume，缺失时不编造；新4项真实 reducer/factory/两条恢复 producer 检查通过。前端单独保存起点来源，实时秒表和实机验证尚在衔接，不用本地 hydration 时间当原运行起点。
- 生命周期正在完整接调用者：强持实际 execution host 和 Home admission Task、原窗口唯一 writer 重连、连接退场与核心执行分离、Source 60秒无订阅且无活动的卸载，以及明确 archive/delete/host/app drain。新代码尚未统一验收；仍不能宣称关闭原执行窗超过30秒后继续任务已对齐。见 `2026-10-15-source-thread-execution-lifecycle-audit.md`。
- 固定Source native execpolicy helper 已完成真正 Starlark、shell AST、requirements、filter/overlay、amendment和静态Windows release：按节点最新180/180，实际冷目录9/9；不依赖.tmp。产品的冻结最终argv→原生策略→审批prefix→执行→规则持久化/提示词与wheel/桌面consumer正在完整接链，尚未记消费者验收。见 `2026-10-15-native-source-execpolicy-helper.md`。

- 新增用户截图 `codex-clipboard-ff50df00-fe70-4eee-90b1-fb65584abbce.png` 已逐项核对：常规设置灰色圆角触发器、应用真实图标、完整标签、菜单全宽标签与右侧选中勾。本批移除共用 SelectMenu 强制 ellipsis / nowrap，清除设置行固定宽度与旧原生 select 包装 padding，补齐 General、Browser、Archive、FeatureFlags、Provider、MCP、Plugins、Scheduler、Voice 等实际消费者。真实实机发现并修复400→500字重继承，字体/字号/原字重保持；Source静态向下箭头、不自创旋转/scale、2px菜单间距已核对。统一312项通过；真实`after06`六场景/10张图及`after07`四组/7张图均exit0/errors=[]，精确10%/hover20%（8bit ±1/255）、窄窗/cold及Source打开目标slot通过。Root另外实际查看after07目标菜单和窄弹窗。after07 bundle `index-DQRQ8DWm.js` 是该设置批验收构建；后续新增行为独立验证，见`2026-10-14-settings-select-source.md`。
- Source `Ec/Oc → cBc/eUr → nut/Ylt` 的第三种 Enter 已补实际设置/持久化/main及side消费：只有真实硬换行算多行，运行中相反跟进chord不改全局偏好，原user_message/compact admission与草稿链保持。统一8文件212节点首轮208通过，4项新fixture误用内部busy_behavior；实际wire是streaming_behavior=steer/follow_up。只纠正fixture并复跑受影响49节点全部通过，按各节点最新值212通过；tsc/build均通过，实机结果以下一段及`2026-10-14-enter-behavior-desktop.md`为准。
- 上述 Enter production build已成功，后续同批通知/控制标记构建为`index-B5o7lS_j.js`、index SHA256 `0df90ae743a199b216dda27fc72dfa2c2f3a9c0dd464bd32e437799dd30db02e`。独立真实`production-enter-run03`的19个场景通过（General三项/原400字体、main+side三模式idle/nativeIME/Shift/hard-soft、12种真实running相反跟进/上传附件/semantic receipt/新草稿）；原profile三种值真正退出进程后的`production-enter-cold01`三项通过。原`/compact`短窗口refused根因修复后，仅同owner、原history与artifact补`production-enter-compact02`三项，均实际HTTP请求→compacting→native_completion completed，errors=[]/exit0。没有重新seed历史或重复19个已通过场景；处理状态补图单独记录，见`2026-10-14-enter-behavior-desktop.md`。
- 通知/自动未读/Source whole-marker新批已闭合：真实completion/heartbeat/failed_runs_only/continuation来源、canonical终态原子未读、只凭实际native焦点+可见对话清已读、原文保留与最终显示控制。实机首次抓到partial-terminal先保存导致状态判重漏未读，已改依本轮真实completion事实判重；也修真实send_conversation_list成功却返回None误报warning。最新后端167、前端273、native11各自scope通过；真实HTTP+独立认证WS+隔离Electron+原profile冷重启`production-completion-unread-run08`五场景全部通过、errors=[]/exit0。Root另查看未读点与whole-marker实际PNG。测试范围重叠不相加，见`2026-10-14-source-completion-unread-chain.md`。
- Snapshot Async Guardian V2新批已闭合：固定Luna的真实tool-less raw Responses、首个非空delta原文判定、16并发/重试/输入输出预算、后台drain实际usage与private真实错误、所有需审批consumer中央缓存与同步退回、真实config/model/root/权限/环境鲜度、精确requireGuardian与requireFresh分离、retry/escalatedExec/modelRequirements、Luna catalog变更失效、当前工具生命周期start/finish/host清理。注册provider/authstorage实际cap gate有10个新增transport节点；Source所有当前MCP只按MCP身份，不虚构Node/CUA。最终新批节点取最新297 passed / 0 failed/error/skipped，Source旧同步/checkpoint相关一起验证但不和此前326叠加；bundled Python实际import/host close/真实模型requirements helper与raw API也通过。首批6失败、第二批真实private error/async-generator关闭缺陷均保留。Source flags缺省false且不新增内部桌面控件。WS pool/prewarm在后续批已完成；Conversation/decisions/Node仍待继续，官方account/billing/owner监听按最新范围排除，见`2026-10-14-source-guardian-async-snapshot.md`。
- Snapshot WS后续批已闭合：独立8 idle/16 socket/16 classifier许可、完整Snapshot/稳定retry身份、UUIDv7、firstdelta/usage/drain生命周期、非阻塞HTTP fallback、Source timeout/cooldown/age、registered provider凭据更新key失效、Host及实际model/permission绑定后的受管后台prewarm。新29节点连同必要旧调用链统一112/112、exit0、无pending/athrow警告，不和297相加。Source普通main pool未改，真实account watcher/401 recovery/provider专有transport/Conversation/decisions等缺口保留，见`2026-10-15-source-guardian-websocket-pool.md`。
- 手动短窗口compact实际根因已修：显式manual消费不再借自动20000-token tail早退；本地和原生都保留真正inflight工具原对象与在模型请求期间抵达的真实结果，不吞settle异常。相关最终9文件119节点按最新完整scope合并全通过，不相加重叠批次；旧扩展fixture已修正，显式ask+bypass真实拒绝的否定case保留，完整文件13/13。原profile三种Enter实际compact companion三项通过，真实新草稿与原artifact保留；photo01真实处理→完成两图已由Root另查看，普通用户窗口未操作。见`2026-10-15-source-manual-compact-window.md`。
- Source global Pinned拖动排序已闭合producer→持久顺序→canonical inventory→Source pointer/Alt键盘；不增手柄，不改变activity/pin_at、项目/最近默认排序。backend37、frontend126及必要新增4项窄复验均通过，不相加重叠范围；真实`production-pinned-reorder-run02`五项包含完整cold、两窗/workspace草稿、真实上传owner均通过。逐图发现并修复portal缩进与字体继承，最终`final-visual03`当前mouse/ESC/逐步ACK的Alt两组通过，errors=[]/exit0；Source ghost inert、20%/70%、12px共享缩进、13px原字号、2px/8px插入线及同一行跨中线已实际核对，Root另看两图。冻结构建`index-DorY0-30.js`/`index-CCxOW9kY.css`、index SHA256 `8c2459edfe2e0330932e7548a7dd7c2519bf3eb55d6d570d5f5ebd1f65e7068e`。原失败companion保留，见`2026-10-15-source-pinned-reorder.md`。
- Source sidebar/Header共享完整Open in与指定真实main thread的新窗口入口已落：实际仓库状态查询取代不成立的gitBranch gate，真实installed catalog、native单次导航意图由main消费ACK确认，startup restore / File fresh Home / 指定thread三种语义、window-owner activeId与显式Home不被inventory自选覆盖。9文件76、native12、TSC/Vite通过；实际run08七项全过，覆盖完整Open in、running同thread restore且仅一次前台采样、两窗草稿/上传/未保存Monaco独立、Home重连、Ctrl点击、secondary槽位fresh和原profile冷恢复。cold-photo01仅补三张settled图，HTTP requests=[]，不重复计功能数。Root另看Open in与cold内容保持图。冻结构建index-ClcLUw5p.js、index SHA256 546dfa4bfc409016e8d347b85d7e184e180cf1d3627f89eb312338f535074cd9。新窗原先呈idle且缺实时过程是同功能尚在修复的follower范围，不能把上述七项替代live过程/控制验收；见2026-10-15-sidebar-open-window-source.md。
- follower当前批按Source订阅与快照顺序接public stream、原execution owner路由与原提交窗口receipt，前端首批218/218；后端首批203中185通过/18失败，旧结果保留，不算完成。只读交叉审查另查出idle新turn provenance、pre-run Stop、原target alias、A执行/B阅读的agent控制、regenerate transition清理及断线receipt误投；连同owner退休的真实终态一起修完整调度边界，修齐后统一后端复验，再进行三窗实际任务验收。无原始执行资源移交，不用前端伪造busy状态，未启动真实QA前不记通过。
- 上述调度边界已补齐并按节点最新去重后端297/297、0 fail/error/skip（新follower30项），完整五批失败与必要窄复验保留；产品在batch03前冻结，后两次只修既有fixture，不相加重跑。精确Source及范围见2026-10-15-source-conversation-window-follower.md。Root另读Source connection_closed/remove_connection及preserves_listener_for_other_connections后确认后续生命周期仍有差距：MiniCode原执行窗断线超30秒会shutdown其task，Source移连接不终止仍存活的thread；不能拿真实cancel/done投影冒称关闭原窗后继续执行已对齐。该项须将conversation执行context与运输生命周期分离，不调大超时或换browser/terminal owner。当前三窗实机五例另验，不能覆盖此关闭owner场景。
- Root复看实际模型steer图另发现多加的箭头/“已引导当前任务”状态，重读当前桌面user-message分支及steered渲染过滤后移除；真实准入、消息、附件和history保持。纯附件steer不再由内部标记产生空泡。与workspace成功toast清理合并进下一次必要前端验证/构建与真实follower QA，尚未记通过；见2026-10-15-source-user-steer-projection.md。
- Async retained Conversation/Decisions新批已通过必要统一验证：原序reservation/16容量、完整HTTP/WS增量prefix及IDs、firstdelta与完整history提交分离、原user/review/image交付proof、取消/usage/实际旧generation释放，以及仅telemetry比较、真实raw provider来源和不采信wire TrustedTool marker。Root另补实际HTTP→冻结工具请求→中央cache审批→真实用户撤销→同步新证据拒绝/旧HTTP不得commit的全链节点并通过。首批180中165pass/15fail（14新测试误读float、1误把网络首达当admission最早）；仅受影响61节点复跑全过，按最新节点最终180/0，原失败保留且不与旧112/297相加。见`2026-10-15-source-guardian-retained-decisions.md`；官方账号/hosted history排除。
- Guardian资源分发实际根因已修：pyproject原未声明独立guardian子包resources，现补完整策略/提示词/Node policy/Notice/LICENSE及llm metadata Notice。独立冻结源码构建真实wheel、检查8项资源并以包内路径启动新的Python导入，build/import均exit0，11 Source slug和未知model无host证明核对通过，无依赖安装/外部请求/普通用户profile操作。见`2026-10-15-guardian-package-resources.md`，不据此称完整桌面安装已验收。
- 代码导航grammar分发另已补齐：普通依赖和sidecar lock原只含tree-sitter/Bash/PowerShell，未包含已实现的JS/TS/TSX/Go/Rust/Java grammar。现将5个固定wheel加到两份真实依赖并安装项目bundled runtime，不改全局Python或parser/fallback。用该interpreter统一复验既有optional-tool文件37/37、0 fail/error/skip，原19个skip节点本次全部实际执行；6种语言真实模块路径/ABI已记录。原382节点冻结批保持历史363+19，不直接累加；非Source Starlark/execpolicy，也未声称重建完整安装包。见2026-10-15-code-navigation-grammar-distribution.md。
- 项目排序生产批已齐：PROJECT_ORDER、各项目occurrence和Recents各自持久，共享Source手动/最近活动模式；真实Source ha/xIe/r7图标已取代近似lucide，字值保持。后端3文件29/29，前端与follower/Root状态清理统一11文件312/312、0失败/跳过；不是此前设置批同名312，范围不相加。protocol-sync首轮发现后端turn.settings.update遗漏前端声明，Root已按真实handler补required conversation_id/turn_id与model/effort typed DTO、payload union及命令集合，无新按钮；等待只复验protocol-sync和一次TSC/Vite后执行三窗/排序各自实机。结果尚未产生前不记桌面通过。
- 项目排序实机已收齐6个去重场景：run04前5项与同原profile cold-final1项，真实菜单/3份Source SVG、鼠标/ESC/Alt、项目/Recents/全局Pinned独立槽位、标题顺序/MRU字节、新建prepend、冷恢复和原草稿/实际上传均验证。后续A→B真实main补图只完善原第6项，不计第7场景；原失败verdict保留。Root另查看最终菜单和拖动图。详见2026-10-15-source-project-occurrence-and-workspace-order.md。其冻结构建index-CLFATioc.js，index SHA256 8c5a4247adc6e05c7a3675443be8c8732e2c4147872bfe5b12af22e5200a78b4；protocol-sync、TSC/Vite和bundle预算均已通过。额外debt初查direct lucide91/硬编码motion2，Root已在source-only尾批收为85/0，保持原glyph与150ms ease-out/160ms ease数值，待本次真实缺陷修复一起最终构建，不冒充旧图已运行尾批。
- 三窗follower run04抓到真实投影缺陷，尚无这五例通过结论：后端真实read_file完成，原窗/observer均收正确本轮message/turn/source seq；前端applyActiveStreamSnapshot却把新user之前旧partial/run_in_progress复活，再由run-start/resume的other-stream guards挡住新assistant，实际过程与最终回复未呈现。旧partial是原进程checkpoint，不是后端绑错新turn。现按当前已admitted user边界、真实started/resume身份和同事务streaming归属修完整链，旧partial/duration/termination_reason/工具与阅读证据保持；仅补必要链测试与受影响实机。terminal source_event_seq为现有可选字段，按真实owner/status和字段保留验证，不为QA新假设扩成必填或改backend cleanup。
- Source原生命令策略仍有明确核心缺口：无Starlark `.rules`/PrefixRule producer和持久prefix amendment；`auto_review.ignore_rules`已用于live模型准入的分类比较，但尚未在命令执行中过滤Allow PrefixRule。普通safe AUTO/Skip不因required_on_models强审符合Source，不能自创“全部安全命令复审”。旧审计中的两个live发布入口缺口已经由下面批次补齐，不再列为未实现；精确审计及caller更正见`2026-10-15-source-exec-policy-live-admission-audit.md`。
- execpolicy已确认真实编译可行性：PATH无cargo不等于无工具链，现实际核实用户.rustup/toolchains的Rust/cargo1.95.0与Source rust-toolchain相同，VS2022 C++可用且registry缓存存在。正在准备固定commit的execpolicy/absolute-path两crate独立helper，真正Starlark0.14.2及Source parser/filter/environment/amendment语义、license/source hashes和打包路径。未接共享consumer前不影响当前冻结QA，也不以helper准备宣称完整规则链完成；禁止.tmp运行依赖、regex/Python eval冒充和自创safe Skip强审。
- 真正Starlark低层native已在本机locked/offline编译并通过两crate原Source测试和14项bridge用例，原始batch03日志保留。原文件/Apache/NOTICE/逐文件SHA已固化；覆盖函数/循环/f-string、token候选、examples定位、仅Allow Prefix过滤保留network/host、后置环境overlay与Source amendment/file.lock。Source shell/bash/PowerShell AST及危险分类正接独立helper后统一验证；尚未接产品consumer或做最终release资源验证，不能宣称整个规则链已实现。共享WS/agent_runner继续冻结。
- Root已核实保存ASAR shared `Ddn`完整类：普通模型/effort经`thread/settings/update`保存下一回合默认；该类仅active approvalsReviewer会再发`turn/settings/update`。据此修正常`llm.model.set`原无条件live publish差异，busy选择仅存next-turn；显式WS/extension live统一按Source feature、原task身份、最新requirements与真实metadata准入。无新增前端开关。6文件统一62/62及必要task replacement、filesystem窄复验按节点最新去重最终73/73，0 fail/error/skip；bootstrap误捕获stream子Task的真实问题已修为QueryEngine原model_owner_task。11个精确Source slug的host-only typed proof不可从extra伪造，未知模型按fallback，step flag默认false且内部隐藏。
- 模型批实机已完成：production-model-selection-next-turn-run02实际3/3，真实busy前端action/WS ACK存B，原Composer steer和真实tool_exec仍A，新user turn才用B；production-model-selection-button-companion01另补1/1实际鼠标合一按钮→模型菜单→B，并保留原草稿。errors=[]/exit0，六张图均由负责agent查看；不把局部通过当完整桌面等价。首轮fresh WS steer暴露的跨窗口失败保留且交上述follower批，不重解释成模型切换通过。见`2026-10-15-source-model-selection-and-live-activation.md`。
- 设置终端 Source `main::Y8/X8/Z8/Q8/Gqe/Kqe` 的真实PATH发现、偏好producer→IPC→PTY argv→cold已修，取消自创PowerShell启动包装，保留真实pwsh/system意图。实际自然退出仍残留PseudoConsole的资源漏也已完整修；28项真实Electron验证通过（PowerShell Unicode/Cmd/GitBash实际PTY，WSL guest未运行不计通过）。没有摆不存在的Windows/WSL agent driver。见`2026-10-14-terminal-settings-source.md`。
- 浏览器真实共享设置链已修：宿主durable save后向实际主窗口广播完整下载/站点权限，真实preload与设置页/站点面板订阅，单origin和全部清除也发布，旧读/迟到ACK不覆盖后提交状态。四个desktop文件统一54项通过；前端含于312项；真实`after06`验证第二主窗口live policy、真实public origin权限grant/clear、两份草稿与cold恢复。范围重叠不相加，见`2026-10-14-browser-settings-sync.md`。
- Source默认权限开关确为checked/disabled true，Full access缺省true也与Source一致；开启前`go/vo`确认、取消不写、关闭立即隐藏菜单，不切换当前任务权限。通知`ylc/clc`已接真实可见请求抑制、semantic成功而非运输ACK的解决收起、原生窗口focus与原conversation/turn/message/task归属；native50项通过，前端包含于312项。Source completion/heartbeat/隐藏与内部thread筛选、OS真实投递/快捷回复未全部闭合，见`2026-10-14-notifications-source.md`。
- 312项统一前端与TypeScript/Vite已通过，首轮10项旧fixture及首轮tsc类型遗漏、各实际QA失败均保留。Source Antigravity使用实际品牌asset而非cmd齿轮/两份generic exe；producer顺序只把System放到Source核心editor后的固定slot，不kind排序后续factory，内置MiniCode editor例外保留。最新open-targets9项与真实Electron IPC顺序/A品牌通过，见`2026-10-14-antigravity-open-target-icon.md`、`2026-10-14-open-target-source-order.md`。
- 同步Guardian/ThreadOwned新批已闭合：真实comp_hash→native issuing window，缺失保持None；Stable默认true不新增桌面开关；最新raw Compaction/ContextCompaction、同次history rewrite、真实reviewer seed、policy/root/reset/model失效、fork/一次原deadline恢复、typed budget/工具与finalSchema、旧schema/真实backup cold、nativeAgentMessage原位发送或omission、真实worker origin/cold角色均验证。最终按节点最新326 passed / 0 failed/error/skipped，真实两个HTTP compact→cold→Guardian QueryEngine请求通过，bundled Python实际导入/typed origin smoke也通过。官方解密及来源缺失的原生服务项不猜身份，仍not_run；Async V2/增强工具尚缺，见`2026-10-14-source-guardian-checkpoints.md`。

- MCP 会话/永久批准已通过真实独立 Electron + WS + stdio MCP + 冷进程重启验证，分别见 `2026-10-14-mcp-session-approval.md`、`2026-10-14-mcp-persistent-approval.md`。
- 原生 `tool_search`、完整 discovery 元数据、Code Mode 原生媒体/预算、私有 journal 与冷恢复本批按节点最新去重：363 passed / 0 failed / 0 error / 19 skipped。19 项是未安装可选语法包，不计通过；见 `2026-10-14-source-native-harness-fixtures.md`。不把重叠历史测试数相加。
- 实际修复 SDK search object 的 JSON 归一化、MCP wire name 与授权 identity 混用、Code Mode 绕过 canonical schema 读取，以及首次切换页面同步读取私有 context 的阻塞。新 Notes 接入修改了公共模型/注册表边界，正在做其必要统一回归，不能沿用上面通过数宣称新部分通过。
- 侧栏 occurrence、Source 菜单/Copy/焦点，以及 pin / 显式 read-unread / project move 已通过真实最新桌面验证。153 个前端节点通过；四场景核对冷启动顺序/未读、两份草稿、上传附件原归属与实际执行 cwd。见 `2026-10-14-sidebar-pin-read-project.md`。
- 最新主/子 agent 投影真实生产任务 `production-projection-final-run05` 八项判据通过：实际文件/命令/编辑/diff、组合高光、主/子折叠、真实错误详情、运输包装隐藏、命令间距、手动阅读与默认最终收起、冷重启。两轮实际主任务完成，没有全局 error；Source unit-list 及读取/编辑单复数文案已按原图/ASAR 校正。此项不代表所有桌面行为都对齐。
- History/Notes 9 个 Source 私有服务工具、可信 auth/transport、namespace 原生 body、private journal/冷恢复、每窗口 hint 与 legacy bridge 已接入。最终相关统一批次按节点取最新值：193 passed / 0 failed / 0 error / 0 skipped；包含独立 producer 的61项、public SDK/真实HTTP/身份/冷恢复和必要诊断回归，不和前面的363等重叠范围相加。没有本地自创笔记替代服务，没有可用官方账号的线上实测。见 `2026-10-14-source-history-notes-service.md` 及 `2026-10-14-source-history-notes-sdk-chain.md`。
- 本批根因完整衔接：私有工具保持真实 `/root` 逻辑名称；服务与 Responses 使用同一 rollout session、window UUID、window_number 与 history_ingest_requested；重复 namespace done 不重执行；Source config 更新不被旧 activation seed/registry 重启；Hook 接收剥图片后的真实 JSON；SDK、Inspector、最终 item/done 与预算标签使用同一可信私有归因。原始记录、普通同名读取、usage 和执行证据保留。
- 该历史投影批 bundled Python 实际导入与 smoke 通过：9 个原生操作、真实逻辑子名称、私有 raw 不改、普通 trace 保留。该次主/子过程验收使用 `index-DAAdef6e.js`；当前设置构建以最新批段落及对应报告为准，不将旧图称为当前构建。

历史剩余项记录（已被本文件顶部最新验收逐项更新，不能继续据此派发已完成工作）：当时四类为Source原生命令策略/规则持久化；真实增强Node工具与第三方provider专用transport边界；远程执行环境/高级审阅；桌面剩余控件。其中原生命令策略、follower实时过程/原owner控制、项目内手动顺序/工作区标题顺序已经有后续实际完成证据。远程执行和高级审阅不能只因Rust中存在API就增加桌面入口，须再核实际桌面调用与可用本地后端；官方账号及其hosted history/billing/解密线上服务按用户要求排除。当前续做项以顶部最新段落与各自报告为准。全量等价尚未完成，不给推测百分比或工时。

以下为历史批次记录，状态以最新段落及其最新报告为准。

## 此前 MCP / Skill / Prompt 批次

- 本轮最新测试按 classname + case 去重：398 passed / 0 failed / 0 error / 0 skipped。见 `output/mcp-skill-prompt-20261014-latest-results.json`，不和历史 341/518/335 等重叠批次数相加。
- MCP：SDK 原生 stdio → proxy → frozen authorization → CodeVM 已实际运行；完整 content/structuredContent/isError/未知内容块保留，仅顶层私有 `_meta` 不给脚本。公开 WS 不携带整份大结果。模型使用独立已预算的 `mcp_output`，Source wall-time/header、模型 policy、per-tool tighter limit、20% ceil allowance、图片/音频/加密项、冷恢复/上下文计数均接入。
- MCP 审批：Auto 三态 annotations、Writes/Prompt/Approve、实际 issuing profile 下的 Never、strict review 优先、真实 SDK query 的执行和拒绝链均已通过。不能把仍缺的 MCP 会话/永久批准记忆算已对齐。
- Skill/Prompt：owner config stack、enable/disable、product、真实 Agent Plugins v1/direct children、8,000 UTF-8 bytes、完整 Source 使用规则/路径别名、captured 模板与合作模式、空模板、unknown descriptor 的官方本地 prompt 已接入；bootstrap 每回合更新 manager 与 readable roots。
- 最新独立 Electron `production-guardian-run05`：4 场景通过，7 张 PNG，0 page errors。真实 WS/repository/file I/O、原草稿保留；模型 HTTP 受控。测试没有操作用户正常窗口。
- `av==17.1.0` 已加入 pyproject 与 sidecar lock，并已安装到当前 bundled Python；bundled runtime 的实际 native-body import / decode smoke 通过。
- 详细记录：`2026-10-14-source-mcp-skill-prompt-integration.md`。

## 历史待办快照（不可直接作为当前未完成列表）

当时记录：MCP Source 永久批准、完整 discovery/search 元数据与原生 tool_search；Code Mode 媒体 detail/完整 Source 输出契约；TokenBudget Notes 服务；Guardian ThreadOwned checkpoint/async V2/增强工具；remote execution environments；高级 review 和桌面剩余控件。后续已完成项及账号排除范围以上方最新段落和各自报告为准，这份旧快照不得继续当作未完成清单。未完成全量等价，不提供推测完成率。

### 后续批：MCP 会话批准已验证

- Source Auto 以 raw server/plugin/tool 身份记住，独立于参数；Prompt/Writes 不记住。只有真实批准结果、原请求校验完成后才写入 thread-owned store；strict grant review、显式 ask/deny 不被缓存降级。SDK AgentSession 与 WS conversation 使用各自真实生命周期；其他 conversation 不借用。
- 后端 scope 最新 case 去重：115 passed；新 Source MCP producer 全文件 51 passed。前端 4 文件最新 121 passed（其中 InlineAgentPrompt 最新 51），tsc / Vite passed。首轮夹具 abstract method、手工 strict flag、accessible-name 失败保留。
- `production-mcp-session-run01`：真实 Electron + WS + stdio MCP + 文件 I/O；模型 HTTP 受控。2 场景 passed，4 张 PNG，0 page errors；一次对话批准后，同回合换参数及下一回合共 4 次调用真实执行，只有 1 个审批请求。未操作用户正常窗口。
- Hosted Apps 缓存需要真实 trusted connector/account producer；不从任意 MCP `_meta` 伪造该权限。永久批准尚未实现。
- 正在后续验证标题/outputSchema/nullable root properties 的发现和调用；不能沿用前一批通过数宣称这部分已经通过。

以下是先前阶段的历史记录，其“正在实现/未验证”不代表上述本批的最新状态；历史失败及限制保留作审计。

用户已授权重构、多 agent、继续补齐；不用 lean-mode，不写防御性兼容，不动用户正常窗口／草稿／历史。

## 已验证

- `request_permissions` producer、冻结审批／subset、turn/session/environment grant、command/patch consumer；Source feature 默认关闭。
- 权限/审批/hook Python 批次命令两次：首轮有 explicit static ask 未保留和测试 monkeypatch teardown 问题，均修复；第二轮 exit 0。对应 8 文件：test_codex_request_permissions、test_permission_grant_control_chain、test_codex_command_hook_owner、test_permission_mode_switch_approvals、test_approval_turn_deadline、test_tool_agent_approval_boundary、test_approval_channel_required、test_hooks_contract。
- 前端 5 文件 78 项通过，tsc 与 Vite 构建通过（旧 bundle/dynamic import/Browserslist 警告仍有）。随后 permission path badge 按 ASAR rr 改成仅提示完整路径的静态 span；尚需这处最新 DOM 验证／最终构建。
- 真实独立 offscreen Electron `production-authority-run05`：4 场景通过、5 PNG，无 page errors。真实 WS/repo/file I/O，仅模型 HTTP 受控。会话授权跨回合消费、回合授权消费、批准后实际 outside patch、回合结束实际拒绝 outside patch；草稿恢复。run01–04 失败保留，其中 run02 发现并修正真实 producer `projection_visibility='silent'` 不支持，应为已有 `debug`；run03/04 为 fixture selector／wire join 断言错误。
- Source 角色与模型请求：最新 case 去重 335 passed / 0 failed / 0 skipped。见 `output/source-roles-20261014-results.json` 和 `2026-10-14-codex-source-roles-and-model-requests.md`。

## 正在实现／尚未整体验证

### Guardian

- `/root/permissions_grant` 已完成独立 `backend/agent/guardian/` contract/retained/action/policy/prompt/config/runtime/pool/service 和 `test_codex_guardian.py`，未运行统一测试。
- 正在按实际 ASAR 实现 Guardian 前端／typed wire／stream replay：in_progress 组合、approved 隐藏、denied/timed_out/aborted 真实独立记录与对应 exec/patch/MCP 详情；private reviewer 不冒充普通子 agent。普通 guardian_warning 无 UI，仅真正 circuit breaker interruption 显示 Source 中性说明。
- Source schema wire name `codex_output_schema`，Guardian strict false（普通 true）；`ModelRequestPolicy` / OpenAI 完整消费已由 source_roles 实现，Root已接 Anthropic普通stream到已有 output_config.format（API无该strict位，需记录差异）。
- Root新增 `guardian/host.py` per-conversation pool owner；AgentSession/RunContext/WS按会话借用。SDK session close与WS shutdown关闭pool。
- ContextBuilder新 `guardian_evidence`、clone深复制、snapshot保存/恢复、clear reset；旧无证据 snapshot 有 history 时标 incomplete，不从模型文本补授权。
- TurnIterationRuntime只从真实 boundary_input 录原输入，和既有 admission snapshot/rollback 同链；agent/内部任务来源归 assistant（retained已按Source处理）。ContextBuilder仅model_final/model_commentary录原assistant；ControlToolRouter录原question/选项与真实answer配对。
- **仍必须核对／完成**：WS验证响应接受时的原始 acceptance order（当前只router消费时record，可能落后于steer）；实际 assistant delivered/revision；stale rollback/reset版本单调；per-conversation live evidence与父子 root snapshot传递完整；真实 manual approval 对先前Rejected精确action的developer证据；持久 replay/history reset/fork/compaction一致。勿把这些算完成。
- Root `guardian/tool_approval.py` 接最终 frozen command/patch/MCP→service。execute_serial在Guardian返回None才原human请求，不发假awaiting user span。拒绝是status blocked/error_kind approval_rejected；Abort status cancelled/error_kind approval_aborted。
- RequestPermissionsTool normalized profile→Guardian，批准完整request/turn/strict false；stdin PermHook后、manual前也接Guardian。
- agent_runner scoped/persistence white-list已加guardian_assessment/guardian_warning；permission agent正在 stream_state/协议/factories/前端，若其它agent_runner持久处理缺口需整合。
- 明确尚缺Source ThreadOwned/decryptable parent checkpoint复用、GuardianV2 async、optional conversation history/NodeREPL增强；当前模块为 SourceIndependent history，不假称全部覆盖。

### stdin / commands

- `/root/source_token_context_tools` 正在收敛 `terminal/stdin_authority.py`, manager, command_support, Monitor，Source launch policy/当前策略、same instance lock/check、PermHook+原waiter/digest、原Bash completion once已落盘。
- Root core已映射command PreToolUse为Bash/{command}，hook更新只改command；普通Monitor status/write不重复Pre；terminal_hook.ready false抑制Post，true用原id/args/raw output；foreground优先 runtime_metadata.hook_output。
- permissions agent之前已加background authority、两 foreground hook_output、explicit background terminal_hook.ready=false。
- stdin agent正在补写后Source真实输出/poll/yield，不只Wrote bytes；空poll/pipe Ctrl-C，以及所有transport结果hook抑制。
- 最新真实根因：普通非0完成原来 is_error/status failed，进入ToolIssue/harness错误预算并建议换方法；Source logging/output success true。stdin agent已获独占 command_tool.py foreground正常返回，把正常完成改is_error false/status success，保留exit/process status/stderr；真实sandbox/timeout/runtime失败不改。tests计划 `test_codex_stdin_approval.py`，统一验证未跑。

### granular

- `/root/source_roles` 已完成上一批角色，正在独立 granular TOML/wire→typed parser→PermissionContext/checker/WS/producer。
- 有Source依据的五位：sandbox_approval/rules/mcp_elicitations必填bool，skill_approval/request_permissions缺省false；MCPtool和network只Never禁，不伪归rules/sandbox；当前Source没有 skill_approval producer，不自创。
- 已授权它改 config/requirements/layers/profiles、checker/build_context 与PermissionContext granular区、WS permission_runtime projection、MCP registry request_owner origin live policy＋bootstrap.app elicitation gate；Never空properties form特例；Frontend approval_policy union字段/Inspector须与Guardian agent协调。

## 验证／工作环境

- 用户正常MiniCode之前PID31160 / Roaming minicode-desktop，未操作。所有QA临时profile/workspace。
- 最新frontend已构建权限DOM前版本，可称 build15；Guardian前端／静态path badge尚需最终编译，不能沿旧图宣称当前整体验证。
- QA脚本 `output/playwright/codex-control-audit-20261013/permission-authority-desktop-qa.cjs`；只调用production createConversation action做fixture bootstrap，其余可见UI、真实outbox/WS，非setState。
- `.tmp/codex-src` commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`，installed ASAR26.1007.2314.0。read helper已提取pending-request-item-panel、permission-request-model、agent-activity-item等。
- 新offscreen profile未初始化Windows native sandbox accounts，git prompt snapshot有真实 unavailable warning；不能据这份QA称受限ConPTY/Guardian OS只读实测通过。未用unsafe fallback。
- 工具：直接node已安装入口，npm/npx wrapper旧异常不重装；已读playwright skill（npx存在），桌面是既有Electron API独立脚本，未转写新Playwright specs。
- 仍须继续其它已明确缺口：高级review、TokenBudget外围/Notes/auth/hooks、V2 aliases/nesting、remote exec environments、桌面剩余控件；无可靠完成百分比，不按测试数或1063静态节点算完成率。
