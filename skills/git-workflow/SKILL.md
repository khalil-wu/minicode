---
name: git-workflow
description: Git 操作规范与 Conventional Commit 格式
version: 1.0.0
triggers: [git, commit, branch, merge, 提交, 分支, 版本]
conflicts: []
tools_required: [run_command]
mcp_required: []
linked_resources: []
---

# Git Workflow Expert

你现在进入 Git 工作流模式。所有 Git 操作遵循以下规范：

## Commit Message 格式 (Conventional Commits)

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

**Type 列表：**
- `feat`: 新功能
- `fix`: Bug 修复
- `docs`: 文档更新
- `style`: 格式调整（不影响逻辑）
- `refactor`: 重构（无功能变化）
- `perf`: 性能优化
- `test`: 测试相关
- `chore`: 构建/工具/配置
- `ci`: CI/CD 变更

**示例：**
```
feat(agent): 添加 Skills 自动检测功能

- 实现关键词触发机制
- 支持冲突 Skill 自动停用
- 添加 Layer 1/Layer 2 分级加载

Closes #42
```

## 分支规范

- `main`: 稳定发布分支
- `dev`: 开发主线
- `feat/<name>`: 功能分支
- `fix/<name>`: Bug 修复分支
- `release/<version>`: 发布准备

## 工作流程

1. **检查状态**: `git status` + `git diff`
2. **暂存**: `git add -p`（交互式暂存，逐块确认）
3. **提交**: 生成规范 commit message
4. **推送**: `git push origin <branch>`

## 安全规则

- ⚠️ **禁止** force push 到 main/dev
- ⚠️ **禁止** 提交 .env、密钥、凭据文件
- ✅ 每次 commit 前执行 `git diff --staged` 确认
