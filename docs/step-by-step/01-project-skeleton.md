# 01 Project Skeleton

## 做了什么

- 初始化了 Python 后端项目的最小目录结构
- 创建了 `FastAPI` 应用入口 `app.py`
- 添加了项目依赖定义和基础测试配置

## 为什么先做这一步

Agent 项目一开始最容易乱的地方，是还没确定结构就急着写逻辑。先把应用入口、依赖和测试跑通，相当于先把施工现场搭好。

## 涉及文件

- `pyproject.toml`
- `.gitignore`
- `app.py`
- `agent/__init__.py`
- `tests/test_app_bootstrap.py`

## 这一步在 Agent 项目里的作用

它还没有真正实现 agent，但是它建立了所有后续能力的落点：Web 入口、Python 包结构、测试运行方式。
