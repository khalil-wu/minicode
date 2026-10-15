# 模型、思考强度与权限菜单

本批修复模型和思考强度选择的执行链，以及权限菜单的实际可见选项和确认流程。没有修改用户设置、历史、未保存草稿或正常桌面窗口。新生产构建与独立桌面验证由主任务统一执行；本记录不把静态检查或组件测试当成桌面任务通过。

对照版本是已安装 Codex `26.1007.2314` 的 `app.asar`。提取源码在 `output/playwright/codex-control-audit-20261013/codex-open-source/`。参考 checkout 为 `.tmp/codex-src`，本次菜单的实际行为依据安装包的 composer 生产者与设置消费者，不从 shared 枚举推断可见按钮。

修改前核对的是 2026-10-13 20:27:36 的 `frontend/dist/index.html` 指向的 `index-BZz6dTAf.js` 及其真实导入模块。该产物包含四权限选项、固定 `medium` reset 和旧 Full access 确认说明。没有用旧 MiniCode 截图判断现状。Codex 原图 `03-codex-effort-popover.png` / `04-codex-model-menu.png` 捕获于 2026-10-04，用于布局和视觉对照，实际菜单条件采用当前安装包源码。

| 控件 | Codex 实际生产者与消费者 | 本批处理 |
|---|---|---|
| 模型与强度 | `app-primary::X7e → DW → R5e → b8e → p8e/Y6e` | 保留共用按钮；模型切换成功后回强度面板；tooltip 为“选择模型”，不再显示 provider/wire/迭代预算 |
| 选择不同模型 | `R5e` 保留新模型支持的当前 effort，否则使用该模型的 `defaultReasoningEffort` | `llm.model.set → handle_model_command → set_selected_model → _set_selected_provider_model` 原子应用模型和有效强度；不再按旧档位寻找最近支持档 |
| 默认与 reset | `DW` 设置 selection mode `default`，`$Li/Xx` 从推荐 preset 中挑模型与 effort，`b8e/p8e` reset 调同一默认 callback | 使用真实目标项目配置的默认模型与强度，恢复同一个 pair；默认来源写明“项目配置的模型与强度”；不把 `medium` 或列表首项冒充默认 |
| 权限可见菜单 | `permissions-mode-dropdown::vn → yn`（`app-primary::SC` 是该懒加载组件） | 默认组与 Full access；confirm/auto/plan 原始值保留，agent Plan 独立 |
| 权限默认组 | `yn/sn/cn` 把 auto/granular/read-only 归为同一项 | 采用当前 zh-CN “请求批准”；说明“编辑外部文件和使用互联网时始终询问”；默认 hand，Full access shield-alert |
| Full access | `yn::It` 在允许时每次点击都打开 `Ht`，包括已是 Full access；无 skip flag | 每次点击都确认；说明包含文件、命令、互联网三类能力及真实风险说明，取消不写权限 |
| Full access 与 Ultra | `app-primary::qF → Rqe`：builtin Full access、目标 ultra、之前非 ultra | 模型选择、默认恢复、强度选择共用组合确认；“继续”先写真实可用的受限权限，接受后再写强度；关闭取消不写；跨会话等待不会把强度写到新会话 |
| managed requirements | Codex `_Wi/$Ui`、`yn` 的 allowed/pending 条件，sandbox 与 approval policy 是实际权限 tuple | `WebSocketSession._runtime_permission_payload` 提供真实 `available_modes`；前端据此禁用不可选项，不添加空的 guardian/profile 按钮 |

Codex 当前权限菜单通常按默认、条件性“替我批准”、Full access、自定义 profiles、Custom 排序。`tn/Xt` 是另一个 permission selections 契约分支，不能混成固定六项菜单。本批只显示 MiniCode 已有完整消费者的默认组和 Full access。安装包确有独立 read-only 预设，但当前桌面菜单把它归入默认项；因此没有新造 Read-only 按钮。Full access 的橙色应用于整行和 trigger，保留 MiniCode 字体。

当前 Codex zh-CN 的默认标题是“请求批准”（`composer.permissionsDropdown.default.shortLabel` 与 `default.approvalOptionLabel`）；模型 Default 是“默认”，描述“推荐模型集”。MiniCode 没有 Codex 推荐模型 preset/isDefault 契约，本批没有声称支持推荐模型集、跨模型 Power presets、订阅锁定模型或 Fast/service tier 选项。Full access cybersecurity 分支和 guardian reviewer 也没有用假选项模拟。现有权限 tuple 的 allowlist 已接通，完整 Codex permission profile/auto-review 能力仍是明确缺口。

新增模型投影：

- `llm.model.updated` 和 provider capabilities 传 `default_model_selection`（provider、model、reasoning_effort、conversation_id、working_directory）、`model_selection_mode` 和 `available_model_options`。
- `llm.model.set` 接受 `selection_mode: model/default`；模型模式支持显式 effort pair；默认模式由后端读取目标项目配置。
- 成功结果为 `command.result(command: model.set)`；Footer 经原有 awaited outbox 等待语义结果。
- `conversation.model_selection.selection_mode` 随模型/effort 持久化，并在冷读取时保留。
- 默认 metadata 带目标会话与工作区；按钮只使用当前 owner 的默认值。跨项目切换不会借用旧项目默认组合。
- `permission.available_modes` 由真实 `get_config_requirements().resolve_permission_mode` 结果生成，不从 UI 文案猜测授权。

配置 fixture：`output/playwright/codex-control-audit-20261013/model-control-provider-config.json` 是真实 settings JSON，最外层 `llm`，仅含本地受控端点和 fixture 凭据。已通过 `config_helpers.load_llm_settings` 与真实 `ModelRuntime(settings_snapshot=..., provider_configs={})` 解析，catalog error 为 None：

| 模型 | supports | catalog default | 项目配置默认 |
|---|---|---|---|
| audit-model-a | low / medium / high | high | audit-model-a + medium |
| audit-model-b | low / high | low | — |

桌面可据此验证 A(medium)→B 得到 low（旧行为会 high）；A(high)→B 保留 high；reset 恢复 A+medium，而非 A 的 catalog high。受控 provider HTTP 可替换端口；前端、WebSocket、配置、ModelRuntime 和 conversation repository 都使用生产链，不伪造 store/server events。

本批统一验证记录：

- 后端 `model-permission-backend-run01.{log,xml}`：6 个模块 55 项通过，0 跳过，覆盖 live owner、冷选择、pair reset、managed allowlist、provider projection、图像与模型选择串行。
- 前端 `model-permission-frontend-run01.{log,xml}`：DialogService 3、composer-slice 8、sessionEvents 67 共 78 项通过。Footer 初批有两条旧测试失败：固定 medium reset 断言，以及 deferred fixture 未等待异步调用；它们没有被计为通过。
- 修正旧断言后仅重跑 Footer：`model-permission-frontend-run02.{log,xml}` 66 项通过，0 跳过。最终本批 144 个前端场景均已验证；没有重复其他三个已通过模块。
- TypeScript 首轮仅报另一批 CodeReviewMenu/Composer/DiffPanel 尚未添加的九个 i18n 键；没有本批类型错误。统一生产构建仍需再次确认全部源码。
- 最后两处仅修正 inline color 覆盖 stylesheet 以及 permissions 的 accessible label，未新增图标测试或重复整批测试；统一桌面截图将核对。

本批没有运行正常窗口、启动外部应用、点击联网文档或修改用户配置。

实际桌面补证使用 `frontend/tests/e2e/electron-codex-model-permission.spec.ts` 与既有 `offscreen-desktop-entry.cjs`。仅 provider HTTP 回复受控，Desktop、IPC、WebSocket、配置、ModelRuntime、命令结果和 repository 都运行生产代码。第一个任务的 fixture 初始化调用既有 `createConversation` 生产 action，并核对真实 WS/repository 创建结果；之后模型、强度、权限、重命名、新建第二任务与侧栏切换均由 UI 操作。没有调用 `setState` 或注入假 server events，没有使用用户正常 profile 或系统 clipboard。

保留了三轮原记录：

| 轮次 | 实际结果 | 原因与后续 |
|---|---|---|
| `production-model-permission-run01` / build11 | 两个测试在 fixture 初始化处失败；业务场景未计通过 | Node 创建路径返回 `C:\\Temp`，服务端返回同一 Windows 路径的 `C:\\temp` 表示。只修正测试的 Windows 路径比较，用服务端实际 workspace 核默认 metadata |
| `production-model-permission-run02` / build11 | 双模型第一个场景失败，后续跳过；独立 Ultra 三个场景全部通过 | 后端真实返回 A、B 两项且 `models_source=configured`，菜单却只有 A；发现 `provider-models.ts` 只接收 custom 的 live 目录，遗漏 configured/extension 消费者 |
| `production-model-permission-run03` / build12 | 双模型六个场景全部通过；只重跑该测试 | 修复真实 catalog 来源后，完整目录进入菜单。没有把 fixture 伪造为 live，没有重跑已经通过的 Ultra |

catalog 修复只增加后端实际发布的 `configured` / `extension` 权威来源，保留未知、fallback、unavailable 自定义目录只显示当前模型的规则，以及已有 provider 判断、列表合并和去重。`model-catalog-source-run01.{log,xml}` 8 项通过。因此本批最终前端单元场景为 152 项，后端为 55 项；生产 TypeScript/Vite 在主任务统一 build12 中通过。

最终桌面证据为 `production-model-permission-run03/model-defaults` 与 `production-model-permission-run02/ultra-permissions`：九个业务场景通过，两个 verdict 均为 true，pageErrors 为空。每次 warm/cold 检查都确认 native windows `visible=[false]`。24 张新 PNG 附有同名 JSON（真实选择状态、字体、颜色、间距、尺寸），另保留完整 wire、provider requests/responses、repository manifests、renderer/desktop 输出和原始独立 profile。

| 桌面场景 | 实际 provider 请求及消费者证据 |
|---|---|
| A medium → B | 请求 `audit-model-b / low`；真实 WS model.set、ModelRuntime 与 manifest 一致 |
| A high → B | 请求 `audit-model-b / high`；没有被 B 的 low 默认值覆盖 |
| reset | 请求 `audit-model-a / medium`；manifest 与 UI selection mode 为 default，项目 settings 的 A/medium 保持不变 |
| 会话归属 | 第二任务保存 B/high/model，第一任务保留 A/medium/default；两个独立草稿通过侧栏切换保留 |
| 冷恢复 | 关闭并重启独立真实桌面/backend 后，侧栏打开两项任务；请求分别为 A/medium 与 B/high，mode 与默认 metadata owner 正确 |
| Full access | 取消不发权限写命令；接受真实写 bypass；已是 Full access 时重选仍确认；草稿保持 |
| Ultra 关闭 | 真正 Escape 关闭，不发送 effort/permission 写命令 |
| Ultra Continue | 先收到受限权限语义结果，再发送 Ultra；真实 manifest 与 UI 一致，草稿保持 |
| Ultra Full access | 保留 bypass，真实请求 `audit-ultra-model / ultra`；没有额外权限写命令 |

provider 还会收到实际辅助请求；它们在原请求记录中保留，没有算成额外业务场景。图像已检查实际模型列表、Full access 能力说明和 Ultra 组合确认，不能据此声称实现了尚缺的推荐 Power presets、guardian reviewer、permission profiles 或所有条件性的 Codex 控件。

最终补图采用主任务的统一 build14，`production-model-permission-run04/model-defaults` 六个双模型桌面场景全部通过，19 张 PNG 和同名 JSON 已更新，verdict 为 true，pageErrors 与 renderer warnings 均为空，warm/cold native windows 始终 `visible=[false]`。只在既有 spec 的实际 provider probe 完成后增加一条 UI 不存在“已安排压缩上下文”调度 toast 的断言，fixture、元数据和 provider 回复没有改变；未重跑 Ultra。

补图原因是主任务人工核对 run03 的第 08 图时发现非 Source 的“已安排压缩上下文 (85%)”自绘 toast。主任务在 `runtimeEvents` 的 `budget.warning` 消费者移除了该 toast，预算告警、Inspector 的真实预算事实及实际 ContextCompaction 生命周期保留。该生产修改由主任务的两个目标模块 94 项验证通过，本分支没有重复测试或编译，也没有将其重计为本分支新增单元场景。

run04 的完整 wire 中仍有四个真实 `budget.warning`（包括 warm 与 cold），百分比原精度保留；实际 UI 无预算调度 toast 的断言通过，且第 08 新图已人工核对没有该提示。这证明没有通过删除事件、篡改 fixture 或掩盖原始警告获得截图。Inspector 与 compaction 的既有行为范围由主任务的上述 94 项检查支持；本次桌面测试没有把未操作的 Inspector 或未新增的 compaction 场景算作通过。

当前 gallery 应使用 run04 的最新 19 张模型/权限图，加 run02 的五张 Ultra 图；run03、run02 主模型失败、run01 路径断言失败与原始证据继续保留。最终本批桌面计数仍为九个业务场景，而不是把六个重跑场景重复累加。
