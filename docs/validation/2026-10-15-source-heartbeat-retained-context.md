# Heartbeat H1–H3 实现与验证

基线为 `18e8bd8dc097aa892c76ede818beb15d9b8659f1`，Rust 依据为 `.tmp/codex-src` 的 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。H4 调度和 Memory WS 分别由其他负责人交付，不纳入本报告的实现声明。旧 mount-target、input_origin 176 项及 Node/CUA 349 项没有重做。

本报告是版本化的实现与验证记录。Source 的可审阅入口为固定 pin 下的 [heartbeat parser](https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/history/src/heartbeat.rs)、[retained reconciliation](https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/history/src/reconciled_retained_context.rs)、[transcript collection](https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/guardian-context/src/transcript.rs) 和 [client request gate](https://github.com/openai/codex/blob/822e58cc3d666166c7446c5b1ea2e52f5d09594c/codex-rs/core/src/client.rs#L1016)。本地原始失败日志、逐节点映射及字节 hash 仍保留在 `output/source-heartbeat-h1-h3-20261015/` 与 `output/ci-repair-20261015/`，不替代这里的稳定说明。

## 实现

- H1：按 Source 的精确 LF envelope、UTF8/行预算和 host origin/单文本形状识别 heartbeat；仅保存同 automation、同 inherited/local 范围的最近指令版本。invocation 时间不参与正文比较。未知 heartbeat 阻断版本桥接。合并不会删除真实 invocation、公开消息或 user revision。
- H2：保存实际 admission 的 public message ID、turn、acceptance order 和当前正文位置；只读补齐 surviving live inputs，保留 unknown order/missing warning，排序后再合并版本。最新输入 turn 独立于被保留的首次指令版本，用它选择实际当前 turn 的 Skill 信任。
- H3：在完整、已按配置 source 过滤的 transcript 上收集版本和 1-based 引用，再裁取 delta。重复项保留真实位置，改用 Source 原文引用，并清除完整原文交付证明。每个新物理窗口的第一个可见版本保持全文。同步 Guardian 和两种异步请求路径都接同一个 collection。
- 真实 native 往返：host 在 admission 生成独立 `msg_UUIDv7`，与 public client message ID 分离。Responses 主请求和 compact 共用的输入构建器发送该 ID、真实 turn 和有 gate 的 content kinds；order、公开 ID 和正文位置保持私有。runtime/task 前缀保留原文本与 role，实际单文本 heartbeat 单独为一个 InputText。compact fixture 只回传实际收到的消息，不能替生产请求手工补身份。
- native 冷恢复只有在真实 native window 的 ID/turn 与 host transcript 映射匹配时才恢复原 public ID、order、origin 及完整交付证明。普通 provider message、旧 user 文本以及碰巧匹配 public ID 的模型字段不会因此获得 host 信任。

## ContentItemKinds 的 Source gate

`features/src/lib.rs:1105` 为 Stable/default true；`core/src/session/session.rs:1778` 从实际 `config.features.enabled` 捕获到 `ModelClient`，不按 provider/hostname 推测。`core/src/client.rs:1016` 在请求准备中清除关闭的 kinds；流式 HTTP/WS 的入口在同文件 1753、2019、2028。

Source compact v2 的真实调用链为 `core/src/compact_remote_v2_attempt.rs:121` → `core/src/compact_remote_v2.rs:392` 的 `client_session.stream`，因此使用同一 gate。MiniCode 现有显式 native `/responses/compact` 和正常 Responses 请求共用 `_build_responses_input`，在该边界应用相同 kinds gate。这里没有改动 Root 的 WS/side-pool 生命周期实现，也没有新增 provider 兼容或异常吞噬逻辑。

MiniCode 的对应接线是 `feature_flags.py` 的 Source 默认值 → `llm_adapter_factory._bind_config_model_request_policy` 捕获实际配置 → adapter 的布尔设置 → `responses_input.prepare_content_item_kinds`。关闭时只改变 wire copy，保留原始 native checkpoint 和 turn；不会改写持久 history。

## 修正旧审计中的 Skill 结论

Source 的 trusted Skill evidence 是运行时、turn-matched、AsyncOnly，不能从 checkpoint 重建为信任。

依据为 `core/src/context/guardian_review_evidence.rs:81`、`ext/guardian-v2/src/async_scorer/trusted_skills.rs`、`extension.rs:157` 和 `guardian-context/src/trusted_skills.rs`。MiniCode 只消费实际 `SkillManager.load_skill_payload` 成功后产生的 host 队列，canonical existing file 必须处于实际 MiniCode home/skills 或 user home/.agents/skills，受 512-byte path、16 paths、2048-byte collection 约束。

异步 developer evidence 使用 Source 原文 `Codex-verified invoked user-owned skill paths:\n` 加 compact JSON 数组，受 768-token 原文预算约束，位于 retained/reviews/trusted-tool 之后、planned request 之前。同步 Guardian 不新增该 section。冷恢复、旧全局 invoked payload、workspace/plugin 路径或单纯 `$skill` 文本都不恢复 trust。

## 测试与失败证据

测试使用既有 bundled Python 3.11 和独立 `.tmp/ci-python311-test-deps`，没有改应用 runtime 安装。每批在完整修改后统一运行；没有循环运行全量 6536 项后端套件。

- `heartbeat-h1-h3-batch01.xml`：301 节点，299 passed、2 failed、0 skipped，135.442 秒。两失败为 native sync fixture 的测试读取器未识别 type 可省略的 EasyInput；随后继续审计发现真实上游身份缺口，接通生产身份后才重验，未把旧 fixture 的手工 ID 计为 roundtrip 证明。
- `heartbeat-h1-h3-batch02.xml`：230 节点，225 passed、5 failed、0 skipped，121.271 秒。包括实际 compact × cold restore × reuse 开关 × kinds 开关的四个真实 HTTP 场景、身份稳定/反伪造/多模态、新配置接线以及受影响的既有 native/factory/checkpoint/retained/authority/public-input 回归。五失败同为测试读取器将带 role 的非 Message 配置项误读为 content；对应产品请求都实际完成。
- `heartbeat-h1-h3-batch03.xml`：只重跑上述五节点，5 passed、0 failed、0 skipped；保留原请求正文与引用数量断言。读取器同时支持正式 Message 与 type 省略的 EasyInput，排除其他 typed item。
- `heartbeat-h1-h3-batch04.xml`：82 节点，82 passed、0 failed、0 skipped，95.277 秒。最后发现普通 provider message 碰巧匹配 public ID 时可能取得 retained 原文交付证明，已修正真实来源边界；扩大 ID 反伪造场景，并统一补验新链和直接受影响的 Guardian host 消费者。

按每个完整测试函数及其当前参数集的最新执行去重后，共 391 个相关节点，391 passed、0 failed、0 skipped；其中本批新增 40 节点全部通过。它们是多批最新执行的去重结果，不是一次全量运行或原始执行数相加。完整节点映射、各轮原始结果与 XML hash 在 `verification.json`；17 个生产/测试文件及 16 个 Source 文件的精确 SHA256 在 `manifest.json`。

本批新增的持久化测试文件为 `backend/tests/test_source_heartbeat_retained_context.py`（21 节点）、`backend/tests/test_source_heartbeat_guardian_chain.py`（11 节点）和 `backend/tests/test_source_accepted_responses_input.py`（8 节点）。其中真实 QueryEngine 接受了 35 次输入，再实际运行第 36 次无 Skill 的 turn；异步 snapshot/conversation × independent/current 四组合各发出真实 HTTP 请求；同步 native compact 四组合都由收到的真实 ID/turn/marker 构造返回，再持久化冷恢复并实际请求 Guardian。传输为明确的本地协议 fixture，不是官方线上服务兼容证明。

最后一轮 host 消费者补验的完整命令为：

```powershell
.\desktop\python-runtime\python.exe output/ci-repair-20261015/pytest-python311.py backend/tests/test_source_accepted_responses_input.py backend/tests/test_source_heartbeat_guardian_chain.py backend/tests/test_codex_guardian_checkpoint_chain.py backend/tests/test_codex_guardian_retained_request.py -q --tb=short --junitxml=output/ci-repair-20261015/heartbeat-h1-h3-batch04.xml
```

公开 transcript 原文、用户草稿、附件、公有 API 的 input origin 和调度行为均不由这批改写。H4 的 host admission 接线调用本批稳定的 `record_admitted_user_input`。
