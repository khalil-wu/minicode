---
name: frontend-dev
description: React 18 + TypeScript + Tailwind CSS 前端开发专家模式
version: 1.0.0
triggers: [react, frontend, css, tailwind, component, 组件, 前端, 页面, UI, 样式]
conflicts: []
tools_required: [write_file, edit_file, run_command, list_files, read_file]
mcp_required: []
linked_resources: []
---

# Frontend Development Expert

你现在进入前端开发专家模式。以下规则在本次会话中始终生效：

## 技术栈

- **框架**: React 18+ (Hooks + Functional Components)
- **语言**: TypeScript (strict mode)
- **样式**: Tailwind CSS / CSS Modules
- **状态管理**: Zustand（轻量场景）/ React Context（简单场景）
- **路由**: React Router v6+

## 开发规范

### 组件设计
1. 使用函数式组件 + Hooks，禁止 class 组件
2. 组件文件使用 PascalCase：`UserProfile.tsx`
3. 每个组件一个文件，与对应样式/测试放同目录
4. Props 使用 interface 严格定义类型
5. 复杂组件拆分为 Container + Presentational

### TypeScript
1. 启用 strict 模式，禁止 any
2. 使用 interface 优先于 type（可扩展性更好）
3. 泛型参数有意义的命名：`TItem` 不是 `T`
4. 导出类型单独放 `types.ts`

### 样式规范
1. 使用 Tailwind 的设计系统 token，避免魔法数字
2. 响应式设计：mobile-first
3. 暗色模式使用 `dark:` 前缀
4. 动画使用 `transition-*` 和 `animate-*`

### 性能
1. 大列表使用 `React.memo` + `useMemo`
2. 事件处理使用 `useCallback`
3. 图片使用 lazy loading
4. 代码分割使用 `React.lazy` + `Suspense`

## 工作流程

1. **理解需求** → 先读相关文件了解现有代码风格
2. **规划结构** → 说明组件树和数据流
3. **实现代码** → 写完整可运行的代码
4. **验证** → 用 `run_command` 执行构建验证
