# 🎯 MiniCode 升级计划 - 执行摘要

> **项目路径:** `C:\Desktop\MiniCode`  
> **指南位置:** `.implementation-guide/` 目录  
> **目标:** 对标 Claude Code Desktop 2026 Redesign

---

## 📂 已创建的指南文件

| 文件 | 内容 | 预计时间 |
|------|------|---------|
| **README.md** | 总览和实施顺序 | - |
| **phase1-1-drag-panels.md** | 拖拽面板调研 | 2 天 |
| **phase1-1-drag-panels-code.md** | 拖拽面板代码 | 2 天 |
| **phase1-3-claudemd.md** | CLAUDE.md 注入 | 1 天 |
| **phase1-4-param-permissions.md** | 参数级权限 | 1 天 |
| **phase1-2-routines-backend.md** | Routines 后端 | 1 周 |
| **phase1-2-routines-frontend.md** | Routines 前端 | 1 周 |

---

## 🚀 推荐实施顺序

### 第一步：CLAUDE.md（30分钟 - 立即见效）✅
```bash
cd C:\Desktop\MiniCode
code .implementation-guide/phase1-3-claudemd.md
```
- 单文件修改：`backend/agent/context.py`
- 测试：创建 `CLAUDE.md`，发送消息验证

### 第二步：拖拽面板（1-2天 - 用户体验提升）✅
```bash
code .implementation-guide/phase1-1-drag-panels.md
code .implementation-guide/phase1-1-drag-panels-code.md
```
- 修改 3 个文件：`stores/types.ts`, `stores/index.ts`, `shell/MainSlots.tsx`
- 测试：`npm run dev` 拖拽面板

### 第三步：参数级权限（1天 - 安全基础）✅
```bash
code .implementation-guide/phase1-4-param-permissions.md
```
- 修改 1 个文件：`backend/permissions/checker.py`
- 测试：配置规则，验证匹配

### 第四步：Routines 系统（2周 - 核心功能）✅
```bash
code .implementation-guide/phase1-2-routines-backend.md
code .implementation-guide/phase1-2-routines-frontend.md
```
- 新建 `backend/routines/` 目录
- 创建 5 个后端文件 + 前端组件
- 安装依赖：`pip install apscheduler`

---

## 💡 关键调研点（在修改前必读）

### 拖拽面板
- [ ] 阅读 `frontend/src.v2/shell/MainSlots.tsx`
- [ ] 理解 `panelSlots` 数据结构
- [ ] 查看 `ResizeHandle` 实现

### CLAUDE.md
- [ ] 阅读 `backend/agent/context.py`
- [ ] 找到 `build_context` 函数
- [ ] 理解 system prompt 构建位置

### 参数级权限
- [ ] 阅读 `backend/permissions/checker.py`
- [ ] 找到 `_match_rule` 函数
- [ ] 理解现有规则格式

### Routines
- [ ] 理解 `backend/agent/loop.py` 的 agent 执行流程
- [ ] 查看 `backend/ws/` 的 WebSocket 事件处理
- [ ] 理解前端 `stores/` 的状态管理

---

## 🧪 测试命令速查

### 启动开发环境
```bash
# 后端
cd C:\Desktop\MiniCode
python -m backend

# 前端
cd frontend
npm run dev
```

### 测试 API
```bash
# 测试 Routines API
curl http://localhost:8000/api/routines
```

### 运行测试
```bash
cd backend
pytest tests/ -v
```

---

## 📊 验证清单

### Phase 1 完成标准
- [ ] ✅ 拖拽面板：鼠标拖动面板，顺序改变，刷新后保持
- [ ] ✅ CLAUDE.md：项目根目录创建 `CLAUDE.md`，agent 遵循指令
- [ ] ✅ 参数级权限：配置 `Bash(git commit:*)` 规则生效
- [ ] ✅ Routines：创建定时任务，手动触发，查看列表

### 质量标准
- [ ] 前端无 TypeScript 编译错误（`npm run build`）
- [ ] 后端无 Python 错误（`python -m backend`）
- [ ] 浏览器 console 无 error
- [ ] 操作流畅，无明显卡顿

---

## 🎓 参考资源

### 项目内文档
- **调研报告:** `PROJECT_REVIEW_2026-06-13.md`
- **优化计划:** `OPTIMIZATION_PLAN_2026-06-13.md`
- **项目 README:** `README.md`

### 外部参考
- **@dnd-kit:** https://docs.dndkit.com/
- **APScheduler:** https://apscheduler.readthedocs.io/
- **Claude Code 参考:** `C:\Users\ago\ClaudeCode-ref\`
- **详细计划:** `C:\Users\ago\.claude\plans\codex-claudecode-claudecode-ref-claudec-delegated-journal.md`

---

## 🐛 常见问题

### 拖拽不工作
- 检查 `panelSlots` 中每个 slot 是否有唯一 `id`
- 验证 `@dnd-kit` 已安装：`cd frontend && npm list @dnd-kit`

### CLAUDE.md 不生效
- 检查文件编码是否为 UTF-8
- 确认 `workspace_root` 路径正确
- 查看后端日志是否有加载成功的信息

### 权限规则不匹配
- 确认参数顺序与工具调用一致
- 检查 glob 模式是否正确（`*` 匹配任意内容）
- 打印日志查看 `tool_args` 的实际结构

### Routines 不触发
- 检查 cron 表达式是否合法
- 验证调度器是否启动（查看后端日志）
- 确认 `enabled: true`

---

## 🎉 下一步行动

### 立即开始（现在就可以做）
1. 打开 VS Code：`code C:\Desktop\MiniCode`
2. 阅读第一个指南：`code .implementation-guide/phase1-3-claudemd.md`
3. 30 分钟实现 CLAUDE.md 支持
4. 立即测试，获得成就感 🎊

### Week 1 目标
- ✅ CLAUDE.md 支持（Day 1）
- ✅ 拖拽面板（Day 2-3）
- ✅ 参数级权限（Day 4）
- 🧪 测试和文档（Day 5）

### Week 2-3 目标
- 🔄 Routines 后端（Day 6-10）
- 🎨 Routines 前端（Day 11-15）

### Week 4 目标
- 🧪 集成测试
- 📝 更新文档
- 🎥 录制 Demo

---

## 📝 进度追踪

复制下面的模板到 `PROGRESS.md`，随时更新：

```markdown
# Phase 1 实施进度

## 本周计划
- [ ] 实施 CLAUDE.md
- [ ] 实施拖拽面板
- [ ] 实施参数级权限

## 完成情况
- [ ] 1.1 拖拽面板 - 0%
- [ ] 1.2 Routines - 0%
- [ ] 1.3 CLAUDE.md - 0%
- [ ] 1.4 参数级权限 - 0%

## 遇到的问题
（记录问题和解决方案）

## 下周计划
（根据本周进度调整）
```

---

**🚀 准备好了吗？从 `.implementation-guide/phase1-3-claudemd.md` 开始！**
