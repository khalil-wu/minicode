# 代码主题与 MCP App 媒体上下文

本批继续依据实际 Codex 桌面截图、安装资源和 `.tmp/codex-src` 对齐行为。字体、字号、字重与已有草稿保持原样；官方账号服务不在范围内。

## 代码主题

设置的主题行加入导入、复制及代码主题菜单。菜单使用实际主题三色生成 Aa 预览，区分当前手动修改后的颜色与候选主题颜色，按当前明暗模式筛选。选择主题时一起提交代码主题及界面三色、语义颜色，保留已有对比度和窗口偏好；没有只写下拉框名称。

可用目录为 Codex 中采用的 **15 个开源主题、23 个明暗变体**。逐项比较实际安装资源后，确认完整 colors、TextMate tokenColors、semanticTokenColors 与 `@shikijs/themes 3.23.0` 的 23 个变体一致；3.20.0 只有 21 个一致，因此主题数据使用 3.23.0。实际 Shiki core、JavaScript 引擎和语言包保持对应路径的 3.20.0。种子推导的 23 份期望来自独立提取的 Source 函数，没有用实现本身生成测试期望。

14 个 Codex 定制主题的资源只留在本地审计目录，未把许可不明的安装资源放进仓库。该目录因此不是桌面端完整的 29 主题目录，不宣称已闭合全部主题差距。分发目录包含 `licenses/shiki-themes.txt`。

Markdown 代码块、展开的 Diff 和 Monaco 编辑器消费真实 TextMate 规则。Monaco 保留原模型与编辑语义，仅更换实际语法颜色；同模式改色会更新已挂载的独立 Diff 和搜索替换预览。旧异步结果不能覆盖新的主题或内容，切回没有显式代码主题的模式会恢复原生 tokenizer。代码主题不导入字体，也不应用主题的加粗、斜体等字体样式。

分享格式沿用 `codex-theme-v1:` 加 JSON，支持 Source 的 URI 编码正文。导入校验模式、目录支持、颜色和完整字体结构后，仅应用颜色与代码主题；字体字段按用户约定丢弃。导出包含 `fonts: {code:null,ui:null}`，不公开当前用户字体设置。无效输入禁用提交，应用失败保留输入和真实错误，关闭后恢复触发按钮焦点。

导入保留 `opaqueWindows`，但本批没有新增操作系统半透明窗口能力。Source app Diff caller 对原始语法前景/背景的最终覆盖链仍有未闭合部分；没有用主题数据一致代替全界面像素一致证明。

Source 数据、SHA、符号、版本比较与分享合同：

- `output/code-theme-20261016/report.md`
- `output/code-theme-20261016/theme-data-manifest.json`
- `output/theme-sharing-20261016/source-contract.md`

## MCP App 媒体

此前 App 的 `updateModelContext` 可以接收图片，但 ContextBuilder 把整个上下文 JSON 化后送为文字，像素未进入模型。现在原 owner 下最新的 App 上下文逐块投影到既有图片/PDF 输入链，保留块顺序、URI、结构化内容、模型能力和既有媒体预算。原始上下文不修改；切换到支持图片的模型后仍能消费原像素。

新验证通过真实 stdio MCP App 和 Chat、Responses、Anthropic 三种 HTTP 请求核对图片；另覆盖原生 PDF、最新更新替换、owner 隔离及媒体预算。它不是 Codex 私有 sandbox relay 的实现，也没有把 App 数据当成人类授权。

后端冻结提交为 `2f21babf247e68b75d9706342bb0f0fd495e4502`。两个定向文件 **14 passed、0 skipped、0 failed**。原始结果、HTTP 收据和文件 SHA 位于 `output/source-cua-public-audit-20261016/APP-MEDIA-RESULTS.json` 与 `app-media-context-03.xml`。

## 验证边界

完整本地前端回归为 370 文件、4392 项：4391 passed、1 failed。唯一失败是旧 ToolCallCard mock 没有新增的主题字段；只补测试夹具后该文件 13/13 复验通过。没有把这个组合结果冒称为重新跑过整套。随后实机发现嵌套 Settings 焦点 trap 忽略显式触发器，补齐真实根因后关联 5 文件 25/25 通过，含带父 trap 的导入回归。Monaco 的注释、字符串、HTML script/style 嵌入语言及双模型未保存状态有真实原生 tokenizer 验证；旧异步 Diff 定义不能全局切回旧主题的竞态也有回归。

最终 TSC、Vite、bundle/UI debt 和协议同步通过。入口 `assets/index-WzIWjdjR.js`；index SHA256 `684f9691c65912832d1bf9e6f21d34f920783284f71683328623c41eaa2f9b76`。本地完整结果和后续构建日志在 `output/code-theme-20261016/`；完整 GitHub CI 按最终提交独立核对，收据保存在 `output/ci-theme-media-20261017/`，不借用此前 CI 结果代替。

实际生产 Electron 使用自有 profile、真实后端和受控模型 HTTP。`output/playwright/code-themes-20261017/inspection.json` 保留操作和实际 WS：浅色 8 个候选、深色 Dracula、真实分享串导入、无效提交禁用、13px/400 原字体、代码块同模式更新，以及键盘编辑的真实脏 Monaco 内容均已核对。目录 `12` 之后的截图和 `validation.json` 核对最终入口、重载后的两个主题、活动草稿 `THEME_ACTIVE_DRAFT_KEEP`、隐藏工作区草稿 `THEME_UNSENT_DRAFT`、未保存 `THEME_DIRTY_KEEP` 和导入后回到原触发器。前面的 Dyp 构建验证与最后 Wz 焦点修复后复验分别保留，没有声称所有操作都在同一入口重跑。

首轮隔离窗口的手动双 requestAnimationFrame 截图等待停住，后续改用实际 Electron renderer 截图；原记录保留。一次 QA 读取不存在的 `store.projects.length` 产生的 page error 也保留并明确归因，不记成产品 callback 错误。系统剪贴板没有被此次实机 QA 改写；复制边界由组件验证，不扩大成所有系统 MIME 实测。额外尝试 Git 状态时，自有 profile 没有 Windows 原生沙箱账号，真实请求失败并保留日志；不计为 Git 成功，也未创建系统账号或改变普通用户窗口。

新增后端已另构建 **r14**，此前 r13 保留为历史候选。r14 wheel 大小 16,097,288 bytes，SHA256 `f40c935517136e49b44d633179aa2c56a26918e122ee3180233caf5785ec54a3`，1272 个冻结输入、1251 个 backend 成员。源码、冻结目录、wheel 和全新安装的字节一致，Git 比较仅有 166 项历史 CRLF 差异，无内容差异。新安装原冷验 **41/41**，MCP startup errors=[]；已安装包的新增媒体测试另为 **7/7**。第一次因为生产解释器缺 dev-only pytest-asyncio 而未执行的 7 例仍按失败保留，补私有 QA 插件后才计通过；主机审计 PATH/日志编码错误也单列。见 `output/source-executor-remote-final-wheel-r14-20261016/REPORT.md`。它仍是 Windows backend 候选，不是全产品桌面安装器或 Linux 验收。

公开 Codex 最新 tree 仍没有系统 Computer Use 执行服务；官方 CUA 样例明确使用标准 PyAutoGUI/Playwright，不能把样例当作产品私有 runtime。系统 Computer Use、私有 App relay、外部 SSH/WSL、WFP、长任务/断网和全区域状态验收继续保留为明确边界。
