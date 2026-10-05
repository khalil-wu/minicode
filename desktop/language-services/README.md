# Editor language services

Run npm run language:prepare from desktop after cloning. Desktop packaging
does this automatically and copies the pinned dependency tree to
resources/language-services.

The editor launches the bundled Pyright and YAML servers through Electron's Node
runtime. A source backend launched without Electron uses Node from PATH. Open
editor buffers are synchronized with the servers without writing them to disk.
Python formatting uses python -m ruff, included in the Python runtime dependencies.

These are user editor operations in a trusted workspace. Agent LSP tools continue
to use the existing sandbox-bound LSP manager.
