# Editor language services

Run npm run language:prepare from desktop after cloning. Desktop packaging
does this automatically and copies the pinned dependency tree to
resources/language-services.

The editor launches the bundled Pyright and YAML servers through Electron's Node
runtime. A source backend launched without Electron uses Node from PATH. Open
editor buffers are synchronized with the servers without writing them to disk.
Python formatting uses python -m ruff, included in the Python runtime dependencies.

C/C++ uses the official clangd 23.1.0 Windows standalone release from
https://github.com/clangd/clangd/releases/tag/23.1.0, prepared by
`npm run clangd:prepare` under desktop. The complete release, including its Apache
2.0 license with LLVM exceptions and built-in headers, stays under
`language-services/clangd/clangd_23.1.0` and is copied by the existing packaging
resource entry. Nothing is added to the global PATH. On startup the editor reads
the installed GCC/Clang compiler's actual target and include directories; clangd
also honors the workspace's compile_commands.json. Open buffers stay unsaved.

These are user editor operations in a trusted workspace. Agent LSP tools continue
to use the existing sandbox-bound LSP manager.
