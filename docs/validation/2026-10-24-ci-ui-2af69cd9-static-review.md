# CI UI 静态审阅：其余 57 张捕获（2af69cd9）

2026-10-24，已查看本批 **57 张原始 PNG 的原始分辨率图像及 7 张分组联系表**。在这些捕获状态中，没有新确认的明显、且有当前 Source 依据的视觉缺陷。此结论限定于下述静态视图，不等同于功能或动效全部通过。

**截图边界：** 原 receipt 明确记录为 Electron renderer viewport；有限动画被快进，连续动画被临时重置。截图不验证自然动效、持续时间、原生系统窗框或按钮实际执行结果。receipt 中的运行断言与本次人工静态审阅是两类证据。

## 范围和计数

- 原 CI [receipt](../../output/full-parity-audit-20261023/ci-ui-2af69cd9/receipt.json) 为 `success=true`，包含 **110 张原始捕获**。
- 已有 [主设置页审阅](../../output/layout-metrics-20261024/settings-ci-static-review-2af69cd9.md) 覆盖 53 张：三组 17 页设置基线及两张自定义对比度捕获。
- 本文覆盖其余 **57 张**，精确文件名、分组和图像尺寸见 [manifest](../../output/full-parity-audit-20261023/ci-ui-2af69cd9-review/manifest.json)。57 张均逐张查看，联系表用于组间对照。
- 原截图目录另有审阅时生成的 3 张主设置联系表；它们是派生文件，不计入 110 张捕获或本次 57 张。

| 分组 | 原始图数 | 范围 | 联系表 |
|---|---:|---|---|
| 主对话、重连与终止错误 | 8 | `01-current-build-idle` 至 `06-actual-terminal-model-failure`，按 manifest 的 runtime 项 | [runtime](../../output/full-parity-audit-20261023/ci-ui-2af69cd9-review/runtime-contact.png) |
| 主题、导入主题弹窗和 DPI | 12 | 1280/1800 主题行、两级菜单、无效导入及暗色弹窗；DPR 1.25/1.5 外观、子菜单焦点和末项 | [themes](../../output/full-parity-audit-20261023/ci-ui-2af69cd9-review/themes-contact.png) |
| 实际导入流程 | 7 | `import-01` 至 `import-07` | [import](../../output/full-parity-audit-20261023/ci-ui-2af69cd9-review/import-contact.png) |
| Config scope 和文件编辑器 | 13 | manifest 的所有 `settings-06-*` 项 | [config](../../output/full-parity-audit-20261023/ci-ui-2af69cd9-review/config-contact.png) |
| 设置链接及刷新状态 | 3 | `settings-12-dark-info-link`、`settings-12-refresh-pending`、`settings-15-refresh-pending` | [settings-status](../../output/full-parity-audit-20261023/ci-ui-2af69cd9-review/settings-status-contact.png) |
| 外壳菜单 | 5 | `shell-01` 至 `shell-05` | [shell](../../output/full-parity-audit-20261023/ci-ui-2af69cd9-review/shell-contact.png) |
| 置顶和自动化 | 9 | `thread-01` 至 `thread-09` | [thread](../../output/full-parity-audit-20261023/ci-ui-2af69cd9-review/thread-contact.png) |
| 合计 | **57** | | |

## Source 依据

使用当前 [settings/menus/popovers 审计及已批准实现记录](../../output/layout-metrics-20261024/settings-menus-popovers-audit.md)、同目录的 `source-core-component-rules.txt`、`source-manifest.json`，以及 `output/full-parity-audit-20261023/readable/webview/assets` 中对应 Source 模块。安装包记录为 Windows package `26.1016.8622.0` / app `26.1016.62342`；它与本地 public checkout 是不同构建，本文不把二者视为同一产物。

审阅采用该审计底部已批准的实现记录，不使用前文历史候选表的旧 MiniCode 数值。Source 普通菜单的 4px surface padding、13px 字号及 5px/8px row padding，以及外侧 768px 轴包含 20px 水平 padding 后得到的 728px card，是相关布局参考。Source 默认 520px dialog 不是所有弹窗的统一宽度；自定义 RRULE 使用窄弹窗，不据此判成默认宽度错误。历史 `ui-config-review.md` / `ui-scheduler-review.md` 只提供历史范围，不能证明当前捕获仍有同一问题。

## 逐组观察

**主对话与运行状态（8 张）。** 首页、输入框、用户气泡、过程分隔线及重试文本位于各自列内。重试与终止错误使用中性色；展开记录中的原始错误可读。返回原对话及重新载入的捕获中保留 `KEEP_A_DRAFT`。侧栏高亮位于会话行，工作区标题没有同时高亮；长会话名截断后未与行内控件碰撞。上述可见状态不单独证明重试时序或后台 owner 对应关系。

**主题与弹窗（12 张）。** 1280/1800 宽度的主题卡片和 trigger 保持对齐。菜单勾选、图标、两级 flyout 和文字留在 viewport 内。子菜单滚动边缘可见前后条目的局部，这是已滚动列表边界；后续 focus/last-item 捕获在两个 DPR 下均显示完整 Xcode 条目，选中背景也包含在菜单内，未据此报告条目裁切。浅色无效主题与暗色导入弹窗的输入、取消和 disabled action 可读；长输入只在输入框内部截断。

**导入（7 张）。** Provider、选项、进行中、成功和失败视图的主要内容均位于 viewport 内。长路径在结果区域内换行。进行中捕获没有完成 CTA；成功捕获显示勾选与确定动作；失败捕获保留原始 `[Errno 17] File exists` 和换行后的路径。`import-04` 是有意滚到底部的捕获，标题在滚动区域上方离开视图不算 viewport 裁切。打开的导入文件仍保留其他工作区上下文，保存 toast 可见；实际写入和保存结果仍以已有运行断言为依据。

**Config 与编辑器（13 张）。** Scope/verbosity 菜单、管理锁提示、disabled surface 和打开配置动作均可读且未碰撞。编辑器可见行号、代码字体和 minimap；ancestor/open/repair 状态中 `keep-unsaved.txt` 保留未保存标记。`settings-06-project-reopen-scrolled-menu` 特意使用 1280×560 viewport 并滚动父容器，Config 标题顶部及下方设置/侧栏超出截图范围是该捕获的滚动状态；菜单本身仍封闭在 viewport 内，不把滚动结果报告为布局缺陷。

**设置状态（3 张）。** 暗色 Hooks 信息链接可见为蓝色并保持可读。刷新图标出现在记录的 pending 状态；单帧不能证明它是否持续旋转或旋转多久。

**外壳菜单（5 张）。** Profile/help flyout 位于 viewport 底边上方；help 末项的焦点背景完整包含在菜单内。会话 copy submenu 向左展开，各条目及快捷键留在 surface 内。捕获未建立所有菜单在所有窗口尺寸下的通用结论。

**置顶和自动化（9 张）。** 面板中的长编辑文本换行，长 target 行按行宽截断。Target/model/notification/pause 菜单均适配捕获视图。RRULE 编辑弹窗及删除确认弹窗的文本和主要动作完整可见，动作层级清楚。截图可见原始内容，不能单独证明更新、暂停、删除或运行确已发生。

## 结果和未覆盖项

本次没有新增可执行的视觉缺陷项，因此没有产品补丁。主设置页报告中的 Personalization 文档链接颜色项属于另 53 张的审阅范围，已由主任务处理；不重复计入本文发现。

本次只读取证据并新增本审阅记录、manifest 和联系表，没有运行测试、构建或 Git，没有操作桌面窗口、用户 profile、偏好或未保存内容。未覆盖自然动画、交互时序、真实任务行为与截图一一对应的后端事件、原生窗框，以及没有进入这些截图的菜单和功能。原 receipt 的通过不能替代这些范围的验证；历史失败和跳过也没有被本文重新记为通过。
