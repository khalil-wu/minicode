# Phase 1.2: Routines 定时任务系统

## 📂 涉及文件

### 后端（新建）
```
backend/routines/          # 新建目录
├── __init__.py
├── models.py              # 数据模型
├── scheduler.py           # 调度器
├── manager.py             # 任务管理
└── api.py                 # FastAPI 路由
```

### 后端（修改）
- `backend/permissions/checker.py` - 添加无头执行支持
- `backend/main.py` - 注册 Routines API

### 前端（新建）
- `frontend/src.v2/overlays/ScheduledTasksPanel.tsx`

### 前端（修改）
- `frontend/src.v2/shell/SidebarLeft.tsx` - 添加 Routines 按钮
- `frontend/src.v2/stores/types.ts` - 添加 ScheduledTaskSlice
- `frontend/src.v2/hooks/useWebSocket.ts` - 处理 routine 事件

## 🔍 第一步：创建后端数据模型

### 1. 创建目录
```bash
cd C:\Desktop\MiniCode
mkdir backend\routines
touch backend\routines\__init__.py
```

### 2. 创建 models.py
```python
# backend/routines/models.py
from dataclasses import dataclass, field
from typing import Literal
from datetime import datetime

@dataclass
class ScheduledTask:
    id: str
    name: str
    prompt: str
    workspace_root: str
    schedule: str | None  # cron 表达式，如 "0 */2 * * *"
    trigger_type: Literal["schedule", "api", "manual"]
    permission_mode: Literal["auto", "bypass"] = "auto"
    daily_cap: int = 20
    runs_today: int = 0
    enabled: bool = True
    last_run_at: str | None = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

@dataclass
class TaskRun:
    id: str
    task_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    started_at: str
    completed_at: str | None = None
    result_summary: str | None = None
    messages_count: int = 0
    token_usage: dict | None = None
    error: str | None = None
```

## ✏️ 第二步：实现调度器

### 创建 scheduler.py
```python
# backend/routines/scheduler.py
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class RoutineScheduler:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.running_tasks: dict[str, asyncio.Task] = {}
    
    def start(self):
        """启动调度器"""
        self.scheduler.start()
        logger.info("Routine scheduler started")
    
    def stop(self):
        """停止调度器"""
        self.scheduler.shutdown()
        logger.info("Routine scheduler stopped")
    
    def add_task(self, task_id: str, cron_expr: str, callback):
        """添加定时任务"""
        trigger = CronTrigger.from_crontab(cron_expr)
        self.scheduler.add_job(
            callback,
            trigger=trigger,
            id=task_id,
            replace_existing=True
        )
        logger.info(f"Added routine task: {task_id} with schedule: {cron_expr}")
    
    def remove_task(self, task_id: str):
        """移除定时任务"""
        try:
            self.scheduler.remove_job(task_id)
            logger.info(f"Removed routine task: {task_id}")
        except Exception as e:
            logger.warning(f"Failed to remove task {task_id}: {e}")
```

**需要安装依赖：**
```bash
pip install apscheduler
```

## 🧪 第三步：实现任务管理器

### 创建 manager.py
```python
# backend/routines/manager.py
import os
import json
from pathlib import Path
from .models import ScheduledTask, TaskRun

class RoutineManager:
    def __init__(self, data_dir: str = "data/scheduled_tasks"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
    
    def save_task(self, task: ScheduledTask):
        """保存任务配置"""
        path = self.data_dir / f"{task.id}.json"
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(task.__dict__, f, indent=2)
    
    def load_task(self, task_id: str) -> ScheduledTask | None:
        """加载任务配置"""
        path = self.data_dir / f"{task_id}.json"
        if not path.exists():
            return None
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return ScheduledTask(**data)
    
    def list_tasks(self) -> list[ScheduledTask]:
        """列出所有任务"""
        tasks = []
        for path in self.data_dir.glob("*.json"):
            if path.stem.startswith("run_"):
                continue  # 跳过运行记录
            try:
                task = self.load_task(path.stem)
                if task:
                    tasks.append(task)
            except Exception as e:
                print(f"Failed to load task {path}: {e}")
        return tasks
    
    def delete_task(self, task_id: str):
        """删除任务"""
        path = self.data_dir / f"{task_id}.json"
        if path.exists():
            path.unlink()
```

**提示：** 这是简化版实现，生产环境建议使用数据库（SQLite）
