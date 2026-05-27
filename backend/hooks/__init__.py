"""Hooks system — run user-defined commands before/after tool calls."""
from backend.hooks.manager import HookManager, HookResult, get_hook_manager

__all__ = ["HookManager", "HookResult", "get_hook_manager"]
