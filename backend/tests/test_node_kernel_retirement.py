"""Real MCP regressions for inspector evaluation and original kernel retirement."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil

import pytest

from backend.tests.test_node_repl_companion import body, companion


@pytest.fixture
def kernel_source(monkeypatch):
    import backend.mcp.node_repl as node

    binary = os.environ.get("MINICODE_KERNEL_TEST_NODE") or shutil.which("node")
    assert binary is not None, "These real-process regressions require an installed Node runtime"
    monkeypatch.setenv("MINICODE_EDITOR_NODE", binary)
    source = Path(os.environ["MINICODE_KERNEL_TEST_SOURCE"]).read_text(encoding="utf-8") if os.environ.get("MINICODE_KERNEL_TEST_SOURCE") else node._SERVER
    monkeypatch.setattr(node, "_SERVER", source)
    return node, source


async def wait_file(path, contents=None):
    async with asyncio.timeout(10):
        while not path.exists() or contents is not None and path.read_text() != contents:
            await asyncio.sleep(0.001)


def notifications(caplog):
    return [record.args[2] for record in caplog.records if record.name == "backend.mcp.client"
        and isinstance(record.args, tuple) and len(record.args) == 3 and isinstance(record.args[2], dict)]


@pytest.mark.asyncio
async def test_idle_kernel_keeps_its_vm_context_through_full_gc(kernel_source, monkeypatch, tmp_path):
    node, source = kernel_source
    # Collect from the worker host after its completed call has released every
    # execution reference. Inspector IDs alone do not keep a vm.Context alive.
    source = source.replace("parentPort.postMessage({ kind: 'finished', id: message.id });", """
    setImmediate(() => {
      require('node:v8').setFlagsFromString('--expose_gc');
      vm.runInNewContext('gc')();
      require('node:fs').writeFileSync('idle-gc-finished', 'collected');
      parentPort.postMessage({ kind: 'finished', id: message.id });
    });
    """, 1)
    monkeypatch.setattr(node, "_SERVER", source)
    async with companion(tmp_path) as fixture:
        first = await fixture.client.call_tool("js", {"code": "const retained = 41; nodeRepl.write(retained)"})
        assert not first.is_error and body(first) == "41", body(first)
        await wait_file(tmp_path / "idle-gc-finished", "collected")
        second = await fixture.client.call_tool("js", {"code": "nodeRepl.write(retained + 1)"})
        assert not second.is_error and body(second) == "42", body(second)


@pytest.mark.asyncio
async def test_actual_import_cpu_double_reset_keeps_original_output_and_new_generation(kernel_source, tmp_path):
    async with companion(tmp_path) as fixture:
        for index in range(12):
            active = asyncio.create_task(fixture.client.call_tool("js", {"code":
                f"const fs=await import('node:fs');const lost=12;nodeRepl.write('original');fs.writeFileSync('ready-{index}','1');while(true){{}}"}))
            await wait_file(tmp_path / f"ready-{index}")
            stale = asyncio.create_task(fixture.client.call_tool("js", {"code":
                f"const fs=await import('node:fs');fs.writeFileSync('stale-{index}','ran')"}))
            await asyncio.sleep(0)
            resets = [asyncio.create_task(fixture.client.call_tool("js_reset", {})) for _ in range(2)]
            assert not (await asyncio.wait_for(resets[1], 10)).is_error
            current = asyncio.create_task(fixture.client.call_tool("js", {"code":
                "await new Promise(r=>setImmediate(r));nodeRepl.write(typeof lost)"}))
            assert not (await resets[0]).is_error
            interrupted, queued, fresh = await asyncio.gather(active, stale, current)
            assert interrupted.is_error and "original" in body(interrupted) and "reset by js_reset" in body(interrupted)
            assert queued.is_error and not (tmp_path / f"stale-{index}").exists()
            assert not fresh.is_error and body(fresh) == "undefined", body(fresh)


@pytest.mark.asyncio
async def test_reset_owns_original_call_before_kernel_ready(kernel_source, monkeypatch, tmp_path):
    node, source = kernel_source
    gate = """const startupFS=require('node:fs');
if (!startupFS.existsSync('release-startup')) {
  await new Promise(resolve=>{
    const watcher=startupFS.watch(cwd,(_event,name)=>{
      if (String(name)==='release-startup') {watcher.close();resolve()}
    });
    startupFS.writeFileSync('startup-entered','1');
  });
}
"""
    source = "import {writeFileSync as kernelProbeWrite} from 'node:fs';\n" + source
    source = source.replace("const context = vm.createContext(", gate + "const context = vm.createContext(", 1)
    source = source.replace("generation++;", "generation++;kernelProbeWrite('reset-observed',String(generation));", 1)
    monkeypatch.setattr(node, "_SERVER", source)
    async with companion(tmp_path) as fixture:
        active = asyncio.create_task(fixture.client.call_tool("js", {"code":
            "const fs=await import('node:fs');fs.writeFileSync('old-user-ran','incorrect');nodeRepl.write('old')"}))
        await wait_file(tmp_path / "startup-entered")
        resets = [asyncio.create_task(fixture.client.call_tool("js_reset", {})) for _ in range(2)]
        await wait_file(tmp_path / "reset-observed", "2")
        assert not active.done() and not any(task.done() for task in resets)
        following = asyncio.create_task(fixture.client.call_tool("js", {"code": "nodeRepl.write('new generation')"}))
        (tmp_path / "release-startup").write_text("release actual initialization")
        results = await asyncio.wait_for(asyncio.gather(active, *resets, following), 10)
        assert results[0].is_error and "reset by js_reset" in body(results[0]), body(results[0])
        assert not any(result.is_error for result in results[1:]), [body(result) for result in results]
        assert body(results[-1]) == "new generation" and not (tmp_path / "old-user-ran").exists()


@pytest.mark.asyncio
async def test_timeout_cancel_and_reset_share_original_kernel_retirement(kernel_source, tmp_path):
    async with companion(tmp_path) as fixture:
        for index in range(5):
            cancel = asyncio.Event()
            active = asyncio.create_task(fixture.client.call_tool("js", {"code":
                f"const fs=await import('node:fs');fs.writeFileSync('overlap-{index}','1');while(true){{}}", "timeout_ms": 40},
                request_owner={"cancel_event": cancel}))
            await wait_file(tmp_path / f"overlap-{index}")
            reset = asyncio.create_task(fixture.client.call_tool("js_reset", {}))
            cancel.set()
            outcome = await asyncio.wait_for(asyncio.gather(active, return_exceptions=True), 10)
            assert isinstance(outcome[0], asyncio.CancelledError) or outcome[0].is_error
            assert not (await asyncio.wait_for(reset, 10)).is_error
            current = await asyncio.wait_for(fixture.client.call_tool("js", {"code": "nodeRepl.write('after overlap')"}), 10)
            assert not current.is_error and body(current) == "after overlap", body(current)


@pytest.mark.asyncio
async def test_direct_cpu_timeout_reset_and_cancel_release_inspector_dispatch(kernel_source, tmp_path):
    async with companion(tmp_path) as fixture:
        timed = await asyncio.wait_for(fixture.client.call_tool("js", {"code": "const directLost=1;while(true){}", "timeout_ms": 100}), 10)
        assert timed.is_error and "execution exceeded 100 ms" in body(timed), body(timed)
        for mode in ("reset", "cancel"):
            fresh = await fixture.client.call_tool("js", {"code": "const fs=await import('node:fs');nodeRepl.write(typeof directLost)"})
            assert not fresh.is_error and body(fresh) == "undefined", body(fresh)
            cancel = asyncio.Event()
            active = asyncio.create_task(fixture.client.call_tool("js", {"code":
                f"const directLost=2;fs.writeFileSync('{mode}-direct-entry','1');while(true){{}}"},
                request_owner={"cancel_event": cancel}))
            await wait_file(tmp_path / f"{mode}-direct-entry")
            if mode == "cancel":
                cancel.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(active, 10)
            else:
                assert not (await asyncio.wait_for(fixture.client.call_tool("js_reset", {}), 10)).is_error
                interrupted = await asyncio.wait_for(active, 10)
                assert interrupted.is_error and "reset by js_reset" in body(interrupted), body(interrupted)
        observed = await asyncio.wait_for(fixture.client.call_tool("js", {"code": "nodeRepl.write(typeof directLost)"}), 10)
        assert not observed.is_error and body(observed) == "undefined", body(observed)


@pytest.mark.asyncio
async def test_outer_evaluation_preserves_repl_imports_images_and_late_metadata(kernel_source, tmp_path, caplog):
    caplog.set_level("INFO", logger="backend.mcp.client")
    modules = tmp_path / "extra/node_modules/actual-package"
    modules.mkdir(parents=True)
    (modules / "package.json").write_text('{"type":"module","main":"index.js"}')
    (modules / "index.js").write_text('export const runtime=process.version')
    async with companion(tmp_path) as fixture:
        assert not (await fixture.client.call_tool("js_add_node_module_dir", {"path": str(modules.parent)})).is_error
        first = await fixture.client.call_tool("js", {"code":
            "const saved=41;let persisted=1;var number=7;function callable(){return number};const pkg=await import('actual-package');nodeRepl.write(pkg.runtime);setTimeout(()=>console.log('late:'+nodeRepl.requestMeta.owner),30)"},
            request_meta={"owner": "first"})
        assert not first.is_error and body(first).startswith("v"), body(first)
        second = await fixture.client.call_tool("js", {"code":
            "const saved=42;let persisted=2;var number=8;function callable(){return number*2};await new Promise(r=>setTimeout(r,80));nodeRepl.write(JSON.stringify({saved,persisted,number,called:callable(),process:typeof process,owner:nodeRepl.requestMeta.owner}));await nodeRepl.emitImage({bytes:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jVZkAAAAASUVORK5CYII=','base64'),mimeType:'image/png',detail:'original'})"},
            request_meta={"owner": "second"})
        assert not second.is_error, body(second)
        assert json.loads(second.content[0]["text"]) == {"saved": 42, "persisted": 2, "number": 8, "called": 16, "process": "undefined", "owner": "second"}
        persisted = await fixture.client.call_tool("js", {"code": "nodeRepl.write(saved+persisted)"})
        assert not persisted.is_error and body(persisted) == "44", body(persisted)
        assert second.content[1]["type"] == "image" and second.content[1]["_meta"]["codex/imageDetail"] == "original"
        late = next(item for item in notifications(caplog) if item.get("content", [{}])[0].get("text") == "late:first")
        assert late["requestMeta"] == {"owner": "first"}
        assert "late:first" not in body(second)
        assert not (await fixture.client.call_tool("js_reset", {})).is_error
        restored = await fixture.client.call_tool("js", {"code":
            "const pkg=await import('actual-package');nodeRepl.write(JSON.stringify({saved:typeof saved,runtime:pkg.runtime}))"})
        assert not restored.is_error and json.loads(body(restored))["saved"] == "undefined", body(restored)


@pytest.mark.asyncio
async def test_execution_binding_preserves_strict_directives_and_hashbang(kernel_source, tmp_path):
    async with companion(tmp_path) as fixture:
        strict = await fixture.client.call_tool("js", {"code":
            "'custom';'use strict';'another';const path=await import('node:path');let implicit;try{strictImplicit=1}catch(error){implicit=error.name}nodeRepl.write(JSON.stringify({strictThis:(function(){return this===undefined})(),scriptThis:this===globalThis,implicit,global:typeof strictImplicit}))"})
        assert not strict.is_error and json.loads(body(strict)) == {"strictThis": True, "scriptThis": True, "implicit": "ReferenceError", "global": "undefined"}, body(strict)
        hashbang = await fixture.client.call_tool("js", {"code": "#!/usr/bin/env node\nnodeRepl.write('hashbang accepted')"})
        assert not hashbang.is_error and body(hashbang) == "hashbang accepted", body(hashbang)
        comment = await fixture.client.call_tool("js", {"code": "#!/usr/bin/env node"})
        assert not comment.is_error and not comment.content, body(comment)


@pytest.mark.asyncio
async def test_late_uncaught_error_is_logged_for_original_request_and_dead_kernel_is_not_reused(kernel_source, monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO", logger="backend.mcp.client")
    node, source = kernel_source
    source = "import {writeFileSync as kernelProbeWrite} from 'node:fs';\n" + source
    source = source.replace("current.exited.resolve(code);", "current.exited.resolve(code);kernelProbeWrite('actual-worker-exit',String(code));", 1)
    monkeypatch.setattr(node, "_SERVER", source)
    async with companion(tmp_path) as fixture:
        first = await fixture.client.call_tool("js", {"code":
            "const old=1;setTimeout(()=>{throw new Error('owned late callback fault')},20);nodeRepl.write('scheduled')"},
            request_meta={"owner": "late original"})
        assert not first.is_error and body(first) == "scheduled"
        await wait_file(tmp_path / "actual-worker-exit", "1")
        errors = [item for item in notifications(caplog) if "owned late callback fault" in json.dumps(item)]
        assert errors and any(item.get("requestMeta") == {"owner": "late original"} for item in errors), errors
        fresh = await asyncio.wait_for(fixture.client.call_tool("js", {"code": "nodeRepl.write(typeof old)"}), 10)
        assert not fresh.is_error and body(fresh) == "undefined", body(fresh)


@pytest.mark.asyncio
async def test_actual_module_worker_exit_settles_original_call_and_next_kernel_works(kernel_source, tmp_path):
    (tmp_path / "stop.mjs").write_text("export function stop(){process.exit(0)}")
    async with companion(tmp_path) as fixture:
        exited = await asyncio.wait_for(fixture.client.call_tool("js", {"code": "const mod=await import('./stop.mjs');mod.stop()"}), 10)
        assert exited.is_error and "Node kernel exited before execution completed (code 0)" in body(exited), body(exited)
        fresh = await asyncio.wait_for(fixture.client.call_tool("js", {"code": "nodeRepl.write(typeof mod)"}), 10)
        assert not fresh.is_error and body(fresh) == "undefined", body(fresh)
