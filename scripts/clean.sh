#!/bin/bash
# MiniCode 项目清理脚本
# 用于清理 Python 缓存、临时文件等冗余内容

set -e

echo "🧹 MiniCode 项目清理脚本"
echo "========================"

# 统计清理前的情况
echo ""
echo "📊 清理前统计..."
BEFORE_PYCOMPILED=$(find . -name "*.pyc" -o -name "*.pyo" 2>/dev/null | wc -l)
BEFORE_PYCACHE=$(find . -type d -name "__pycache__" 2>/dev/null | wc -l)
echo "  - Python 编译文件: $BEFORE_PYCOMPILED"
echo "  - __pycache__ 目录: $BEFORE_PYCACHE"

# 1. 清理 Python 缓存
echo ""
echo "📦 清理 Python 缓存..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete 2>/dev/null || true
find . -type d -name ".pytest_cache" -exec rm -rf  + 2>/dev/null || true
find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
echo "✅ Python 缓存已清理"

# 2. 清理临时文件
echo ""
echo "📂 清理临时文件..."
find . -name ".DS_Store" -delete 2>/dev/null || true
find . -name "Thumbs.db" -delete 2>/dev/null || true
find . -name "*~" -delete 2>/dev/null || true
find . -name "*.bak" -delete 2>/dev/null || true
find . -name "*.swp" -delete 2>/dev/null || true
echo "✅ 临时文件已清理"

# 3. 清理日志文件（可选）
echo ""
read -p "是否清理日志文件？(y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]
then
    echo "🗑️  清理日志文件..."
    find . -name "*.log" -type f -delete 2>/dev/null || true
    echo "✅ 日志文件已清理"
fi

# 4. 清理 Node modules（可选，需要重新安装）
echo ""
read -p "是否清理 node_modules？(y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]
then
    echo "🗑️  清理 node_modules..."
    find . -name "node_modules" -type d -prune -exec rm -rf {} + 2>/dev/null || true
    echo "✅ node_modules 已清理（记得重新运行 npm install）"
fi

# 5. 显示清理结果
echo ""
echo "📊 清理完成！"
echo "========================"

# 统计清理后的情况
AFTER_PYCOMPILED=$(find . -name "*.pyc" -o -name "*.pyo" 2>/dev/null | wc -l)
AFTER_PYCACHE=$(find . -type d -name "__pycache__" 2>/dev/null | wc -l)

echo ""
echo "清理统计："
echo "  - Python 编译文件: $BEFORE_PYCOMPILED → $AFTER_PYCOMPILED"
echo "  - __pycache__ 目录: $BEFORE_PYCACHE → $AFTER_PYCACHE"

echo ""
echo "当前项目状态："
echo "  - Python 文件数: $(find . -name "*.py" -type f 2>/dev/null | wc -l)"
echo "  - TypeScript 文件数: $(find . -name "*.ts" -o -name "*.tsx" 2>/dev/null | wc -l)"

echo ""
echo "✨ 项目已清理完毕！"
