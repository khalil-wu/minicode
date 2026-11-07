# Model-requested plugin installation

MiniCode can list installable plugin candidates and ask the desktop client to
install an exact candidate. The request replaces the composer using the
existing prompt queue. It preserves the original draft and attachments.

The model receives `list_available_plugins_to_install` and
`request_plugin_install` when `apps`, `plugins`, and `tool_suggest` are enabled,
the host can receive user input, and at least one candidate is available.
Installation requests are limited to the root thread and execute serially.
The tool instructions require an explicit user request for that plugin and
exhausting available tool search first.

Add candidates from registered marketplaces in user settings:

```json
{
  "tool_suggest": {
    "discoverables": [
      {"type": "plugin", "id": "review@my-marketplace"}
    ]
  }
}
```

The directory also includes eligible bundled/public defaults. Already installed
plugins, disabled suggestions, unavailable packages and entries blocked by
managed policy are excluded. Official-account app connectors and remote
recommendation services are outside this local installation flow.

Choosing **Install plugin** invokes the existing marketplace installer for the
candidate captured by the pending request. The client sends acceptance after
installation finishes. The backend independently checks the installed plugin
identity before returning `completed: true`. A client acceptance without an
installed package returns `user_confirmed: true, completed: false`.

The next model step captures the updated Skill, MCP and Hook catalogs. Existing
tool calls retain their captured hooks and MCP ownership while those calls
finish. The installed package's Python extension lifecycle remains managed by
the normal extension generation owner.

Choosing **Don't suggest this plugin again** persists its exact ID under
`tool_suggest.disabled_tools`. This suppresses later suggestions without
uninstalling a package or changing unrelated settings. Installation failures
remain visible in the prompt so the user can retry or decline. A response for
an expired or different conversation/turn is rejected.
