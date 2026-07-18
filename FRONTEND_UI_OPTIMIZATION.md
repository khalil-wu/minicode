# MiniCode 前端 UI/UX 优化方案

> 审计日期：2026-07-10  
> 审计对象：`frontend/src.v2`、前端测试、构建产物，以及前后端事件投影链路  
> 方法：以真实代码、测试、构建和多视口截图为依据；遵循 Ponytail 原则，优先删除、复用和收敛，不引入新的 UI 框架、状态库或动画依赖。

## 实施状态（前四批，2026-07-10）

前四批已完成并验证，本文后续“当前”“失败”等表述仍保留审计时基线，实施结果以本节为准：

- **协议同步已修复**：前端运行时事件集合已补齐缺失事件，并加入输入队列协议；115 个服务端事件和 97 个客户端命令均与后端一致。
- **重复 Onboarding 已删除**：首次启动不再被向导遮挡，Cowork/Code 空状态和现有设置入口成为唯一主路径；对应 feature flag、store 状态、组件和测试分支一并移除。
- **窄屏侧栏已改为真实 Drawer**：`<=768px` 时 Header 左右按钮复用现有 Sidebar 内容，支持左右互斥、Escape、遮罩关闭、焦点约束和可访问性状态。
- **Composer 三档布局已落地**：桌面、平板、手机使用同一组控件和 CSS grid；手机隐藏次要 usage/effort/workspace 信息，保留附件、权限、模型和固定 44px 发送/停止触点，并限制输入区视口高度、处理底部安全区。
- **关键伪按钮已替换**：Composer 上下文项删除动作和 Toast 关闭动作改用具名原生按钮；Toast 的 `status`/`alert` 语义按严重级别区分。
- **无调用组件已删除**：移除 `SharedBackdrop`，未新增 UI 依赖。
- **输入排队已完成**：Agent 正在回复且输入非空时，Enter 和发送按钮会把消息加入当前会话 FIFO；输入为空时仍是 Stop。排队气泡显示状态并可单条取消，当前回复的 plan、todo 和 progress 不会被下一条消息提前重置；只有收到 `dequeued` 后才进入下一回合 streaming。
- **实时工具投影已结构化**：命令、文件读写、工作区搜索、Web、浏览器预览优先消费后端的 `resultKind`、`activityKind`、`panelHint` 和错误分类；工具名兼容推断只保留在旧 transcript hydration 边界。
- **聊天列表已收敛**：删除未使用的 `VirtualMessageList` 和 `useVirtualScroll`，保留已有 40-turn 窗口与 `content-visibility`；移除被 `!important` 覆盖的行内样式和重复滚动容器规则，消息正文统一到稳定阅读轴。
- **工作台布局与小图标已统一**：Header 收敛为 start/center/end 三组，Settings、连接状态和窗口控制拥有明确语义，并只显示当前模式确实可用的侧栏控件；Task Template、错误提示、状态通知和错误兜底中的 Emoji/字符图标全部替换为现有 Lucide。新增共享图标按钮命中区、焦点态和 1.75px stroke 规则，桌面维持高密度，`<=768px` 时触点扩大到 44px；Sidebar 导航图标统一为 16px，右侧面板标签恢复可读宽度。窄屏 Drawer 在导航后关闭，Side Chat 与 Drawer 互斥并完整约束、恢复焦点。

验证结果：当前全量 110 个前端测试文件、1,088 项测试通过，`npm run build` 与协议同步脚本通过；输入排队、Composer、消息气泡、投影、hydration、图标语义、Header/Sidebar 布局、Drawer 导航和焦点恢复均有目标测试。此前“打开工作区并绑定会话”目标 E2E 和多视口验收继续有效；390×844 左右 Drawer 截图位于 `frontend/test-results/optimization-mobile-left.png` 与 `optimization-mobile-right.png`。构建仅保留既存的大 chunk 提示，未新增运行时依赖。

## 1. 结论先行

MiniCode 的前端不需要重写。项目已经具备完整工作台、聊天流、编辑器、终端、文件树、设置、命令面板、Agent 活动时间线、响应式断点和较完整的测试。当前体验的主要问题不是“组件不够”，而是同一界面经过多轮追加后，出现了四类累积性问题：

1. **窄屏功能与可见控件不一致**：小于等于 `768px` 时左右侧栏不再渲染，但 Header 仍提供左右侧栏按钮，用户点击后没有可见结果。
2. **信息架构重复**：首屏同时出现 Cowork/Code 和 Chat/File 两套高层切换，新用户还会被 Onboarding 再次要求完成模型、工作区和新会话配置。
3. **样式所有权失控**：`workbench-polish.css` 依靠大量 `!important` 覆盖组件样式，同一选择器被反复定义，导致每次局部调整都可能产生跨页面回归。
4. **Agent 数据被前端二次猜测**：后端已经提供 `activity_kind`、`result_kind`、`display_summary` 等结构化字段，前端仍在超大投影文件中用名称和文本正则重新分类。

最短有效路线不是做一套新设计系统，而是：

- 删除重复的 Onboarding 和未使用的 Backdrop；
- 修复窄屏侧栏按钮和 Composer；
- 只保留一层主模式导航；
- 给聊天正文建立稳定阅读轴；
- 将样式逐块归还给已有组件，最终删除 `workbench-polish.css`；
- 让实时事件直接消费后端结构化展示字段，只在历史记录水合边界保留兼容逻辑。

## 2. 审计范围与基线

### 2.1 代码规模

本次审计得到的前端基线如下。数字用于判断优化优先级，不作为机械 KPI。

| 项目 | 当前值 | 含义 |
| --- | ---: | --- |
| 前端生产文件 | 253 个 | 功能面已经完整，不适合整体重写 |
| 前端生产代码 | 约 68,204 行 | 应收敛现有模块，而不是继续平铺新组件 |
| 前端单测文件 | 103 个 | 可支持渐进式修改 |
| 前端单测 | 1,060 项全部通过 | 当前行为有较强回归保护 |
| CSS 文件 | 23 个 | 样式入口较多，但真正问题是覆盖关系 |
| CSS 总量 | 约 9,682 行 | 已接近需要明确所有权的规模 |
| 行内样式 | 约 745 处 | 不应全量迁移，只处理响应式和交互状态 |
| `!important` | 747 处 | 表明层叠顺序长期依赖补丁 |
| `workbench-polish.css` 中的 `!important` | 600 处 | 样式债务最集中的文件 |
| 主包 | 约 1.40 MB，gzip 404.76 KB | 首屏还有收敛空间，但不是当前第一优先级 |
| Monaco API 包 | gzip 约 926.78 KB | 应继续保持按需加载 |
| TypeScript worker | 约 6.9 MB | 属于编辑器能力成本，不应进入聊天首屏关键路径 |

### 2.2 已执行验证

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| `npm run build` | 通过 | 当前前端可构建 |
| 前端单测 | 103 文件、1,060 项通过 | 基础行为稳定 |
| `python scripts/check-protocol-sync.py` | 失败 | 前端运行时集合缺少 4 个后端事件 |
| E2E：打开工作区并绑定会话 | 失败 | 首次启动 Onboarding 遮罩拦截主操作 |
| 多视口人工审计 | 已完成 | 覆盖 1440、768、740、390 像素宽度 |

审计截图位于 `frontend/test-results/`：

- `ui-audit-empty-1440.png`
- `ui-audit-empty-768.png`
- `ui-audit-empty-740.png`
- `ui-audit-empty-390.png`
- `ui-audit-chat-1440.png`

### 2.3 工作树说明

审计时仓库存在大量用户未提交修改和删除项。因此本文描述的是 **2026-07-10 当前工作树**，不是某个干净提交。实施时应按小提交逐项修改，不应借优化之名恢复或覆盖无关改动。

## 3. 当前界面与数据链路

### 3.1 可见界面结构

当前 `WorkbenchShell` 大致是：

```text
HeaderBar
├─ 全局操作：设置、侧栏、新会话、搜索、主题、上下文、连接状态
├─ Cowork 模式
│  ├─ SidebarLeft
│  ├─ CoworkHome 或 ChatPane
│  └─ SidebarRight
├─ Code 模式
│  ├─ SidebarLeft
│  ├─ MainSlots
│  ├─ SidebarRight
│  └─ BottomDock
└─ Chat 模式
   └─ ChatPane
```

这个结构本身可继续使用。问题在于模式概念、窄屏渲染规则和 Header 控件没有形成同一份契约。

### 3.2 Agent 信息到 UI 的链路

```text
WebSocket ServerEvent
  -> useWebSocket
  -> runtimeEvents / chatStreamEvents
  -> Zustand messages / blocks
  -> chatSurfaceState
  -> turn projection
  -> ChatTurn / AgentTurn / cells
```

链路中同时存在：

- 协议类型；
- 运行时事件处理；
- Store 中间结构；
- Chat surface 投影；
- Agent loop 投影；
- 最终 cell 组件。

这不是立即合并成一个文件的理由。真正需要消除的是“同一事实在多个层级再次推断”。协议适配、状态存储和视图投影可以分层，但 `result_kind` 不应在每层重新猜一次。

## 4. 优先级总表

| ID | 优先级 | 问题 | 用户影响 | 最短处理 |
| --- | --- | --- | --- | --- |
| UI-01 | P0 | 小于等于 `768px` 时侧栏按钮仍显示，但侧栏组件不渲染 | 可见按钮无响应 | 复用现有 Sidebar 作为窄屏 Drawer；若本阶段不做 Drawer，则先隐藏无效按钮 |
| UI-02 | P0 | Onboarding 遮挡首次使用和 E2E | 用户无法直接进入主任务 | 删除向导，复用 Cowork 空状态中的模型、工作区、新会话入口 |
| UI-03 | P0 | 前端协议运行时集合少 4 个事件 | 新事件可能被验证层拒绝或形成隐性漂移 | 将已有类型加入 `SERVER_EVENT_TYPES`，CI 固定运行同步检查 |
| UI-04 | P0 | 窄屏 Composer 高度失衡、Footer 多行无稳定规则 | 390/768 宽度下输入区挤压正文 | 仅重排现有控件，定义三档布局和固定发送按钮尺寸 |
| UI-05 | P1 | 聊天阅读轴过宽 | 长回答扫描困难，问答两端距离过大 | 给内容区设置统一 `max-width` 和稳定 gutter |
| UI-06 | P1 | Cowork/Code 与 Chat/File 的层级重叠 | 用户不清楚是在切产品模式还是切面板 | Header 只保留一个主模式；Chat/File 降为 Code 内部面板标签 |
| UI-07 | P1 | `workbench-polish.css` 大量覆盖 | 修改成本高、回归范围不可预测 | 冻结新增规则，按组件迁回并删除旧覆盖 |
| UI-08 | P1 | 多个 Dialog 缺少统一焦点约束 | 键盘和读屏用户可能跳到遮罩后方 | 复用已有 `useFocusTrap`，补返回焦点、Escape 和标题关联 |
| UI-09 | P1 | 可点击 `span` / `div` | 键盘不可达，语义错误 | 替换为原生 `button`，不新增抽象 |
| UI-10 | P1 | 部分主题颜色对比不足 | 文本和状态信息可读性不稳定 | 调整少量 token，而不是给组件单独补颜色 |
| UI-11 | P1 | 前端继续用正则推断 Agent 活动类型 | 新工具容易显示错误，投影文件持续增长 | 实时链路只消费后端结构化字段；旧记录兼容集中在 hydration |
| UI-12 | P2 | `summaryItems` 计算并测试但无 UI 消费者 | 无效复杂度和维护成本 | 删除投影字段、构建逻辑及专属测试 |
| UI-13 | P2 | 中英文混用 | 产品语气不统一 | 先统一主流程文案，不启动全量 i18n 工程 |
| UI-14 | P2 | 动画时长值分散 | 动效节奏不一致 | 收敛到现有 token；尊重 `prefers-reduced-motion` |
| UI-15 | P2 | 主包和编辑器资源较大 | 冷启动与低配设备压力 | 在 P0/P1 完成后基于真实加载瀑布继续拆分 |
| UI-16 | P0 | Agent 回复期间无法继续提交输入 | 用户必须等待当前回合结束，连续工作流被打断 | 复用现有消息与 placeholder，增加 conversation FIFO 和单条取消 |

## 5. P0：先修复“看得见但不能用”

### 5.1 UI-01：窄屏侧栏按钮与渲染规则冲突

#### 证据

- `frontend/src.v2/shell/WorkbenchShell.tsx` 定义 `NARROW_WORKBENCH_MAX_WIDTH = 768`。
- 窄屏时，Cowork 和 Code 分支都通过 `!narrow` 直接停止渲染 `SidebarLeft`、`SidebarRight` 和 `BottomDock`。
- `frontend/src.v2/shell/HeaderBar.tsx` 的左、右侧栏按钮不读取这个状态，仍调用 `setLeftSidebarWidth` 和 `toggleRightPanel`。
- 在 740px 和 390px 视口实测，点击按钮后 DOM 中侧栏数量仍为 0。

#### 目标行为

| 宽度 | 左侧栏 | 右侧栏 | BottomDock |
| --- | --- | --- | --- |
| `>= 1024px` | 固定栏，可调整宽度 | 按状态固定显示 | Code 模式按状态显示 |
| `761-1023px` | 固定栏或折叠栏，保持现有桌面逻辑 | 按状态显示 | 保持现有逻辑 |
| `<= 768px` | 点击 Header 后以全高 Drawer 显示 | 点击 Header 后以全高 Drawer 显示 | 不固定占据聊天高度；从 Code 面板入口打开 |

#### 最短实施方案

1. 在 `WorkbenchShell` 顶层只计算一次 `narrow`，不要在 Cowork 和 Code 子壳分别注册 resize listener。
2. 窄屏仍复用 `SidebarLeft` 和 `SidebarRight`，外面只包一个通用的 Drawer 容器。
3. Drawer 使用已有 z-index token、Backdrop 和 `useFocusTrap`；按 Escape、点击遮罩和完成导航都可关闭。
4. Header 按钮的 `aria-expanded`、`aria-controls` 与真实 Drawer 状态一致。
5. 打开一个 Drawer 时关闭另一个，避免 390px 宽度上叠两层导航。

不要为移动端复制 `MobileSidebarLeft` / `MobileSidebarRight`。同一份内容组件加一层布局容器即可。

#### 临时止损

如果一个提交内无法完成 Drawer，应先在 `<=768px` 隐藏左右侧栏按钮。无按钮优于无响应按钮，但这只能作为短期提交，不能作为最终移动端能力。

### 5.2 UI-02：删除重复的 Onboarding

#### 证据

`frontend/src.v2/overlays/Onboarding.tsx` 是三步向导，主要动作是：

- 介绍产品；
- 打开 Provider 设置；
- 选择工作区；
- 进入应用。

这些操作在 Cowork 空状态、Composer Footer、Settings 和 Header 中已经存在。向导没有独立配置能力，只是再次路由到现有入口。它还依赖 `minicode.onboarding-done` 本地状态，并在 E2E 中遮住“打开工作区”的真实主操作。

#### 建议

直接删除：

- `frontend/src.v2/overlays/Onboarding.tsx`；
- `App.tsx` 中的 lazy import 和条件渲染；
- `ui-slice.ts` 中 `onboardingOpen`、`setOnboardingOpen` 及 localStorage 键；
- `stores/types.ts` 中对应字段；
- Agent capability 对 `onboarding_wizard` 的 UI 特判；
- 仅服务于 Onboarding 的测试断言。

保留 Cowork 空状态，并确保它按缺失条件显示最短路径：

```text
无 Provider -> 主按钮“选择模型”
有 Provider、无工作区 -> 主按钮“打开工作区”
两者齐备 -> 输入框直接可用
```

产品介绍不应继续占用阻塞式 Modal。MiniCode 的第一屏应是可工作的应用，而不是教程。

### 5.3 UI-03：修复协议运行时集合漂移

`frontend/src.v2/protocol/streaming-types.ts` 已声明以下事件类型，`runtimeEvents.ts` 也已有处理逻辑，但 `frontend/src.v2/protocol/events.ts` 的 `SERVER_EVENT_TYPES` 运行时 Set 未包含它们：

- `stream_event`
- `rate_limit`
- `session.state_changed`
- `tool_use_summary`

因此 `python scripts/check-protocol-sync.py` 当前失败。最短修复是将这 4 个字面量加入现有 Set，并给 `server-event-validation.test.ts` 增加正反例。不要为了 4 个值引入协议生成器；现有同步脚本已经足以守住跨语言镜像，关键是将它固定进 CI。

### 5.4 UI-04：Composer 的三档响应式布局

#### 当前问题

- 768px 时 Footer 已经自然换成多行，但各控件没有明确分组，视觉顺序依赖剩余宽度。
- 390px 时工作区、权限、模型、推理强度、上下文和发送按钮争夺同一横向空间。
- 动态标签长度会改变 Composer 高度，挤压消息区。

#### 目标结构

```text
桌面： [附件][权限][工作区]           [上下文][强度][模型][发送]
平板： [附件][权限][工作区]                         [发送]
       [上下文][强度][模型]
手机： [多行输入区.................................]
       [附件][权限]                 [模型][发送/停止]
```

#### 修改原则

- 发送/停止按钮保持固定宽高，状态变化不得引起布局跳动。
- 工作区标签在窄屏隐藏文字或移入 title，不重复显示完整路径。
- Context ring 属于次要状态，手机上可隐藏；其信息仍可从设置或上下文面板查看。
- Reasoning effort 仅在 Provider 支持时显示，手机上放入已有菜单区域，不新建设置页面。
- Footer 使用 CSS grid 定义区域，不再依赖多个 `flex: 1` spacer 自然挤压。
- Composer 最大高度受视口约束，输入区内部滚动，不能覆盖上一轮回复。
- 处理 `env(safe-area-inset-bottom)`，但不引入设备检测库。

主要修改文件：

- `frontend/src.v2/composer/FooterRow.tsx`
- `frontend/src.v2/composer/Composer.tsx`
- `frontend/src.v2/composer/composer.css`

### 5.5 UI-16：运行中输入排队

输入队列不需要独立任务中心，也不需要新的状态库。它是当前 conversation 的短期输入状态，直接复用现有用户消息、assistant placeholder、Composer 和 WebSocket outbox。

#### Composer 状态矩阵

| Agent 状态 | 输入内容 | 主按钮 | 附加动作 |
| --- | --- | --- | --- |
| idle | 空 | disabled/send | 无 |
| idle | 非空 | Send | Enter 立即发送 |
| streaming | 空 | Stop | 停止当前回复 |
| streaming | 非空 | Queue message | 保留独立的 Stop current response |

`Shift+Enter` 始终换行。排队提交后清空 Composer，但不重置当前回合的 plan、todo、progress、streaming 文本或工具活动。

#### 消息与协议投影

前端乐观创建用户气泡和对应 assistant placeholder，并通过稳定的 `user_message_id` / `assistant_message_id` 与服务端更新关联：

```text
queued     -> 用户气泡显示 Queued，可取消；assistant placeholder 保持非 streaming
dequeued   -> 清除 Queued，placeholder 才进入 streaming，成为下一回合
cancelled  -> 用户气泡显示 Queue cancelled；queue_full 时同时显示可恢复错误
```

取消命令只删除指定排队项，不停止当前回复。Stop 只停止当前回复，服务端 cleanup 后继续 FIFO。这样两个动作语义不会混在一个按钮里。

#### 已落地文件与验收

- `sendChatMessage.ts`：发送/排队共享一个命令构造路径，不复制附件和上下文逻辑；
- `Composer.tsx`、`FooterRow.tsx`：实现 send/stop/queue 状态矩阵和双动作布局；
- `sessionEvents.ts`：集中处理 `queued | dequeued | cancelled`；
- `UserMessageCell.tsx`、`ChatTurn.tsx`：状态徽标、单条取消和上下文菜单；
- protocol/store 类型：只增加一个服务端事件和一个客户端取消命令；
- 测试覆盖 Enter、按钮状态、乐观消息、出队后 streaming、取消与 queue full 投影。

## 6. P1：重建稳定的信息层级

### 6.1 单一主导航模型

建议将界面概念固定为三层：

```text
第 1 层：产品模式        Cowork | Code
第 2 层：工作区域        Conversation / Files / Editor / Diff / Terminal
第 3 层：上下文检查器    Agent activity / Changes / Usage / Inspector
```

具体规则：

- Header 只承担全局操作和 Cowork/Code 主模式。
- Chat 不再作为与 Cowork、Code 平级的第三种长期模式；Cowork 的主工作面本来就是对话。
- File 只作为 Code 工作区的标签或左侧导航内容，不与 Chat 形成第二组全局模式。
- 右栏只展示当前任务的上下文信息，不复制主导航。
- 新会话继承当前主模式和工作区绑定，Header 的 `+` 行为保持现有逻辑。

如果当前 `chat` appMode 有兼容用途，迁移分两步：先停止在新 UI 中创建该模式，再将旧持久化值在 store hydration 时映射为 `cowork`。不需要一次性修改所有历史数据。

### 6.2 聊天阅读轴

#### 当前问题

1440px 截图中，用户消息靠右、回答和活动靠左，内容横跨工作区。长 Markdown、工具记录和 Diff 的扫描路径过长。

#### 目标

为整个 turn 建立一个共享内容轴：

```css
.chat-reading-column {
  width: min(100%, 880px);
  margin-inline: auto;
  padding-inline: var(--space-4);
}
```

这里的 `880px` 是起始建议，应以真实 Markdown 和 Diff 截图微调。关键不是具体数字，而是：

- 用户消息、Agent 活动和最终回答使用同一列宽；
- 用户气泡可以右对齐，但不能脱离 turn 的内容轴；
- 代码块和 Diff 可在列内横向滚动，不将整页撑宽；
- 窄屏 gutter 逐级减小，但不让文字贴边；
- Composer 与消息阅读列对齐，形成稳定垂直基线。

优先在 `MessageList` / `ChatTurn` 的共享容器上做一次约束，不要给每个 cell 单独设置 max-width。

### 6.3 Agent 活动层级

每一轮只需要三个视觉层次：

1. 用户请求；
2. 可折叠的工作过程；
3. 最终回答。

工作过程内部保持现有结构化 cell，但默认策略应统一：

- 正在运行：展开当前活动，并显示明确状态；
- 完成：折叠重复的读取、搜索和命令明细，只保留一行结构化摘要；
- 失败或等待批准：保持展开，错误/批准控件靠近对应工具；
- 最终回答：永远不埋在活动折叠区内。

`project-turn.ts` 中的 `summaryItems` 当前有计算和测试，却没有 UI 消费者。不要再创建一个消费者来证明它有用；直接删除字段、构建逻辑和只覆盖该字段的测试。已有 `ActivityGroupCell` 自己生成并显示摘要，保留一个真实路径即可。

### 6.4 空状态

空状态应是一块工作区域，不是多个营销 Card。它只需要：

- 清晰显示当前模式和工作区；
- 一个主输入框；
- 缺失模型或工作区时给出一个主动作；
- 最多 3 个紧凑的任务模板，点击后填入 Composer，不直接发送；
- 最近会话放在左栏，不在中央重复。

删除 Onboarding 后，空状态成为唯一新手入口，因此必须覆盖键盘导航、加载、错误和 Provider 未配置状态。

## 7. P1：样式系统止血与收敛

### 7.1 问题证据

`frontend/src.v2/styles/workbench-polish.css` 约 74.4 KB，其中约 600 个 `!important`。本次统计中，同一选择器被多次定义：

| 选择器 | 出现次数 |
| --- | ---: |
| `.chat-turn-process` | 64 |
| `.mc-sidebar-right` | 42 |
| `.composer-container` | 28 |

这意味着浏览器最终样式取决于文件顺序和最后一次覆盖，而不是组件本身的明确契约。

### 7.2 不要做的事

- 不要一次性把 745 处行内样式全部搬进 CSS。
- 不要再增加 `workbench-polish-v2.css`。
- 不要引入 CSS-in-JS、另一套 utility framework 或新主题库。
- 不要按文件大小机械拆 CSS；拆分必须对应组件所有权。

### 7.3 渐进迁移方法

每次只处理一个可截图的视觉区域：

1. 选定组件，例如 Composer。
2. 列出该组件来自行内样式、组件 CSS、tokens 和 `workbench-polish.css` 的所有规则。
3. 将基础布局、响应式和交互状态放回 `composer.css`。
4. 删除 `workbench-polish.css` 中对应规则，而不是留下双份。
5. 跑组件测试和 5 个目标视口截图。
6. 确认无回归后再处理下一个区域。

建议顺序：

```text
Composer
-> Header / WorkbenchShell
-> SidebarLeft / SidebarRight
-> Chat reading column / ChatTurn
-> Agent cells
-> overlays
-> 删除剩余 workbench-polish.css
```

### 7.4 样式所有权规则

| 样式类型 | 唯一归属 |
| --- | --- |
| 色彩、字体、间距、圆角、层级、动画时间 | `styles/*tokens*.css` |
| 组件基础布局和状态 | 组件相邻 CSS |
| 跨组件工作台布局 | `shell` 层 CSS |
| 一次性动态数值，如拖拽宽度 | 行内 style |
| 响应式规则 | 拥有该布局的组件 CSS |
| 紧急浏览器兼容覆盖 | 原组件 CSS，并附原因注释 |

只有确实需要压过第三方库内联样式时才允许 `!important`。阶段结束时应能解释每一处剩余用法，而不是追求绝对为零。

### 7.5 动效收敛

生产代码中出现约 40 种时间值，但 token 只声明了 4 种动画时长。建议保留现有 4 档，并将常用行为映射为：

| 行为 | 时长档位 |
| --- | --- |
| hover / press / focus | fast |
| menu / tooltip | normal |
| drawer / modal | slow |
| Agent pulse / progress | 只控制循环节奏，不影响布局 |

所有非必要动画在 `prefers-reduced-motion: reduce` 下关闭。流式文本不要逐字做位移动画，避免长回答期间持续触发布局和绘制。

## 8. P1：可访问性与语义

### 8.1 Dialog 契约

项目已有 `frontend/src.v2/hooks/useFocusTrap.ts`，但目前主要由 Onboarding 使用，其他多个 `role="dialog"` 未统一使用。删除 Onboarding 后应把这个 Hook 用在真实 Dialog 上。

所有模态层必须满足：

- 打开时焦点进入首个可操作元素或标题；
- Tab / Shift+Tab 不离开 Dialog；
- Escape 关闭非破坏性 Dialog；
- 关闭后焦点返回触发按钮；
- 使用 `aria-labelledby` 或明确 `aria-label`；
- 背景不可通过指针或键盘操作；
- 确认危险操作时，默认焦点不放在危险按钮上。

Side Chat 已设置 `role="dialog"` 和 `aria-modal`，但还应补焦点约束和返回焦点。

### 8.2 原生交互元素

已发现：

- `ActionChipRegion` 的删除动作由可点击 `span` 承担；
- Toast 的交互由可点击 `div` 承担。

直接替换为 `<button type="button">`。保留视觉样式，增加 `aria-label`，让 Enter 和 Space 自动工作。不要写自定义 keydown 来模拟按钮。

### 8.3 Backdrop

`frontend/src.v2/components/SharedBackdrop.tsx` 当前没有调用方，应直接删除。需要遮罩的 Drawer/Dialog 使用一个真实共享容器，或在现有 `DialogService` 内统一；不要保留“以后也许会用”的组件。

### 8.4 对比度

本次 token 实测中存在以下低于常用正文目标的组合：

| 组合 | 对比度 |
| --- | ---: |
| light accent / page | 4.40:1 |
| light accent / raised | 3.76:1 |
| dark text-on-accent | 3.00:1 |
| light warning / base | 3.57:1 |

处理方法：

- 正文和小字号交互文本以 `4.5:1` 为最低目标；
- 大图标、边框和大字号状态可按 `3:1` 评估；
- 只调整 `accent`、`text-on-accent`、`warning` 等 token；
- 同时验证 light/dark，不在组件里追加主题特例；
- 状态不能只靠颜色，仍需图标、文字或形状。

## 9. P1：收敛 Agent 事件投影

### 9.1 当前问题

`chatSurfaceState.ts` 已超过 1,800 行，当前工作树实际接近 2,000 行；`project-turn.ts` 超过 1,200 行。两者包含大量：

- 工具名正则；
- 错误文本正则；
- 文件/命令/搜索类型推断；
- 显示摘要回退；
- 历史记录兼容；
- UI cell 构建。

后端已经在工具事件中提供：

- `result_kind`
- `activity_kind`
- `display_summary`
- `display_scope`
- `panel_hint`

继续在实时前端按工具名猜测，会让新增工具必须同时修改后端和多个前端分类器。

### 9.2 目标边界

```text
实时事件：信任后端结构化字段
历史水合：只在 transcriptHydration 做旧格式兼容
视图投影：按 kind 选择 cell，不再识别工具名
```

实施顺序：

1. 先让后端所有成功、失败、拒绝、超时工具结果都填充结构化字段。
2. 给前端协议类型将关键字段从可选逐步改为必填。
3. `chatStreamEvents.ts` 对实时事件直接落库，不再调用文本分类器。
4. 旧 transcript 缺字段时，仅在 `transcriptHydration.ts` 推断一次，并标记 `legacyProjection` 供诊断。
5. 删除 `chatSurfaceState.ts` 中只服务于实时事件的工具名/错误文本正则。
6. 将剩余投影按 cell 类型拆成纯函数文件，但只在真实边界清晰后拆，不按行数拆。

### 9.3 不能破坏的兼容性

- 旧会话仍可打开；
- 正在运行的会话重连后不会重复生成工具 cell；
- 同一个 `tool_call_id` 只对应一个最终结果；
- Diff、命令退出码、审批状态和 Subagent 状态保持现有展示；
- 未识别 kind 显示通用 Tool cell，不应让整轮渲染失败。

## 10. P2：性能优化

性能工作应排在功能一致性和样式所有权之后。当前项目已经使用 `React.lazy` 加载 Settings、CommandPalette、Editor、Terminal 和代码高亮，说明基础方向正确。

### 10.1 先测什么

使用浏览器和 Vite 已有能力记录：

- 冷启动到空状态可交互时间；
- 打开 100 轮会话后的首次渲染时间；
- 流式输出时每秒 React commit 次数；
- 首次进入 Code 模式时 Monaco 和 worker 的加载；
- 首次打开 Diff / Terminal / Settings 的增量资源；
- 390px 设备上打开键盘后的 Composer 可视高度。

不需要先安装 bundle analyzer。Vite 构建输出、浏览器 Network/Performance 和现有测试已足够判断最大块。

### 10.2 建议动作

- 保证 `EditorPanel` 及顶层 worker import 只随 Code/Editor chunk 加载，不进入 Cowork 首屏。
- 对长会话继续使用已有虚拟列表，但验证动态 cell 高度和折叠后测量更新。
- Zustand selector 只订阅组件需要的字段，避免流式 token 让整个工作台重渲染。
- 对 Markdown 流式内容批量刷新，避免每个极小 token 都触发完整高亮。
- Mermaid、语法高亮和 Monaco 仅在内容真正出现时加载。
- 删除无 UI 消费者的派生数据，比继续 `useMemo` 更有效。

### 10.3 建议预算

先记录真实基线，再设置不回退预算：

- Cowork 首屏不得加载 Monaco、xterm 或 Mermaid 主体；
- 主包 gzip 不得因 UI 优化增长；
- 100 轮会话滚动和流式回复期间不出现持续长任务；
- Drawer、Dialog 和 Composer 状态切换不产生布局抖动。

## 11. 文案与语言

当前主界面以英文为主，但 Onboarding、Subagents、部分 Agent 活动和错误提示使用中文，存在同屏混用。

最短方案不是立即引入完整 i18n：

1. 先确定当前发行版主语言。
2. 将 Header、空状态、Composer、审批、错误、Agent 状态这 6 条主流程统一。
3. 将后端提供的用户可见摘要也遵循同一语言配置。
4. 暂不翻译开发者诊断、日志和协议字段。
5. 等第二种语言确有发布需求时，再抽离文案表。

若当前产品目标用户主要是中文用户，建议主流程统一为简体中文；工具名、模型名、代码术语保留原文。

## 12. 逐文件修改清单

| 文件 | 建议操作 | 验收重点 |
| --- | --- | --- |
| `frontend/src.v2/App.tsx` | 删除 Onboarding import/渲染 | 首次启动直接进入可操作空状态 |
| `frontend/src.v2/overlays/Onboarding.tsx` | 删除 | E2E 不再被遮罩拦截 |
| `frontend/src.v2/stores/ui-slice.ts` | 删除 onboarding 状态和持久化；保留真实 UI 状态 | 旧 localStorage 键存在时不报错 |
| `frontend/src.v2/stores/types.ts` | 删除 onboarding 类型；收敛窄屏 Drawer 与排队消息状态 | TypeScript 构建通过 |
| `frontend/src.v2/stores/agent-slice.ts` | 删除 onboarding capability 特判 | capability 更新不再修改无效状态 |
| `frontend/src.v2/shell/WorkbenchShell.tsx` | 顶层统一窄屏判断；复用 Sidebar 做 Drawer；补焦点管理 | 390/740/768/1440 均可访问侧栏 |
| `frontend/src.v2/shell/HeaderBar.tsx` | 控件与实际能力同步；补 expanded/controls | 不再出现无响应按钮 |
| `frontend/src.v2/composer/FooterRow.tsx` | 现有控件重排，移除 spacer 驱动布局 | 长模型名和路径不挤压发送按钮 |
| `frontend/src.v2/chat/sendChatMessage.ts` | 复用发送路径创建 queued 用户消息和 placeholder | 附件、上下文、ID 与普通发送一致 |
| `frontend/src.v2/chat/sessionEvents.ts` | 集中投影 queue updated 三种状态 | queued 不提前重置当前回合，dequeued 才 streaming |
| `frontend/src.v2/chat/cells/UserMessageCell.tsx` | 增加排队徽标与单条取消 | 键盘、触摸和上下文菜单均可操作 |
| `frontend/src.v2/composer/composer.css` | 建立三档 grid 和 safe area | 390px 输入、菜单、键盘不重叠 |
| `frontend/src.v2/chat/MessageList.tsx` | 提供共享阅读列容器 | 所有 turn 共享同一内容轴 |
| `frontend/src.v2/chat/components/ChatTurn.tsx` | 保持三层层级；减少单 cell 宽度覆盖 | 用户、过程、回答顺序稳定 |
| `frontend/src.v2/chat/chatSurfaceState.ts` | 实时事件停止正则分类；逐步删除兼容分支 | 新旧会话投影一致 |
| `frontend/src.v2/chat/transcriptHydration.ts` | 集中旧记录兼容 | 兼容逻辑只有一个入口 |
| `frontend/src.v2/agent-loop/projection/project-turn.ts` | 删除未消费 `summaryItems` | 对应测试和类型同步减少 |
| `frontend/src.v2/composer/ActionChipRegion.tsx` | 点击 span 改 button | 键盘可删除 chip |
| `frontend/src.v2/overlays/ToastContainer.tsx` | 点击 div 改 button 或内部显式按钮 | Toast 操作可聚焦、有名称 |
| `frontend/src.v2/components/SharedBackdrop.tsx` | 删除未使用文件 | `rg SharedBackdrop` 无生产引用 |
| `frontend/src.v2/protocol/events.ts` | 补运行时事件与输入队列协议 | protocol sync 通过 |
| `frontend/src.v2/styles/workbench-polish.css` | 冻结、逐区迁回、最终删除 | 每轮都同时减少规则和 `!important` |
| `frontend/src.v2/styles/*tokens*.css` | 只修正共享 token 与对比度 | light/dark 同时达标 |

## 13. 分阶段实施顺序

### 阶段 0：恢复契约绿灯

1. 补齐 `SERVER_EVENT_TYPES` 的 4 个值。
2. 将协议同步脚本加入常规 CI。
3. 更新 E2E fixture，使首次启动真实覆盖空状态，而不是绕过遮罩。

完成标准：

- `python scripts/check-protocol-sync.py` 通过；
- 前端全量单测和 build 通过；
- E2E 可从首次启动完成“打开工作区”。

### 阶段 1：解决主流程阻塞

1. 删除 Onboarding 及相关状态。
2. 修复窄屏左右侧栏。
3. 重排 Composer。
4. 建立聊天阅读列。

完成标准：

- 390、740、768、1440 四档主流程均可用；
- Header 无无效控件；
- 输入区不遮挡消息；
- 首次启动不需要关闭教程。

### 阶段 2：样式与可访问性

1. 从 Composer 开始迁回 `workbench-polish.css` 规则。
2. 统一 Drawer/Dialog 焦点行为。
3. 修正原生语义和对比度。
4. 统一动效 token 和 reduced-motion。

完成标准：

- 关键流程可全键盘完成；
- light/dark 对比度检查通过；
- 每个迁移组件不再依赖 `workbench-polish.css` 的同名覆盖；
- `!important` 数量持续下降且没有新增。

### 阶段 3：投影与性能

1. 后端补齐结构化展示字段。
2. 实时前端删除工具名/文本推断。
3. 删除 `summaryItems` 等无消费者状态。
4. 测量并收敛首屏与长会话渲染。

完成标准：

- 新工具只需声明后端投影元数据即可正确显示；
- 旧 transcript 仍兼容；
- 主包不增长；
- 长会话和流式回复无明显持续长任务。

## 14. 测试与验收矩阵

### 14.1 视口

| 视口 | 重点 |
| --- | --- |
| 1440 x 900 | 三栏布局、阅读轴、长回答、Diff |
| 1024 x 768 | 侧栏与主区域最小稳定宽度 |
| 768 x 1024 | Composer 换行、菜单边界、右栏 |
| 740 x 900 | 窄屏 Drawer 临界点 |
| 390 x 844 | 手机输入、safe area、长标签、审批 Dialog |

### 14.2 主流程

- 首次启动，无 Provider；
- 有 Provider，无工作区；
- 打开工作区并创建会话；
- 发送普通文本、附件、`@mention` 和 skill；
- 流式回答、停止、继续输入；
- 工具成功、失败、超时和等待审批；
- 打开 Diff、Terminal、Editor、Settings、Command Palette；
- 断线、重连、rate limit、会话恢复；
- 100 轮历史记录滚动；
- 键盘完成上述操作。

### 14.3 自动化建议

组件测试应覆盖行为，不锁死像素：

- Header 在窄屏按钮能打开真实 Drawer；
- 两个 Drawer 互斥；
- Escape 关闭并返回焦点；
- Composer 的发送按钮在长标签下仍存在且可点击；
- 实时事件按结构化 kind 进入对应 cell；
- 旧 transcript 缺 kind 时由 hydration 回退。

Playwright 应至少保留：

- 首次启动到发送第一条消息；
- 390px 打开左右 Drawer 和 Composer 菜单；
- 768px 工作区绑定；
- 1440px Agent 工具回合和 Diff；
- light/dark 两套截图。

### 14.4 每阶段命令

```powershell
cd frontend
npm test -- --run
npm run build
npx playwright test
cd ..
python scripts/check-protocol-sync.py
```

如果全量 E2E 依赖桌面运行时，应将纯浏览器可运行用例与 Electron 用例分开报告，不能用跳过掩盖失败。

## 15. 明确不做

本轮优化明确不包含：

- 重写 React 前端；
- 引入新的组件库、状态库、CSS-in-JS 或动画库；
- 用另一个全局 CSS 文件覆盖现有补丁；
- 全量迁移所有行内 style；
- 为两种语言提前搭建完整翻译平台；
- 为移动端复制一套 Sidebar/Composer；
- 仅为降低文件行数而拆文件；
- 在没有性能数据前替换 Monaco、Markdown 或虚拟列表方案；
- 将全部 Agent 状态塞进一个新 Store；
- 为未验证需求预留新模式或新面板。

## 16. 最终完成定义

当以下条件全部满足，可以认为本轮前端重点优化完成：

- 首次启动直接进入可操作工作区，没有重复 Onboarding；
- 390px 到 1440px 的所有可见 Header 控件都有真实结果；
- Cowork/Code 是唯一主模式层，Chat/File 不再与之竞争；
- 聊天、活动和 Composer 共享稳定阅读轴；
- Composer 在 390、740、768px 下不遮挡、不跳动；
- streaming 时空输入可 Stop、非空输入可 Queue，排队气泡可单条取消且不提前重置当前回合；
- Dialog、Drawer、Toast 和 Chip 可用键盘及读屏操作；
- 关键颜色达到对比度目标；
- 协议同步检查、前端单测、build 和主流程 E2E 全部通过；
- 实时 Agent UI 不再依靠工具名或错误文本猜测已有结构化字段；
- `workbench-polish.css` 被删除，或只剩有明确临时说明且有删除计划的少量规则；
- 没有为本轮优化引入新的运行时依赖。
