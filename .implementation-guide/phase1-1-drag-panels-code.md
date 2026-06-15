### 修改 3: shell/MainSlots.tsx

#### 3.1 添加导入
```typescript
import {
  DndContext, closestCenter, PointerSensor,
  useSensor, useSensors, DragEndEvent
} from '@dnd-kit/core';
import {
  SortableContext, useSortable,
  verticalListSortingStrategy
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { GripVertical } from 'lucide-react';
```

#### 3.2 包裹 DndContext
找到面板渲染的地方，替换为：
```typescript
const sensors = useSensors(
  useSensor(PointerSensor, { activationConstraint: { distance: 8 } })
);

const handleDragEnd = (event: DragEndEvent) => {
  const { active, over } = event;
  if (!over || active.id === over.id) return;
  
  const oldIndex = panelSlots.findIndex(s => s.id === active.id);
  const newIndex = panelSlots.findIndex(s => s.id === over.id);
  reorderPanels(oldIndex, newIndex);
};

return (
  <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
    <SortableContext items={panelSlots.map(s => s.id)} strategy={verticalListSortingStrategy}>
      {/* 原有面板渲染逻辑 */}
    </SortableContext>
  </DndContext>
);
```

#### 3.3 修改每个面板
在面板组件中使用 `useSortable`：
```typescript
const { attributes, listeners, setNodeRef, transform, transition, isDragging } = 
  useSortable({ id: slot.id });

const style = {
  transform: CSS.Transform.toString(transform),
  transition,
  opacity: isDragging ? 0.5 : 1
};

return (
  <div ref={setNodeRef} style={style}>
    {/* 在 header 添加拖拽手柄 */}
    {!slot.maximized && (
      <button {...listeners} {...attributes} className="cursor-grab">
        <GripVertical size={16} />
      </button>
    )}
    {/* 删除 ChevronLeft/Right 按钮 */}
  </div>
);
```

## 🧪 第三步：测试

```bash
cd frontend
npm run dev
```

**验证清单：**
- [ ] 拖拽手柄可点击，光标变为 grab
- [ ] 拖拽面板，其他面板自动移动
- [ ] 刷新页面，顺序保持
- [ ] 最大化面板时手柄消失

## 🐛 调试提示
- 如果拖不动：检查 `id` 是否唯一
- 如果顺序错乱：检查 `reorderPanels` 逻辑
- 如果有报错：查看 console.error
