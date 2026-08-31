from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMMessage
from backend.services import plugin_settings_service


def test_plugin_mentions_use_enabled_local_inventory_and_connected_mcp(monkeypatch) -> None:
    monkeypatch.setattr(
        plugin_settings_service,
        "get_plugin_settings",
        lambda: {
            "plugins": [
                {
                    "name": "docs",
                    "displayName": "Official Docs",
                    "description": "Trusted local metadata",
                    "shortDescription": "Trusted local summary",
                    "enabled": True,
                    "skill_count": 2,
                    "mcp_server_names": ["docs-search", "offline-server"],
                },
                {
                    "name": "disabled",
                    "enabled": False,
                    "skill_count": 1,
                    "mcp_server_names": ["disabled-server"],
                },
            ],
        },
    )

    resolved = plugin_settings_service.resolve_enabled_plugin_mentions(
        [
            {
                "config_name": "forged-name",
                "path": "plugin://docs",
                "display_name": "Forged display",
                "mcp_server_names": ["forged-server"],
            },
            {"path": "plugin://disabled"},
            {"path": "plugin://missing"},
        ],
        connected_mcp_servers=["docs-search", "forged-server", "disabled-server"],
    )

    assert resolved == [{
        "config_name": "docs",
        "display_name": "Official Docs",
        "description": "Trusted local summary",
        "has_skills": True,
        "mcp_server_names": ["docs-search"],
        "available_apps": [],
    }]


def test_plugin_capabilities_are_turn_scoped_developer_instructions() -> None:
    state = AgentState(user_message="Use the selected plugin")
    state.prompt_context["plugin_injections"] = [{
        "config_name": "docs",
        "display_name": "Official Docs",
        "has_skills": True,
        "mcp_server_names": ["docs-search"],
        "available_apps": [],
    }]

    content = ContextBuilder._build_plugin_instructions(state)
    assert "Capabilities from the `Official Docs` plugin" in content
    assert "`docs-search`" in content

    system, messages = AnthropicAdapter._convert_messages([
        LLMMessage(role="system", content="base"),
        LLMMessage(role="developer", content=content),
        LLMMessage(role="user", content="Use it"),
    ])
    assert content in system
    assert messages == [{"role": "user", "content": "Use it"}]
