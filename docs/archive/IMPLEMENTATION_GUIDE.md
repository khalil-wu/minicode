# MiniCode 全面实施指南
**基于 Claude Code 和 Codex 最佳实践**

> 📍 项目路径: `C:\Desktop\MiniCode`  
> 📅 创建时间: 2026-06-14  
> 🎯 目标: 对标 Claude Code Desktop 2026 Redesign

---

## 📋 总览

本指南分为 4 个 Phase，每个 Phase 包含详细的文件路径、代码修改点、测试步骤。

| Phase | 内容 | 预计时间 | 优先级 |
|-------|------|---------|--------|
| Phase 1 | 核心体验对齐 | 3-4 周 | P0 ⭐⭐⭐⭐⭐ |
| Phase 2 | 性能与稳定性 | 2 周 | P1 ⭐⭐⭐⭐ |
| Phase 3 | 高级工具面板 | 2 周 | P1 ⭐⭐⭐ |
| Phase 4 | 体验完善 | 2 周 | P2 ⭐⭐ |

---

## 🚀 Phase 1: 核心体验对齐（P0 - 上市必需）

### 任务 1.1 拖拽式面板布局（1 周）

#### 📂 涉及文件
```
frontend/src.v2/
├── shell/
│   └── MainSlots.tsx          # 主要修改
├── stores/
│   ├── types.ts               # 添加类型
│   └── index.ts               # 添加 action
└── package.json                # 已安装 @dnd-kit
```

#### 🔍 调研步骤

**Step 1: 理解当前实现**
```bash
cd C:\Desktop\MiniCode
code frontend/src.v2/shell/MainSlots.tsx
```

查找关键点：
- [ ] 当前如何渲染面板列表？（搜索 `panelSlots.map`）
- [ ] 当前如何调整顺序？（搜索 `ChevronLeft`、`ChevronRight`）
- [ ] 面板状态存储在哪里？（搜索 `useAppStore`）
- [ ] ResizeHandle 如何工作？（理解拖拽调整大小的逻辑）

**Step 2: 查看 store 结构**
```bash
code frontend/src.v2/stores/types.ts
code frontend/src.v2/stores/index.ts
```

查找：
- [ ] `panelSlots` 的数据结构（类型定义）
- [ ] 现有的面板操作 actions（如 `addPanel`, `removePanel`）

**Step 3: 参考 @dnd-kit 文档**
```bash
# 在浏览器打开
https://docs.dndkit.com/presets/sortable
```

重点阅读：
- [ ] `DndContext` 的基本用法
- [ ] `SortableContext` 的 items 参数
- [ ] `useSortable` hook 的返回值（`attributes`, `listeners`, `setNodeRef`）
- [ ] `onDragEnd` 事件处理

#### ✏️ 修改步骤

**修改 1: `frontend/src.v2/stores/types.ts`**

在 `PanelSlice` 接口中添加新的 action：

```typescript
interface PanelSlice {
  // ... 现有字段
  panelSlots: PanelSlot[];
  
  // 新增：拖拽重排 action
  reorderPanels: (fromIndex: number, toIndex: number) => void;
}
```

**修改 2: `frontend/src.v2/stores/index.ts`**

实现 `reorderPanels` action：

```typescript
// 在 createStore 中的 PanelSlice 部分
reorderPanels: (fromIndex: number, toIndex: number) => {
  const slots = [...get().panelSlots];
  const [moved] = slots.splice(fromIndex, 1);
  slots.splice(toIndex, 0, moved);
  set({ panelSlots: slots });
  
  // 持久化到 localStorage
  try {
    localStorage.setItem('minicode_panel_slots', JSON.stringify(slots));
  } catch (e) {
    console.error('Failed to persist panel slots:', e);
  }
}
```

**修改 3: `frontend/src.v2/shell/MainSlots.tsx`**

导入 @dnd-kit：

```typescript
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from '@dnd-kit/core';
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { GripVertical } from 'lucide-react';
```

将现有的面板列表包裹在 DndContext 中：

```typescript
function MainSlots() {
  const panelSlots = useAppStore(s => s.panelSlots);
  const reorderPanels = useAppStore(s => s.reorderPanels);
  
  // 配置传感器
  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: {
        distance: 8, // 8px 移动后才激活拖拽
      },
    }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  );
  
  // 处理拖拽结束
  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    
    const oldIndex = panelSlots.findIndex(s => s.id === active.id);
    const newIndex = panelSlots.findIndex(s => s.id === over.id);
    
    if (oldIndex !== -1 && newIndex !== -1) {
      reorderPanels(oldIndex, newIndex);
    }
  };
  
  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      onDragEnd={handleDragEnd}
    >
      <SortableContext
        items={panelSlots.map(s => s.id)}
        strategy={verticalListSortingStrategy}
      >
        <div className="flex flex-1 overflow-hidden">
          {panelSlots.map((slot, index) => (
            <SortablePaneFrame key={slot.id} slot={slot} index={index} />
          ))}
        </div>
      </SortableContext>
    </DndContext>
  );
}
```

创建新的 `SortablePaneFrame` 组件：

```typescript
function SortablePaneFrame({ slot, index }: { slot: PanelSlot; index: number }) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: slot.id });
  
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  };
  
  const isMaximized = slot.maximized || false;
  
  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`flex flex-col ${isMaximized ? 'flex-1' : ''}`}
    >
      {/* PaneFrame header 添加拖拽手柄 */}
      <div className="flex items-center gap-2 px-2 py-1 bg-gray-800 border-b border-gray-700">
        {/* 只在非最大化时显示拖拽手柄 */}
        {!isMaximized && (
          <button
            {...listeners}
            {...attributes}
            className="cursor-grab active:cursor-grabbing p-1 hover:bg-gray-700 rounded"
            aria-label="Drag to reorder"
          >
            <GripVertical size={16} className="text-gray-400" />
          </button>
        )}
        
        {/* 面板标题 */}
        <span className="text-sm text-gray-300">{slot.kind}</span>
        
        {/* 移除旧的 ChevronLeft/Right 按钮 */}
        {/* ... 保留其他按钮（关闭、最大化等） */}
      </div>
      
      {/* 面板内容 */}
      <PanelContent slot={slot} />
    </div>
  );
}
```

#### 🧪 测试步骤

1. **启动开发服务器**
```bash
cd C:\Desktop\MiniCode\frontend
npm run dev
```

2. **验证拖拽功能**
- [ ] 鼠标悬停在 GripVertical 图标上，光标变为 grab
- [ ] 拖拽面板到新位置，其他面板自动让位
- [ ] 释放鼠标，面板顺序正确交换
- [ ] 刷新页面，面板顺序从 localStorage 恢复

3. **验证边界情况**
- [ ] 最大化面板时，GripVertical 图标消失
- [ ] 只有一个面板时，仍可拖拽但无效果
- [ ] 快速连续拖拽不会出现卡顿

#### 📝 调试技巧

如果拖拽不工作：
1. 检查 console 是否有 React 错误
2. 验证 `panelSlots` 中每个 slot 都有唯一的 `id`
3. 在 `handleDragEnd` 中添加 `console.log` 查看事件触发

---

### 任务 1.2 Routines 定时任务系统（2 周）

#### 📂 涉及文件
