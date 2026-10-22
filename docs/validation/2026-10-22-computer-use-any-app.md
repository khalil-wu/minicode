# Computer use 的真实 Any App 插件消费者

当前 Desktop `26.1016.6573.0` 的 `computer-use-settings-11cbb99c5129.js::Hr` 从实际 host/plugin 和 local marketplace inventory 构建控制行。`qr` 精确比较 `plugin.id`；导入常量 `N` 对应 `app-shared-9cc135f1bdc1.js::k8e`，值为 **`computer-use@openai-bundled`**。能力过滤的短名判断不是控制行的身份匹配，其他来源的同名插件不能冒充这条记录。

`settings-plugin-controls-281db1383c9b.js::Et` 的实际形态是标题/图标进入插件详情、未安装时 Install、已安装时启停开关。Any App 没有单独的 Manage 按钮或删除菜单；本批不加这些控件。

## 实际调用链

`computer_use_controls.py` 读取同一 workspace config stack 的真实 `PluginManager` inventory、已注册且已 materialize 的 `MarketplaceRegistry` 记录。它仅接受上述完整身份，保留实际安装、启用、策略、加载错误、名称与可用图标。没有记录就没有控制行；关闭 plugins 特性不保留旧行。本地 logo 使用实际 installed plugin path、icon variant 和主题信息，通过既有带授权的 `/api/plugins/asset` helper 加载；未安装的目录记录不伪造已安装路径或图标。Install 等待期间保留原文字与现有 spinner，安装和启停复用原插件界面的300秒HTTP期限，不加重试。

provider 已知且本机可用时，`computer_use_settings.py` 把该真实 control 记录和来源版本纳入 `selection_id`，供 `ComputerUseSettings` 呈现。安装/启停通过 `POST /api/settings/computer-use/controls/plugin`；原 session、conversation、workspace、provider、selection 以及当前 control 权限须保持一致。请求不接受用户指定任意 plugin ID 或市场。

原控制动作调用已有 `routes_skills.install_plugin_api` 或 `update_plugin_state_api`，继续使用原配置 hook、受管来源、安装事务和 `_refresh_plugin_runtime_state`。磁盘发现通过既有异步 IO 边界执行；回执先完成真实 mutation 和 runtime refresh，再返回 fresh settings。`runtime_refresh` 的实际失败信息不改成成功，前端保留已保存状态与错误。

详情复用现有 PluginsTab。未安装的 requested canonical ID 从真实 marketplace catalog 打开，已安装的从真实 installed inventory 打开；均不自动安装或启用。草稿、附件和其他会话不受设置操作影响。

## Plugin CUA 执行链

本地插件仅对明确声明 `cua_repl` 且启用 `computer` surface 的server保留canonical key，并携真实 `plugin_id/plugin_server_name` 与manifest路径；普通插件的前缀和元数据不变，manual声明覆盖同名插件声明，多插件沿原顺序选首项。host只offline登记，真正SDK turn捕获receiving lease后再admit；MCP policy先检查原声明，environment policy使用 `runtime.cua_repl_config` 检查原command/args，deny继续生效。生成builtin后仍保留原plugin审批target和独立companion身份。

只读 `plugin_cua_declaration` 可以读取disabled plugin原manifest，不建立连接，不把server/policy disabled改为true。插件启停与provider readiness分开，禁用后可从真实声明继续管理，再启用恢复同一实际声明。当前实际Source computer-use bundle没有MCP声明；本批不为它虚构server，也不自动提供私有runtime。已验证configured/plugin两种origin的完整生产体SDK启动与原turn清理，使用受控OS facade，不操作真实桌面。

## 范围

此处的插件安装/启停消费者不意味着官方插件的 Skill、安装 runtime 或 API 已与 MiniCode 自有 `minicode-win32` 完全兼容。配置来源与 companion 执行准入、receiving lease、原 turn/thread 关闭必须分别由生产执行链证明，不能通过相同 plugin ID 推导。

Office 的 `setAppEnabled(connector_openai_codex_document_control)` 是独立 connector 授权，不以 plugin enabled 冒充；外部 Chromium extension detection、Install URL、Manage 子页与 `openExtensionSettings` 亦未接通。本批不创建这些空控件。Source Windows 仍无点击声音行。

本批所有运行验证只使用隔离临时 config、真实本地 plugin/marketplace 文件与受控接收端。没有操作用户桌面、现有配置或运行中的用户实例。验证收据如下：前端初批114/114，HTTP期限定向1/1，真实asset/theme/pending定向3/3，后两份筛选未执行的case不计通过或新增覆盖；TS与原UI门槛通过。后端AnyApp最终文件收据 `any-app-backend-final.xml` 为10 passed/1 failed，唯一失败是fixture的SkillLoader旧STATE_ROOT别名仍指向原目录；隔离同一实际owner后该完整安装/停用/再启用链定向1/1通过（`any-app-skill-owner-final.xml`），合为11个不同case最后结果通过，不声称一次11/11。此前合并组因fixture缺必需transport、非法产品enum及本地来源缺`./`的失败收据保留，生产parser、断言和权限均未放宽。companion/原SDK的已过范围不为这些fixture修复重复运行；完整CI以最终提交为准。

## 最终整合收据

共享fixture的owner别名修正后，root以最终输入重新集中验证本文件，**11/11通过、0error/skip**（`output/full-parity-20261021/any-app-final-owner-batch.xml/.log`）。前述失败轮仍保留，不追溯改结果。plugin manifest/policy/provenance 6/6、native及完整生产体SDK25/25通过；它们与早期103个已通过用例有重叠，不累加虚报一次全量运行。前端114项集中与后续asset/pending定向结果按各自收据保留。

最终生产构建及原bundle预算通过，入口 `index-CNWHBEua.js`；原三份入口及hashed assets实际保留，未重载正在运行的用户窗口。新增plugin companion用例已加入Windows harness；本批的完整CI以最终提交自身收据为准。
