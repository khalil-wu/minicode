# Source 媒体计量与 Changes 执行器归属

本批从 `8a600b86` 继续，固定参考 `.tmp/codex-src` 的 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。没有修改前端字体或增加尚未接通的菜单控件。最终提交、完整 CI 和新安装候选仍需在本批验证结束后记录，不能沿用上一批结果。

## 移除 PyAV 消费链

r17 的真实失败发生在正常 Python 退出期间，PyAV 携带的 C++ 析构经 TLS / pthread_once 进入空地址写入。后续只读调查核对原 dump、CRT、线程状态、内存计数和四个有明确假设的独立进程，仍未找到可靠触发；没有依据认定全局 OOM、已清空 CRT heap 或已修复上游。原失败继续保留，详见 `output/pyav-native-exit-20261017/REPORT.md`。

MiniCode 原来只用 PyAV 计算音频时长和 original 图片尺寸。该实现与 Source 有实际差异：Source 只读默认音轨或容器的元数据时长，无时长时按整个 URL 的 UTF-8 字节估算；旧代码会解码所有音频帧。原图的 MIME 接受、完整像素解码和接受的格式也不同。因此本批移除这一依赖，直接消费固定 Source 算法，不对旧 DLL 加预热或退出补丁。

- 复用现有原生组件的同步 JSON 传输。`media/audioTokens` 直接调用 `codex_utils_audio::estimate_audio_token_count`；`media/originalImageBytes` 使用从 Source history 窄提取、逐字节核对的原常量、parser 与 estimator。两个方法不创建网络 execution，也不改变 policy、routing 或 lease。
- Python 的 MCP、Code Mode、历史及上下文计量继续共享现有消费者。各媒体类型使用 Source 的 32 项 SHA1 缓存语义，缓存不保存原 URL；仅同 key 协调初始化，原生 RPC 不占全局 LRU 锁，不同 key 可并发。原生进程/RPC 错误继续向上传递，不缓存假估值。原图无法解码时的 `None → 7373 bytes` 是原 Source 行为。
- `pyproject.toml` 和 sidecar lock 均移除 av；生产 Python 运行时未做卸载或 pip 操作。新路径是否实际不加载 av 及其 DLL，由本批真实调用和新安装另验。

新 Windows 原生组件为 23,962,624 bytes，SHA256 `f544fccea426d4c5f3462a6f3236dca0146272477a93bd67c69a947d3fe06143`；比旧组件增加 1,783,296 bytes。构建器验证固定 Source 文件、原图窄提取字节和所有 bridge 输入，并分发原许可及依赖 notices。新增 notices 保留上游 MPL 文本的行末空格，Git 属性仅对该生成的许可文本关闭行末空格检查，未改许可字节。

Source 实际计量的六个本地静音样本如下；FFmpeg 只用于产生测试文件，FFprobe 不是 Source oracle，产品和测试运行都不依赖 FFmpeg。

| 容器 | Source tokens | 上下文 bytes |
| --- | ---: | ---: |
| WAV | 20 | 80 |
| MP3 | 20 | 80 |
| M4A，Source 选定的 track timing 含 encoder padding | 22 | 88 |
| Ogg | 20 | 80 |
| WebM | 21 | 84 |
| 无 duration 元数据的 live WebM，按完整 URL 计量 | 977 | 3908 |

## 实际 Changes 链的归属

审计发现右栏 `requestGitChanges` 实际使用 WS `diff.git_working_tree/staged`，经 `backend/ws/handlers/diff.py` 直接调用本机 Git；此前 HTTP Editor Git 的执行器验证不能代替这条链。本批将其读取和同栏暂存、取消暂存及撤销，统一绑定原会话 attachment 和执行器。`file://` 工作区保持执行器 URI 身份，捕获失败不能转回本机，结果继续携带原 owner/request。

重新核对实际桌面 worker 与主进程后，发现此前把模型命令的沙箱权限套给 UI Git 是错误入口。冻结 worker 的 review apply/stage/revert 经 `lOe → l3 → s2 → $ → H1 → host.spawn`，主进程经 `getHost → sessions.spawn → startProcessSession` 发出 `process/spawn`。固定 Source 的 `app-server-protocol/src/protocol/v2/process.rs` 明确规定该入口为宿主上的 standalone process，**without a Codex sandbox**；`process_exec_processor.rs` 直接调用 PTY/pipe spawn，不读取 thread 或 agent permission profile。

因此 UI Git 从请求开始就绑定捕获的目标 host/cwd，以 app-owned 进程执行；保留原会话、请求、attachment、工作区信任及撤销 confirmed 门。删除围绕错误入口增加的 Git metadata/worktree 复合权限、额外 cwd/SID 组合及临时位置分支。该选择发生在执行前，没有“沙箱失败后换权限重试”。模型 Git 工具与它们的既有 sandbox/permission profile 不变，并单独验证。

读取保留 Source 已证的 Git 环境与配置边界。真实 app-server 有 `process/writeStdin(closeStdin:true)`，目前采用的 exec-server `process/write` 没有 EOF；补丁使用一条明确的目标执行器传输路径，写入和清理均保留实际错误，不重试、不换临时位置或执行器。这是传输适配，不能冒称桌面 worker 也写临时文件。真实子目录 patch 暴露的 `git apply` 静默跳过已按 Git prefix 修正。

独立的模型命令回归发现旧 local restricted-token 启动手拼 Source wrapper 参数，遗漏真实 private desktop capability。现改为通过已有原生组件调用 Source 原函数创建并保活桌面对象，等待 wrapper 实际退出；不使用假桌面名或权限降级。该组件保留既有单独固定版本 `v0.158.0-alpha.2.1`，不与参考 checkout 或实际测试 peer `0.163.0-alpha.10.2` 混称同一构建。新 runtime 已核输入和全部资源字节、确认目标目录无进程占用后激活，identity exit 0；证据为 `output/source-changes-menu-20261017/sandbox-activation.json`。

## 分范围验证

| 范围 | 当前实际结果 |
| --- | --- |
| Windows 原生构建 | 冷构建 21m37s；新组件身份、Source/bridge 哈希和 6 个资源已核对。Root 确认无目标进程占用后激活，保留旧字节，身份进程 exit 0。 |
| 原生媒体 | release profile 的独立 `media_meter` 测试 3/3；真实 JSON 19/19。首次 `--lib` 测试编译遇原 Source 窄引模块的 cfg(test) 假设，以及最初 pipe-WebM 的错误 duration 假设，均保留为原诊断，没有改 Source 或转成通过。 |
| 初轮 Python 媒体 / MCP / Code Mode / App / 评论 | 5 文件 68/68，0 failed/error/skipped，进程 exit 0、无 av 模块。该轮发生在缓存并发修正和 helper 归属移动之前，不能代替下列最终清理回归或新安装。 |
| 最终清理的统一受影响回归 | **164/164**，0 failed/error/skipped，120.597s；嵌入 Python 3.11.9 进程 exit 0，`av` / `av.*` 模块列表为空。覆盖本地/真实 Source 的 Changes WS 18 项、Git diff/作用域/确认门、独立模型 Git 权限、Windows native、媒体 26 项、Code Mode 媒体及四项 Git 基础回归。 |
| 模型 restricted-token 实际执行 | 上述统一批中，真实 Runner→原生 Source launcher→OS 边界验证工作区内写成功、外部写拒绝和 exit 7 原样返回；仅提供无需账号 provisioning 的独立 runtime 查找，不替代 runner、helper 或 OS。 |
| 完整 CI / 独立新安装 | 待本批最终冻结后运行，旧 `bf14f6f0` 的 CI 和 r17 结果不证明本批。 |

媒体集中证据在 `output/source-media-meter-20261017/`，实际激活为 `activation.json`，初轮 Python 为 `root-backend.xml` / `root-backend-receipt.json`；最终清理为 `output/source-changes-menu-20261017/final-batch.{log,xml}`、`final-batch-receipt.json`，同目录保存 worker/主进程源码链及全部旧失败。内核边界、协议同步、重复工具检查通过；文件尺寸扫描仍有既有 79 项软警告，未借本批扩大重构范围。

Changes 四项菜单尚未借本批宣称完成。完整文件加载需要 objectId、rename、host 和 snapshot 链；隐藏空白需要 Git diff/统计的 `--ignore-all-space`；富预览只影响 Source 选择的文本格式；imports 的实际过滤 leaf 仍未留存，不能用正则自行补造。官方账号相关服务继续排除，Windows readiness、系统 Computer Use、私有 MCP relay、外机/WSL/WFP、长任务及全 UI 状态的既有边界仍保留。
