# MiniCode 项目清理方案

## 🎯 目标

清理项目中的冗余文件，包括：
- Python 缓存文件（__pycache__, *.pyc）
- 临时文件和日志
- 过时的文档
- 已删除功能的残留文件
- 构建产物

---

## 📊 当前冗余文件统计

### 1. Python 缓存和编译文件

**统计：**
- *.pyc / *.pyo 文件：**663 个**
- __pycache__ 目录：**30+ 个**

**位置：**
```
./backend/agent/__pycache__/
./backend/api/__pycache__/
./backend/llm/__pycache__/
./backend/mcp/__pycache__/
... (30+ directories)
```

### 2. Git 中已删除但未提交的文件

**已删除的文件（需要 git rm）：**
```
backend/agent/plan.py
backend/agent/planner.py
backend/agent/policies/grounded_reply.py
backend/agent/policies/realtime_search.py
backend/agent/policies/web_search_guard.py
backend/context/__init__.py
backend/context/builder.py
backend/context/models.py
```

### 3. 临时目录

```
./.tmp/
./.pytest_cache/
./frontend/.tmp/
./tests/.tmp/
```

### 4. 冗余文档（根目录）

**可能需要整理的文档：**
```
AGENT_UX_OPTIMIZATION_COMPLETE.md     (最新)
AGENT_UX_OPTIMIZATION_PLAN.md         (最新)
AGENT_UX_SUMMARY.md                   (最新)
frontend_bug_scan_20260614.md         (过期？)
IMPLEMENTATION_COMPLETE.md            (过期？)
IMPLEMENTATION_GUIDE.md               (过期？)
OPTIMIZATION_PLAN_2026-06-13.md       (过期)
OPTIMIZATION_SUMMARY.md               (可合并)
PHASE1_OPTIMIZATION_COMPLETE.md       (过期)
PHASE2_3_OPTIMIZATION_COMPLETE.md     (过期)
PHASE5_ANIMATION_COMPLETE.md          (过期)
PROJECT_REVIEW_2026-06-13.md          (过期)
TASK_DISPLAY_DIAGNOSIS.md             (最新)
TASK_DISPLAY_TEST_GUIDE.md            (最新)
TESTING_CHECKLIST.md                  (可保留)
UI_UX_OPTIMIZATION_COMPLETE.md        (最新)
UI_UX_OPTIMIZATION_PLAN.md            (最新)
README.md                             (保留)
```

---

## 🧹 清理方案

### Phase 1: 清理 Python 缓存（立即执行）

#### 1.1 清理 __pycache__ 和 *.pyc

```bash
# 删除所有 __pycache__ 目录
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null

# 删除所有 .pyc 和 .pyo 文件
find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

# 删除 .pytest_cache
find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null
```

**预期效果：**
- 删除 663+ 个 .pyc 文件
- 删除 30+ 个 __pycache__ 目录
- 节省磁盘空间：~10-50 MB

---

### Phase 2: 清理临时文件和目录（立即执行）

```bash
# 删除 .tmp 目录
rm -rf ./.tmp
rm -rf ./frontend/.tmp
rm -rf ./tests/.tmp

# 删除系统临时文件
find . -name ".DS_Store" -delete
find . -name "Thumbs.db" -delete
find . -name "*~" -delete
```

---

### Phase 3: Git 清理（需要确认）

#### 3.1 提交已删除的文件

```bash
# 查看已删除的文件
git status | grep "deleted:"

# 提交所有删除
git add -u

# 或者逐个确认
git rm backend/agent/plan.py
git rm backend/agent/planner.py
git rm backend/agent/policies/grounded_reply.py
git rm backend/agent/policies/realtime_search.py
git rm backend/agent/policies/web_search_guard.py
git rm backend/context/__init__.py
git rm backend/context/builder.py
git rm backend/context/models.py

# 提交
git commit -m "chore: remove deprecated files and modules"
```

---

### Phase 4: 整理文档（需要人工判断）

#### 4.1 保留的文档（核心文档）

```
README.md                             ✅ 保留（项目说明）
AGENT_UX_OPTIMIZATION_COMPLETE.md     ✅ 保留（Agent UX 最新）
AGENT_UX_OPTIMIZATION_PLAN.md         ✅ 保留（详细方案）
TASK_DISPLAY_DIAGNOSIS.md             ✅ 保留（诊断指南）
TASK_DISPLAY_TEST_GUIDE.md            ✅ 保留（测试指南）
UI_UX_OPTIMIZATION_COMPLETE.md        ✅ 保留（UI UX 最新）
UI_UX_OPTIMIZATION_PLAN.md            ✅ 保留（详细方案）
TESTING_CHECKLIST.md                  ✅ 保留（测试清单）
```

#### 4.2 归档的文档（移到 docs/archive/）

```
frontend_bug_scan_20260614.md         → docs/archive/
IMPLEMENTATION_COMPLETE.md            → docs/archive/
IMPLEMENTATION_GUIDE.md               → docs/archive/
OPTIMIZATION_PLAN_2026-06-13.md       → docs/archive/
OPTIMIZATION_SUMMARY.md               → docs/archive/
PHASE1_OPTIMIZATION_COMPLETE.md       → docs/archive/
PHASE2_3_OPTIMIZATION_COMPLETE.md     → docs/archive/
PHASE5_ANIMATION_COMPLETE.md          → docs/archive/
PROJECT_REVIEW_2026-06-13.md          → docs/archive/
AGENT_UX_SUMMARY.md                   → docs/archive/ (内容已包含在 COMPLETE 中)
```

#### 4.3 文档整理命令

```bash
# 创建归档目录
mkdir -p docs/archive

# 移动过期文档
mv frontend_bug_scan_20260614.md docs/archive/
mv IMPLEMENTATION_COMPLETE.md docs/archive/
mv IMPLEMENTATION_GUIDE.md docs/archive/
mv OPTIMIZATION_PLAN_2026-06-13.md docs/archive/
mv OPTIMIZATION_SUMMARY.md docs/archive/
mv PHASE1_OPTIMIZATION_COMPLETE.md docs/archive/
mv PHASE2_3_OPTIMIZATION_COMPLETE.md docs/archive/
mv PHASE5_ANIMATION_COMPLETE.md docs/archive/
mv PROJECT_REVIEW_2026-06-13.md docs/archive/
mv AGENT_UX_SUMMARY.md docs/archive/

# 添加到 git
git add docs/archive/
git commit -m "docs: archive old documentation"
```

---

### Phase 5: 更新 .gitignore（防止未来污染）

#### 5.1 确保 .gitignore 包含以下内容

```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
*.egg-info/
.pytest_cache/
.coverage
htmlcov/

# Temporary files
.tmp/
*.tmp
*.bak
*.swp
*~

# OS files
.DS_Store
Thumbs.db

# IDEs
.vscode/
.idea/
*.sublime-*

# Logs
*.log
logs/

# Build artifacts
dist/
build/
*.egg

# Node
node_modules/
.npm/
```

---

## 📋 执行清单

### 立即执行（安全操作）

- [ ] **清理 Python 缓存**
  ```bash
  find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
  find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete
  ```

- [ ] **清理临时文件**
  ```bash
  rm -rf ./.tmp ./frontend/.tmp ./tests/.tmp
  find . -name ".DS_Store" -delete
  ```

- [ ] **清理 pytest 缓存**
  ```bash
  find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null
  ```

### 需要确认（涉及 git）

- [ ] **提交已删除的文件**
  ```bash
  git add -u
  git commit -m "chore: remove deprecated modules"
  ```

- [ ] **归档旧文档**
  ```bash
  mkdir -p docs/archive
  mv OPTIMIZATION_PLAN_2026-06-13.md docs/archive/
  # ... (其他旧文档)
  git add docs/archive/
  git commit -m "docs: archive old documentation"
  ```

### 可选优化

- [ ] **创建清理脚本**
  - 创建 `scripts/clean.sh` 自动化清理

- [ ] **更新 .gitignore**
  - 确保包含所有常见临时文件模式

---

## 🎯 预期效果

### Before（清理前）

```
Total Files: ~5000+
Python Cache: 663 .pyc files + 30 __pycache__ dirs
Temp Dirs: 3+ directories
Root Docs: 18 markdown files
Git Deleted: 8 files (未提交)
```

### After（清理后）

```
Total Files: ~4000
Python Cache: 0
Temp Dirs: 0
Root Docs: 8 markdown files (核心) + archive/
Git Status: Clean
Disk Space Saved: ~50-100 MB
```

---

## 🚀 自动化清理脚本

### 创建 `scripts/clean.sh`

```bash
#!/bin/bash
# MiniCode 项目清理脚本

set -e

echo "🧹 MiniCode 项目清理脚本"
echo "========================"

# 1. 清理 Python 缓存
echo ""
echo "📦 清理 Python 缓存..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete
find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
echo "✅ Python 缓存已清理"

# 2. 清理临时文件
echo ""
echo "📂 清理临时文件..."
rm -rf ./.tmp ./frontend/.tmp ./tests/.tmp 2>/dev/null || true
find . -name ".DS_Store" -delete 2>/dev/null || true
find . -name "Thumbs.db" -delete 2>/dev/null || true
find . -name "*~" -delete 2>/dev/null || true
find . -name "*.bak" -delete 2>/dev/null || true
echo "✅ 临时文件已清理"

# 3. 清理 Node modules（可选，重新安装需要时间）
echo ""
read -p "是否清理 node_modules？(y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]
then
    echo "🗑️  清理 node_modules..."
    find . -name "node_modules" -type d -prune -exec rm -rf {} + 2>/dev/null || true
    echo "✅ node_modules 已清理（记得重新运行 npm install）"
fi

# 4. 显示清理结果
echo ""
echo "📊 清理完成！"
echo "========================"

# 统计
echo "剩余 Python 文件数量："
find . -name "*.py" -type f | wc -l

echo "剩余 .pyc 文件数量："
find . -name "*.pyc" -type f | wc -l

echo ""
echo "✨ 项目已清理完毕！"
```

### 使用方法

```bash
# 赋予执行权限
chmod +x scripts/clean.sh

# 运行清理
./scripts/clean.sh
```

---

## 📝 维护建议

### 1. 定期清理（建议每周）

```bash
# 快速清理缓存
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
```

### 2. Git 提交前清理

```bash
# 提交前检查
git status

# 清理未跟踪的文件
git clean -xdn  # 预览
git clean -xdf  # 执行（谨慎！）
```

### 3. 使用 pre-commit Hook

创建 `.git/hooks/pre-commit`：
```bash
#!/bin/bash
# 提交前自动清理

# 清理 Python 缓存
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete

# 清理临时文件
find . -name "*~" -delete
```

---

## ⚠️ 注意事项

### 不要删除的文件/目录

```
.git/              ✅ 保留（Git 仓库）
.venv/             ✅ 保留（Python 虚拟环境）
.claude/           ✅ 保留（Claude Code 配置）
node_modules/      ⚠️  可重新安装，但需要时间
frontend/dist/     ⚠️  构建产物，可重新构建
backend/__init__.py ✅ 保留（Python 包标记）
```

### 清理前备份

```bash
# 创建备份（可选）
tar -czf minicode-backup-$(date +%Y%m%d).tar.gz \
  --exclude=node_modules \
  --exclude=__pycache__ \
  --exclude=.git \
  .
```

---

## 📊 清理效果估算

| 项目 | 文件数 | 大小 | 操作 |
|------|--------|------|------|
| **Python 缓存** | 663+ | 10-30 MB | 删除 |
| **临时目录** | - | 5-10 MB | 删除 |
| **Git 未提交** | 8 | < 1 MB | 提交 |
| **过期文档** | 10 | ~200 KB | 归档 |
| **系统文件** | 少量 | < 1 MB | 删除 |
| **总计** | 700+ | **50-100 MB** | - |

---

## 🎯 总结

### 立即可以做的（安全操作）

1. ✅ 清理 Python 缓存（__pycache__, *.pyc）
2. ✅ 清理临时文件（.tmp, *~）
3. ✅ 清理系统文件（.DS_Store）

### 需要确认的操作

1. ⚠️ 提交 git 删除的文件
2. ⚠️ 归档旧文档到 docs/archive/
3. ⚠️ 清理 node_modules（需重新安装）

### 长期维护

1. 📅 定期运行清理脚本
2. 🔧 使用 git hooks 自动清理
3. 📝 保持 .gitignore 更新

---

**准备好了吗？** 让我开始执行清理！🧹
