# Linux 后端与 SSH 宿主安装

MiniCode 的 Linux 后端支持 x86_64、glibc >= 2.31、Python >= 3.11。最低构建和安装验证环境为 Ubuntu 20.04/glibc 2.31。Node 功能使用 Node.js 22；本机命令、搜索和沙箱需要 bash、Git 2.36+、ripgrep、bubblewrap。Git 2.36 引入 Source 工作树协议实际使用的 `git worktree list --porcelain -z`，Ubuntu 20.04 自带的 Git 2.25 不满足这条能力；应安装合适版本并让登录 PATH 选择它，不改用会损失特殊路径表示的旧协议。沙箱还要求系统允许相应的 user/mount/network namespaces；安装成功不代表系统已允许这些内核操作。

Linux backend wheel 包含 MiniCode 自己的 backend/harness，以及固定 Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 的 execpolicy/network 原生组件。它不是 Codex app-server，也不需要官方账号登录。当前没有 Linux 桌面安装包、PyPI 发布或 SSH 自动安装/更新服务。

## 安装经过 CI 冷验的 wheel

从相应提交的成功 MiniCode CI run 下载 `minicode-linux-backend-wheel` artifact 并解压。该 artifact 只在 `Linux wheel cold installation (glibc 2.31)` 通过后上传；对应 `linux-wheel-install-evidence` 包含 wheel SHA-256、实际安装路径、两个 native 的身份/ELF/ldd 和依赖版本。不要把较旧 run 的无 ABI 约束 native artifacts 当作当前 Linux 安装包。

在独立虚拟环境安装解压后的 wheel：

```sh
python3.11 -m venv "$HOME/.local/share/minicode/venv"
"$HOME/.local/share/minicode/venv/bin/python" -m pip install ./minicode-*-py3-none-linux_x86_64.whl
"$HOME/.local/share/minicode/venv/bin/python" -m pip check
"$HOME/.local/share/minicode/venv/bin/minicode-host" --version
```

将该 venv 的 `bin` 目录加入远端登录 shell 的 PATH，使 SSH 能执行 `minicode-host`。桌面连接会调用已安装的这个入口；`--version` 仅确认 CLI 版本，不能替代 CI 中两个原生引擎的实际运行验证。升级时先停止由此安装启动的 host，再在同一安装环境安装相应提交的新 wheel；桌面更新不会自行替换远端 Python 包。

后端状态默认存放于 `~/.minicode`。使用独立验证环境时，通过 CLI 的 `--state-root` 指定新目录，避免与日常状态混用。安装包与状态分别管理，升级不需要删除状态目录。

Python/YAML 语言服务需要对应提交的 `desktop/language-services`：

```sh
npm ci --prefix /path/to/minicode/desktop/language-services --omit=dev --no-audit --no-fund
export MINICODE_EDITOR_LANGUAGE_SERVICES_DIR=/path/to/minicode/desktop/language-services
export MINICODE_EDITOR_NODE=/absolute/path/to/node
```

使这些配置在启动 `minicode-host` 的登录环境中可见。C/C++ 语言服务另外需要 PATH 中的 clangd。Linux wheel 本身不包含 Node 或语言服务 node_modules。

## 从源码准备 Linux native resources

使用该 Linux 主机上的 Rust 1.95.0（execpolicy）与 1.99.1（network）、Python >=3.11，以及 C/C++ compiler、pkg-config、libssl-dev、cmake 等构建依赖；网络构建还在 CI 中配有 clang。两个运行时固定在不同的 Source 提交：execpolicy 为 822e58cc，network 为 970111f6（network 脚本自行从 checkout 取该提交，不依赖工作树所在的提交）。以下命令在 MiniCode checkout 根目录执行：

```sh
git clone https://github.com/openai/codex .tmp/codex-src
rustup toolchain install 1.95.0 1.99.1 --profile minimal
execpolicy_cargo="$(rustup which --toolchain 1.95.0 cargo)"
network_cargo="$(rustup which --toolchain 1.99.1 cargo)"
"$execpolicy_cargo" fetch --locked --manifest-path desktop/native-execpolicy/Cargo.toml
python3.11 desktop/scripts/prepare-execpolicy-runtime.py \
  --cargo "$execpolicy_cargo" --output-directory backend/permissions/resources/execpolicy/x86_64-unknown-linux-gnu
python3.11 desktop/scripts/prepare-network-runtime.py \
  --cargo "$network_cargo" --source-checkout .tmp/codex-src --build-directory .tmp/native-network-build \
  --output-directory backend/network/resources/x86_64-unknown-linux-gnu
python3.11 -m pip install -e .
```

这是当前主机上的源码开发安装，不保证在更新 libc 上构建的二进制可以转移到旧系统。可重复分发的 Linux wheel 应使用下述固定 CI 基线。不会通过更改文件名、替换 manifest target、吞掉 loader 错误或在运行时降级来支持不同平台。

## 构建与分发约束

两个 native producer 与独立 wheel 安装 job 均使用 Docker 官方 `ubuntu:20.04` 的 linux/amd64 manifest `sha256:c664f8f86ed5a386b0a340d981b8f81714e21a8b9c73f658c4bea56aa179d54a`。Python 固定为 python-build-standalone `3.11.17+20261013`，下载 SHA-256 为 `846bf682e2b67393f2affa5e5c0994e446abc3a7cc5bb699890c36346823644c`，不借用新 runner 的 setup-python toolcache。

CI 在构建时实际确认 glibc 2.31；编译缓存 namespace 包含该 ABI 和镜像身份，旧 ubuntu-latest 的 target cache 不会恢复到这个基线。产物上传前核 ELF 格式、GLIBC 符号上限、实际动态依赖和 Source identity。wheel 在独立 staging 中只加入本 run 的 Linux resources，并在最低支持环境的新 venv 中非 editable 安装，从仓库外使用 `python -I` 冷验。安装后的 native 文件不会补 chmod 来掩盖 wheel 权限错误。

原有 ubuntu-latest backend/desktop 测试继续消费这些较旧基线的产物，验证较新系统行为。当前 `linux_x86_64` wheel tag 不编码 glibc 下限；安装者仍须遵守上面的最低要求。以后若公开发布 manylinux wheels，需要根据真实 ELF 审计生成相应标签，不能只重命名现有 wheel。
