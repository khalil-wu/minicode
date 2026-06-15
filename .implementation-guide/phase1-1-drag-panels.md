# Phase 1.1: 拖拽面板布局实施指南

## 📂 涉及文件
- `frontend/src.v2/shell/MainSlots.tsx` - 主要修改
- `frontend/src.v2/stores/types.ts` - 添加类型
- `frontend/src.v2/stores/index.ts` - 添加 action

## 🔍 第一步：调研现有代码

### 1. 打开 MainSlots.tsx
```bash
code frontend/src.v2/shell/MainSlots.tsx
```

**查找关键点：**
- [ ] 搜索 `panelSlots.map` - 当前如何渲染面板
- [ ] 搜索 `ChevronLeft` - 当前如何移动面板（需要删除）
- [ ] 搜索 `useAppStore` - 面板状态在哪里
- [ ] 搜索 `ResizeHandle` - 拖拽调整大小的实现

### 2. 查看 Store 结构
```bash
code frontend/src.v2/stores/types.ts
code frontend/src.v2/stores/index.ts
```

**理解：**
- [ ] `panelSlots` 的数据结构
- [ ] 现有的 actions（addPanel, removePanel）
- [ ] localStorage 持久化逻辑

### 3. 学习 @dnd-kit（已安装）
打开文档：https://docs.dndkit.com/presets/sortable

**重点阅读：**
- DndContext 基本用法
- SortableContext 的 items 参数
- useSortable hook
- onDragEnd 事件

## ✏️ 第二步：修改代码

### 修改 1: stores/types.ts
在 `PanelSlice` 或相关接口添加：
```typescript
reorderPanels: (fromIndex: number, toIndex: number) => void;
```

### 修改 2: stores/index.ts
添加 action 实现：
```typescript
reorderPanels: (fromIndex, toIndex) => {
  const slots = [...get().panelSlots];
  const [moved] = slots.splice(fromIndex, 1);
  slots.splice(toIndex, 0, moved);
  set({ panelSlots: slots });
  localStorage.setItem('minicode_panel_slots', JSON.stringify(slots));
}
```
