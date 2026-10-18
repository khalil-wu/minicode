# Git、独立审查、设置与窗口外观收尾

本批依据实际 Codex 桌面 `26.1013.4894.0` 资源与 `.tmp/codex-src` 的 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。官方账号及其依赖服务按用户要求排除，旧 CI 不代替本批验证。

## 实际变化

| 范围 | 结果 |
| --- | --- |
| Git 设置 | 分支前缀用于实际自动建分支；推送明确携带 `force_with_lease`；草稿 PR 偏好和合并/压缩合并选项进入现有明确操作，使用 Source 的并排按钮。保存设置不会执行 Git。 |
| Git 文案 | 提交/PR 指令进入各自无工具的 `side_query`，不塞进主 agent 提示词。异步生成保留原会话、执行器、分支与后续编辑的归属。迟到的设置 GET 不覆盖已确认保存的新选择。 |
| 独立审查 | `/review` 可在当前聊天或独立真实会话执行，沿原执行器、模型、权限和正常输入准入，保留源草稿与附件。原始 slash 的上下文和用户限定条件不再在转交时丢失，也不改变源持久输入的 owner。 |
| 工作树 | 单独持久化创建时的分支，后续切分支或修改前缀不会改变清理对象。旧记录按原注册、准入 prompt 和对应 run 升级，恢复与冷删除复用准确归属；没有原证据时保留资源，不能猜分支删除。 |
| 自定义指令 | 改成独立编辑页，显式保存后返回、取消放弃、返回确认脏内容；隐藏或切换设置保留草稿。继续写真实 `INSTRUCTIONS.md`，不虚构 hosted memory。 |
| 窗口外观 | Windows Mica/不透明状态与 macOS vibrancy 分支接入主进程，高级选项控制半透明侧栏和实际对比度。保留 MiniCode 原字体；macOS 不以 Windows 结果冒称实机通过。 |

Source 片段及消费者见 `output/git-settings-20261018/{source-contracts,review-delivery-source-contracts,review-pane-behavior-contract}.json`。Source inline review 会显式打开 Changes；detached 先导航新聊天，不能根据 inline 给 detached 强加开面板。

## 真实桌面与模型

独立生产 Electron 经 `/review` 菜单启动 `6.1 Sol / 高`。模型实际读文件、运行命令，指出小仓库 `pricing.py:2` 将折扣变成加价的错误，Changes 对应行显示完整评论。最终答案后过程自动收起、停止按钮消失；切回源会话仍有 `/review` 草稿且历史为空。记录为 `output/playwright/desktop-alignment-finish-20261018/review-ui-receipt.json`，截图为 `review-detached-comment.png`。

该次模型把完整说明放在代码评论，正文只有简短引导，没有完全遵守 Source 提示词的 prose 信息量要求；未另造策略补写正文。部分 Playwright 指针操作命中隐藏控件超时，后续导航使用已观察的可见控件 DOM click，仍走真实 handler，但不算指针命中区域通过。原生目录 picker 自动化的旧失败独立保留，不直接归因产品。

最终前端入口为 `assets/index-Bcb9YBtH.js`，CSS 为 `index-LmCT3jvV.css`。自有 primary 验收页重载后核对新入口、原 `/review` 草稿、`supportsTranslucency=true / opaque=false`，字体仍为原字体族、15px、400；见 `latest-frontend-reload-state-restored.txt`。透明开关、对比度 45→46 和指令保存由同会话前段操作留证。正确原生重拍见 `output/native-cua-appearance-recheck-20261018/`；首次遮挡截图已撤回。

被用户采用的窗口和对话没有被本轮关闭、清空或迁移，原 `data/` 未修改或纳入提交。运行中的 backend 未为最后两项修复被重启，不能声称旧进程加载了新模块。真实双 agent 编码及 CUA 另见[编码消费者](2026-10-18-real-coding-consumer.md)和[CUA 消费者](2026-10-17-installed-cua-source-consumer.md)。

## 集中验证

一批实现完成后集中验证，最后仅重跑受影响节点，各行不能相加为全产品数；首轮失败和环境 setup 错误保留。

| 范围 | 结果与记录 |
| --- | --- |
| Git 设置保存与迟到读取 | 2/2；`git-race-final.{log,xml}` |
| 原执行器独立 review、两种工作树冷清理、已接受邮箱后续任务 | 4/4；`backend-final-recheck.{log,xml}`。最后状态参数修正后，仅复验两个受影响工作树节点，2/2，`worktree-upgrade-final.{log,xml}`，替代相同节点而不重复累计。 |
| 独立 review 输入框 | 1 个精确节点通过；`review-frontend.{log,xml}`，过滤未执行项不算通过。 |
| 桌面 capability 与生命周期 | 21/21；`desktop-contracts.log` |
| 最终前端 | TSC、Vite build 和两项预算通过；`build-final.log`、`budgets-final-recheck.log` |

相对记录均位于 `output/playwright/desktop-alignment-finish-20261018/`。更早集中验证及必要复验保留，不宣称重跑全部产品测试。GitHub CI 以本轮提交对应 run 为准。大型编码/长时断网、普通 WSL、WFP 流量矩阵、私有 MCP relay、主题分发来源和全界面逐状态对照仍见[当前差距](2026-10-16-current-desktop-gaps.md)，不声称全产品等价。

## 首次 CI 发现与修正

`1972157d` 的 [CI 39122516570](https://github.com/khalil-wu/minicode/actions/runs/39122516570) 为 6 success / 3 failure，原日志保留。前端 8 个失败来自两个旧设置测试文件：编辑器高级区域改为准确定位；删除 5 个已移除来源面板或保存中强行输入的过时用例，不用 skip/todo 掩盖。来源变化的真实 `guidelines.updated`/Inspector 链保留原覆盖，独立编辑页的 ACK、禁用、返回和草稿竞态已有专项覆盖。root 的旧 review fixture 改用真实工作区路径接口，并明确提供本地 Git 执行上下文。

backend 两个失败还暴露了实际遗漏：工作树获取后、注册未成功时的退休记录没有写入原创建分支。现已在该原始调用点传入捕获的 `temporary_branch`，不从当前分支或新前缀推断。两个 owner/epoch × 两种已发布状态共四个既有节点复验通过。前端三个受影响文件 14/14、root 原失败节点 1/1；分别见 `ci-acquisition-owner-final`、`ci-settings-final`、`ci-root-fixture-final` 的日志/XML。不因前两类是旧用例就忽略真实后端失败。

生产前端没有在本次 CI 修正中变化，不重复构建。`1972157d` 的最新完整前后端窗口另行冷启动并核对 Git 设置实际消费者，见 `output/playwright/alignment-final-1972157d-20261018/boot-receipt.json`；原用户窗口保留。之后的一行后端修复仍需由新进程加载，不能借该冷启动记录冒称已经加载。
