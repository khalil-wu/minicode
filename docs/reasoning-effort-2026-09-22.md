# 思考强度切换与 Luna 请求失败

## 日志结论

本机 MiniCode 经 `http://127.0.0.1:8317/v1` 调用 Responses。
09-22 16:17:43 Luna 有成功请求；16:17:52 的 Luna/high 请求随后返回 400：
`Missing required parameter: 'input[9].summary'.`
16:20:13 的 Luna/low、16:21:10 的 Sol/medium 同样因为 `input[3].summary` 缺失失败。
这些请求中的 `reasoning.effort` 都存在，不能把错误归因于档位没有发送。

推理输出只有 encrypted_content、没有公开摘要时，适配器把空数组 summary 丢掉；
后续轮次原样重放这种记录，因此违反 Responses 输入契约。Astra 的成功记录是不同
请求，不构成 Luna 不支持推理强度的证据。

## 修复

- 保存 provider item 时保留 `summary: []`；构造输入时也为旧记录补上缺失字段。
  不丢弃加密状态、不清空历史、不修改原始持久记录。
- 思考强度选择即时预览，后端拒绝时恢复已确认值；早先的回包不会覆盖较新的选择。
- 选择面板保持打开，只有模型/会话或档位集合变化时才重新创建控件。
  滑块拖动与已提交值分开，释放时提交一次。
- 纯档位编辑复用已选模型的能力信息，不重复刷新 OAuth/供应商认证；真正切换模型
  仍刷新认证，模型请求本身仍执行既有的认证更新。

## 验证

- Python 相关回归 115 项通过；前端相关回归 105 项通过；桌面前端构建通过。
- 本地真实网关：Luna 连续 high、low、medium 三轮成功，答案依次为 1057、1060、1063。
  第一轮收到真实加密推理项和空摘要；测试刻意删除历史中的 summary 模拟旧存档，
  后两轮由修复后的适配器补齐并成功重放。
- 脱敏证据：`.tmp/reasoning-live-result.json`。没有在报告中保存密钥或加密内容。

官方档位说明：[GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
支持 none、low、medium、high、xhigh、max；
[Reasoning 指南](https://developers.openai.com/api/docs/guides/reasoning)说明推理用量也会随任务难度调整。

## Astra 入口缺失补修

随后在用户实际窗口确认：内置精确模型表遗漏 `gpt-6-astra`，网关仅给出模型名，
未声明推理档位，导致前端按“不支持”隐藏控件。已补充 Astra 的已知能力，供应商
显式声明仍优先。相关后端回归 32 项通过；重新启动实际桌面端后，通过 DOM 和
截图确认模型名右侧出现档位入口，并展开了低、中、高、极高选项。

依据：[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)。
