# Phase 1.3: CLAUDE.md 项目指令注入

## 📂 涉及文件
- `backend/agent/context.py` - 主要修改

## 🔍 第一步：调研现有代码

```bash
code backend/agent/context.py
```

**查找关键点：**
- [ ] 搜索 `build_context` - 上下文构建入口
- [ ] 搜索 `system_prompt` - 系统提示词构建
- [ ] 搜索 `workspace_root` - 工作目录获取
- [ ] 理解消息列表的构建流程

## ✏️ 第二步：添加 CLAUDE.md 加载逻辑

### 修改 1: 添加加载函数
在 `context.py` 中添加（建议在文件顶部工具函数区域）：

```python
import os
from pathlib import Path
from typing import Optional

# 缓存：避免重复读取
_claude_md_cache: dict[str, tuple[str, float]] = {}  # {path: (content, mtime)}

def _load_project_instructions(workspace_root: str) -> Optional[str]:
    """
    加载项目级指令（CLAUDE.md）
    查找顺序：.claude/CLAUDE.md → CLAUDE.md → .clauderc
    """
    if not workspace_root or not os.path.isdir(workspace_root):
        return None
    
    # 查找顺序
    candidates = [
        os.path.join(workspace_root, ".claude", "CLAUDE.md"),
        os.path.join(workspace_root, "CLAUDE.md"),
        os.path.join(workspace_root, ".clauderc"),
    ]
    
    for path in candidates:
        if not os.path.isfile(path):
            continue
        
        try:
            # 检查缓存
            mtime = os.path.getmtime(path)
            if path in _claude_md_cache:
                cached_content, cached_mtime = _claude_md_cache[path]
                if cached_mtime == mtime:
                    return cached_content
            
            # 读取文件
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            
            # 更新缓存
            _claude_md_cache[path] = (content, mtime)
            return content
        
        except Exception as e:
            print(f"Warning: Failed to load {path}: {e}")
            continue
    
    return None
```

### 修改 2: 注入到上下文
在 `build_context` 或类似函数中，找到 system prompt 构建的地方：

```python
# 示例位置（具体取决于你的代码结构）
def build_context(...):
    # ... 现有逻辑
    
    # 构建系统提示词
    system_prompt = "You are an AI coding assistant..."
    
    # ✅ 注入 CLAUDE.md
    project_instructions = _load_project_instructions(workspace_root)
    if project_instructions:
        system_prompt += f"\n\n## Project-Specific Instructions\n\n{project_instructions}"
    
    # ... 继续构建消息列表
```

## 🧪 第三步：测试

### 1. 创建测试文件
```bash
cd C:\Desktop\MiniCode
echo "# Test Instructions\nAlways respond with 'CLAUDE.md works!'" > CLAUDE.md
```

### 2. 启动后端
```bash
python -m backend
```

### 3. 发送测试消息
在前端发送：`Hello`

**预期结果：**  
Agent 的回复中包含 "CLAUDE.md works!" 或明显遵循了 CLAUDE.md 中的指令

### 4. 验证缓存
- [ ] 修改 CLAUDE.md 内容
- [ ] 再次发送消息，验证新指令生效
- [ ] 删除 CLAUDE.md，验证指令消失

## 📝 高级用法

### 使用 .claude/CLAUDE.md（推荐）
```bash
mkdir .claude
echo "# Production Instructions\n..." > .claude/CLAUDE.md
```
优先级高于根目录的 CLAUDE.md

### 多项目支持
不同 workspace 自动加载对应的 CLAUDE.md，无需手动切换
