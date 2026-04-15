# 03 Fake LLM And Tools

## 做了什么

- 实现了一个可预测的 `FakeLLM`
- 实现了 `ToolRegistry`
- 添加了两个模拟工具：`echo` 和 `summarize_text`

## 为什么不一开始接真模型

如果第一步就接真实 LLM，很多问题会混在一起：网络、密钥、提示词、随机性。先用假模型，可以把注意力集中在 agent loop 的结构上。

## 涉及文件

- `agent/fake_llm.py`
- `agent/tools.py`
- `tests/agent/test_fake_llm.py`
- `tests/agent/test_tools.py`

## 这一步在 Agent 项目里的作用

它让“模型决策”和“工具执行”第一次变成可单独测试的模块，这是后面拼出完整 loop 的前提。
