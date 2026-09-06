const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const port = Number(process.env.PORT || 8787);
const token = process.env.CODEX_CHAT_TOKEN;
if (!token) throw new Error('CODEX_CHAT_TOKEN is required');
const page = fs.readFileSync(path.join(__dirname, 'index.html'));

function send(res, status, body, type = 'application/json') {
  res.writeHead(status, {'content-type': `${type}; charset=utf-8`});
  res.end(type === 'application/json' ? JSON.stringify(body) : body);
}
function auth(req) { return req.headers.authorization === `Bearer ${token}`; }
function body(req) { return new Promise((resolve, reject) => { let s = ''; req.on('data', x => s += x); req.on('end', () => { try { resolve(JSON.parse(s || '{}')); } catch (e) { reject(e); } }); req.on('error', reject); }); }

http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/') return send(res, 200, page, 'text/html');
  if (req.method !== 'POST' || req.url !== '/api/chat') return send(res, 404, {error: 'not found'});
  if (!auth(req)) return send(res, 401, {error: 'unauthorized'});
  try {
    const input = await body(req);
    const prompt = String(input.prompt || '').trim();
    const cwd = path.resolve(String(input.cwd || '/root/minicode'));
    if (!prompt) throw new Error('请输入消息');
    if (!cwd.startsWith('/root/') || !fs.statSync(cwd).isDirectory()) throw new Error('工作目录必须是 /root 下的已有目录');
    res.writeHead(200, {'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache'});
    const args = ['exec', '--json', '--model', String(input.model || 'gpt-6-astra'), '--dangerously-bypass-approvals-and-sandbox', '--skip-git-repo-check', '-C', cwd, prompt];
    const child = spawn('codex', args, {cwd, env: {...process.env, CI: '1', TERM: 'dumb'}});
    child.stdout.on('data', data => res.write(`data: ${JSON.stringify({stream: data.toString()})}\n\n`));
    child.stderr.on('data', data => res.write(`data: ${JSON.stringify({stream: data.toString()})}\n\n`));
    child.on('close', code => { res.write(`data: ${JSON.stringify({done: true, code})}\n\n`); res.end(); });
    req.on('close', () => child.kill('SIGTERM'));
  } catch (e) { send(res, 400, {error: e.message}); }
}).listen(port, '0.0.0.0', () => console.log(`codex-chat listening on ${port}`));
