# Editor language servers on an independent executor

Editor requests use the selected executor's process service and file URIs. The
server runs in the accepted workspace and permission scope; dirty buffers travel
through `didOpen` / `didChange` without being saved. A client retains the original
environment registration until its documents, renderer, or window are released.
Changing the registry entry never moves an existing server to another executor.

The default target PATH commands are `pyright-langserver --stdio`,
`yaml-language-server --stdio`, and `clangd --use-dirty-headers`. MiniCode does not discover a binary
on the controller or install a server automatically. C/C++ defaults include only
the language and standard in `fallbackFlags`; clangd reads the target project's
own `.clangd` / compilation database. Compiler paths and query-driver arguments
must refer to the target machine.

An optional target workspace `.minicode/language-servers.json` supplies explicit
launches. This is a MiniCode editor configuration, not a Source environment-file
extension. MiniCode reads it through the captured executor filesystem and never
creates or rewrites it:

```json
{
  "language_servers": {
    "python": {
      "argv": ["/opt/project-tools/bin/pylsp"],
      "env": {},
      "settings": {"pylsp": {"plugins": {}}}
    },
    "cpp": {
      "argv": ["clangd", "--use-dirty-headers", "--query-driver=/usr/bin/g++"],
      "initialization_options": {"fallbackFlags": ["-std=c++20"]}
    }
  }
}
```

Each language entry accepts `argv`, `env`, `initialization_options`, and
`settings`; omitted languages use the target PATH defaults. Formatting and other
operations use the target server's LSP capabilities. For example, Pyright does
not provide formatting; select a Python server that does if formatting is needed.
The local desktop's existing Python formatter remains local to that desktop.

Renderer cancellation sends `$/cancelRequest` to the same live server. Closing a
document sends `didClose`; closing its last document sends `shutdown` and `exit`,
then confirms the original process handle has closed before releasing its lease.
Protocol or launch errors remain errors; MiniCode does not switch to a controller
server after a target failure.
