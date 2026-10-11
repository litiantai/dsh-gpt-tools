import { ErrorNotice } from './errors';
import { Alert, Button, Input, Modal, Select, Space, Table, Tag, Tooltip } from 'antd';
import { useState } from 'react';
import { activeCall, currentProgress, type ActiveCall } from './currentProgress';
import { recordLabel } from './RecordDetails';
import { time } from './components';
import DeliveryTasks from './DeliveryTasks';
import RecordInspector, {type LinkedRecord} from './RecordInspector';

export interface PullRequest {
  id:string; pr_url:string; number:number|string; title:string;
  status:'unreviewed'|'approved'|'fixing'|'merged'; git_status:'open'|'closed'|'merged'|'unknown'; updated:number;
  head_branch?:string; base_branch?:string; draft?:boolean;
  requirement_title?:string; requirement_id?:string; shared_pr?:boolean; delivery_status?:string; reason?:string;
  delivery_id?:string; delivery_version?:number; manual_merge_allowed?:boolean; manual_merge_reason?:string;
  retryable?:boolean; retry_reason?:string;
  progress?:{status:string;reason?:string;updated?:number;call?:ActiveCall};
}
export interface RepairIssue {
  requirement?:{id:string;title:string;status:string;updated?:number};
  delivery_id?:string; delivery_status?:string; delivery_version?:number; call?:ActiveCall; retryable?:boolean; retry_reason?:string;
  last_attempt?:{status:string;reason?:string;updated:number};
  id:string; pr_url:string; title:string; path:string; start_line?:number; severity:string;
  status:string; created:number; updated:number; reason?:string; evidence?:string; batch_title:string;
}
export interface DeliveryBoardData {
  dispatch?:{status:string;reason:string;started?:number;healthy_since?:number};
  prs:PullRequest[]; release_prs?:PullRequest[]; issues:RepairIssue[]; checked_at?:number; sync_error?:string;
}
const prLabels={unreviewed:'未评审',approved:'已评审 · 评审通过',fixing:'已评审 · 问题修复',merged:'已合并'};
const issueLabels:Record<string,string>={pending:'待修复',repairing:'修复中',blocked:'修复受阻',waiting:'等待执行',waiting_update:'等待平台更新',running:'执行中'};
const safeLink=(url:string)=>url.startsWith('https://github.com/');

export default function DeliveryBoard({data,kind,loading,onRetry,onManualMerge,onRetryRelease,busy}:{onRetry?:(issue:RepairIssue)=>void;onManualMerge?:(pr:PullRequest)=>void;onRetryRelease?:(pr:PullRequest)=>void;busy?:boolean;data?:DeliveryBoardData;kind:'prs'|'release_prs'|'issues';loading?:boolean}) {
  const [filter,setFilter]=useState('unmerged');
  const isPR=kind!=='issues';
  const [query,setQuery]=useState('');
  const [selected,setSelected]=useState<LinkedRecord>();
  const matches=(values:unknown[])=>values.join(' ').toLowerCase().includes(query.toLowerCase());
  const prs=(kind==='release_prs'?data?.release_prs || []:data?.prs || []).filter(pr=>(filter==='all' || filter==='unmerged' && pr.status!=='merged' && pr.git_status!=='closed' || pr.status===filter) && matches([pr.requirement_title,pr.title,pr.number,pr.pr_url]));
  const issues=(data?.issues || []).filter(issue=>matches([issue.title,issue.path,issue.pr_url]));
  return <>
    {data?.sync_error && <Alert type="warning" showIcon message={data.sync_error}/>}
    <Space wrap className="project-record-filters">
      <Input.Search aria-label={isPR?'搜索 PR':'搜索未完成问题'} placeholder={isPR?'搜索需求、PR 编号或标题':'搜索问题、文件或 PR'} allowClear value={query} onChange={e=>setQuery(e.target.value)} style={{width:260}}/>
      {isPR && <Select aria-label="PR 状态筛选" value={filter} onChange={setFilter} style={{width:190}} options={[
        {value:'unmerged',label:'未合并（默认）'},{value:'all',label:'全部 PR'},
        ...(kind==='release_prs'?[{value:'merged',label:'已合并'}]:Object.entries(prLabels).map(([value,label])=>({value,label}))),
      ]}/>}
      <span className="muted">{isPR?kind==='prs'?`${prs.length} 条需求关联 · ${new Set(prs.map(pr=>pr.pr_url)).size} 个 PR`:`${prs.length} 个 release PR`:`${issues.length} 条未完成问题`}</span>
      {isPR && <span className="muted">{data?.checked_at?`GitHub 状态同步于 ${time(data.checked_at)}`:'尚未从 GitHub 确认状态'}</span>}
    </Space>
    {isPR?<Table<PullRequest> locale={{emptyText:kind==='prs' && filter==='unmerged'?'当前没有未合并的需求 PR；可切换「全部 PR」查看历史。':'没有符合条件的 PR'}} rowKey="id" dataSource={prs} loading={loading}
      style={kind==='release_prs'?{containerType:'inline-size'}:undefined}
      expandable={kind==='release_prs'?{rowExpandable:r=>!!r.delivery_id,expandedRowRender:r=><DeliveryTasks deliveryId={r.delivery_id!}/>,columnWidth:110,columnTitle:'当日问题',expandIcon:({expanded,onExpand,record})=>record.delivery_id?<Button size="small" aria-expanded={expanded} onClick={e=>onExpand(record,e)}>{expanded?'收起问题':'展开问题'}</Button>:null}:undefined}
      pagination={{pageSize:10,showSizeChanger:false}} scroll={{x:kind==='prs'?1100:1230}} columns={[
      ...(kind==='prs'?[{title:'需求',width:280,render:(_:unknown,r:PullRequest)=><><div>{r.requirement_title}</div>{r.shared_pr && <small className="muted">历史需求共用 PR</small>}</>}]:[]),
      {title:kind==='prs'?'需求 PR':'Release PR',width:kind==='release_prs'?220:undefined,render:(_,r)=>safeLink(r.pr_url)?<a href={r.pr_url} target="_blank" rel="noreferrer">#{r.number} {r.title}</a>:r.title},
      {title:'状态',width:180,render:(_,r)=><Space direction="vertical" size={0}><Tag color={kind==='release_prs'?(r.git_status==='merged'?'success':r.delivery_status==='blocked'?'error':'default'):(r.status==='merged' || r.status==='approved'?'success':r.status==='fixing'?'warning':'default')}>{kind==='release_prs'?(r.git_status==='merged'?'已合并':r.git_status==='closed'?'已关闭':r.delivery_status==='blocked'?'异常':r.delivery_status==='release_failed' || r.delivery_status==='repairing_release'?'待修复':'待合并'):prLabels[r.status]}</Tag>{r.git_status==='closed' && <small className="muted">PR 已关闭（未合并）</small>}{r.git_status==='unknown' && <small className="muted">GitHub 状态未确认</small>}{r.draft && <small className="muted">草稿 PR</small>}</Space>},
      {title:'当前进度',width:200,render:(_,r)=>r.progress?<span title={currentProgress(r.progress,data?.dispatch?.reason).reason}>{currentProgress(r.progress,data?.dispatch?.reason).label}</span>:r.delivery_status?recordLabel(r.delivery_status):'尚无执行记录'},
      {title:'分支',width:240,render:(_,r)=>r.head_branch?`${r.head_branch} → ${r.base_branch}`:'—'},
      {title:'更新时间',width:150,render:(_,r)=>time(r.updated)},
      {title:kind==='release_prs'?'操作':'关联详情',width:130,fixed:kind==='release_prs'?'right':undefined,render:(_,r)=><Space direction="vertical">
        {kind==='release_prs' && r.delivery_id && r.git_status!=='merged' && r.git_status!=='closed' && <Tooltip title={r.manual_merge_reason || '立即验证当前 release，通过后自动合并；后续成果进入新批次'}><span><Button size="small" type="primary" disabled={busy || !r.manual_merge_allowed || !onManualMerge} onClick={()=>onManualMerge?.(r)}>手动合并</Button></span></Tooltip>}
        {kind==='release_prs' && r.delivery_id && r.delivery_status==='blocked' && r.git_status!=='merged' && r.git_status!=='closed' && <Tooltip title={r.retry_reason || '从异常中断的阶段继续，检查通过后自动合并'}><span><Button size="small" type="primary" disabled={busy || !r.retryable || !onRetryRelease} onClick={()=>onRetryRelease?.(r)}>重试</Button></span></Tooltip>}
        {(r.requirement_id || r.delivery_id) && <Button size="small" onClick={()=>setSelected({kind:r.requirement_id?'requirements':'deliveries',record:{id:r.requirement_id || r.delivery_id,title:r.requirement_title || r.title}})}>查看证据</Button>}
      </Space>},
    ]}/>:<>
      <p className="muted">仅显示尚未修复完成的问题；同一 PR 可有多条。转入需求池的问题显示关联需求进度，完成后移出；PR 合并不代表缺陷已修复。</p>
      <Table<RepairIssue> rowKey="id" dataSource={issues} loading={loading} pagination={{pageSize:10,showSizeChanger:false}} scroll={{x:1450}} columns={[
        {title:'问题',width:320,render:(_,r)=><><div>{r.title}</div>{r.path && <small className="muted">{r.path}{r.start_line?`:${r.start_line}`:''}</small>}</>},
        {title:'关联 PR',width:150,render:(_,r)=>safeLink(r.pr_url)?<a href={r.pr_url} target="_blank" rel="noreferrer">PR #{r.pr_url.split('/').pop()}</a>:<span title={r.batch_title}>尚未创建 PR</span>},
        {title:'当前进度',width:200,render:(_,r)=><Tag color={activeCall(r)?'processing':r.status==='blocked'?'error':'default'}>{r.requirement?`已转需求 · ${recordLabel(r.requirement.status)}`:activeCall(r)?`${recordLabel(r.call!.action)} · 执行中`:r.status==='waiting'?`${recordLabel(r.delivery_status)} · 等待执行`:issueLabels[r.status] || r.status}</Tag>},
        {title:'严重程度',width:110,render:(_,r)=>({critical:'严重',high:'高',medium:'中',low:'低'}[r.severity] || '—')},
        {title:'当前说明 / 上次结果',width:260,render:(_,r)=><>{r.requirement?r.reason:activeCall(r)?'本轮执行尚未结束，等待回执':r.reason?<ErrorNotice value={r.reason}/>:r.retry_reason || '—'}{r.last_attempt && <details><summary>上次结果 · {time(r.last_attempt.updated)}</summary><span>{issueLabels[r.last_attempt.status] || r.last_attempt.status}</span>{r.last_attempt.reason && <ErrorNotice value={r.last_attempt.reason}/>}</details>}</>},
        {title:'更新时间',width:150,render:(_,r)=>time(r.updated)},
        {title:'操作',width:150,fixed:'right',render:(_,r)=>r.delivery_id?<Space direction="vertical">{r.requirement?<Button size="small" onClick={()=>setSelected({kind:'requirements',record:r.requirement!})}>查看关联需求</Button>:<Tooltip title={r.retry_reason}><span><Button size="small" disabled={busy || !r.retryable || !onRetry} onClick={()=>onRetry?.(r)}>重试修复</Button></span></Tooltip>}<Button size="small" onClick={()=>setSelected({kind:'deliveries',record:{id:r.delivery_id,title:r.batch_title}})}>查看证据</Button></Space>:'—'},
      ]}/>
    </>}
    <Modal title={String(selected?.record.title || '关联证据')} open={!!selected} onCancel={()=>setSelected(undefined)} footer={null} width={900} destroyOnHidden>
      {selected && <RecordInspector key={`${selected.kind}:${selected.record.id}`} kind={selected.kind} record={selected.record}/>}
    </Modal>
  </>;
}
