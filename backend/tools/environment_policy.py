"""Model tool exposure derived from one real receiving-step snapshot."""
from __future__ import annotations

from dataclasses import replace
import sys


def policy_for_environment_snapshot(policy, snapshot, *, stable_environment_tools: bool):
    if snapshot is None:
        return policy
    ready = snapshot.turn_environments()
    count = len(snapshot.environments) if stable_environment_tools else len(ready)
    platform = ready[0].peer_info.get("platformOs") if len(ready) == 1 else None
    return replace(policy, environment_count=count,
        advertise_environment_tools=stable_environment_tools or bool(ready),
        environment_login_parameter=stable_environment_tools or any(environment.config.allow_login_shell for environment in ready),
        windows_shell_guidance=platform == "windows" if platform is not None else sys.platform == "win32")
