# Codex 前端投影与后端衔接：2026-10-13 集成核对

依据是当前安装的 Codex 26.1007.2314.0 的只读 ASAR、用户提供的实际截图，以及 `.tmp/codex-src` revision `822e58cc`。MiniCode 保留自己的后端。字体、字号和现有字体风格未调整；本记录不把受控模型验收描述为公网模型或全部服务等同。

## 过程投影

- 活动组合头展示最新真实动作与目标，组头扫描，展开的工具记录静态。结束后恢复编辑、探索、命令的组合摘要；原始记录顺序保留。
- 主过程、子过程、命令详情和活动详情展开使用高度与透明度过渡；组合和详情使用 300 ms、`cubic-bezier(.19,1,.22,1)`，关闭时保留动画所需内容。系统减少动画设置即时生效。
- Tool Result 的 `stdout_preview` / `stderr_preview` / `exit_code` 在事件、journal、主/子历史和 API 中同源保存。空字符串也是有效的已知输出，不能回退到模型运输包装。
- 前端消费真实 typed 流。旧命令快照沿现有 monitor decoder 解码，不通过泛化删词或 CSS 隐藏日志。实际程序输出 `output_bytes`、`next_cursor` 等字面文本仍保留。
- VM 组合输出、代码计算和执行包装不出现在常规过程；实际读取、编辑、命令、独立 VM 错误仍可展开。原始 VM 回执和 artifact 保留。
- 工具失败不增加红色、失败标记或泛化提示；实际 status、原命令、stderr 和退出码保留。完整最终答案自动收起过程；用户主动的阅读选择保留。
- Send / Followup 消费真实 resolved recipient，广播对应实际收件人。Send 只投递；Followup 才启动闲置 agent 的下一轮，不虚构新启动或展示派遣正文。

## 目录与设置

`workspace_root` 表示项目归属，`execution_cwd` 表示实际执行目录。无项目任务由后端按配置基础目录生成 `base/YYYY-MM-DD/slug/`、`outputs/` 和 `work/`；基础目录不能直接成为任务 cwd 或项目归属。会话切换、隐藏发送、附件、预览、权限和终端按同一实际 owner 处理。仅真正生成的 taskdir 进入已有 trust ledger，选择默认基础目录不扩大工具的写入范围。

默认文件打开位置、Shell、关闭确认、通知、语言、网页/本地 URL、完整网址、浏览历史和截图附件均需要实际消费者。General 的完整访问开关仅控制 composer 菜单是否提供该选项，不改变现有任务权限、草稿或附件。语言切换使用 React context，不以 locale 重建编辑器或对话；用户、模型和程序的原文不翻译。语言覆盖与边界另见 `2026-10-13-ui-language-boundary.md`。

## 已完成的验证

| 批次 | 结果 |
| --- | --- |
| 前端目录、恢复、投影和偏好初始集成 | 15 文件首轮 481 通过 / 1 新 fixture 缺少导出；修正后对应 2 文件 10 通过。新增 typed 空输出/真实字面日志回归后该批最终 484 项通过，重复项不累加。 |
| 过程、Markdown、菜单权限与语言消费者 | 10 文件 284 项通过；随后过程语言/真实原文/阅读选择 3 文件 69 项通过。 |
| 生产构建 | run01 因子 agent 行缺少翻译 hook 失败，修正后 run02、run03 与尾轮 run04/run05/run06 的 TypeScript + Vite 完整构建通过。run04 包含最新目录树、语言桥接、浏览历史与侧栏宽度。 |
| 生产 Electron 过程 run03 | 1 个端到端测试、5 个场景、12 张截图；真实后端、VM、工具、子任务、WS、持久化和重启，模型端点受控。全部断言通过，包含 disclosure 距目标小于16px、Git结果无错误；关键运行/最终/子任务/重启图逐张查看，无旧轮Git toast。 |
| 生产 Electron compact run07 | Enter 成功压缩、发送按钮触发真实 HTTP 400 失败；durable ACK、状态行、新草稿、引用、附件及历史保留通过。46 秒。 |
| Git 非交互输入 | 3 个相关文件 114 项通过，包含持有父 stdin 管道的实际 Git 子进程回归。 |

证据均保存在 `output/playwright/codex-rebuild-20261012/`。compact run07 的启动命令用了错误的环境变量名，实际输出先写入 `output/playwright/compact-composer-e2e/`，随后逐文件复制到 `compact-composer/run-07/`；不重新执行来虚增通过项。

## 逐图发现与后续验收

run02 的通过断言并未覆盖所有可见反馈。逐图发现 Git 工作树/暂存区后台读取产生 `Command failed` 红色 toast，durable command result 证实是 Git 等待超时。与 Codex `git-utils/src/git_process.rs` 的 `stdin(Stdio::null())` 对照后，修正 MiniCode 非交互 diff Git 的 stdin，带输入的 Git 操作继续使用 PIPE。后端关闭日志另发现 BufferedReader stdin 被 daemon 持锁；该生命周期问题与目录/Shell批次一起修复，不以隐藏界面解决。

命令短行的 disclosure 原先被 `flex:1` 推到整行右边；已改为紧跟目标，生产 run03 已通过实际几何断言。run02 的这些图片保留为发现问题的证据，不能作为后一修复的最终截图。

尾轮目录/Shell run06 为19项通过；projectless recent producer34项、完整browser raw认证34项、REST旧cwd admission修正后的27项均通过且零跳过，重复范围不能相加。生产设置IPC run04六组服务、八图、两次实际重启通过；loading期间图不作为settled视觉依据，补充区域QA另行记录。

Files 独立目录树及原文件保存、复制路径、添加到聊天、真实执行owner隔离5文件36项通过。浏览预览producer→API→raw签名→parsed内容→owner workbench的相关5文件39项通过。浏览根和执行根独立；原生外部打开目前仅使用配置的默认应用，没有虚构多目标菜单。

侧栏宽度直接核对当前ASAR：左默认340（原saved值保留）；右regular min320/max stage−352，默认按CPt的shell高度/主内容宽度公式计算。移除按tab修改宽度的两个producer与组件层预设；拖动期间只更新内存，松手存ratio；切标签保留比例、resize随界限调整、双击恢复响应默认。相关5文件首轮115/119通过，fixture修正后118/120通过；源公式的max可超过640，最后修正2个旧默认断言后SidebarRight38/38通过，合计范围最终120/120。增加实际pointercapture结束路径后SidebarRight39/39通过，对应范围121项；run05完整构建通过。失败轮完整保留。

正常用户profile启动第一轮因Electron Node环境启动断言退出；第二轮用删除ELECTRON_RUN_AS_NODE键的真实node spawn成功，PID25224。正常backend8000、WS和现有8条会话历史恢复已在日志确认；未清除用户资料。用户物理Esc停止Computer Use，之后没有桌面自动输入，启动成功依据是process/sidecar日志而非屏幕截图。

fresh Files及settled设置区域的尾轮生产验收由两个agent独立profile进行。设置补图run01为高级披露locator同时命中nested details，run02/03采样开栏动画时没有开始真实drag，run04的handle外侧受clip导致聊天区拦截；这些是QA证据问题，不将其描述为产品保存错误。run05采用settled内侧实际命中，要求真实dragging状态后再验松手与重启。

Files前三轮已确认真实tree、parsed文本和signed PNG；SaveAs首轮以Page download事件等待超时，第二轮QA handler误用Electron evaluate无require环境，第三轮真实Chromium下载interrupted、零字节（browser-raw200）。根因是Blob URL在Electron消费前撤销。现已通过owned native SaveFile IPC把原始Uint8Array持有到showSaveDialog与实际fs.writeFile完成；取消返回false，写失败真实拒绝。tree、PreviewPanel、会话导出、偏好导出、Inspector和Markdown图片保存共同使用该路径。web端保留原Blob到pagehide，没有任意延迟或假ACK。前端7文件102项、desktop2文件19项集中验证通过。

fresh Files生产run04-final实际验了native取消、63-byte原text与392-byte原PNG、tree真实两次上传（第一份keep形成已有附件，第二份note新增时保留第一份）、A/B独立草稿、owner/cwd、重启恢复及新session signed PNG。脚本最后误把trust ledger数组当作{roots}，原失败日志保留；依据真实数组契约另行只读final-verification.json通过。只读账本仅实际A/B cwd，base无write trust，recentWorkspaces为空；无renderer错误。08/10截在中央loading不能作为可见恢复证据，同profile实际补录11/12，等待composer可见并核对A附件ready和B独立草稿。

History补图run06发现实际“打开页面”新增visit后，已visited设置页重新active不refetch，原生仅按选中id删导致同URL/日期新visit残留。已修完整chain：active/query/source/owner/pending/删除ACK真实读取并取消旧scope回执；native删除所选记录对应URL+本地日期全部visit，再读取canonical history。前端7项与native7项通过（范围重叠不累加）。生产最小run07退出0、failures=[]，真实打开生成2visit，重开DOM/native一致，分组移除后原生零残留；原draft/ready附件保留，实际process restart恢复right ratio。4张新图加4张未变run06引用图，均已逐图查看；新General下半图含三类通知与闭合高级，其viewport包含断言通过。

左栏拖动内存更新、结束保存、<120折叠保留展开宽度、实际workspace−240渲染界限与双击340补齐。集中2文件59项通过；左右接口合并后另3文件66项通过，重复范围不累加。最终run06完整构建通过。正常用户窗口没有强制重启或刷新，12:01启动时加载run04，最后保存/History/左栏修复将在用户下次正常启动生效。

用户随后指出两条过程摘要间距不同。定位到ThinkingCell的MarkdownRenderer增加.md-body包装，原首尾margin规则只命中wrapper，Markdown段落保留0.6em；PlainText直接margin0。改为重置过程正文实际首尾block边距，并让组合摘要gap6与单项一致，字体字号、最终答案正文、折叠逻辑均保持。此低影响几何修复不新增CSS镜像单测；最新run07-spacing完整TypeScript/Vite构建通过。独立layout fixture直接hydrateMessages→projectMessagesToTurns→ChatTurn/MarkdownRenderer，4种plain/Three.js正文×单项/组合交叉样本在收起/展开/重收起下实际测量：上下正文间隔均8 CSS px，icon/label与末label/arrow均6px，段落首尾margin0，几何一致。run01的网络诊断误判/ws-outbox.ts静态模块为WS请求（几何已通过、无renderer错误），修正pathname后run02 passed=true、errors/network=[]；3张图均查看。这是实际前端组件布局验证，不是后端任务。未执行、失败和跳过不能算通过。
