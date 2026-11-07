# Google Cloud Vertex AI

MiniCode includes the `gcp` provider for Vertex AI's OpenAI-compatible Responses
endpoint. Configure it in the application's user `settings.json` (under
`MINICODE_STATE_ROOT` when that variable is set). Provider credentials and
endpoints cannot be supplied by a project's `.minicode/config.toml`.

```json
{
  "llm": {
    "provider": "gcp",
    "gcp": {
      "model": "gpt-6.1-sol",
      "reasoning_effort": "medium"
    }
  },
  "model_providers": {
    "gcp": {
      "project_id": "your-google-cloud-project",
      "location": "global"
    }
  }
}
```

The supported model catalog contains `gpt-6.1-sol`, `gpt-6-astra`, and
`gpt-6-luna`. Model availability also depends on your Vertex project.

`GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` supply fields omitted from
the configuration. Location defaults to `global`. An explicit `base_url`
overrides project and location resolution. The generated endpoint is:

```text
https://aiplatform.googleapis.com/v1/projects/PROJECT/locations/LOCATION/publishers/openai/v1
```

The default credential command is `gcloud auth application-default
print-access-token` (`gcloud.cmd` on Windows). Install Google Cloud CLI and
configure Application Default Credentials before sending a request. Reading
the bundled model catalog does not run the credential command.

The command runs in the captured session working directory, with a 30-second
timeout. Tokens are reused for 50 minutes and refreshed after a rejected
HTTP 401. An explicit `model_providers.gcp.auth` object replaces the default:

```json
{
  "command": "C:/tools/vertex-token.exe",
  "args": ["--profile", "development"],
  "timeout_ms": 5000,
  "refresh_interval_ms": 300000
}
```

The command must write the bearer token to standard output and exit
successfully. `cwd` is optional; relative command paths and an explicit
relative `cwd` are resolved against the session directory. A refresh interval
of `0` disables periodic refresh; a rejected HTTP 401 still triggers one
refresh and replay. Cancelling or timing out authentication terminates and
reaps the command process. Application network restrictions also govern
external credential commands.

Vertex uses Responses over HTTP, direct function tools, namespaces, client
tool search, and remote compaction. Responses WebSocket, Responses Lite,
custom tools, reasoning summaries, hosted web search, and image generation
are disabled for this provider. Guardian and memory extraction prefer
`gpt-6-luna`; memory consolidation uses `gpt-6.1-sol`.
