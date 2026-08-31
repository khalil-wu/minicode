import asyncio
from types import SimpleNamespace

from backend.bootstrap import app as bootstrap_app
from backend.bootstrap.app import AppBootstrap
from backend.config import AppConfig, LLMSettings


def test_bootstrap_loads_hooks_without_invented_setup_event(monkeypatch, tmp_path) -> None:
    import backend.config as config_mod
    import backend.hooks.manager as hooks_manager
    import backend.mcp.manager as mcp_manager_mod
    import backend.skills.executor as skill_executor_mod
    import backend.skills.loader as skill_loader_mod
    import backend.skills.manager as skill_manager_mod
    import backend.tasks.scheduler as scheduler_mod

    settings_file = tmp_path / "settings.json"
    # "Setup" is not a cc/codex/pi hook event; the invented surface was removed.
    settings_file.write_text('{"hooks": {}}', encoding="utf-8")

    class _HookManager:
        pre_tool = []
        post_tool = []


    class _SkillManager:
        def __init__(self, loader):
            self.loader = loader

        def discover(self):
            return None

        def list_all(self):
            return []

    class _McpManager:
        def __init__(self, *args, **kwargs):
            pass

        async def start_all(self):
            return []

        async def stop_all(self):
            return None

        def get_all_status(self):
            return []

    class _Scheduler:
        async def start(self):
            return None

    monkeypatch.setattr(config_mod, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(bootstrap_app, "load_config", lambda: AppConfig(llm=LLMSettings(api_key="")))
    monkeypatch.setattr(bootstrap_app, "FileMemory", lambda: object())
    monkeypatch.setattr(skill_loader_mod, "SkillLoader", lambda: object())
    monkeypatch.setattr(skill_manager_mod, "SkillManager", _SkillManager)
    monkeypatch.setattr(skill_executor_mod, "SkillExecutor", lambda manager: object())
    monkeypatch.setattr(mcp_manager_mod, "MCPServerManager", _McpManager)
    monkeypatch.setattr(scheduler_mod, "get_global_scheduler", lambda: _Scheduler())
    monkeypatch.setattr(hooks_manager.HookManager, "from_settings", classmethod(lambda cls, settings, workspace_root=None: _HookManager()))

    bootstrap = AppBootstrap(
        build_tool_registry=lambda *args, **kwargs: SimpleNamespace(),
        create_session_llm=lambda *args, **kwargs: object(),
        ws_manager=SimpleNamespace(),
        on_mcp_status_change=lambda *args, **kwargs: None,
    )

    asyncio.run(bootstrap.startup())

    # No invented Setup event fires at startup; session_start runs per session
    # from loop_preflight with source="startup" (cc alignment).
    asyncio.run(bootstrap.shutdown())
