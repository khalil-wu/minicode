# MiniCode

MiniCode 是一个本地优先的 AI 编程桌面工作台：Electron 桌面壳负责安全 IPC、内置浏览器和终端，FastAPI 后端负责 Agent、Workspace、Git、MCP 和任务运行，React 前端提供会话、审阅和项目界面。

## 特性

- **对话式编程** — 通过自然语言与 AI 对话，完成代码编写、调试、重构等任务
- **工具调用系统** — 内置文件读写、Shell 执行、代码搜索等丰富工具，AI 可自主调用
- **MCP 集成** — 支持 Model Context Protocol，可连接外部工具和数据源
- **Skills 技能系统** — 可扩展的技能框架，支持技能市场，按需加载专业能力
- **多模型支持** — 兼容 OpenAI、Anthropic 等多种 LLM 提供商
- **审阅与 Git** — 工作区/暂存区 Diff、文件级 stage/unstage/revert、行内评论和 PR/CI 状态
- **Workspace 管理** — 项目级上下文、Git worktree、文件监听、受信任路径边界
- **内置浏览器** — 多标签页、Agent 控制、元素/区域批注、Console/Network 诊断、站点权限和安全下载策略
- **定时任务** — 项目作用域 cron、时区、Worktree 隔离、heartbeat、立即运行、取消/重试和运行历史
- **上下文压缩** — 智能对话压缩，在长会话中保持关键信息不丢失
- **Checkpoint 回溯** — 对话级别的快照与回退，随时回到任意决策点
- **子代理委派** — 将探索性任务分发给子代理并行执行，提升效率
- **文件记忆** — 跨会话的项目知识持久化（AGENTS.md / .minicode/memory/）

## 技术栈

| 层级 | 技术 |
|------|------|
| 桌面壳 | Electron 33、受限 preload IPC、内置 BrowserView / PTY |
| 前端 | React 18、TypeScript 5、Vite 8、Zustand、Monaco、xterm |
| 后端 | Python 3.11+、FastAPI、Uvicorn、Pydantic |
| LLM | OpenAI API、Anthropic API（可扩展） |
| 构建 | Hatchling（Python）、npm、electron-builder |

## 项目结构

```
MiniCode/
├── backend/                # Python 后端
│   ├── main.py             # FastAPI 应用工厂
│   ├── version.py          # 后端版本
│   ├── agent/              # Agent 核心循环、工具调用、上下文管理
│   ├── artifact/           # Artifact 生成与管理
│   ├── llm/                # LLM 调用抽象
│   ├── mcp/                # Model Context Protocol 实现
│   ├── memory/             # 记忆与上下文持久化
│   ├── services/           # WebSocket/API 业务服务
│   ├── conversations/      # 会话持久化
│   ├── skills/             # 技能系统（加载、执行、市场）
│   ├── tools/              # 内置工具定义
│   └── workspace/          # 工作区、worktree、文件监听
├── frontend/               # React 前端（Vite）
│   ├── src.v2/             # 前端源码（组件、页面、状态管理）
│   ├── package.json        # 前端依赖
│   └── vite.config.ts      # Vite 配置
├── desktop/                # Electron 桌面端
│   ├── main.js             # 主进程
│   ├── preload.js          # 受限桌面 API
│   └── package.json        # 桌面端依赖与 electron-builder 配置
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

- **Python** >= 3.11
- **Node.js** >= 20.19
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

# 启动 Electron（使用已构建的相对路径前端）
cd desktop && npm run start:client
```

### 捕获当前界面到 Figma

MiniCode 的开发版可通过 html.to.design Electron SDK 将当前窗口保存为本地
`.h2d` 文件。先启动 Vite 前端，再启动带捕获功能的 Electron：

```powershell
# 终端 1
cd frontend
npm run dev

# 终端 2
cd desktop
npm run dev:figma
```

在 Electron 菜单中选择 `View > Capture current window to Figma...`，或按
`Ctrl+Shift+F12`。保存后，将 `.h2d` 文件拖入 html.to.design Figma 插件。
捕获功能仅在未打包的开发版中启用，默认启动和正式安装包不会加载 SDK。

### 构建

```bash
# 构建前端
cd frontend && npm run build

# 生成 Windows 安装包
cd desktop && npm run dist:win
```

### Agent 沙箱（Windows）

Windows 工作区命令和验证命令都运行在 Linux 容器沙箱中。首次使用前构建镜像：

```powershell
docker build -t minicode-agent-sandbox:latest backend/sandbox
```

Docker 或 Podman 不可用时，MiniCode 会 fail closed，不会回退到宿主机普通命令执行。可用环境变量覆盖运行时和镜像：

```powershell
$env:MINICODE_SANDBOX_RUNTIME = "docker"  # 或 podman
$env:MINICODE_SANDBOX_IMAGE = "minicode-agent-sandbox:latest"
```

沙箱内的 shell 是 Linux 容器中的 `pwsh`；只有显式 bypass/批准的 escalation 才允许使用 Windows host PowerShell。验证命令与普通命令共享相同的沙箱和权限边界。

### 发布门禁

```bash
# Python（默认同时收集 tests 与 backend/tests）
python -m pytest
python scripts/check-protocol-sync.py

# 前端
cd frontend
npm run test
npm run build
npm run check:bundle-budget
npm run check:ui-debt
npm run test:e2e:electron

# Electron
cd ../desktop
npm run test:unit
npm run test:e2e
npm run build:frontend:desktop
```

Monaco、Mermaid、终端和右侧大型面板均按需加载；`check:bundle-budget` 会阻止主入口或单个产物重新膨胀。

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

当前版本以 `backend/version.py` 与 `desktop/package.json` 为准；发布前运行：

```bash
cd desktop && npm run version:check
```

## 许可证

私有项目，保留所有权利。
