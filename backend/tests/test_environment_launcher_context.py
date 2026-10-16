from __future__ import annotations

import json
import sys

import pytest

from backend.environments import EnvironmentSpec
from backend.environments.transport import open_transport


PRIVATE_NAMES = (
    "CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN", "NODE_REPL_AUTH_TOKEN", "CODEX_GUARDIAN_DECISIONS_API_KEY",
    "OPENAI_FEDERATION_RULE_ID", "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
    "MINICODE_RUNTIME_TOKEN", "MINICODE_EMBEDDED_BROWSER_TOKEN", "MINICODE_EVAL_API_KEY", "MINICODE_AUDIT_API_KEY",
)
ORDINARY_NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_API_KEY", "MY_CUSTOM_ACCESS_TOKEN")


def mixed_case(name):
    return "".join(letter.lower() if index % 2 else letter.upper() for index, letter in enumerate(name))


async def inspect_owned_child(tmp_path, extra):
    selected = (*PRIVATE_NAMES, *ORDINARY_NAMES)
    script = "import json,os,sys; print(json.dumps({k.upper():v for k,v in os.environ.items() if k.upper() in " + repr(selected) + "}),flush=True); sys.stdin.buffer.read()"
    transport = await open_transport(EnvironmentSpec("owned-child", program=sys.executable, args=("-u", "-c", script),
                                                      env=extra, cwd=tmp_path))
    try:
        result = json.loads(await transport.recv())
    finally:
        receipt = await transport.close()
    assert receipt.exit_code == 0 and not receipt.forced
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["inherited", "explicit", "both"])
async def test_real_launcher_child_drops_exact_private_context_and_preserves_ordinary_api_variables(tmp_path, monkeypatch, origin):
    monkeypatch.delenv("MINICODE_SUBPROCESS_ENV_SCRUB", raising=False)
    for name in PRIVATE_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name in ORDINARY_NAMES:
        monkeypatch.setenv(name, "owned-ambient-" + name)
    extra = {}
    if origin in {"inherited", "both"}:
        for name in PRIVATE_NAMES:
            monkeypatch.setenv(mixed_case(name), "owned-private-inherited")
    if origin in {"explicit", "both"}:
        extra.update({mixed_case(name): "owned-private-explicit" for name in PRIVATE_NAMES})
    extra["ANTHROPIC_API_KEY"] = "owned-explicit-ordinary-api"
    extra["MY_CUSTOM_ACCESS_TOKEN"] = "owned-explicit-user-token"
    result = await inspect_owned_child(tmp_path, extra)
    assert not set(PRIVATE_NAMES).intersection(result)
    assert result == {"OPENAI_API_KEY": "owned-ambient-OPENAI_API_KEY", "CODEX_API_KEY": "owned-ambient-CODEX_API_KEY",
                      "ANTHROPIC_API_KEY": "owned-explicit-ordinary-api", "MY_CUSTOM_ACCESS_TOKEN": "owned-explicit-user-token"}


@pytest.mark.asyncio
async def test_launcher_preserves_existing_explicit_actions_scrub_semantics(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_SUBPROCESS_ENV_SCRUB", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "owned-ambient-api")
    result = await inspect_owned_child(tmp_path, {"ANTHROPIC_API_KEY": "owned-explicit-actions-blocked",
                                                 "MY_CUSTOM_ACCESS_TOKEN": "owned-ordinary-not-in-actions-set"})
    assert "ANTHROPIC_API_KEY" not in result
    assert result["OPENAI_API_KEY"] == "owned-ambient-api"
    assert result["MY_CUSTOM_ACCESS_TOKEN"] == "owned-ordinary-not-in-actions-set"
