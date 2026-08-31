#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniCode 端到端测试脚本

测试 Agent 智能性、UI 美观性和性能
"""

import asyncio
import json
import time
import sys
from pathlib import Path

# 设置输出编码。不要替换 sys.stdout 本身；pytest 的捕获器依赖这个对象。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# 测试配置
BACKEND_WS_URL = "ws://localhost:8765"
FRONTEND_URL = "http://localhost:5173"
DESKTOP_URL = "http://localhost:3000"

class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    END = '\033[0m'

def print_success(msg):
    print(f"{Colors.GREEN}[PASS] {msg}{Colors.END}")

def print_error(msg):
    print(f"{Colors.RED}[FAIL] {msg}{Colors.END}")

def print_warning(msg):
    print(f"{Colors.YELLOW}[WARN] {msg}{Colors.END}")

def print_info(msg):
    print(f"{Colors.BLUE}[INFO] {msg}{Colors.END}")

def print_section(title):
    print(f"\n{Colors.BLUE}{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}{Colors.END}\n")

# ============================================================
# 测试 1: 后端健康检查
# ============================================================

def check_backend_health():
    """测试后端是否运行"""
    print_section("测试 1: 后端健康检查")

    # 检查后端进程
    import subprocess
    try:
        result = subprocess.run(
            ["netstat", "-an"],
            capture_output=True,
            text=True,
            timeout=5
        )

        if "8765" in result.stdout:
            print_success("后端 WebSocket 服务运行在 8765 端口")
            return True
        else:
            print_error("后端 WebSocket 服务未运行")
            print_info("请运行: python -m backend.main")
            return False
    except Exception as e:
        print_error(f"检查后端失败: {e}")
        return False

# ============================================================
# 测试 2: 前端健康检查
# ============================================================

def check_frontend_health():
    """测试前端是否运行"""
    print_section("测试 2: 前端健康检查")

    import subprocess
    try:
        result = subprocess.run(
            ["netstat", "-an"],
            capture_output=True,
            text=True,
            timeout=5
        )

        checks = {
            "5173": "前端开发服务器 (Vite)",
            "3000": "桌面端 (Electron)",
        }

        all_ok = True
        for port, name in checks.items():
            if port in result.stdout:
                print_success(f"{name} 运行在 {port} 端口")
            else:
                print_error(f"{name} 未运行")
                all_ok = False

        return all_ok
    except Exception as e:
        print_error(f"检查前端失败: {e}")
        return False

# ============================================================
# 测试 3: WebSocket 连接测试
# ============================================================

async def check_websocket_connection():
    """测试 WebSocket 连接"""
    print_section("测试 3: WebSocket 连接")

    try:
        print_info(f"尝试连接到 {BACKEND_WS_URL}...")

        async with websockets.connect(BACKEND_WS_URL, timeout=5) as ws:
            print_success("WebSocket 连接成功")

            # 测试 ping/pong
            start = time.time()
            await ws.ping()
            latency = (time.time() - start) * 1000

            if latency < 50:
                print_success(f"WebSocket 延迟: {latency:.2f}ms (优秀)")
            elif latency < 100:
                print_success(f"WebSocket 延迟: {latency:.2f}ms (良好)")
            else:
                print_warning(f"WebSocket 延迟: {latency:.2f}ms (需要优化)")

            return True
    except Exception as e:
        print_error(f"WebSocket 连接失败: {e}")
        return False

# ============================================================
# 测试 4: Agent 系统提示检查
# ============================================================

def check_agent_guidance():
    """检查 Agent 系统提示是否完善"""
    print_section("测试 4: Agent 系统提示")

    guidance_file = Path("backend/agent/prompting.py")

    if not guidance_file.exists():
        print_error("guidance.py 文件不存在")
        return False

    content = guidance_file.read_text(encoding="utf-8")

    # 检查关键指导内容
    checks = {
        "todo_write": "任务管理工具",
        "≥2 steps": "多步骤任务判断",
        "in_progress": "任务状态管理",
        "Example": "具体示例",
    }

    all_ok = True
    for keyword, description in checks.items():
        if keyword in content:
            print_success(f"包含 {description}: '{keyword}'")
        else:
            print_error(f"缺少 {description}: '{keyword}'")
            all_ok = False

    return all_ok

# ============================================================
# 测试 5: 前端任务组件检查
# ============================================================

def check_frontend_components():
    """检查前端任务组件"""
    print_section("测试 5: 前端任务组件")

    components = {
        "frontend/src.v2/chat/components/InlineTaskList.tsx": "对话内联任务列表",
        "frontend/src.v2/chat/components/inline-task-list.css": "任务列表样式",
        "frontend/src.v2/panels/TaskManagerPanel.tsx": "侧栏任务面板",
    }

    all_ok = True
    for path, name in components.items():
        file_path = Path(path)
        if file_path.exists():
            size = file_path.stat().st_size
            print_success(f"{name} 存在 ({size} bytes)")
        else:
            print_error(f"{name} 不存在")
            all_ok = False

    return all_ok

# ============================================================
# 测试 6: Z-Index 系统检查
# ============================================================

def check_zindex_system():
    """检查 Z-Index 系统"""
    print_section("测试 6: Z-Index 系统")

    zindex_file = Path("frontend/src.v2/styles/z-index.css")

    if not zindex_file.exists():
        print_error("z-index.css 文件不存在")
        return False

    content = zindex_file.read_text(encoding="utf-8")

    # 检查关键层级
    layers = {
        "--z-toast": "Toast 通知",
        "--z-modal": "模态框",
        "--z-drawer": "抽屉",
        "--z-sidebar": "侧边栏",
    }

    all_ok = True
    for var, name in layers.items():
        if var in content:
            print_success(f"{name} 层级定义: {var}")
        else:
            print_error(f"缺少 {name} 层级定义")
            all_ok = False

    return all_ok

# ============================================================
# 测试 7: 样式文件检查
# ============================================================

def check_style_files():
    """检查样式文件"""
    print_section("测试 7: 样式文件")

    styles = {
        "frontend/src.v2/styles/breakpoints.css": "响应式断点",
        "frontend/src.v2/styles/scroll.css": "滚动优化",
        "frontend/src.v2/styles/z-index.css": "Z-Index 系统",
        "frontend/src.v2/styles/animations.css": "动画",
    }

    all_ok = True
    for path, name in styles.items():
        file_path = Path(path)
        if file_path.exists():
            size = file_path.stat().st_size
            print_success(f"{name} 存在 ({size} bytes)")
        else:
            print_error(f"{name} 不存在")
            all_ok = False

    return all_ok

# ============================================================
# 测试 8: Hook 文件检查
# ============================================================

def check_hooks():
    """检查 React Hooks"""
    print_section("测试 8: React Hooks")

    hooks = {
        "frontend/src.v2/hooks/useFocusTrap.ts": "焦点陷阱 Hook",
    }

    all_ok = True
    for path, name in hooks.items():
        file_path = Path(path)
        if file_path.exists():
            content = file_path.read_text(encoding="utf-8")

            # 检查 Hook 导出
            if "export function useFocusTrap" in content:
                print_success(f"{name} - useFocusTrap ✓")
            else:
                print_warning(f"{name} - useFocusTrap 未找到")

            if "export function useEscapeKey" in content:
                print_success(f"{name} - useEscapeKey ✓")
            else:
                print_warning(f"{name} - useEscapeKey 未找到")

            if "export function usePreventScroll" in content:
                print_success(f"{name} - usePreventScroll ✓")
            else:
                print_warning(f"{name} - usePreventScroll 未找到")
        else:
            print_error(f"{name} 不存在")
            all_ok = False

    return all_ok

# ============================================================
# 测试 9: 文档完整性检查
# ============================================================

def check_documentation():
    """检查文档完整性"""
    print_section("测试 9: 文档完整性")

    docs = {
        "README.md": "项目说明",
        "AGENT_UX_OPTIMIZATION_COMPLETE.md": "Agent UX 报告",
        "UI_UX_OPTIMIZATION_COMPLETE.md": "UI/UX 报告",
        "CLEANUP_COMPLETE.md": "清理报告",
        "WORK_SUMMARY.md": "工作总结",
        "TESTING_PLAN.md": "测试计划",
    }

    all_ok = True
    for path, name in docs.items():
        file_path = Path(path)
        if file_path.exists():
            size = file_path.stat().st_size / 1024  # KB
            print_success(f"{name} 存在 ({size:.1f} KB)")
        else:
            print_warning(f"{name} 不存在")

    return all_ok

# ============================================================
# 测试 10: 项目清洁度检查
# ============================================================

def check_project_cleanliness():
    """检查项目清洁度"""
    print_section("测试 10: 项目清洁度")

    # 检查是否有 __pycache__
    import subprocess
    result = subprocess.run(
        ["find", ".", "-type", "d", "-name", "__pycache__"],
        capture_output=True,
        text=True,
        timeout=10
    )

    pycache_count = len([l for l in result.stdout.strip().split('\n') if l])

    if pycache_count == 0:
        print_success("无 __pycache__ 目录")
    else:
        print_warning(f"发现 {pycache_count} 个 __pycache__ 目录")

    # 检查是否有 .pyc 文件
    result = subprocess.run(
        ["find", ".", "-name", "*.pyc"],
        capture_output=True,
        text=True,
        timeout=10
    )

    pyc_count = len([l for l in result.stdout.strip().split('\n') if l])

    if pyc_count == 0:
        print_success("无 .pyc 文件")
    else:
        print_warning(f"发现 {pyc_count} 个 .pyc 文件")

    return pycache_count == 0 and pyc_count == 0

# ============================================================
# 主测试流程
# ============================================================


def test_e2e_diagnostics_remain_a_manual_utility() -> None:
    """Keep this file importable without treating ambient services as CI."""

    assert callable(main)

async def main():
    """主测试流程"""
    print(f"\n{Colors.BLUE}")
    print("╔════════════════════════════════════════════════════════════╗")
    print("║                                                            ║")
    print("║          MiniCode 端到端测试与验证                         ║")
    print("║                                                            ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print(Colors.END)

    results = {}

    # 运行所有测试
    results["后端健康"] = check_backend_health()
    results["前端健康"] = check_frontend_health()
    results["WebSocket"] = await check_websocket_connection()
    results["Agent 系统提示"] = check_agent_guidance()
    results["前端组件"] = check_frontend_components()
    results["Z-Index 系统"] = check_zindex_system()
    results["样式文件"] = check_style_files()
    results["React Hooks"] = check_hooks()
    results["文档完整性"] = check_documentation()
    results["项目清洁度"] = check_project_cleanliness()

    # 汇总结果
    print_section("测试结果汇总")

    passed = sum(1 for v in results.values() if v)
    total = len(results)
    percentage = (passed / total) * 100

    for test_name, result in results.items():
        status = "✅ 通过" if result else "❌ 失败"
        color = Colors.GREEN if result else Colors.RED
        print(f"{color}{status}{Colors.END} - {test_name}")

    print(f"\n总计: {passed}/{total} 通过 ({percentage:.1f}%)")

    if percentage == 100:
        print(f"\n{Colors.GREEN}🎉 所有测试通过！MiniCode 状态良好！{Colors.END}")
    elif percentage >= 80:
        print(f"\n{Colors.YELLOW}⚠️  大部分测试通过，仍有一些需要修复的问题{Colors.END}")
    else:
        print(f"\n{Colors.RED}❌ 多个测试失败，需要立即修复{Colors.END}")

    # 给出建议
    print_section("优化建议")

    if not results["后端健康"]:
        print("1. 启动后端服务: python -m backend.main")

    if not results["前端健康"]:
        print("2. 启动前端服务: cd frontend && npm run dev")

    if not results["项目清洁度"]:
        print("3. 清理项目: ./scripts/clean.sh")

    print(f"\n详细测试计划请查看: TESTING_PLAN.md")
    print(f"更多文档请查看根目录的 *.md 文件\n")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}测试被用户中断{Colors.END}")
    except Exception as e:
        print(f"\n{Colors.RED}测试失败: {e}{Colors.END}")
        import traceback
        traceback.print_exc()
