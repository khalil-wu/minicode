# 实施指南总览 - MiniCode 全面升级

## 📚 文档结构

已创建的详细指南（位于 `.implementation-guide/`）：

1. ✅ **phase1-1-drag-panels.md** - 拖拽面板布局（调研步骤）
2. ✅ **phase1-1-drag-panels-code.md** - 拖拽面板代码实现
3. ✅ **phase1-3-claudemd.md** - CLAUDE.md 项目指令注入
4. ✅ **phase1-4-param-permissions.md** - 参数级权限 Glob 匹配
5. ✅ **phase1-2-routines-backend.md** - Routines 后端实现
6. ✅ **phase1-2-routines-frontend.md** - Routines 前端实现

## 🎯 Phase 1 实施顺序（推荐）

### Week 1: 基础功能（快速见效）
```
Day 1-2: Phase 1.1 拖拽面板 ⭐⭐⭐⭐⭐
  → 最直观的用户体验改进
  → 前端已有 @dnd-kit，实施成本低
  
Day 3: Phase 1.3 CLAUDE.md ⭐⭐⭐⭐⭐
  → 后端单文件修改
  → 立即提升项目可配置性
  
Day 4-5: Phase 1.4 参数级权限 ⭐⭐⭐⭐
  → 安全性增强
  → 为后续自动化任务打基础
```

### Week 2-3: Routines 系统（核心差异化）
```
Day 6-10: Phase 1.2 Routines 后端 ⭐⭐⭐⭐⭐
  → 创建新目录结构
  → 实现调度器和任务管理
  → 集成 APScheduler
  
Day 11-15: Phase 1.2 Routines 前端 ⭐⭐⭐⭐⭐
  → 创建 UI 组件
  → WebSocket 事件处理
  → 完整的 CRUD 功能
```

### Week 4: 测试与优化
```
Day 16-18: 集成测试
  → 端到端测试所有新功能
  → 修复 bug
  
Day 19-20: 文档和打磨
  → 更新 README.md
  → 录制 Demo 视频
```

## 🚀 快速开始

### 1. 从最简单的开始
```bash
cd C:\Desktop\MiniCode
code .implementation-guide/phase1-3-claudemd.md
```
**原因：** 单文件修改，30 分钟完成，立即见效

### 2. 然后是视觉改进
```bash
code .implementation-guide/phase1-1-drag-panels.md
```
**原因：** 前端已有依赖，1-2 天完成，用户体验显著提升

### 3. 安全基础
```bash
code .implementation-guide/phase1-4-param-permissions.md
```
**原因：** 为自动化任务打基础，1 天完成

### 4. 最后是大功能
```bash
code .implementation-guide/phase1-2-routines-backend.md
code .implementation-guide/phase1-2-routines-frontend.md
```
**原因：** 核心差异化功能，1-2 周完成

## 💡 实施技巧

### 边做边测试
每完成一个小功能立即测试：
```bash
# 后端修改后
python -m backend

# 前端修改后
cd frontend && npm run dev
```

### 使用 Git 分支
```bash
git checkout -b feature/phase1-drag-panels
# 完成后
git add .
git commit -m "feat: implement drag-and-drop panel layout"
git checkout main
git merge feature/phase1-drag-panels
```

### 遇到问题时
1. 检查 console.error（前端）或日志（后端）
2. 参考原有代码的实现模式
3. 查看 `.implementation-guide/` 中的调试提示

## 📊 进度追踪模板

```markdown
## Phase 1 进度

### 1.1 拖拽面板 [    ] 0%
- [ ] 调研现有代码
- [ ] 修改 stores/types.ts
- [ ] 修改 stores/index.ts
- [ ] 修改 MainSlots.tsx
- [ ] 测试验证

### 1.3 CLAUDE.md [    ] 0%
- [ ] 调研 context.py
- [ ] 实现 _load_project_instructions
- [ ] 注入到上下文
- [ ] 测试验证

### 1.4 参数级权限 [    ] 0%
- [ ] 调研 checker.py
- [ ] 实现参数提取
- [ ] 扩展 _match_rule
- [ ] 测试验证

### 1.2 Routines [    ] 0%
- [ ] 创建后端目录
- [ ] 实现数据模型
- [ ] 实现调度器
- [ ] 实现 API
- [ ] 实现前端 UI
- [ ] 集成测试
```

## 🎓 学习资源

### @dnd-kit 文档
https://docs.dndkit.com/

### APScheduler 文档
https://apscheduler.readthedocs.io/

### Claude Code 参考
`C:\Users\ago\ClaudeCode-ref\README.md`

### MiniCode 现有文档
- `C:\Desktop\MiniCode\PROJECT_REVIEW_2026-06-13.md`
- `C:\Desktop\MiniCode\README.md`

## ✅ 验证清单

### Phase 1 完成标准
- [ ] 拖拽面板：可拖动，顺序持久化
- [ ] CLAUDE.md：项目根目录放置后生效
- [ ] 参数权限：配置 `Bash(git commit:*)` 生效
- [ ] Routines：可创建、列表、手动触发

### 质量标准
- [ ] 无 TypeScript 编译错误
- [ ] 无 console.error
- [ ] 刷新页面状态保持
- [ ] 性能流畅（60fps）

## 📞 需要帮助？

如果实施过程中遇到问题：
1. 查看对应 `.md` 文件的调试提示
2. 检查现有代码的相似实现
3. 运行 `git log` 查看最近的修改模式

---

**祝实施顺利！🚀**
