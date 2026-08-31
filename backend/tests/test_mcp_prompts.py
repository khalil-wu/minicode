import asyncio

from backend.services.tool_registry_factory import build_tool_registry as _build_tool_registry
from backend.artifact.store import ArtifactStore
from backend.mcp.client import MCPClient, MCPServerCapabilities
from backend.tools.mcp_tools import GetMcpPromptTool, ListMcpPromptsTool


class _PromptClient(MCPClient):
    def __init__(self) -> None:
        super().__init__(server_name="docs")
        self._connected = True
        self._server_capabilities = MCPServerCapabilities(prompts=True)
        self.prompt_args = None

    async def _request(self, method, params=None):
        if method == "prompts/list":
            return {
                "prompts": [
                    {
                        "name": "review",
                        "description": "Review a patch",
                        "arguments": [
                            {
                                "name": "path",
                                "description": "File path",
                                "required": True,
                            }
                        ],
                    }
                ]
            }
        if method == "prompts/get":
            self.prompt_args = params
            return {
                "description": "Rendered review",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": f"Review {params['arguments']['path']}"},
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Check behavior and tests."}],
                    },
                ],
            }
        return None


class _PromptManager:
    def __init__(self) -> None:
        self.client = _PromptClient()

    def iter_connected_clients(self):
        return [("docs", self.client)]

    def get_client(self, name):
        return self.client if name == "docs" else None

    def get_all_tools(self):
        return {}


def test_mcp_client_lists_and_renders_prompts() -> None:
    async def run() -> None:
        client = _PromptClient()

        prompts = await client.list_prompts()
        rendered = await client.get_prompt("review", {"path": "backend/app.py"})

        assert prompts[0].name == "review"
        assert prompts[0].arguments[0].name == "path"
        assert prompts[0].arguments[0].required is True
        assert "Rendered review" in rendered
        assert "user: Review backend/app.py" in rendered
        assert "assistant: Check behavior and tests." in rendered

    asyncio.run(run())


def test_mcp_prompt_tools_expose_prompt_catalog_and_rendered_prompt() -> None:
    async def run() -> None:
        manager = _PromptManager()

        listed = await ListMcpPromptsTool(manager).execute({})
        rendered = await GetMcpPromptTool(manager).execute({
            "server": "docs",
            "name": "review",
            "arguments": {"path": "backend/app.py"},
        })

        assert "Server: docs | Prompt: review" in listed.content
        assert "path (required)" in listed.content
        assert "MCP prompt docs/review rendered successfully" in rendered.content
        assert "Review backend/app.py" in rendered.content
        assert manager.client.prompt_args == {
            "name": "review",
            "arguments": {"path": "backend/app.py"},
        }

    asyncio.run(run())


def test_default_registry_registers_mcp_prompt_bridge(tmp_path) -> None:
    registry = _build_tool_registry(
        ArtifactStore(storage_dir=tmp_path),
        mcp_manager=_PromptManager(),
    )
    tool_names = set(registry.list_tools())
    summary = registry.build_capability_summary()

    assert {"list_mcp_prompts", "get_mcp_prompt"} <= tool_names
    assert summary["mcp_prompt_bridge"] is True
