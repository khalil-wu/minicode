# MiniCode UI/UX 优化方案

## 🎯 目标

优化所有影响用户体验的 UI 问题，包括：
- 打开后的遮挡问题
- 布局和间距问题
- 响应式设计
- 动画和过渡
- 可访问性

---

## 🔍 常见 UI 问题诊断

### 1. 遮挡问题（Z-Index 层级）

#### 问题描述
- 模态框被其他元素遮挡
- 侧边栏打开时覆盖主内容
- Toast 通知被遮挡
- Tooltip 显示在错误的层级

#### 当前层级（从 WorkbenchShell.tsx）
```
Position: fixed
├── 900: Side Chat (sideChatOpen)
├── ?: Modals (CommandPalette, Settings, etc.)
├── ?: Toast Container
├── ?: SharedBackdrop
└── 0: WorkbenchShell (base)
```

#### 建议的 Z-Index 系统
```css
/* z-index.css - 统一的层级管理 */
:root {
  --z-base: 0;              /* 基础内容 */
  --z-header: 100;          /* 顶部导航栏 */
  --z-sidebar: 200;         /* 侧边栏 */
  --z-dock: 250;            /* 底部 Dock */
  --z-dropdown: 500;        /* 下拉菜单 */
  --z-tooltip: 600;         /* Tooltip */
  --z-modal-backdrop: 900;  /* 模态框背景 */
  --z-modal: 1000;          /* 模态框内容 */
  --z-toast: 1100;          /* Toast 通知 */
  --z-debug: 9999;          /* 调试工具 */
}
```

---

### 2. 布局问题

#### 问题 A：固定高度导致内容截断

**症状：**
- 长内容滚动不可见
- 底部内容被截断

**解决方案：**
```css
/* 使用 flexbox 和 min-height: 0 */
.container {
  display: flex;
  flex-direction: column;
  min-height: 0; /* 关键：允许 flex 子元素收缩 */
}

.scrollable-content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
```

#### 问题 B：侧边栏宽度不当

**症状：**
- 侧边栏太宽，主内容区域太窄
- 小屏幕上侧边栏占据过多空间

**解决方案：**
```css
/* 响应式侧边栏宽度 */
.sidebar-left {
  width: clamp(200px, 20vw, 280px);
  /* 最小 200px，理想 20vw，最大 280px */
}

.sidebar-right {
  width: clamp(250px, 25vw, 400px);
}

/* 小屏幕上使用 overlay */
@media (max-width: 768px) {
  .sidebar-left,
  .sidebar-right {
    position: fixed;
    left: 0;
    top: 0;
    width: min(320px, 80vw);
    z-index: var(--z-sidebar);
    box-shadow: var(--shadow-large);
  }
}
```

---

### 3. 打开后遮挡问题

#### 问题 A：侧栏打开时遮挡输入框

**当前问题：**
```tsx
// WorkbenchShell.tsx:84
{rightPanelOpen && <SidebarRight />}
```

如果 `SidebarRight` 的 `position: fixed`，会覆盖主内容。

**解决方案：**
```css
/* 方案 1：使用 flex 布局，不使用 fixed */
.cowork-layout {
  display: flex;
  gap: 0;
}

.main-content {
  flex: 1;
  min-width: 0; /* 允许收缩 */
}

.sidebar-right {
  width: 320px;
  flex-shrink: 0;
  /* 不使用 position: fixed */
}
```

```css
/* 方案 2：如果必须用 fixed，添加右侧边距 */
.main-content {
  margin-right: 0;
  transition: margin-right 200ms ease;
}

.main-content.sidebar-open {
  margin-right: 320px;
}
```

#### 问题 B：模态框打开时输入框仍可交互

**症状：**
- 模态框打开，但背景内容仍可点击
- 键盘输入可能进入错误的输入框

**解决方案：**
```tsx
// 模态框打开时禁用背景
useEffect(() => {
  if (modalOpen) {
    document.body.style.overflow = 'hidden';
    // 捕获焦点
    const previouslyFocused = document.activeElement;
    modalRef.current?.focus();
    
    return () => {
      document.body.style.overflow = '';
      (previouslyFocused as HTMLElement)?.focus();
    };
  }
}, [modalOpen]);
```

---

### 4. 动画性能问题

#### 问题：动画卡顿

**原因：**
- 动画使用了触发 layout 的属性（width, height, margin）
- 动画元素过多

**解决方案：**
```css
/* ❌ 避免 */
.panel {
  transition: width 300ms ease;
}

/* ✅ 使用 transform */
.panel {
  transform: translateX(-100%);
  transition: transform 300ms ease;
}

.panel.open {
  transform: translateX(0);
}
```

```css
/* 开启硬件加速 */
.animated-element {
  will-change: transform;
  /* 动画结束后移除 will-change */
}
```

---

### 5. 响应式设计问题

#### 问题：小屏幕上布局混乱

**解决方案：**
```css
/* 移动端优化 */
@media (max-width: 640px) {
  /* 隐藏次要侧边栏 */
  .sidebar-left {
    display: none;
  }
  
  /* 输入框字体大小防止缩放 */
  input, textarea {
    font-size: 16px; /* iOS 不会缩放 */
  }
  
  /* 减小间距 */
  .message-list {
    padding: 16px 12px;
  }
}

@media (max-width: 1024px) {
  /* 侧边栏变为 overlay */
  .sidebar-right {
    position: fixed;
    right: 0;
    top: 0;
    height: 100vh;
    transform: translateX(100%);
    transition: transform 250ms ease;
  }
  
  .sidebar-right.open {
    transform: translateX(0);
  }
}
```

---

## 🎨 具体优化实施

### 优化 1：统一 Z-Index 系统

**创建新文件：** `frontend/src.v2/styles/z-index.css`

```css
/**
 * z-index.css - 统一的层级管理系统
 * 
 * 遵循 10 的倍数，为未来扩展留空间
 */

:root {
  /* ── 基础层 ────────────────── */
  --z-base: 0;
  --z-below: -1;
  
  /* ── 内容层 ────────────────── */
  --z-content: 1;
  --z-sticky: 10;
  
  /* ── UI 元素 ────────────────── */
  --z-header: 100;
  --z-sidebar: 200;
  --z-dock: 250;
  --z-fab: 300;              /* Floating action button */
  
  /* ── 浮层 ────────────────── */
  --z-dropdown: 500;
  --z-popover: 550;
  --z-tooltip: 600;
  
  /* ── 覆盖层 ────────────────── */
  --z-drawer: 800;           /* 抽屉 */
  --z-modal-backdrop: 900;   /* 模态框背景 */
  --z-modal: 1000;           /* 模态框 */
  --z-toast: 1100;           /* Toast 通知 */
  --z-loading: 1200;         /* 全屏加载 */
  
  /* ── 系统层 ────────────────── */
  --z-debug: 9999;           /* 调试工具 */
}

/* 应用到具体元素 */
.header-bar {
  z-index: var(--z-header);
}

.sidebar-left,
.sidebar-right {
  z-index: var(--z-sidebar);
}

.bottom-dock {
  z-index: var(--z-dock);
}

.dropdown-menu {
  z-index: var(--z-dropdown);
}

.tooltip {
  z-index: var(--z-tooltip);
}

.modal-backdrop {
  z-index: var(--z-modal-backdrop);
}

.modal {
  z-index: var(--z-modal);
}

.toast-container {
  z-index: var(--z-toast);
}
```

**在 `frontend/src.v2/main.tsx` 中导入：**
```tsx
import "./styles/reset.css";
import "./styles/tokens.css";
import "./styles/z-index.css";  // 🆕 新增
import "./styles/utilities.css";
import "./styles/animations.css";
```

---

### 优化 2：修复侧栏遮挡问题

**方案：确保侧栏使用 flex 布局，而不是 fixed**

**检查点：**
1. `SidebarRight` 是否使用 `position: fixed`？
2. 主内容区域是否有正确的 margin？
3. 响应式时是否正确切换为 overlay？

**修复示例（如果需要）：**

```tsx
// SidebarRight.tsx 的容器样式
<div
  style={{
    width: 'clamp(280px, 25vw, 400px)',
    flexShrink: 0,
    display: 'flex',
    flexDirection: 'column',
    borderLeft: '1px solid var(--border-subtle)',
    background: 'var(--surface-soft)',
    overflow: 'hidden',
    // 不使用 position: fixed
  }}
>
  {/* 内容 */}
</div>
```

---

### 优化 3：Toast 通知位置和层级

**问题：** Toast 可能被其他元素遮挡

**文件：** `frontend/src.v2/overlays/ToastContainer.tsx`

**确保：**
```tsx
<div
  style={{
    position: 'fixed',
    top: 16,
    right: 16,
    zIndex: 'var(--z-toast)', // 使用统一的 z-index
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
    pointerEvents: 'none', // 允许点击穿透
  }}
>
  {toasts.map(toast => (
    <div
      key={toast.id}
      style={{
        pointerEvents: 'auto', // 但 toast 本身可点击
        // ...
      }}
    >
      {/* Toast 内容 */}
    </div>
  ))}
</div>
```

---

### 优化 4：模态框焦点管理

**创建 Hook：** `frontend/src.v2/hooks/useFocusTrap.ts`

```typescript
import { useEffect, useRef } from 'react';

export function useFocusTrap(isActive: boolean) {
  const containerRef = useRef<HTMLDivElement>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!isActive) return;

    // 保存当前焦点
    previouslyFocusedRef.current = document.activeElement as HTMLElement;

    // 禁止背景滚动
    document.body.style.overflow = 'hidden';

    // 焦点移到容器
    containerRef.current?.focus();

    // 焦点陷阱
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;

      const focusable = containerRef.current?.querySelectorAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      );
      if (!focusable || focusable.length === 0) return;

      const first = focusable[0] as HTMLElement;
      const last = focusable[focusable.length - 1] as HTMLElement;

      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown);

    return () => {
      document.body.style.overflow = '';
      document.removeEventListener('keydown', handleKeyDown);
      previouslyFocusedRef.current?.focus();
    };
  }, [isActive]);

  return containerRef;
}
```

**使用：**
```tsx
// CommandPalette.tsx
export function CommandPalette() {
  const isOpen = useAppStore(s => s.commandPaletteOpen);
  const containerRef = useFocusTrap(isOpen);

  return (
    <div ref={containerRef} tabIndex={-1} role="dialog" aria-modal="true">
      {/* 模态框内容 */}
    </div>
  );
}
```

---

### 优化 5：响应式布局

**创建断点系统：** `frontend/src.v2/styles/breakpoints.css`

```css
/**
 * breakpoints.css - 响应式断点
 */

:root {
  --breakpoint-sm: 640px;
  --breakpoint-md: 768px;
  --breakpoint-lg: 1024px;
  --breakpoint-xl: 1280px;
  --breakpoint-2xl: 1536px;
}

/* 工具类 */
@media (max-width: 640px) {
  .sm-hide {
    display: none !important;
  }
}

@media (max-width: 768px) {
  .md-hide {
    display: none !important;
  }
}

@media (max-width: 1024px) {
  .lg-hide {
    display: none !important;
  }
}

/* 侧边栏响应式 */
@media (max-width: 1024px) {
  .sidebar-left {
    display: none; /* 或切换为 overlay */
  }
  
  .sidebar-right {
    position: fixed;
    right: 0;
    top: 0;
    height: 100vh;
    width: min(400px, 80vw);
    z-index: var(--z-drawer);
    box-shadow: var(--shadow-large);
    transform: translateX(100%);
    transition: transform 250ms cubic-bezier(0.4, 0, 0.2, 1);
  }
  
  .sidebar-right.open {
    transform: translateX(0);
  }
  
  /* 添加遮罩 */
  .sidebar-backdrop {
    position: fixed;
    inset: 0;
    background: var(--backdrop-subtle);
    z-index: calc(var(--z-drawer) - 1);
    opacity: 0;
    pointer-events: none;
    transition: opacity 250ms ease;
  }
  
  .sidebar-backdrop.visible {
    opacity: 1;
    pointer-events: auto;
  }
}
```

---

### 优化 6：滚动优化

**问题：** 嵌套滚动容器导致滚动不流畅

**解决方案：**
```css
/* 滚动容器优化 */
.scroll-container {
  overflow-y: auto;
  overscroll-behavior: contain; /* 防止滚动穿透 */
  -webkit-overflow-scrolling: touch; /* iOS 平滑滚动 */
  scrollbar-gutter: stable; /* 防止滚动条出现时跳动 */
}

/* 自定义滚动条（可选）*/
.scroll-container::-webkit-scrollbar {
  width: 8px;
}

.scroll-container::-webkit-scrollbar-track {
  background: transparent;
}

.scroll-container::-webkit-scrollbar-thumb {
  background: var(--border-soft);
  border-radius: 4px;
}

.scroll-container::-webkit-scrollbar-thumb:hover {
  background: var(--border-strong);
}
```

---

### 优化 7：输入框体验

**问题：** 输入框被虚拟键盘遮挡（移动端）

**解决方案：**
```tsx
// ChatInput.tsx
export function ChatInput() {
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const handleFocus = () => {
      // 等待键盘弹出
      setTimeout(() => {
        inputRef.current?.scrollIntoView({
          behavior: 'smooth',
          block: 'center'
        });
      }, 300);
    };

    const input = inputRef.current;
    input?.addEventListener('focus', handleFocus);
    return () => input?.removeEventListener('focus', handleFocus);
  }, []);

  return (
    <textarea
      ref={inputRef}
      style={{
        fontSize: '16px', // iOS 不缩放
        // ...
      }}
    />
  );
}
```

---

## 📋 检查清单

### 层级检查

- [ ] Header Bar 在最上层（z-index: 100）
- [ ] Toast 在所有内容之上（z-index: 1100）
- [ ] 模态框在 Toast 之下（z-index: 1000）
- [ ] 侧边栏不遮挡主内容
- [ ] Tooltip 在下拉菜单之上

### 布局检查

- [ ] 侧边栏宽度合理（200-400px）
- [ ] 主内容区域不被挤压
- [ ] 长内容可以滚动
- [ ] 底部内容不被截断
- [ ] flex 容器使用 min-height: 0

### 响应式检查

- [ ] 小屏幕（< 640px）上布局正常
- [ ] 平板（640-1024px）上布局正常
- [ ] 桌面（> 1024px）上布局正常
- [ ] 侧边栏在移动端切换为 overlay
- [ ] 输入框字体大小 ≥ 16px（iOS）

### 交互检查

- [ ] 模态框打开时背景不可交互
- [ ] 焦点正确管理
- [ ] ESC 键关闭模态框
- [ ] Tab 键循环焦点
- [ ] 滚动平滑无卡顿

### 动画检查

- [ ] 所有动画使用 transform 或 opacity
- [ ] 动画帧率 60 FPS
- [ ] 无闪烁或抖动
- [ ] 过渡时长 100-300ms
- [ ] 使用 will-change（谨慎）

### 可访问性检查

- [ ] 所有交互元素可键盘访问
- [ ] 焦点轮廓清晰可见
- [ ] 颜色对比度 ≥ 4.5:1
- [ ] aria-label 正确
- [ ] role 属性正确

---

## 🚀 实施计划

### 第 1 步：创建基础文件（10 分钟）

1. 创建 `z-index.css`
2. 创建 `breakpoints.css`
3. 创建 `useFocusTrap.ts`
4. 在 `main.tsx` 中导入

### 第 2 步：修复层级问题（20 分钟）

1. 应用 z-index 变量到所有组件
2. 测试 Toast、模态框、侧边栏层级
3. 确保无遮挡

### 第 3 步：优化响应式（30 分钟）

1. 添加响应式样式
2. 测试不同屏幕尺寸
3. 优化移动端体验

### 第 4 步：性能优化（20 分钟）

1. 检查所有动画
2. 替换 layout 触发属性为 transform
3. 优化滚动性能

### 第 5 步：测试和验证（20 分钟）

1. 使用检查清单逐项测试
2. 修复发现的问题
3. 跨浏览器测试

**总计：~100 分钟**

---

## 📊 预期效果

### Before

- ⚠️ Toast 被模态框遮挡
- ⚠️ 侧边栏打开时遮挡输入框
- ⚠️ 模态框打开时背景仍可交互
- ⚠️ 小屏幕上布局混乱
- ⚠️ 动画有时卡顿

### After

- ✅ 层级清晰，无遮挡
- ✅ 侧边栏不影响主内容
- ✅ 模态框焦点管理正确
- ✅ 响应式布局流畅
- ✅ 动画 60 FPS 流畅

---

## 💡 最佳实践

### 1. 层级管理

```css
/* ✅ 使用 CSS 变量 */
.modal {
  z-index: var(--z-modal);
}

/* ❌ 避免硬编码 */
.modal {
  z-index: 9999;
}
```

### 2. 动画性能

```css
/* ✅ 使用 transform */
.panel {
  transform: translateX(-100%);
  transition: transform 300ms ease;
}

/* ❌ 避免 width */
.panel {
  width: 0;
  transition: width 300ms ease;
}
```

### 3. 响应式设计

```css
/* ✅ 使用 clamp() */
.sidebar {
  width: clamp(200px, 20vw, 320px);
}

/* ❌ 避免固定值 */
.sidebar {
  width: 300px;
}
```

### 4. 焦点管理

```tsx
// ✅ 使用 useFocusTrap
const ref = useFocusTrap(isOpen);

// ❌ 忘记恢复焦点
useEffect(() => {
  if (isOpen) {
    modalRef.current?.focus();
  }
  // 没有 cleanup
}, [isOpen]);
```

---

准备好了吗？让我们开始实施这些优化！🚀
