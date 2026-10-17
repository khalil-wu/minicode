// Controlled Source package boundary. Never launches or controls an application.
import { appendFile } from 'node:fs/promises';
export class WindowsHelperTransport {
  constructor(options) { this.options = options; this.closed = false; }
  async request(method, params, options) {
    await appendFile(this.options.helperCommand, JSON.stringify({ method, params, metadata: options.codexTurnMetadata }) + '\n');
    if (method === 'error') throw new Error('ACTUAL_SOURCE_PACKAGE_ERROR');
    if (method === 'slow') await new Promise(resolve => setTimeout(resolve, 100));
    if (method === 'approval') {
      const approval = await options.createElicitation({
        message: 'Allow Codex to use Fixture App?',
        meta: { codex_approval_kind: 'mcp_tool_call', connector_id: 'computer-use', connector_name: 'Computer Use',
          persist: ['session', 'always'], riskLevel: 'low', tool_params: { app: 'fixture-app' },
          tool_params_display: [{ name: 'app', display_name: 'App', value: 'Fixture App' }] },
      });
      if (approval.action !== 'accept') throw new Error('Computer Use was not approved to use Fixture App');
      return { approval, metadata: options.codexTurnMetadata };
    }
    return { method, params, metadata: options.codexTurnMetadata };
  }
  async close() {
    this.closed = true;
    await new Promise(resolve => setTimeout(resolve, 30));
    await appendFile(this.options.helperCommand, JSON.stringify({ method: 'closed' }) + '\n');
  }
}
