"""Use the configured real provider in an isolated Electron acceptance task.

Only provider configuration is read from configured-state. Credentials stay in
memory/environment; output retains the task's own files, state and evidence.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def preflight(settings):
    from backend.agent.provider_lifecycle import LIFECYCLE_RUNTIME_METADATA_KEY
    from backend.llm.base import LLMMessage, StreamEventType
    from backend.llm.openai_adapter import OpenAIAdapter
    import psutil

    parsed = urlparse(settings.base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    listeners = [{"pid": item.pid, "process": psutil.Process(item.pid).name()}
        for item in psutil.net_connections(kind="tcp")
        if parsed.hostname in {"127.0.0.1", "localhost"} and item.status == "LISTEN"
        and item.laddr.port == port and item.pid]
    receipt = {"provider": settings.provider, "model": settings.model, "wire_api": settings.wire_api,
        "endpoint_host": parsed.hostname, "existing_listeners": listeners, "test_starts_provider": False,
        "requests": [], "responses": [], "passed": False}
    class Recorder:
        async def emit_before_provider_request(self, payload):
            receipt["requests"].append({"model": payload["model"]})
            return payload
        async def emit_after_provider_response(self, status, headers):
            receipt["responses"].append({"status": status, "request_id": headers.get("x-request-id", headers.get("request-id", ""))})
    nonce = "REAL_PROJECTION_PREFLIGHT_" + uuid4().hex
    adapter = OpenAIAdapter(settings)
    started = time.monotonic()
    try:
        async with asyncio.timeout(90):
            events = [event async for event in adapter.stream_chat(
                [LLMMessage(role="user", content=f"Reply with exactly {nonce}. Do not use tools.")],
                metadata={LIFECYCLE_RUNTIME_METADATA_KEY: Recorder()})]
        errors = [event.content for event in events if event.type == StreamEventType.ERROR]
        text = "".join(event.content or "" for event in events if event.type == StreamEventType.TEXT_CHUNK)
        receipt.update(passed=not errors and nonce in text, reply=text, errors=errors)
    except Exception as error:
        receipt["error"] = f"{type(error).__name__}: {error}".replace(settings.api_key, "[redacted]")
    finally:
        await adapter.aclose()
    receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return receipt


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configured-state", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--preflight-receipt", type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.environ["MINICODE_STATE_ROOT"] = str(args.configured_state.resolve())
    from backend.config import load_config
    config = load_config(cwd=None)
    if not config.llm.api_key:
        parser.error("The configured provider has no credential")
    if args.preflight_receipt is not None:
        receipt = json.loads(args.preflight_receipt.read_text(encoding="utf-8"))
        assert receipt["passed"] is True
        assert (receipt["provider"], receipt["model"], receipt["wire_api"], receipt["endpoint_host"]) == (
            config.llm.provider, config.llm.model, config.llm.wire_api, urlparse(config.llm.base_url).hostname)
        receipt = {**receipt, "reused_from": str(args.preflight_receipt.resolve())}
    else:
        receipt = asyncio.run(preflight(config.llm))
    (out / "provider-preflight.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2).replace(config.llm.api_key, "[redacted]"), encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("provider", "model", "wire_api", "passed", "elapsed_seconds")}), flush=True)
    if not receipt["passed"]:
        return 1
    env = dict(os.environ,
        MINICODE_REAL_E2E_API_KEY=config.llm.api_key, MINICODE_REAL_E2E_BASE_URL=config.llm.base_url,
        MINICODE_REAL_E2E_MODEL=config.llm.model, MINICODE_REAL_E2E_WIRE_API=config.llm.wire_api,
        MINICODE_REAL_E2E_REASONING_EFFORT=config.llm.reasoning_effort or "",
        MINICODE_FULL_TASK_OUT=str(out), MINICODE_FULL_TASK_TIMEOUT_MS=str(args.timeout_seconds * 1000),
        MINICODE_E2E_EXTERNAL_SERVER="1",
        PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    env.pop("MINICODE_STATE_ROOT", None)
    command = ["node", "node_modules/@playwright/test/cli.js", "test", "tests/e2e/electron-full-task-projection.spec.ts",
        "--config=playwright.config.ts", "--workers=1", "--reporter=line", f"--output={out / 'playwright'}"]
    with (out / "runner.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=ROOT / "frontend", env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        for line in process.stdout:
            clean = line.replace(config.llm.api_key, "[redacted]")
            log.write(clean)
            log.flush()
            print(clean, end="", flush=True)
        code = process.wait()
        (out / "runner-exit.json").write_text(json.dumps({"exit_code": code}), encoding="utf-8")
        return code


if __name__ == "__main__":
    raise SystemExit(main())
