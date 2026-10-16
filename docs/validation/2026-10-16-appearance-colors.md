# 2026-10-16 Appearance 三色编辑验证

本次交付仅增加实际可用的强调色、背景、前景三行，浅色和深色分别保存。首次编辑前不覆盖原有色板；现有字体、字号、字重、默认模式及字体设置没有改变。颜色修改会同时更新页面和已挂载的原生 Monaco，保留当前模型和未保存内容。

机器收据：[appearance-receipt.json](../../output/appearance-theme-20261016/appearance-receipt.json)。本报告属于 Appearance 独立证据，不替代主任务的整体差项和发布记录。

## Source 依据与准确边界

视觉参考为 [Codex 外观截图](../../output/playwright/ui-reference-20261004-v2/desktop/08-codex-settings-appearance.png)。行为和颜色推导来自本地实际提取的 Codex 桌面端包；不是凭截图推断。原始包目录为 `output/playwright/codex-control-audit-20261013/codex-open-source/`。

| 原始包 | 原始文件 SHA256 |
| --- | --- |
| `general-settings-161845fad443.js` | `03b2b04b597e74d0d7e6ba820f558eec8ffafca553d2876aa0fbac82b2e4de06` |
| `app-initial-b8e1c8014bb4.js` | `799331dde54905c4df5e360f23a9621141759e2c0acb230aeab909f9904b646f` |
| `app-shared-737655e1fb23.js` | `a1daa891e89a395b8db399ff6eaa365f12a86880c3d8cf891caf4ccbcb9eebf5` |

[source-evidence.json](../../output/appearance-theme-20261016/source-evidence.json) 保存原始文件哈希、每段原始文本哈希、格式化片段哈希、符号及位置。位置单位是 **JavaScript UTF-16 code unit，右端不包含**，不是文件字节位置。`extract-reference.cjs` 优先解析本地声明/导入；显式 `@kE` 才解析导出别名，避免把同名本地变量当作导出函数。

| 链路 | 符号及 UTF-16 位置 | 本轮采用的行为 |
| --- | --- | --- |
| 当前模式 | general `Ya` 48722–49201 | 依据已解析的浅色/深色变体编辑 |
| 三行编辑 | general `Xa` 49201–56690 | `accent`、`surface`、`ink`；颜色 patch 与字体 patch 分开 |
| 色块、hex、弹层 | general `Ta` 38724–40610 | 胶囊、14px 色块、28px 高度、uppercase 文本、end 对齐、offset 8、padding 12、radius 12、136×136 picker |
| hex 草稿 | general `ka` 40714–40839、`Aa` 40839–40906 | 去除非 hex、补 `#`、限六位；只提交完整六位值；不完整草稿失焦恢复 |
| 焦点/事件 | general `Ea` 40610–40644、`Da` 40644–40679、`Oa` 40679–40714 | Source 阻止弹层自动抢焦点，输入指针/点击不冒泡 |
| 自定义强调色 | general `Fa` 40989–43688 | 仅采用其自定义分支引用 `Ta`；官方账号变更服务不属于本轮 |
| 配置写入 | general 导入 `qr` → app-initial 导出 `kE` → `G1s` 9290182–9295996 | 追踪到真实主题设置链，不按名称猜测 |
| 明暗独立和字体保留 | app-initial `t0s` 9297233–9297422、`n0s` 9297422–9297902 | 分开 light/dark 设置；三色 patch 不改字体 |
| 基础色及 editor | app-initial `Pba` 5606194–5606420 | editor 浅色 surface→white 0.12，深色 surface→ink 0.07 |
| 分层、边框、选中与文字 | app-initial `Bba` 5614868–5614974、`Iba` 5610769–5612463、`Rba` 5612935–5614782、`Fba` 5606420–5610769、`Vba` 5614974–5615089 | 对应 MiniCode 现有 surface/text/border/accent/editor token |
| RGB 计算 | app-initial `YJ` 5615359–5615496、`qJ` 5615244–5615318、`Gba` 5615641–5615863、`Kba` 5615863–5615940、`qba` 5615940–5616015 | 逐通道 RGB 四舍五入混色、alpha、强调色前景对比度选择 |

Source 默认对比度来自 app-initial `VJ` 赋值 5584054–5584431：light 45、dark 60。本轮固定使用这两个默认值，不提供未完成的对比度 UI。`Zba` 5616364–5616415、`exa` 5616429–5616453、`txa` 5616454–5616482、`nxa` 5616483–5616507、`rxa` 5616508–5616533 的赋值片段也已保存，避免只摘函数而遗漏常数。

映射不是完整主题引擎移植：采用三色相关的 RGB 推导，接入 MiniCode 已存在的 token。语义 diff/skill 色、语法 keyword/string 等色不在本轮修改范围。默认未编辑状态保持原 CSS，不把 Source 默认值强行覆盖当前 MiniCode。

弹层使用真实 `react-colorful` **5.8.0**，没有调用系统原生颜色对话框。安装版 Codex 的 `THIRD_PARTY_NOTICES.txt:30068` 标明该库及版本；当前 ASAR 中有带 `react-colorful` 签名的 `webview/assets/dist-90de7f10bdd4.js`（SHA256 `6b6177a951fcb8c1cea633bb1c44972e4918ccb18b490bb8440a0b5b093dee3e`）。历史参考导入的是 `dist-1c07dc47c1c3.js`，它不在当前安装 ASAR，不能宣称二者是同一历史资产或同一哈希。对应 [身份收据](../../output/appearance-theme-20261016/source-colorful-identity.json) 的 `hasPicker:false` 仅表示当前压缩包没有字面量 `HexColorPicker`，库签名搜索仍命中；历史 `Ta` 则明确渲染导入的 `Ni`。

Source 色胶囊 `w-24` 为 96px、输入 `text-xs` 为 12px。MiniCode 本轮保留原继承字体 13px，实机发现 96px 会裁切六位 hex，所以只把胶囊宽度调整为 `max(96px, 8em)`，维持 Source 的 8em 比例。最终三个输入的 `clientWidth=scrollWidth=64px`，字体仍为原 Anthropic/CJK 栈、13px、400。没有为套用控件宽度而改变全局排版。

## 状态、启动与编辑器的完整链路

- 沿用现有 `UISlice`、localStorage 读写方式，键为 `minicode.appearance.colors`；没有新持久化层。
- 状态为 `{ light: 三色完整快照|null, dark: 三色完整快照|null }`。初始两者均为 `null`，不写任何覆盖。
- 首次从已挂载 UI 编辑某变体时，从实际 CSS 读取当前三色并存完整快照，再更新所选项。后续修改沿用该变体快照。
- 完整快照解决真实启动顺序：App 可先导入 store 再加载 CSS。`applyTheme()` 的初始调用可以直接应用已保存快照，无须在 CSS 尚未到达时读取 computed style；`theme-boot.js` 保持原有模式启动职责，文件未修改。
- 切换模式或系统模式变化先移除此前实际应用的 owned token，再应用新变体；未修改变体恢复原 CSS。只管理颜色 token，字体变量不参与清理或写入。
- `EditorPanel` 原来的布局 effect 依赖增加 `appearanceColors`，调用现有 `defineMiniCodeMonacoTheme`，更新同模式下的真实 Monaco 主题；不重建 model、不保存文件、不修改 editor 字体选项。
- 弹层为 body portal。此次真实浏览器 QA 发现触发器打开后原 Settings focus trap 使 Tab 无法进入 portal；已补触发器→第一个 slider、slider 间与退出的键盘路径。保留 Source 的打开时不抢焦点；Escape 只关弹层并恢复色块焦点；外部点击/移出焦点关闭弹层。

## 自动验证，保留失败而不合并粉饰

证据：[去重逐节点清单](../../output/appearance-theme-20261016/frontend-deduplicated-verification.json)、[最后调用收据](../../output/appearance-theme-20261016/final-invocations.json)。节点身份为 JUnit 的 `(classname, name)`，按实际后续结果覆盖旧结果。

| 实际调用 | 结果 | 说明 |
| --- | --- | --- |
| `frontend-batch01`，11 文件 | 183 passed、12 failed | 原始日志和 XML 完整保留 |
| `frontend-batch02`，只重跑受 CSS fixture 影响的 4 文件 | 160 passed，0 fail/skip | 修正测试真实 CSS 注入后通过 |
| `frontend-batch03`，只重跑最后键盘修改影响的 `AppearanceColors.chain` | 3 passed，0 fail/skip | 覆盖从触发器 Tab 进入真实 picker，随后 hue 编辑/Escape |
| 去重后最终范围 | **195 个唯一节点，195 passed，0 fail/error/skip** | 不是一次未中断的 195 例重跑，也不能把 183+160+3 累加为通过数 |

batch01 的 12 个失败根因是测试 CSS fixture：Vitest 默认 CSS plugin 把 `?inline` 内容替换为 `""`（本地 `vitest/dist/chunks/cli-api.BfdDOPPI.js:9946–9964`），导致 JSDOM 中有效颜色为空。`frontend/test/app-theme-tokens.ts` 改为 `readFileSync` 读取真实 `tokens.css` 再注入；`SettingsCenter`、`SettingsInput` 测试只改 fixture。没有在生产代码中添加空 CSS 兜底或吞异常。未受影响的 7 文件沿用 batch01 已通过结果。

验证范围包括输入/纯 RGB 契约、无 CSS 的 store 冷启动、明暗/系统切换、原生 Monaco 主题和 model 保留、Settings 真实挂载、token/语言/启动不变量。原生 Monaco 独立用例和挂载 EditorPanel 用例均核对同模式改色及内容保持；没有新增每项功能后各跑一套测试的流程。

## 真实浏览器记录

使用独立 Playwright CLI session `appearance-theme-20261016`，1536×960 视口，临时 Vite 入口挂载**生产 SettingsCenter 和生产 EditorPanel**及真实 CSS/字体。Monaco 仅使用自有 `file:///appearance.txt` 缓冲区，无真实工作区文件读写。没有操作正常 MiniCode 窗口、账号、用户 profile、隐藏草稿或 `data/`。

| 操作与实际证据 | 结果 |
| --- | --- |
| [最终默认外观](../../output/playwright/appearance-theme-20261016/02-default-final.png)、`02-default-metrics.log` | 原三色 `#424242/#FFFFFF/#1A1C1F`；13px、400；六位 hex 无裁切 |
| [实际 picker](../../output/playwright/appearance-theme-20261016/05-hex-color-picker.png)、`08-picker-keyboard-fixed.log` | 136×136；Tab 进入 SV、再进入 Hue；ArrowRight 把 `#2468AC` 改为 `#243FAB` |
| `09-picker-escape-fixed.log`、`10-picker-outside.log` | Escape 后 popup=false、settings=true、焦点回到色块；外部点击只关弹层 |
| [浅色自定义](../../output/playwright/appearance-theme-20261016/11-light-custom.png)、`11-light-custom-metrics.log` | `#267C51/#F3E8D7/#2A2630`；Monaco 背景 `rgb(244,235,220)`，符合 Source surface→white .12 |
| [深色未修改](../../output/playwright/appearance-theme-20261016/12-dark-default.png)、`12-dark-default-metrics.log` | 三个基础色 inline 值都为空；恢复原深色 `#C4C4C4/#161718/#E7E8E9`；editor `rgb(24,24,24)` |
| [深色自定义](../../output/playwright/appearance-theme-20261016/13-dark-custom.png)、`13-dark-custom-metrics.log` | `#2468AC/#101010/#D0D0D0`；Monaco 背景 `rgb(29,29,29)`，符合 Source surface→ink .07 |
| [切回浅色的未保存编辑器](../../output/playwright/appearance-theme-20261016/14-light-editor-dirty.png)、`04-dirty-before.log`、`14-light-restored-dirty-metrics.log` | 真实键盘输入 `QA_DIRTY`；整个改色与切换过程始终为 `$model1`、同一 URI、同一完整三行内容；恢复浅色 editor `rgb(244,235,220)` |
| [关闭设置时冷启动](../../output/playwright/appearance-theme-20261016/15-cold-start-before-settings.png)、`15-cold-start-before-settings.log` | 浏览器重载后 settings=false，尚未打开设置就已有保存色板与 editor 背景；两套完整快照均存在 |

baseline、浅色编辑、深色编辑、冷启动的 `--font-app`、`--app-text-scale`、`--code-text-scale`、`--mc-font-reading`、`--editor-font-family`、`--editor-font-size`、`--editor-font-weight`、`--fw-medium`、`--fw-semibold` 逐项一致。原样为空的 editor-font-weight 不被填造。独立重载会重建该 QA 自有缓冲区；本报告**不声称**浏览器重载保存了未保存文本，同实例保护由重载前证据证明。

没有后端的独立 harness 显示真实“索引失败 · 重试”，该项不算后端通过，也未隐藏。最后冷启动 console 为 0 error、0 warning（另有 React DevTools info）。早期错误导入/500、favicon/404、旧 snapshot ref、错误 harness 布局使 editor 高度仅 5px、HMR 重复 createRoot 警告、最初 Tab 路径失败及一次不支持的 CLI `network` 命令均保留为未通过/设置过程；不是生产成功项。布局仅修临时 harness 的 flex 父容器；最后改用实际 `requests` 命令保存记录。相关 `.playwright-cli/` 原始记录保留。

操作结束后已关闭所属独立浏览器、停止所属 Vite server，移除 `frontend/.tmp/appearance-theme-20261016.{html,tsx}`；审计用入口副本保留在 `output/playwright/appearance-theme-20261016/harness.{html,tsx}`。

Root 随后使用本次最终 dist 做了额外的独立生产 Electron 壳验证（独立 profile、临时工作区、offscreen 窗口）：真实入口 `file:///C:/Desktop/MiniCode/frontend/dist/index.html`，index SHA 与下表一致。[原始操作与结果](../../output/playwright/latest-ui-followup-20261016/inspection.json) 记录实际点击设置/外观、hex 输入、色块、Tab 进入 Color slider、ArrowLeft 改色、Escape 返回原触发器且设置保留、dark 清除 light inline、light 恢复、返回聊天和重载；草稿 `UNSENT_LATEST_UI_DRAFT` 及色板均保留，`errors=[]`。字体为原 Anthropic Sans 栈、13px、400。对应 [生产壳调色器图](../../output/playwright/latest-ui-followup-20261016/03-latest-picker-keyboard.png) 与 [重载后的草稿及颜色图](../../output/playwright/latest-ui-followup-20261016/06-latest-reload-draft-and-theme.png) 已查看。此补验与前述组件 harness 分开记录；未执行真实模型任务或 OS sandbox 断言。

## 最终构建与冻结文件

2026-10-16 **13:38:37Z–13:39:07Z** 执行一次最终 `npm run build`，exit 0；随后 `check:bundle-budget` 和 `check:ui-debt` 均 exit 0。记录见 `frontend-final-build.log`、`frontend-bundle-budget.log`、`frontend-ui-debt.log`。本次构建包含 Root 已冻结的其他前端改动及最后聊天 JS title 投影修复；不是只构建 Appearance。

当前入口以真实 `frontend/dist/index.html` 为准。旧 chunk 可能仍在 dist，不能按目录内最大文件猜入口。

| 当前文件 | 字节数 | SHA256 |
| --- | ---: | --- |
| `frontend/dist/index.html` | 3224 | `17dff27b79573909e5cedaf6d1a27dff9e5465c65fed84c31f23dbb6a69ba129` |
| `assets/index-CCh13oC-.js` | 860529 | `2753d0fdc81faed63d02a1e4af7e6b7177d70ef08989f8da12a3f09eab9b65c6` |
| `assets/index-D5-lmIEl.css` | 265373 | `2e4106df5dca6fe0ff2993244bfc5ebb39429aa1d1f1157d15264d2d04d3b4ea` |

收据还包含 index 引用的全部 9 个 CSS 哈希，以及 947 个前端源码/公开资源/配置输入的哈希清单 [frontend-build-inputs.json](../../output/appearance-theme-20261016/frontend-build-inputs.json)。构建已有 Browserslist 数据陈旧、无效 dynamic import 和 chunk 大小警告，均未通过抬高预算消除；两项项目门禁实际通过。

本子任务最终冻结 20 个前端路径，逐文件哈希见机器收据：

```text
frontend/package.json
frontend/package-lock.json
frontend/src.v2/lib/appearance-colors.ts
frontend/src.v2/lib/appearance-colors.test.ts
frontend/src.v2/overlays/AppearanceColors.tsx
frontend/src.v2/overlays/AppearanceColors.css
frontend/src.v2/overlays/AppearanceColors.chain.test.tsx
frontend/src.v2/overlays/AppearanceTab.tsx
frontend/src.v2/overlays/SettingsCenter.test.tsx
frontend/src.v2/overlays/SettingsInput.chain.test.tsx
frontend/src.v2/stores/types.ts
frontend/src.v2/stores/ui-slice.ts
frontend/src.v2/stores/shared-helpers.ts
frontend/src.v2/stores/appearance-startup.test.ts
frontend/src.v2/panels/EditorPanel.tsx
frontend/src.v2/panels/EditorPanel.test.tsx
frontend/src.v2/panels/monacoTheme.test.ts
frontend/src.v2/i18n/en.json
frontend/src.v2/i18n/zh-CN.json
frontend/test/app-theme-tokens.ts
```

`SettingsCenter.tsx` 生产文件未在本子任务中修改。Provider、Browser、Problems 和其他冻结的生产路径未被本子任务修改；没有提交、推送或改动后端/Linux 文件。

## 已读取但本轮未实现的 Source 功能

Source app-shared `wN` 1922945–1925046 是 29 个真正主题 ID 的目录，`kN` 1925074–1925564 是含 fonts、contrast 整数 0..100、opaqueWindows、semanticColors、三种六位 hex 色的 schema。general `ao` 63320–63692 把默认 `codex` 主题显示为 **ChatGPT**。本轮没有添加主题菜单、29 个只改标签的假预设、语法主题选择或字体菜单。

Source 分享格式为 `codex-theme-v1:` 加 JSON `{codeThemeId, theme, variant}`。app-initial `o0s` 赋值 9298295–9298316、`l0s` 赋值 9298433–9298496、`r0s` 9297902–9297953、`i0s` 9297953–9298143、`e0s` 9297010–9297233 保存了真实序列化/解析/变体和已知 codeThemeId 校验链。前缀后支持原 JSON 或 URI 编码 JSON。此段只记录 Source 依据，**本轮没有实现分享、导入或新的公开格式**。官方账号强调色变更服务同样未实现，也不作为账号接入阻塞。
