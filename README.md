# MiniCode

MiniCode 是一个本地运行的 AI 编程助手，包含 Python 后端、React + Vite 前端和 Electron 桌面端。它提供工作区管理、代码编辑、终端、预览以及多模型对话能力。

完整回复后自动收起工具过程，点击可查看执行记录；子任务的工作记录在侧栏独立呈现，Diff 按文件展开。代码工作区提供 Monaco 编辑器、LSP 补全与导航，默认显示代码缩略图。

## 环境要求

- Python 3.11 或更高版本
- Node.js 20.19 或更高版本
- npm
- Git 2.36 或更高版本（工作树管理使用 `git worktree list --porcelain -z`）
- Windows x64 桌面端；Linux x86_64 后端/SSH 宿主要求 glibc 2.31 或更高版本（Ubuntu 20.04 为构建与安装验证基线）

Linux 后端的原生组件、独立 wheel 和 SSH 安装步骤见 [Linux 后端安装](docs/linux-backend-install.md)。当前发布工作流只发布 Windows 桌面安装包；Linux wheel 由成功 CI 的 `minicode-linux-backend-wheel` artifact 提供，远端后端需要预先安装，不会随桌面更新自动安装或升级。

## 安装

在项目根目录执行：

```bash
python -m venv .venv
```

Windows：

```powershell
.venv\Scripts\Activate.ps1
pip install -e .
cd frontend
npm ci
cd ..\desktop
npm ci
```

Linux 源码开发（先按上述安装文档准备同平台 native resources）：

```bash
source .venv/bin/activate
pip install -e .
cd frontend && npm ci
cd ../desktop && npm ci
```

## 启动开发环境

Windows 可直接运行：

```bat
start.bat
```

Linux 可运行：

```bash
bash start.sh
```

也可以分别启动前端和桌面端：

```bash
cd frontend
npm run dev
```

然后在另一个终端执行：

```bash
cd desktop
npm run start:client:devfrontend
```

## 构建

构建前端：

```bash
cd frontend
npm run build
```

构建桌面端目录：

```bash
cd desktop
npm run pack:dir
```

生成 Windows 安装包：

```bash
cd desktop
npm run dist:win
```

## 项目结构

```text
backend/    Python 后端与 WebSocket 服务
frontend/   React + Vite 前端
desktop/    Electron 桌面端
scripts/    开发和构建辅助脚本
skills/     项目内置技能
```

## 配置

模型服务和运行参数通过环境变量或本地配置文件设置。敏感配置请放在 `.env` 或 `config.local.toml` 中，不要提交到仓库。

## 许可证

当前项目未声明开源许可证。
