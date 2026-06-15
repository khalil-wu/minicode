# MiniCode UI/UX 优化计划（对标 Cursor/Claude Code Desktop）

**基于**：MiniCode 源码全量审计 + ClaudeCode-ref 还原分析 + tokens.css 设计规范 + 行业最佳实践

**目标**：达到 Cursor/Windsurf/Claude Code Desktop 的视觉质量和交互体验

---

## 执行摘要

**当前状态**：
- ✅ 功能完备（断点续跑、工具折叠、任务持久化）
- ✅ Design token 系统完善（tokens.css，OKLCH 色彩空间）
- ⚠️ 视觉执行不一致（部分组件未充分使用 token）
- ⚠️ 交互反馈薄弱（hover/active 状态不明显）
- ⚠️ 信息密度需优化（层次不清晰）

**核心问题**：基础设施完善，但**执行层面的视觉细节和交互打磨不足**。

**预计工作量**：1-2 天（高优先级 4-6 小时，中优先级 4-6 小时，低优先级可选）

---

## 高优先级（4-6 小时，立即可见）

### 1. 消息流视觉层次优化（2 小时）

**问题**：用户消息、AI 回复、工具调用视觉区分度不足

**对标 Cursor/Claude Code**：
- **用户消息**：右对齐，浅色背景（`var(--surface-soft)`），圆角卡片
- **AI 回复**：左对齐，无背景或更浅背景（`var(--surface-base)`）
- **工具调用**：内嵌在 AI 回复内，灰色边框（`var(--border-subtle)`），更小字号

**实施**：
```tsx
// frontend/src.v2/chat/cells/UserCell.tsx
<div className="flex justify-end mb-3">
  <div className="bg-[var(--surface-soft)] rounded-lg px-4 py-2.5 max-w-[80%]">
    {content}
  </div>
</div>

// frontend/src.v2/chat/cells/AssistantCell.tsx
<div className="flex justify-start mb-3">
  <div className="bg-transparent px-2 py-1 max-w-[85%]">
    {content}
    {/* ToolCallCard 保持当前样式，但字号改为 text-xs */}
  </div>
</div>
```

**文件**：
- `frontend/src.v2/chat/cells/UserCell.tsx`
- `frontend/src.v2/chat/cells/AssistantCell.tsx`
- `frontend/src.v2/chat/tool-calls/ToolCallCard.tsx`（字号 `text-sm` → `text-xs`）

---

### 2. 输入框 focus 状态增强（30 分钟）

**问题**：输入框 focus 时边框无明显变化

**对标 Cursor/Claude Code**：focus 时边框变为品牌色（蓝色/棕色），有 0.2s 过渡

**实施**：
```tsx
// frontend/src.v2/composer/ComposerTextarea.tsx
className="... border-2 border-[var(--border-soft)] focus:border-[var(--accent-primary)] transition-colors duration-200"
```

**文件**：
- `frontend/src.v2/composer/ComposerTextarea.tsx`

---

### 3. 按钮交互反馈强化（1 小时）

**问题**：按钮 hover/active 状态变化不明显

**对标 Cursor**：
- hover：背景色变深 + 轻微阴影
- active：缩小 0.98x（`transform: scale(0.98)`）

**实施**：
```css
/* frontend/src.v2/styles/utility.css */
.btn-primary {
  transition: all 150ms var(--easing-standard);
}
.btn-primary:hover {
  background: var(--accent-hover);
  box-shadow: var(--shadow-sm);
}
.btn-primary:active {
  transform: scale(0.98);
  background: var(--accent-active);
}
```

**文件**：
- `frontend/src.v2/styles/utility.css`
- `frontend/src.v2/composer/ComposerActions.tsx`（应用 `.btn-primary`）
- `frontend/src.v2/shell/Toolbar.tsx`（New / Settings 按钮）

---

### 4. 工具调用长输出折叠（1.5 小时）

**问题**：工具调用展开后，长输出（>500 字符）无折叠，撑爆视口

**对标 Codex**：长输出默认折叠到 "Show more" 按钮

**实施**：
```tsx
// frontend/src.v2/chat/tool-calls/ToolCallCard.tsx
const [outputExpanded, setOutputExpanded] = useState(false);
const resultSummary = record.displaySummary || rawResultSummary;
const isLongOutput = resultSummary.length > 500;

{isLongOutput && !outputExpanded ? (
  <>
    <div>{resultSummary.slice(0, 500)}...</div>
    <button onClick={() => setOutputExpanded(true)} className="text-[var(--accent-primary)] text-xs">
      Show more
    </button>
  </>
) : (
  <div>{resultSummary}</div>
)}
```

**文件**：
- `frontend/src.v2/chat/tool-calls/ToolCallCard.tsx`

---

## 中优先级（4-6 小时，提升体验）

### 5. Toast 通知图标和颜色（1 小时）

**问题**：toast 只有文本，无图标区分 success/error/info

**对标 Cursor**：success 绿色勾，error 红色叉，info 蓝色 i

**实施**：
```tsx
// frontend/src.v2/overlays/ToastContainer.tsx
import { CheckCircle, XCircle, Info, AlertTriangle } from "lucide-react";

const ICON_MAP = {
  success: <CheckCircle size={16} color="var(--state-success)" />,
  error: <XCircle size={16} color="var(--state-danger)" />,
  info: <Info size={16} color="var(--state-info)" />,
  warning: <AlertTriangle size={16} color="var(--state-warning)" />,
};

<div className="flex items-center gap-2">
  {ICON_MAP[level]}
  <span>{message}</span>
</div>
```

**文件**：
- `frontend/src.v2/overlays/ToastContainer.tsx`

---

### 6. 右侧面板切换过渡（2 小时）

**问题**：Diff/Plan/Inspector 切换无淡入淡出

**对标 Codex**：旧内容淡出 200ms，新内容淡入 200ms

**实施**：
```tsx
// frontend/src.v2/shell/RightStack.tsx
const [animating, setAnimating] = useState(false);

const switchPanel = (newPanel) => {
  setAnimating(true);
  setTimeout(() => {
    setActivePanel(newPanel);
    setAnimating(false);
  }, 200);
};

<div className={`transition-opacity duration-200 ${animating ? 'opacity-0' : 'opacity-100'}`}>
  {activePanel}
</div>
```

**文件**：
- `frontend/src.v2/shell/RightStack.tsx`

---

### 7. 附件 chip 截断优化（30 分钟）

**问题**：长文件名挤压输入区

**对标 Codex**：文件名超 25 字符截断为 `filename...ext`

**实施**：
```tsx
// frontend/src.v2/composer/AttachmentChips.tsx
const truncateFilename = (name: string) => {
  if (name.length <= 25) return name;
  const ext = name.split('.').pop();
  const base = name.slice(0, 18);
  return `${base}...${ext}`;
};

<span title={file.name}>{truncateFilename(file.name)}</span>
```

**文件**：
- `frontend/src.v2/composer/AttachmentChips.tsx`

---

### 8. 字体层级补充（1 小时）

**问题**：标题、正文、标签字号相同

**对标 Cursor**：标题 15px semibold，正文 13px，标签 11px

**实施**：
```tsx
// 审查并调整以下组件的字号
- 消息标题（用户名/时间戳）：text-sm → text-base font-semibold
- 正文内容：保持 text-sm（13px）
- 工具调用标签：text-sm → text-xs（11px）
- 按钮文字：text-sm → text-xs font-medium
```

**文件**：
- `frontend/src.v2/chat/cells/*.tsx`（统一调整）
- `frontend/src.v2/chat/tool-calls/ToolCallCard.tsx`

---

## 低优先级（可选，细节打磨）

### 9. 圆角半径统一（30 分钟）

**问题**：按钮 6px，卡片 8px，输入框 4px，不统一

**实施**：全局统一到 `--radius-sm: 6px`（小组件）和 `--radius-md: 8px`（卡片）

---

### 10. 代码块复制按钮（1 小时）

**问题**：AI 回复中的 markdown 代码块无复制按钮

**实施**：为 `<pre><code>` 添加悬浮复制按钮（复用 ToolCallCard 的 Copy 逻辑）

---

### 11. 自定义滚动条（30 分钟）

**问题**：Windows 默认滚动条宽且突兀

**实施**：
```css
/* frontend/src.v2/styles/global.css */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}
::-webkit-scrollbar-thumb {
  background: var(--border-soft);
  border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover {
  background: var(--border-strong);
}
```

---

### 12. loading 骨架屏（1.5 小时）

**问题**：消息加载时空白

**实施**：为 MessageList 的 loading 状态添加 3 行灰色脉动骨架屏（shimmer effect）

---

## 实施顺序建议

**第一阶段（4 小时，立即可见）**：
1. 消息流视觉层次（#1）
2. 输入框 focus 状态（#2）
3. 按钮交互反馈（#3）
4. 工具调用长输出折叠（#4）

**第二阶段（4 小时，提升体验）**：
5. Toast 图标（#5）
6. 右侧面板过渡（#6）
7. 附件截断（#7）
8. 字体层级（#8）

**第三阶段（可选，2-3 小时）**：
9-12. 细节打磨

---

## 验证标准

完成后对比 Cursor/Claude Code Desktop，检查：
- ✅ 消息流视觉层次清晰（一眼区分用户/AI/工具）
- ✅ 交互反馈明显（hover/active 状态可见）
- ✅ 信息密度合理（不拥挤，有呼吸感）
- ✅ 过渡动画流畅（200ms 标准）

---

## 风险缓解

1. **CSS 优先级冲突**：使用 `!important` 时需在 commit message 注明原因
2. **性能影响**：transition 只用于小组件（按钮/输入框），大列表避免
3. **兼容性**：`::-webkit-scrollbar` 不支持 Firefox，需 fallback

---

*本文档基于 MiniCode 源码审计（2026-06-11）+ ClaudeCode-ref 还原分析 + tokens.css 设计规范编写。*
