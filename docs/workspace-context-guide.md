# 项目上下文管理 - 使用指南

## 概述

参考 Claude Code 的核心能力，MiniCode 现在支持**项目文件夹导入**和**智能上下文管理**。

## 核心功能

### 1. 项目导入 (WorkspaceContext)

**后端实现** (`backend/workspace/context.py`):
- 自动发现项目类型（Python/Node/Rust/Go/Java）
- 加载 `CLAUDE.md` 项目指令
- 构建文件索引（尊重 `.gitignore`）
- 提供项目摘要给 Agent

**API Endpoint**:
```bash
POST /api/workspace/import
{
  "path": "/path/to/project"
}
```

**响应**:
```json
{
  "success": true,
  "project": {
    "root_path": "/path/to/project",
    "project_type": "python",
    "name": "MyProject",
    "file_count": 150,
    "total_size": 2048000
  },
  "summary": "# 项目上下文\n...",
  "file_count": 150
}
```

### 2. 增强的搜索工具

#### Glob 工具 (`glob_files`)
快速文件模式匹配：
```python
# 查找所有 Python 文件
glob_files(pattern="**/*.py")

# 查找 src 下的 TypeScript 文件
glob_files(pattern="src/**/*.ts")
```

#### Grep 工具 (`grep_files`)
代码内容搜索（支持 ripgrep）：
```python
# 搜索函数定义
grep_files(pattern="def.*main", glob="**/*.py")

# 搜索 TODO 注释并显示上下文
grep_files(pattern="TODO", context=2)
```

### 3. Agent 集成

项目上下文自动注入到 Agent system prompt：

```python
# backend/agent/context.py
async def build(self, user_message: str, state: AgentState):
    # 自动注入项目上下文
    if state.workspace_context:
        workspace_summary = state.workspace_context.get_project_summary()
        system_content += workspace_summary
```

## 前端使用

### 导入项目

```typescript
import { ProjectImportModal } from '@/components/ProjectImportModal';

// 在你的组件中
const [showImport, setShowImport] = useState(false);

const handleImport = async (path: string) => {
  const response = await fetch('/api/workspace/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });

  const data = await response.json();
  console.log('项目已导入:', data.project);
};

<ProjectImportModal
  isOpen={showImport}
  onClose={() => setShowImport(false)}
  onImport={handleImport}
/>
```

## 与 Claude Code 的对比

| 功能 | Claude Code | MiniCode (现在) |
|------|-------------|----------------|
| 项目发现 | ✅ | ✅ |
| CLAUDE.md 支持 | ✅ | ✅ |
| .gitignore 尊重 | ✅ | ✅ |
| 文件索引 | ✅ | ✅ |
| Glob 工具 | ✅ | ✅ |
| Grep 工具 | ✅ | ✅ (支持 ripgrep) |
| LSP 集成 | ✅ | ❌ (待实现) |
| Git 工具 | ✅ | ❌ (待实现) |

## 下一步计划

### P1 - 权限系统增强
- 文件系统沙箱（只能访问项目目录）
- 命令白名单/黑名单
- 用户反馈学习

### P2 - 更多工具
- LSPTool（代码智能：跳转定义、重命名）
- GitTool（status/diff/commit）
- WebSearchTool（联网搜索）

### P3 - 对话存储优化
参考 CC 的存储结构：
```
.claude/sessions/{session_id}/
  ├── meta.json          # 会话元数据
  ├── transcript.jsonl   # 消息流（追加写）
  ├── snapshot.json      # 最新状态快照
  └── file_cache/        # 文件状态缓存
```

## 示例：完整工作流

1. **用户导入项目**
   ```
   用户: 导入项目 C:\Projects\MyApp
   ```

2. **系统分析项目**
   - 检测到 Python 项目（pyproject.toml）
   - 加载 CLAUDE.md 指令
   - 构建 150 个文件的索引

3. **Agent 获得上下文**
   ```
   System Prompt:
   # 项目上下文
   **项目名称**: MyApp
   **项目类型**: python
   **文件数量**: 150

   ## 项目指令 (CLAUDE.md)
   [项目特定指令...]
   ```

4. **用户提问**
   ```
   用户: 找到所有 TODO 注释
   Agent: grep_files(pattern="TODO", context=2)
   ```

5. **Agent 智能响应**
   - 理解项目结构
   - 使用正确的工具
   - 提供精准答案

## 技术细节

### WorkspaceContext 类

```python
class WorkspaceContext:
    def __init__(self, root_path: str | Path)

    async def initialize(self) -> ProjectMetadata
    def get_project_summary(self) -> str
    def resolve_path(self, path_str: str) -> Path
    def get_file_list(self, pattern: str | None = None) -> list[str]
```

### 文件索引结构

```python
@dataclass
class FileIndexEntry:
    path: Path
    relative_path: str
    size: int
    mtime: float
    is_text: bool
```

## 常见问题

**Q: 如何更新项目索引？**
A: 重新调用 `/api/workspace/import` 即可重建索引。

**Q: 支持多项目吗？**
A: 当前版本每次只能导入一个项目，未来会支持多项目切换。

**Q: .gitignore 规则完全兼容吗？**
A: 当前是简化版实现，支持基本模式匹配，复杂规则可能不完全兼容。

**Q: 性能如何？**
A: 对于中小型项目（<10k 文件）性能良好，大型项目建议使用 .gitignore 过滤。
