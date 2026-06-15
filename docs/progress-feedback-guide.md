# 大文件操作进度反馈 - 实现指南

## 概述

为 WriteTool 和 CodebaseIndexer 添加实时进度反馈，改善大文件操作的用户体验。

---

## 实现方案

### 1. WriteTool 进度反馈

**修改位置**: `backend/tools/file_tools.py:483`

**实现思路**:
```python
async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
    file_path = _path_arg(args)
    content = args.get("content", "")
    lines = content.split('\n')
    total_lines = len(lines)
    
    # 大文件才发送进度（> 500 行）
    if total_lines > 500 and context and context.emit_event:
        # 分块写入，每 100 行发送一次进度
        for i in range(0, total_lines, 100):
            chunk_lines = lines[i:i+100]
            # 写入逻辑...
            
            # 发送进度事件
            await context.emit_event(
                "tool_progress",
                {
                    "tool_call_id": context.tool_call_id,
                    "type": "write_progress",
                    "lines_written": min(i+100, total_lines),
                    "total_lines": total_lines,
                    "progress": int((i+100)/total_lines * 100)
                }
            )
    else:
        # 小文件：直接写入，不发送进度
        # ... 原有逻辑
```

**前端监听**: `frontend/src.v2/chat/cells/ActivityCell.tsx`

```typescript
// 在 ActivityCell 中监听 tool_progress 事件
useEffect(() => {
  const handleProgress = (event: ToolProgressEvent) => {
    if (event.tool_call_id === cell.id) {
      setProgressText(
        `Writing ${event.lines_written}/${event.total_lines} lines (${event.progress}%)`
      )
    }
  }
  
  // 订阅进度事件
  const unsubscribe = eventBus.on('tool_progress', handleProgress)
  return unsubscribe
}, [cell.id])
```

---

### 2. CodebaseIndexer 进度反馈

**修改位置**: `backend/workspace/codebase_indexer.py` (如果存在)

**实现思路**:
```python
class CodebaseIndexer:
    async def index_workspace(self, root: Path, emit_event: Callable):
        files = list(root.rglob("*.py"))  # + 其他扩展名
        total = len(files)
        
        for i, file in enumerate(files):
            await self._index_file(file)
            
            # 每 50 个文件报告一次
            if (i + 1) % 50 == 0:
                await emit_event(
                    "indexing_progress",
                    {
                        "indexed": i + 1,
                        "total": total,
                        "progress": int((i + 1) / total * 100),
                        "current_file": file.name
                    }
                )
        
        # 完成
        await emit_event("indexing_complete", {"total": total})
```

**前端显示**: `frontend/src.v2/shell/StatusBar.tsx`

```typescript
export function StatusBar() {
  const indexingProgress = useAppStore(s => s.indexingProgress)
  
  if (indexingProgress && !indexingProgress.complete) {
    return (
      <div className="status-bar">
        <Loader size={12} className="animate-spin" />
        <span>
          Indexing... {indexingProgress.indexed}/{indexingProgress.total}
        </span>
        <progress 
          value={indexingProgress.indexed} 
          max={indexingProgress.total}
          className="w-32"
        />
      </div>
    )
  }
  
  return <DefaultStatusBar />
}
```

---

## 优先级评估

**影响**: Medium（改善 UX，但非关键功能）  
**复杂度**: Low-Medium  
**工作量**: 2-3 小时

**建议**: 可以作为 v1.1 的增强功能，不阻塞 v1.0 发布。

---

## 集成清单

- [ ] WriteTool 添加分块写入
- [ ] 前端 ActivityCell 监听 tool_progress
- [ ] CodebaseIndexer 添加进度事件
- [ ] StatusBar 显示索引进度
- [ ] 测试大文件场景（> 1000 行）
