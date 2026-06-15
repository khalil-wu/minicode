# Phase 1.2 (续): Routines API 和前端

## 📝 第四步：创建 FastAPI 路由

### 创建 api.py
```python
# backend/routines/api.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from .manager import RoutineManager
from .scheduler import RoutineScheduler
from .models import ScheduledTask
import uuid

router = APIRouter(prefix="/api/routines", tags=["routines"])
manager = RoutineManager()
scheduler = RoutineScheduler()

class CreateTaskRequest(BaseModel):
    name: str
    prompt: str
    workspace_root: str
    schedule: str  # cron 表达式
    permission_mode: str = "auto"
    daily_cap: int = 20

@router.post("/")
async def create_task(req: CreateTaskRequest):
    task = ScheduledTask(
        id=f"task_{uuid.uuid4().hex[:8]}",
        name=req.name,
        prompt=req.prompt,
        workspace_root=req.workspace_root,
        schedule=req.schedule,
        trigger_type="schedule",
        permission_mode=req.permission_mode,
        daily_cap=req.daily_cap
    )
    manager.save_task(task)
    
    # 注册到调度器
    async def run_task():
        # TODO: 实际执行 agent loop
        print(f"Running task: {task.name}")
    
    scheduler.add_task(task.id, task.schedule, run_task)
    return {"task_id": task.id, "status": "created"}

@router.get("/")
async def list_tasks():
    tasks = manager.list_tasks()
    return {"tasks": [t.__dict__ for t in tasks]}

@router.delete("/{task_id}")
async def delete_task(task_id: str):
    manager.delete_task(task_id)
    scheduler.remove_task(task_id)
    return {"status": "deleted"}

@router.post("/{task_id}/run")
async def trigger_task(task_id: str):
    task = manager.load_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    
    # TODO: 立即执行任务
    return {"status": "triggered"}
```

### 注册到 main.py
```python
# backend/main.py
from fastapi import FastAPI
from backend.routines.api import router as routines_router

app = FastAPI()
app.include_router(routines_router)

# 启动时启动调度器
from backend.routines.api import scheduler

@app.on_event("startup")
async def startup():
    scheduler.start()

@app.on_event("shutdown")
async def shutdown():
    scheduler.stop()
```

## 🎨 第五步：前端实现

### 1. 添加 Store 类型
```typescript
// frontend/src.v2/stores/types.ts
interface ScheduledTaskInfo {
  id: string;
  name: string;
  prompt: string;
  schedule: string;
  enabled: boolean;
  lastRunAt: string | null;
  isRunning: boolean;
}

interface ScheduledTaskSlice {
  scheduledTasks: ScheduledTaskInfo[];
  scheduledTasksPanelOpen: boolean;
  toggleScheduledTasksPanel: () => void;
  fetchScheduledTasks: () => Promise<void>;
}
```

### 2. 实现 Store Actions
```typescript
// frontend/src.v2/stores/index.ts
scheduledTasks: [],
scheduledTasksPanelOpen: false,

toggleScheduledTasksPanel: () => {
  set(s => ({ scheduledTasksPanelOpen: !s.scheduledTasksPanelOpen }));
},

fetchScheduledTasks: async () => {
  const res = await fetch('/api/routines');
  const data = await res.json();
  set({ scheduledTasks: data.tasks });
}
```

### 3. 创建 UI 组件
```tsx
// frontend/src.v2/overlays/ScheduledTasksPanel.tsx
import { X, Play, Trash2 } from 'lucide-react';

export function ScheduledTasksPanel() {
  const { scheduledTasks, scheduledTasksPanelOpen, toggleScheduledTasksPanel } = 
    useAppStore();
  
  if (!scheduledTasksPanelOpen) return null;
  
  return (
    <div className="fixed left-0 top-0 bottom-0 w-80 bg-gray-900 border-r border-gray-700 z-50">
      <div className="flex items-center justify-between p-4 border-b border-gray-700">
        <h2 className="text-lg font-semibold">Routines</h2>
        <button onClick={toggleScheduledTasksPanel}>
          <X size={20} />
        </button>
      </div>
      
      <div className="p-4 space-y-2">
        {scheduledTasks.map(task => (
          <div key={task.id} className="p-3 bg-gray-800 rounded border border-gray-700">
            <div className="flex items-center justify-between mb-2">
              <span className="font-medium">{task.name}</span>
              {task.isRunning && <span className="text-xs text-blue-400">Running...</span>}
            </div>
            
            <div className="text-xs text-gray-400 space-y-1">
              <div>Schedule: {task.schedule}</div>
              <div>Last run: {task.lastRunAt || 'Never'}</div>
            </div>
            
            <div className="flex gap-2 mt-2">
              <button className="px-2 py-1 bg-blue-600 rounded text-xs">
                <Play size={12} /> Run Now
              </button>
              <button className="px-2 py-1 bg-red-600 rounded text-xs">
                <Trash2 size={12} /> Delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
```

### 4. 修改 SidebarLeft
```tsx
// frontend/src.v2/shell/SidebarLeft.tsx
import { Calendar } from 'lucide-react';

// 在侧边栏按钮区域添加
<button
  onClick={() => toggleScheduledTasksPanel()}
  className="p-2 hover:bg-gray-800 rounded"
  title="Routines"
>
  <Calendar size={20} />
  {/* 如果有运行中的任务，显示 badge */}
</button>
```

## 🧪 测试流程

1. **启动后端**
```bash
python -m backend
```

2. **测试 API**
```bash
curl -X POST http://localhost:8000/api/routines \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Daily Report",
    "prompt": "Generate daily report",
    "workspace_root": "C:/Desktop/MiniCode",
    "schedule": "0 9 * * *"
  }'
```

3. **启动前端测试 UI**
```bash
cd frontend
npm run dev
```

4. **验证**
- [ ] 点击侧边栏 Calendar 图标打开面板
- [ ] 看到创建的任务
- [ ] 点击 "Run Now" 手动触发
- [ ] 查看后端日志确认执行

## 📝 后续优化方向
- WebSocket 实时更新任务状态
- 任务运行历史记录
- 更丰富的 cron 表达式编辑器
- 任务执行结果通知
