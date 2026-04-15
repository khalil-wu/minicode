# 04 Agent Loop Endpoint

## 做了什么

- 定义了 `AgentState`
- 实现了 `run_agent_loop()`
- 把 loop 接到了 `POST /api/chat`
- 为 loop 和 API 都补上了测试

## 为什么这一步是核心

前面三步都是在准备零件，这一步第一次把零件串起来，让项目具备了真正的 agent 闭环：输入、模型决策、工具执行、结果返回。

## 涉及文件

- `agent/state.py`
- `agent/loop.py`
- `app.py`
- `tests/agent/test_loop.py`
- `tests/test_chat_api.py`

## 这一步在 Agent 项目里的作用

从这里开始，MiniCode 不再只是一些模块，而是一个真正能工作的最小 agent 后端。
