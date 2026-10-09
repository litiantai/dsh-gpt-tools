// Model discovery uses initialization/control protocols only, never a user turn.
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
let input = '';
for await (const part of process.stdin) input += part;
const config = JSON.parse(input);
const emit = (value) => process.stdout.write(JSON.stringify(value) + '\n');
if (config.provider === 'claude') {
  const { query } = await import('@anthropic-ai/claude-agent-sdk');
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  async function* prompts() { await pending; }
  const q = query({ prompt: prompts(), options: {
    pathToClaudeCodeExecutable: config.bin,
    persistSession: false, tools: [], mcpServers: {},
    extraArgs: { 'strict-mcp-config': null, 'safe-mode': null },
  }});
  try { emit((await q.supportedModels()).map(m => ({ id: m.value, name: m.displayName || m.value, description: m.description }))); }
  finally { q.close(); release(); }
} else if (config.provider === 'codex') {
  const child = spawn(config.bin, ['app-server'], { stdio: ['pipe', 'pipe', 'pipe'] });
  const pending = new Map(); let next = 0;
  child.on('error', error => { for (const p of pending.values()) p.reject(error); });
  child.on('exit', () => { for (const p of pending.values()) p.reject(new Error('Codex 模型目录进程退出')); });
  child.stderr.resume();
  const lines = createInterface({ input: child.stdout });
  lines.on('line', line => {
    try { const msg=JSON.parse(line), p=pending.get(msg.id); if(p) { pending.delete(msg.id); msg.error ? p.reject(new Error(msg.error.message)) : p.resolve(msg.result); } } catch {}
  });
  const rpc = (method, params) => new Promise((resolve,reject) => {
    const id=++next; pending.set(id,{resolve,reject});
    child.stdin.write(JSON.stringify({jsonrpc:'2.0',id,method,params})+'\n');
  });
  try {
    await rpc('initialize', { clientInfo:{name:'dsh-supervisor',version:'1.0.0'}, capabilities:{} });
    child.stdin.write(JSON.stringify({method:'initialized'})+'\n');
    const models=[]; let cursor;
    do { const result=await rpc('model/list',{limit:100,...(cursor ? {cursor} : {})}); models.push(...result.data.map(m=>({id:m.model || m.id,name:m.displayName || m.model || m.id}))); cursor=result.nextCursor; } while(cursor);
    emit(models);
  } finally { child.stdin.end(); child.kill('SIGTERM'); lines.close(); }
} else throw new Error('未知审查器');
