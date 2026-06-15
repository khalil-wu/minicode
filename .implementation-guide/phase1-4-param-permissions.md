# Phase 1.4: 参数级权限 Glob 匹配

## 📂 涉及文件
- `backend/permissions/checker.py` - 主要修改

## 🔍 第一步：调研现有代码

```bash
code backend/permissions/checker.py
```

**查找关键点：**
- [ ] 搜索 `_match_rule` - 当前规则匹配逻辑
- [ ] 搜索 `check_tool` - 工具权限检查入口
- [ ] 理解当前支持的规则格式（如 `Bash`, `Edit`, `*`）

## ✏️ 第二步：扩展匹配逻辑

### 修改 1: 添加参数提取函数
在 `checker.py` 中添加：

```python
import fnmatch
import json
from typing import Any

def _extract_tool_signature(tool_name: str, tool_args: dict[str, Any]) -> str:
    """
    提取工具签名用于参数级匹配
    格式: ToolName(arg1_value:arg2_value:...)
    """
    # 按字母顺序排序参数，确保一致性
    sorted_args = sorted(tool_args.items())
    arg_values = [str(v) for k, v in sorted_args]
    signature = f"{tool_name}({':'.join(arg_values)})"
    return signature

def _parse_param_rule(rule: str) -> tuple[str, list[str]]:
    """
    解析参数级规则
    输入: "Bash(git commit:*)" 或 "Edit(*.md:*)"
    输出: ("Bash", ["git commit", "*"]) 或 ("Edit", ["*.md", "*"])
    """
    if '(' not in rule:
        return rule, []
    
    tool_name, params_part = rule.split('(', 1)
    params_part = params_part.rstrip(')')
    param_patterns = [p.strip() for p in params_part.split(':')]
    return tool_name.strip(), param_patterns
```

### 修改 2: 扩展 _match_rule 函数
找到 `_match_rule` 函数，修改匹配逻辑：

```python
def _match_rule(rule: str, tool_name: str, tool_args: dict[str, Any]) -> bool:
    """
    匹配规则，支持参数级 Glob
    
    示例：
    - "Bash" → 匹配所有 Bash 调用
    - "Bash(git commit:*)" → 只匹配 git commit 命令
    - "Edit(*.md:*)" → 只匹配 .md 文件编辑
    """
    # 解析规则
    rule_tool, param_patterns = _parse_param_rule(rule)
    
    # 工具名通配符匹配
    if not fnmatch.fnmatch(tool_name, rule_tool):
        return False
    
    # 如果没有参数模式，匹配成功
    if not param_patterns:
        return True
    
    # 参数级匹配
    sorted_args = sorted(tool_args.items())
    arg_values = [str(v) for k, v in sorted_args]
    
    # 参数数量必须匹配
    if len(arg_values) != len(param_patterns):
        return False
    
    # 逐个参数 glob 匹配
    for arg_val, pattern in zip(arg_values, param_patterns):
        if not fnmatch.fnmatch(arg_val, pattern):
            return False
    
    return True
```

### 修改 3: 在权限检查中应用
确保 `check_tool` 函数调用更新后的 `_match_rule`：

```python
def check_tool(self, tool_name: str, tool_args: dict) -> PermissionVerdict:
    # ... 现有逻辑
    
    for rule in self.allow_rules:
        if _match_rule(rule, tool_name, tool_args):
            return PermissionVerdict(allow=True, reason="Matched allow rule")
    
    # ... 其余逻辑
```

## 🧪 第三步：测试

### 1. 配置测试规则
在 `settings.json` 或权限配置中添加：

```json
{
  "permissions": {
    "allow": [
      "Bash(git commit:*)",
      "Bash(git status:*)",
      "Edit(*.md:*)"
    ]
  }
}
```

### 2. 测试场景

#### 场景 1: Git 命令
```python
# 应该通过
check_tool("Bash", {"command": "git commit", "args": "-m 'test'"})

# 应该被拒绝
check_tool("Bash", {"command": "rm -rf", "args": "/"})
```

#### 场景 2: 文件编辑
```python
# 应该通过
check_tool("Edit", {"file_path": "README.md", "content": "..."})

# 应该被拒绝
check_tool("Edit", {"file_path": "main.py", "content": "..."})
```

### 3. 运行测试
```bash
cd backend
pytest tests/ -k permission
```

## 📝 配置示例

### settings.json 完整示例
```json
{
  "permissions": {
    "mode": "normal",
    "allow": [
      "Read(*:*)",
      "Glob(*:*)",
      "Grep(*:*)",
      "Bash(git status:*)",
      "Bash(git commit:*)",
      "Bash(git push:*)",
      "Edit(*.md:*)",
      "Edit(*.txt:*)"
    ],
    "deny": [
      "Bash(rm -rf:*)",
      "Bash(sudo:*)",
      "Edit(/etc/*:*)"
    ]
  }
}
```

### 通配符说明
- `*` - 匹配任意内容
- `*.md` - 匹配所有 .md 文件
- `git *` - 匹配所有 git 开头的命令
- `Edit(*:*)` - 匹配所有 Edit 调用（两个参数都是任意）
