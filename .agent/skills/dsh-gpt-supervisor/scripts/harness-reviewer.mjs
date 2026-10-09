// Mounted only in the dedicated review profile; no supervisor lifecycle hooks.
export const name = 'supervisor-review-guard';
export const inject = ['llm', 'tools'];
export function apply(ctx) {
  const mode = process.env.DSH_SUPERVISOR_REVIEW;
  if (!['catalog', 'run'].includes(mode)) throw new Error('独立审查标记缺失');
  // Native entry points must never run in this process, even through a home overlay.
  ctx.tools.guard((exec) => {
    const name = exec.name;
    if (name === 'bash' && (exec.arguments?.run_in_background || Number(exec.arguments?.timeoutMs || 0) > 30000)) { process.stdout.write(JSON.stringify({type:'error',message:'审查命令必须前台执行，timeoutMs 至多 30000'})+'\n'); return '审查命令必须前台执行，timeoutMs 至多 30000'; }
    if (name && !['bash', 'read', 'read_file', 'glob', 'grep'].includes(name)) { process.stdout.write(JSON.stringify({type:'error',message:'审查工具权限不足：'+name})+'\n'); return '审查进程仅允许读取和前台测试'; }
  });
  if (mode !== 'catalog') return;
  void (async () => {
    await ctx.get('loader')?.await();
    const models = [];
    for (const provider of ctx.llm.listProviders()) {
      for (const model of await ctx.llm.listModels(provider.id)) {
        models.push({ id: model.id, name: model.name || model.id, model_provider: provider.id, provider_name: provider.name });
      }
    }
    process.stdout.write(JSON.stringify(models) + '\n');
    ctx.get('appExit')(0);
  })().catch(error => { process.stderr.write(String(error)+'\n'); ctx.get('appExit')(1); });
}
