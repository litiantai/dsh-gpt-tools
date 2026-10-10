import { recordLabel, type RecordData } from './RecordDetails';

export type ActiveCall = {id?:string;action:string;started?:number};
export function activeCall(record?:unknown):ActiveCall|undefined {
  const call=record && typeof record==='object' && 'call' in record?record.call:undefined;
  return call && typeof call==='object' && 'action' in call && typeof call.action==='string'?call as ActiveCall:undefined;
}
const queuedPhases=new Set(['preparing','syncing_feature','code_review','repairing_feature','syncing_release','reviewing_release','repairing_release','validating','merging_feature','merging','planning','plan_review','acceptance_review','syncing','repairing','developing','verifying','investigating','deploying']);
export function currentProgress(record:RecordData, waitReason?:string, review?:RecordData) {
  const call=activeCall(record), status=String(record.status || '');
  if(call && ['pausing','cancelling'].includes(status))return {label:`${recordLabel(status)} · 等待执行结束`,reason:String(record.reason || '等待当前调用停止并核对结果'),running:true};
  if(call)return {label:`${recordLabel(call.action)} · 执行中`,reason:'本轮执行尚未结束，等待回执',running:true};
  if(['plan_review','acceptance_review'].includes(status) && record.review_id) {
    if(review?.id===record.review_id) {
      const phase=({running:'执行中',queued:'等待执行',awaiting_human:'等待人工审批',completed:'回执待处理',blocked:'已阻塞',cancelled:'已取消'} as Record<string,string>)[String(review.status)] || '等待确认审查状态';
      return {label:`${recordLabel(status)} · ${phase}`,reason:review.status==='running'?'本轮审查尚未结束，等待回执':String(review.reason || waitReason || phase),running:review.status==='running'};
    }
    return {label:`${recordLabel(status)} · 等待确认审查状态`,reason:'审查已关联，等待取得最新审查记录',running:false};
  }
  if(queuedPhases.has(status))return {label:`${recordLabel(status)} · 等待执行`,reason:waitReason || String(record.reason || '等待调度器派发'),running:false};
  return {label:recordLabel(status),reason:String(record.reason || ''),running:false};
}
