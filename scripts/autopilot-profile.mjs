// Copy only model routing into an isolated headless home. Do not copy user sessions or tools.
import { readFile, writeFile, mkdir, symlink, lstat, rename, chmod } from 'node:fs/promises';
import { join } from 'node:path';
import { Document, YAMLSeq, parseDocument } from 'yaml';
const [sourceHome, sourceName, workerHome, runtime, guard] = process.argv.slice(2);
const dir = join(workerHome, 'profiles', 'autopilot-worker');
await mkdir(dir, {recursive:true,mode:0o700});
const doc = parseDocument(await readFile(join(sourceHome,'profiles',sourceName,'cordis.patch.yml'),'utf8'));
if (doc.errors.length) throw new Error('Model source profile is invalid');
const out = new Document(); out.contents = new YAMLSeq();
for (const item of doc.contents?.items || []) {
  const id=item.get?.('id');
  if (typeof id==='string' && (id.startsWith('llm-') || id==='agent-default-model')) out.contents.items.push(item.clone());
}
// Resolve credential references locally; never put credentials in prompts or logs.
const references=new Set();
function collect(value) {
  if (!value || typeof value!=='object') return;
  for (const [key,child] of Object.entries(value)) {
    if (['apiKeyEnv','apiKeyRef','credentialRef'].includes(key) && typeof child==='string') references.add(child);
    else collect(child);
  }
}
const routes=out.toJS();
collect(routes);
if (routes.some(item=>item.id==='agent-default-model' && item.config?.provider==='deepseek-official') && !references.size) references.add('DEEPSEEK_API_KEY');
let credentials;
try { credentials=parseDocument(await readFile(join(sourceHome,'.credentials.yaml'),'utf8')); }
catch(e) { if(e.code!=='ENOENT') throw new Error('Cannot read local model credentials'); }
if (credentials?.errors.length) throw new Error('Local model credentials document is invalid');
const refs={};
for (const ref of references) {
  const value=credentials?.getIn(['refs',ref]);
  if (typeof value==='string') refs[ref]=value;
}
const credentialPath=join(workerHome,'.credentials.yaml');
const pending=credentialPath+'.tmp-'+process.pid;
await writeFile(pending,String(new Document({version:1,refs,records:{}})),{mode:0o600});
await chmod(pending,0o600);
await rename(pending,credentialPath);
// Keep Harness's compressed session backend separate from controller review ledgers.
out.contents.items.push(out.createNode({id:'session-persistence-jsonl',config:{root:join(workerHome,'harness-sessions')}}));
const reviewDir=join(workerHome,'profiles','autopilot-review');
await mkdir(reviewDir,{recursive:true,mode:0o700});
await writeFile(join(reviewDir,'package.json'),JSON.stringify({name:'autopilot-review',private:true,dsh:{profile:{bundles:['@deepseek-ai/dsh-base','@deepseek-ai/dsh-headless'],patchReload:'none'}}}),{mode:0o600});
// The worker process and every child already run under the controller's tested
// Seatbelt profile. macOS cannot apply a second Seatbelt profile inside it.
// Avoid nested sandbox startup failure; the outer workspace boundary remains enforced.
out.contents.items.push(out.createNode({id:'sandbox-policy',config:{mode:'danger-full-access'}}));
out.contents.items.push(out.createNode({id:'approval',config:{policy:'never'}}));
await writeFile(join(reviewDir,'cordis.patch.yml'),String(out),{mode:0o600});
out.contents.items.push(out.createNode({insert:[{id:'autopilot-guard',name:guard}]}));
await writeFile(join(dir,'package.json'),JSON.stringify({name:'autopilot-worker',private:true,dsh:{profile:{bundles:['@deepseek-ai/dsh-base','@deepseek-ai/dsh-headless'],patchReload:'none'}}}),{mode:0o600});
await writeFile(join(dir,'cordis.patch.yml'),String(out),{mode:0o600});
const anchor=join(workerHome,'profiles','node_modules');
try { await lstat(anchor); } catch(e) { if(e.code!=='ENOENT') throw e; await symlink(join(runtime,'node_modules'),anchor); }
