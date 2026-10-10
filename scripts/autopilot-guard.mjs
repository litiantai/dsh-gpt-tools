// Process-level file isolation is supplied by sandbox-exec; this adds tool/lifecycle constraints.
import { writeFileSync, renameSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const defineTool=process.env.DSH_AUTOPILOT_RUNTIME ? (await import(pathToFileURL(process.env.DSH_AUTOPILOT_RUNTIME+'/node_modules/@deepseek-ai/dsh-tools/lib/index.js').href)).defineTool : null;
export const name='autopilot-worker-guard';
export const inject=['tools','jobs'];
export function apply(ctx) {
  if (!process.env.DSH_AUTOPILOT_WORKER) throw new Error('Missing autonomous worker identity');
  if(defineTool && process.env.DSH_AUTOPILOT_RESULT) ctx.tools.register(defineTool({
    name:'autopilot_result',description:'提交本阶段的最终结构化回执。方案写入 plan，实现及测试证据写入 summary；缺前提用 blocked，检查失败用 fail。提交后结束本轮。',
    parameters:{status:{type:'string',enum:['pass','fail','blocked','waiting_for_reply'],required:true},plan:{type:'string'},summary:{type:'string'},reason:{type:'string'},collaboration_requests:{type:'array',items:{type:'object',properties:{to_role:{type:'string'},topic:{type:'string'},content:{type:'string'},evidence_ids:{type:'array',items:{type:'string'}}}}}},
    output:{schema:{type:'object',additionalProperties:true},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},
    isConcurrencySafe:()=>false,
    async execute(args) {
      if(args.status==='pass' && !(process.env.DSH_AUTOPILOT_PHASE==='plan'?args.plan:args.summary)?.trim()) throw new Error('通过回执必须提供方案或实现与验证证据');
      const path=process.env.DSH_AUTOPILOT_RESULT;
      writeFileSync(path+'.tmp',JSON.stringify(args),{mode:0o600});renameSync(path+'.tmp',path);
      return {recorded:true};
    },
  }));
  ctx.tools.guard(exec => {
    if (process.env.DSH_AUTOPILOT_TEST_EXECUTION==='local' && exec.name==='bash') return '本项目由本机控制器运行命令和测试；开发 Agent 使用文件工具修改 feat/release 源码，完成后提交代码修改回执。';
    const allowed=['autopilot_result','bash','read','read_file','glob','grep','write','write_file','edit','apply_patch'];
    if (!allowed.includes(exec.name)) return '自主工作进程仅可操作隔离工作区，不启动会话、子代理或外部操作。';
    if (exec.name==='bash' && (exec.arguments?.run_in_background || Number(exec.arguments?.timeoutMs || 0)>300000)) return '工具必须前台执行，单次最多 5 分钟。';
    if (exec.name==='bash' && /\b(?:launchctl|osascript|nohup|setsid)\b|(^|[^&<>])&([^&>]|$)/.test(String(exec.arguments?.command || exec.arguments?.cmd || ''))) return '开发工具不得脱离前台进程生命周期或控制桌面应用。';
    if (process.env.DSH_AUTOPILOT_PHASE!=='develop' && !['autopilot_result','read','read_file','glob','grep'].includes(exec.name)) return '需求分析与方案阶段只读。';
  });
}
