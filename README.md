# MiniCode

> An AI-powered coding assistant desktop application — your intelligent pair programmer.

MiniCode 是一个基于 Electron 构建的桌面端 AI 编程助手，集成了 Python FastAPI 后端和 React 前端，提供对话式编程、工具调用、多文件编辑、项目管理等能力。

## 特性

- **对话式编程** — 通过自然语言与 AI 对话，完成代码编写、调试、重构等任务
- **工具调用系统** — 内置文件读写、Shell 执行、代码搜索等丰富工具，AI 可自主调用
- **MCP 集成** — 支持 Model Context Protocol，可连接外部工具和数据源
- **Skills 技能系统** — 可扩展的技能框架，支持技能市场，按需加载专业能力
- **多模型支持** — 兼容 OpenAI、Anthropic 等多种 LLM 提供商
- **Artifact 画布** — 独立的代码/文档预览面板，实时渲染 AI 生成的内容
- **Workspace 管理** — 项目级上下文管理、Git worktree 支持、文件监听
- **上下文压缩** — 智能对话压缩，在长会话中保持关键信息不丢失
- **Checkpoint 回溯** — 对话级别的快照与回退，随时回到任意决策点
- **子代理委派** — 将探索性任务分发给子代理并行执行，提升效率
- **RAG 管线** — 基于项目的检索增强生成，让 AI 真正理解你的代码库
- **文件记忆** — 跨会话的项目知识持久化（AGENTS.md / .minicode/memory/）

## 技术栈

| 层级 | 技术 |
|------|------|
| 桌面壳 | Electron 47、electron-vite、Electron Forge |
| 前端 | React 19、TypeScript 6、Vite 7、Tailwind CSS 4、Zustand、CodeMirror |
| 后端 | Python 3.12+、FastAPI、Uvicorn、Pydantic |
| LLM | OpenAI API、Anthropic API（可扩展） |
| 构建 | Hatchling（Python）、npm/Forge（桌面端） |

## 项目结构

```
MiniCode/
├── backend/                # Python 后端
│   ├── __main__.py         # CLI 入口（fastapi / uvicorn / ws 模式）
│   ├── main.py             # FastAPI 应用工厂
│   ├── version.py          # 版本号 0.1.0
│   ├── agent/              # Agent 核心循环、工具调用、上下文管理
│   ├── artifacts/          # Artifact 生成与管理
│   ├── auth/               # 认证模块
│   ├── config/             # 配置管理
│   ├── gateway/            # 网关层
│   ├── llm/                # LLM 调用抽象
│   ├── mcp/                # Model Context Protocol 实现
│   ├── memory/             # 记忆与上下文持久化
│   ├── providers/          # LLM 提供商适配
│   ├── rag/                # 检索增强生成
│   ├── sessions/           # 会话管理
│   ├── skills/             # 技能系统（加载、执行、市场）
│   ├── telemetry/          # 遥测与分析
│   ├── tools/              # 内置工具定义
│   └── workspace/          # 工作区、worktree、文件监听
├── frontend/               # React 前端（Vite）
│   ├── src.v2/             # 前端源码（组件、页面、状态管理）
│   ├── package.json        # 前端依赖
│   └── vite.config.ts      # Vite 配置
├── desktop/                # Electron 桌面端
│   ├── src/                # 主进程、preload、IPC
│   ├── resources/          # 应用图标与资源
│   └── package.json        # 桌面端依赖与 Forge 配置
├── cc/                     # Claude Code 源码参考（独立 npm 包）
├── scripts/                # 构建与检查脚本
│   ├── version_sync.py     # 版本同步
│   ├── check-protocol-sync.py
│   ├── check-large-files.py
│   └── check-no-duplicate-tools.py
├── pyproject.toml          # Python 项目配置（Hatchling）
└── .mcp.json               # MCP 服务器配置
```

## 快速开始

### 环境要求

- **Python** >= 3.12
- **Node.js** >= 18
- **npm** >= 9

### 安装依赖

```bash
# 后端
pip install -e .

# 前端
cd frontend && npm install

# 桌面端
cd desktop && npm install
```

### 开发模式

```bash
# 启动后端（FastAPI 开发服务器）
python -m backend --mode fastapi --port 8000

# 启动前端（Vite 开发服务器）
cd frontend && npm run dev

# 启动 Electron（开发模式）
cd desktop && npm run start:dev
```

### 构建

```bash
# 构建前端
cd frontend && npm run build

# 打包桌面应用
cd desktop && npm run make
```

## 使用方式

1. **启动应用** — 打开 MiniCode 桌面端
2. **选择项目** — 通过 Workspace 面板打开你的代码项目
3. **开始对话** — 在聊天面板中用自然语言描述你的编程需求
4. **工具协作** — AI 会自动调用文件编辑、Shell 命令、代码搜索等工具完成任务
5. **查看 Artifact** — 生成的代码、文档会在 Artifact 画布中实时预览
6. **管理会话** — 使用 Checkpoint 功能回溯到任意决策点

## 配置

### MCP 服务器

在项目根目录的 `.mcp.json` 中配置外部 MCP 服务器：

```json
{
  "mcpServers": {
    "my-server": {
      "command": "node",
      "args": ["path/to/server.js"]
    }
  }
}
```

### AGENTS.md

在项目中放置 `AGENTS.md` 文件来定义项目级的 AI 行为规范，包括：
- 代码风格偏好
- 项目架构约定
- 禁止操作列表
- 子代理委派策略

## 版本

当前版本：**0.1.0**

## 许可证

私有项目，保留所有权利。
