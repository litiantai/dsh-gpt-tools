export type RunFlowNode = 'review'|'planning'|'development'|'testing'|'acceptance'|'delivery'|'blocked'|'cancelled';

const runNodes:Record<string,RunFlowNode> = {
  queued:'planning', planning:'planning', plan_review:'review', developing:'development',
  verifying:'testing', acceptance_review:'acceptance', accepted:'acceptance', delivered:'delivery',
  blocked:'blocked', pausing:'blocked', waiting_for_reply:'blocked',
  cancelled:'cancelled', rolled_back:'cancelled',
};

/** 已上线和已完成记录保留在总览/台账；其余未映射状态进入待处理，避免任务消失。 */
export function groupFlowRuns<T extends {status:string}>(runs:T[]):Record<RunFlowNode,T[]> {
  const groups:Record<RunFlowNode,T[]> = {
    review:[], planning:[], development:[], testing:[], acceptance:[], delivery:[], blocked:[], cancelled:[],
  };
  for(const run of runs) {
    if(['online','completed'].includes(run.status))continue;
    const node=Object.hasOwn(runNodes,run.status)?runNodes[run.status]:'blocked';
    groups[node].push(run);
  }
  return groups;
}
