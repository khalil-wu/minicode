import asyncio

import pytest

from backend.mcp.client import MCPClient, MCPServerCapabilities
from backend.security.unicode_sanitizer import (
    UnsafeUnicodeMetadataKey,
    sanitize_untrusted_metadata,
    sanitize_untrusted_unicode,
    unicode_identifier_is_safe,
)


def test_unicode_sanitizer_removes_hidden_controls_but_preserves_normal_text() -> None:
    tag_a = "\U000E0061"
    source = "Ｍｉｎｉ\u200bCode\u202e中文\ue000" + tag_a + " 👩\u200d💻"

    assert sanitize_untrusted_unicode(source) == "MiniCode中文 👩\u200d💻"
    assert unicode_identifier_is_safe("search_docs") is True
    assert unicode_identifier_is_safe("search\u200b_docs") is False


def test_recursive_metadata_never_renames_unsafe_keys() -> None:
    value = {
        "description": "safe\u200b text",
        "read\u200bOnlyHint": True,
    }

    assert sanitize_untrusted_metadata(value) == {"description": "safe text"}
    try:
        sanitize_untrusted_metadata(value, reject_unsafe_keys=True)
    except UnsafeUnicodeMetadataKey:
        pass
    else:  # pragma: no cover - fail closed contract
        raise AssertionError("unsafe metadata key was silently accepted")


class _UnicodeMetadataClient(MCPClient):
    def __init__(self) -> None:
        super().__init__(server_name="unicode-test")
        self._connected = True
        self._server_capabilities = MCPServerCapabilities(resources=True, prompts=True)

    async def _request(self, method, params=None):
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "safe_tool",
                        "description": "Search\u200b docs\u202e",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Find\U000E0061 text"}
                            },
                        },
                        "annotations": {
                            "read\u200bOnlyHint": True,
                            "openWorldHint": False,
                        },
                    },
                ]
            }
        if method == "resources/list":
            return {
                "resources": [
                    {
                        "uri": "docs://guide",
                        "name": "Guide\u200b",
                        "description": "安全\u202e说明",
                    },
                ]
            }
        if method == "resources/templates/list":
            return {
                "resourceTemplates": [
                    {
                        "uriTemplate": "docs://{topic}",
                        "name": "Topic\u200b",
                        "description": "Read\U000E0061 docs",
                    },
                ]
            }
        if method == "prompts/list":
            return {
                "prompts": [
                    {
                        "name": "review",
                        "description": "Review\u200b safely",
                        "arguments": [
                            {"name": "path", "description": "File\u202e path", "required": True}
                        ],
                    },
                ]
            }
        if method == "prompts/get":
            return {
                "description": "Rendered\u200b review",
                "messages": [
                    {
                        "role": "user\u202e",
                        "content": {"type": "text", "text": "Inspect\U000E0061 this"},
                    }
                ],
            }
        raise AssertionError(method)


def test_mcp_metadata_boundaries_are_sanitized_without_changing_wire_identifiers() -> None:
    async def run() -> None:
        client = _UnicodeMetadataClient()
        client._set_server_instructions("Use\u200b tools\u202e safely 👩\u200d💻")

        tools = await client.list_tools()
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        prompts = await client.list_prompts()
        rendered = await client.get_prompt("review")

        assert client.instructions == "Use tools safely 👩\u200d💻"
        assert [tool.name for tool in tools] == ["safe_tool"]
        assert tools[0].description == "Search docs"
        assert tools[0].input_schema["properties"]["query"]["description"] == "Find text"
        assert tools[0].annotations == {"openWorldHint": False}
        assert client._read_only_tools == set()

        assert [(resource.uri, resource.name, resource.description) for resource in resources] == [
            ("docs://guide", "Guide", "安全说明")
        ]
        assert [(template.uri_template, template.name, template.description) for template in templates] == [
            ("docs://{topic}", "Topic", "Read docs")
        ]
        assert [prompt.name for prompt in prompts] == ["review"]
        assert prompts[0].description == "Review safely"
        assert prompts[0].arguments[0].description == "File path"
        assert rendered == "Rendered review\n\nuser: Inspect this"

    asyncio.run(run())


class _UnsafeWireClient(_UnicodeMetadataClient):
    def __init__(self, method: str, response: dict) -> None:
        super().__init__()
        self._unsafe_method = method
        self._unsafe_response = response

    async def _request(self, method, params=None):
        if method == self._unsafe_method:
            return self._unsafe_response
        return await super()._request(method, params)


@pytest.mark.parametrize(
    "method,response,operation,error",
    [
        (
            "tools/list",
            {"tools": [{"name": "hidden\u200b_tool", "inputSchema": {"type": "object"}}]},
            "list_tools",
            "unsafe tool identifier",
        ),
        (
            "tools/list",
            {"tools": [{
                "name": "unsafe_schema",
                "inputSchema": {
                    "type": "object",
                    "properties": {"que\u200bry": {"type": "string"}},
                },
            }]},
            "list_tools",
            "unsafe schema metadata",
        ),
        (
            "resources/list",
            {"resources": [{"uri": "docs://hidden\u200b", "name": "Rejected"}]},
            "list_resources",
            "unsafe resource URI",
        ),
        (
            "resources/templates/list",
            {"resourceTemplates": [{
                "uriTemplate": "docs://{to\u200bpic}",
                "name": "Rejected",
            }]},
            "list_resource_templates",
            "unsafe resource template URI",
        ),
        (
            "prompts/list",
            {"prompts": [{"name": "bad\u200b_prompt"}]},
            "list_prompts",
            "unsafe prompt identifier",
        ),
        (
            "prompts/list",
            {"prompts": [{
                "name": "bad_argument",
                "arguments": [{"name": "pa\u200bth"}],
            }]},
            "list_prompts",
            "unsafe arguments",
        ),
    ],
)
def test_mcp_unsafe_wire_identifiers_fail_the_operation(
    method,
    response,
    operation,
    error,
) -> None:
    client = _UnsafeWireClient(method, response)

    with pytest.raises(ConnectionError, match=error):
        asyncio.run(getattr(client, operation)())
