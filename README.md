# MiniCode

> 本地运行的 AI 编程助手桌面端 — 自主 Agent Loop + MCP 生态 + RAG 知识库

MiniCode 是一个全栈 AI 编程助手，通过自主编码代理（Agent Loop）在本地开发环境中执行代码阅读、修改、搜索、Git 操作等任务。后端基于 Python/FastAPI，前端基于 React/Electron，支持 OpenAI、Anthropic 及兼容 OpenAI 协议的多种 LLM 后端。

## 架构概览

```
┌─────────────────────────────────────────────┐
│  前端 (React 18 + TypeScript + Vite 6)       │
│  Electron 桌面壳                             │
├─────────────────────────────────────────────┤
│  后端 (Python 3.11+ / FastAPI)               │
│    ├── Agent Loop       自主编码代理           │
│    ├── LLM Adapters     OpenAI/Anthropic      │
│    ├── MCP Client       外部工具服务器集成      │
│    ├── RAG Pipeline     文档检索增强生成        │
│    ├── Tool System      20+ 内置工具           │
│    ├── Security         权限控制与沙箱          │
│    └── Terminal         远程终端管理            │
└─────────────────────────────────────────────┘
```

## 技术栈

### 后端

| 模块 | 技术 | 说明 |
|---|---|---|
| 框架 | FastAPI + Uvicorn | REST API + WebSocket |
| LLM | OpenAI / Anthropic SDK | Chat API、Responses API、Anthropic Messages API |
| 向量库 | ChromaDB | RAG 文档嵌入与检索 |
| MCP | MCP Python SDK | 动态加载外部工具服务器 |
| 搜索 | ripgrep + Tree-sitter | 代码搜索与 AST 分析 |
| 解析 | PyMuPDF + python-docx + trafilatura | 文档解析 |

### 前端

| 技术 | 用途 |
|---|---|
| React 18 + TypeScript | UI 框架 |
| Vite 6 | 构建与热更新 |
| Tailwind CSS 3 | 样式 |
| Monaco Editor | 代码编辑器 |
| Xterm.js | 终端仿真 |
| zustand | 状态管理 |
| react-grid-layout | 可拖拽面板布局 |
| react-markdown + remark-gfm | Markdown 渲染 |
| dnd-kit | 拖拽排序 |

### 桌面

- **Electron** — 跨平台打包（macOS / Windows / Linux）
- **electron-builder** — NSIS（Windows）、DMG（macOS）、AppImage（Linux）

## 项目结构

```
MINICODE/
├── backend/               # Python 后端
│   ├── agent/             # Agent Loop 核心
│   │   ├── loop.py        # 主循环（ReAct 模式）
│   │   ├── context.py     # 上下文构建与管理
│   │   ├── message.py     # 消息模型
│   │   ├── state.py       # Agent 状态管理
│   │   └── tool_execution.py  # 工具执行引擎
│   ├── llm/               # LLM 适配层
│   │   ├── openai_adapter.py   # OpenAI API 适配器
│   │   ├── anthropic_adapter.py # Anthropic API 适配器
│   │   └── model_registry.py   # 模型注册中心
│   ├── tools/             # 工具系统（20+ 工具）
│   │   ├── file_tools.py  # 文件读写
│   │   ├── search_tools.py # 网络搜索
│   │   ├── git_tools.py   # Git 操作
│   │   ├── command_tool.py # 命令执行
│   │   ├── web_tools.py   # 网页抓取
│   │   └── ...
│   ├── api/               # FastAPI 端点
│   │   ├── routes_chat.py     # 聊天 API
│   │   ├── routes_llm.c      # LLM 配置 API
│   │   ├── routes_skills.py   # Skills 管理 API
│   │   └── routes_health.py   # 健康检查 API
│   ├── mcp/               # MCP 客户端与管理
│   ├── rag/               # RAG 流水线
│   ├── workspace/         # 工作区管理
│   ├── terminal/          # 终端会话管理
│   ├── security/          # 安全与权限
│   ├── skills/            # Skills 加载与执行
│   ├── conversations/     # 对话记录
│   ├── memory/            # 记忆系统
│   ├── config.py          # 统一配置中心
│   └── main.py            # FastAPI 入口
├── frontend/              # React 前端
│   ├── src.v2/            # 核心源码
│   │   ├── App.tsx        # 根组件
│   │   ├── main.tsx       # 入口
│   │   ├── chat/          # 聊天界面
│   │   ├── composer/      # Composer 组件
│   │   ├── panels/        # 面板系统
│   │   ├── desktop/       # 桌面集成
│   │   ├── stores/        # zustand 状态
│   │   ├── hooks/         # React Hooks
│   │   └── shell/         # Workbench 布局
│   ├── styles/            # 全局样式
│   └── vite.config.ts     # Vite 配置
├── desktop/               # Electron 桌面壳
│   ├── main.js            # 主进程
│   ├── preload.js         # 预加载脚本
│   ├── window-manager.js  # 窗口管理
│   └── pty-manager.js     # PTY 终端管理
├── data/                  # 运行时数据
│   ├── chroma/            # 向量数据库
│   ├── conversations/     # 对话存档
│   ├── memory/            # 记忆存储
│   └── artifacts/         # 文件快照
├── docs/                  # 设计文档
├── tests/                 # 测试（pytest）
├── scripts/               # 开发脚本
├── pyproject.toml         # Python 依赖
└── start.bat / start.sh   # 开发启动脚本
```

## 快速开始

### 前置条件

- Python 3.11+
- Node.js 18+
- npm 9+

### 安装

```bash
# 克隆仓库
git clone <repo-url>
cd minicode

# 安装 Python 依赖
pip install -e ".[dev,search,docparse]"

# 安装前端依赖
cd frontend && npm install && cd ..

# 安装桌面依赖
cd desktop && npm install && cd ..
```

### 配置

在项目根目录创建 `.env` 文件，或直接配置 `settings.json`：

```env
# LLM 配置（至少配置一个）
OPENAI_API_KEY=sk-your-key
# 或
ANTHROPIC_API_KEY=sk-ant-your-key

# 可选：自定义 OpenAI 兼容提供商
# LLM_PROVIDER=custom
# OPENAI_BASE_URL=https://your-custom-endpoint/v1
# CUSTOM_API_KEY=your-custom-key
```

### 启动开发环境

**Windows：**
```bash
.\start.bat
```

**macOS/Linux：**
```bash
./start.sh
```

或分别启动：

```bash
# 终端 1：启动后端（端口 8000）
python -m backend

# 终端 2：启动前端（端口 5173）
cd frontend && npm run dev
```

启动后访问 http://localhost:5173 即可使用。

## 核心功能

### 🤖 自主 Agent Loop

MiniCode 的核心是一个自主 AI 代理，采用 **ReAct** 模式运作：

1. **上下文构建** — 自动收集项目文件、Git 状态、工具列表构成提示词
2. **流式执行** — 模型多步推理 → 调用工具 → 观察结果 → 继续推理，直至任务完成
3. **恢复机制** — LLM 错误自动降级重试、工具调用修复、停顿检测
4. **终止条件** — 无更多工具调用时自动收敛

### 🔧 工具系统

20+ 内置工具，覆盖开发全流程：

- **文件操作**: `read_file`, `write_file`, `edit_file`, `list_files`
- **代码搜索**: `grep_files`, `glob_files`, `fuzzy_search`, AST 分析
- **Web 搜索**: `web_search`, `web_fetch`
- **Git**: `git_status`, `git_diff`, `git_commit`, `git_push`
- **终端**: `run_command`（带沙箱执行）
- **记忆**: `remember`, `recall`, `forget`
- **MCP**: 通过 MCP 协议动态加载外部工具

### 🔌 MCP 工具生态

支持 [Model Context Protocol](https://modelcontextprotocol.io/) 标准，可挂载任意 MCP 服务器作为外部工具，与社区工具生态无缝集成。

### 📚 RAG 知识库

基于 ChromaDB 的文档检索增强生成，支持：
- PDF、Word、Markdown 文档索引
- 语义分块与向量嵌入
- 会话关联检索

### 🛡️ 安全体系

四层权限模型：

| 级别 | 说明 |
|---|---|
| ✅ 自动允许 | 只读操作（读文件、搜索、Git 查看等） |
| 🔍 需审查 Diff | 写操作（创建/编辑文件） |
| ❓ 需确认 | 命令执行、Git 推送、终端操作等 |
| 🚫 永远拒绝 | 敏感路径和环境变量 |

### 💾 持久化

- **对话记录** — 自动保存，支持 Checkpoint 恢复
- **记忆系统** — 跨会话持久化关键信息
- **工作区缓存** — 文件状态、代码索引
- **用户偏好** — UI 布局、主题、快捷键设置（持久化至磁盘）

## 测试

```bash
# 运行全部测试
pytest

# 运行指定测试
pytest tests/test_smoke_api.py
pytest tests/test_tool_execution.py

# 带覆盖率
pytest --cov=backend
```

## 桌面打包

```bash
cd desktop
npm run build    # 构建前端
npm run dist     # 打包当前平台
```

## 文档

项目设计文档位于 `docs/` 目录，涵盖：
- 系统架构设计 (`current-system-design.md`)
- Agent Loop 实现方案 (`codex-desktop-agent-design.md`)
- UI/UX v2 设计 (`ui-design-v2.md`)
- Claude Code 对齐方案 (`cc-alignment-implementation-record.md`)

## 项目状态

**版本**: 0.2.0 — 早期开发阶段，核心 Agent Loop 已验证并与真实 LLM 对接，前端 UI/UX 正在快速迭代。

## 许可

[MIT](LICENSE)
