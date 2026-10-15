# 手动压缩：实际短窗口与进行中工具的上下文所有权

本批来自真正的桌面 `production-enter-run03`：同一会话16个用户回合、31条实际history，ledger估计4380 tokens。自定义Chat模型无原生compact能力，MiniCode默认自动保留最近20,000 tokens；显式 `/compact` 也误用该tail阈值，导致 `recent_start=0`，未请求模型便返回 `context.nothing_to_compact/native_completion=refused`。这不是界面未发送，也不是脚本把不同会话累计成同一历史。原失败及只读manifest/context/model请求记录保留。

Source `.tmp/codex-src/codex-rs/core/src/compact.rs:272` 的手动入口克隆实际当前history并请求模型，没有自动tail阈值的早退。MiniCode `ContextBuilder.compact` 新增内部 `manual` 触发语义，由真实手动coordinator和显式扩展compact消费；自动budget保留tail逻辑不改。手动压缩当前可读窗口，保留已装入的不透明原生prefix、真实用户约束与媒体引用，继续沿原事务CAS、fresh模型lease、Pre/PostCompact hooks、原conversation owner和进度事件提交。

扩大真实扩展调用链验证时发现进行中消息所有权问题：全窗口重写会删除 `StreamingToolExecution` 仍持有的assistant工具消息，随后真实结果刷新与settlement抛 `StopIteration`。现在本地和原生两路都保留尚无真实结果的调用及其尾部；原生只压缩它之前的窗口。模型请求完成后再读取真实尾部，保证期间抵达的平行工具结果不丢失。没有对settle添加吞异常或“找不到就跳过”。

## 检查和实际边界

- 首批7文件96节点95通过、1失败。旧lifecycle fixture用 `append_user` 把“newest instruction”写成未准入上下文却要求它获得用户指令保留；fixture改成真实 `start_turn`，保留原断言，manual lifecycle9节点在诊断批完整通过。
- 进一步扩展已有host fixture `ModelCatalog` 缺实际 `is_using_oauth` 接口，补为该明确API-key测试情境的false；没有给生产添加兼容。两轮中断诊断完整保留，不作为成功。
- 新本地/native/inflight/媒体/用户原指令/cold与真实RPC lifecycle当前受影响4文件 **50 passed / 0 failed/error/skipped**，进程exit0，见 `output/manual-compact-short-window-20261015-batch05.xml`。其中实际扩展in-turn→stream settlement→终态已通过。
- 旧扩展 `bypass` nested exec两节点是fixture显式声明static ask却没有approval channel，生产拒绝正确。fixture现在使用实际`build_context`，只有confirm分支声明ask；另补两个显式ask+bypass必须拒绝的否定case。全文件最终13/13、exit0，真实拒绝读取扩展API完整ToolResult的`approval_policy:never`与原前端request_digest，不向UI添加内部事件。前两次新增断言误读投影的失败保留，详见`2026-10-15-extension-exec-child-permissions.md`。
- 最终按已完成全文件范围替换、部分rerun仅替换实际节点合并，相关 **119 passed / 0 failed/error/skipped**。包括自动、hooks、slash、model ownership和扩展消费者；不把批次直接相加。重命名的manual lifetime旧失败由该完整文件最新结果取代，中断的batch02/03不算通过。逐节点与原始JUnit的SHA见`output/manual-compact-short-window-20261015-latest-results.json`，复核脚本为同目录`manual-compact-short-window-20261015-collect.py`。
- 真实桌面`production-enter-compact02`在原profile、原owner `conv_mv8ablhm_3vl1ei` 和原history/artifact上三项全部通过，errors=[]/exit0。三模式均由真实键盘发user_message，HTTP held期间确有新`.context-compaction-cell[data-processing=true]`，释放模型后收到真实`native_completion=completed`，新草稿与原上传artifact保留。没有seed历史、降低自动阈值或重复此前19个键盘场景。
- compact02原六图中的running文件仍停留旧完成行的viewport，只证明DOM判据，不作为处理中可视证据。仅追加`production-enter-compact-photo01`一个真实新compact处理→完成照片companion，1/1、errors=[]/exit0；两图均实际查看，Root另查看，清楚显示“正在优化对话”→“上下文已压缩”。实际起止构建均为`index-B5o7lS_j.js`、index SHA256 `0df90ae743a199b216dda27fc72dfa2c2f3a9c0dd464bd32e437799dd30db02e`，不借后续pin构建冒称验收。

当前能力仍受真实所选模型的原生支持和权限环境约束。用户已明确官方账号不做，官方原生压缩解密/账号线上接入排除出当前范围；历史not_run保留，不借受控模型或本地媒体fixture冒称完成。
