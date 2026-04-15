# 02 Schemas

## 做了什么

- 定义了聊天请求模型 `ChatRequest`
- 定义了聊天响应模型 `ChatResponse`
- 定义了工具调用记录模型 `ToolCallRecord`

## 为什么这一步很重要

在 agent 项目里，很多混乱来自“数据到底长什么样”没有先说清楚。先把 schema 定好，可以把后续 loop、API、测试都绑定到同一份契约上。

## 涉及文件

- `schemas.py`
- `tests/test_schemas.py`

## 这一步在 Agent 项目里的作用

它给后端规定了统一的数据语言。后面不管是 loop 还是 API，都围绕这一套输入输出结构展开。
