import { readFile } from 'node:fs/promises';
import { createNativeCuaExtension } from './native_cua_bridge.mjs';

/** Local packaged receiver; remote receivers continue to use the transferred bootstrap. */
export async function startNativeCua({ python, pythonModuleRoot, appAccess, approvalHome }) {
  const [source, catalog] = await Promise.all([
    readFile(new URL('./node_repl_server.mjs', import.meta.url), 'utf8'),
    readFile(new URL('./node_repl_catalog.json', import.meta.url), 'utf8'),
  ]);
  const pythonSource = `import sys,runpy;sys.path.insert(0,${JSON.stringify(pythonModuleRoot)});runpy.run_module('backend.mcp.native_cua_windows',run_name='__main__')`;
  const apiModule = new URL('./native_cua_api.mjs', import.meta.url).href;
  globalThis.__minicodeNodeReplExtension = ({ send }) => createNativeCuaExtension({
    send, python, pythonSource, apiModule, appAccess, approvalHome,
  });
  const server = source.replace('/*__MINICODE_NODE_REPL_CATALOG__*/ {}', JSON.stringify(JSON.parse(catalog)));
  await import('data:text/javascript;base64,' + Buffer.from(server).toString('base64'));
}
