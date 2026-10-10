import {CollaborationPanel, RequirementCard} from './ProjectIntelligence';
import { ErrorNotice, errorText, serializeError } from './errors';
import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Alert, Button, Card, Empty, Image, Space, Spin, Tabs } from 'antd';
import { currentProgress } from './currentProgress';
import { api } from './api';
import DevelopmentPlan from './DevelopmentPlan';
import { CheckList, RawRecord, ReceiptList, RecordValue, recordLabel, unpack, type RecordData } from './RecordDetails';

export const contextKinds = new Set(['signals','requirements','runs','releases','deliveries','code_reviews','reviews','inspections','evidence','evaluations','daily_reports']);
const kindNames:Record<string,string> = {signals:'发现信号',requirements:'需求',runs:'研发任务',releases:'发布',deliveries:'交付批次',code_reviews:'代码审查',inspections:'巡查',evidence:'证据',evaluations:'评测',daily_reports:'日报',reviews:'监工审查',sessions:'执行会话',events:'事件',monitoring:'运行检查'};
export type LinkedRecord = {kind:string;record:RecordData};
type Context = {record:RecordData;related:LinkedRecord[];missing:{kind:string;id:string}[]};
const object = (value:unknown):value is RecordData => !!value && typeof value==='object' && !Array.isArray(value);
const nonempty = (value:unknown) => value!=null && value!=='' && (!Array.isArray(value) || value.length>0);

// 历史结果可能是 JSON 字符串，检查项也可能只保存在某次执行回执里。
function fields(record:unknown, field:string, path='', depth=0):{path:string;value:unknown}[] {
  const value=unpack(record);
  if(!object(value) || depth>7)return [];
  const found=nonempty(unpack(value[field]))?[{path,value:unpack(value[field])}]:[];
  for(const key of ['result','summary','details','detail','data','packet','suggestion','investigation_result','review_packet']) {
    found.push(...fields(value[key],field,[path,key].filter(Boolean).join('.'),depth+1));
  }
  if(field!=='receipts' && Array.isArray(value.receipts))value.receipts.forEach((receipt,index)=>{
    found.push(...fields(receipt,field,`${path ? path+'.' : ''}receipts[${index}]`,depth+1));
  });
  return found;
}

function sourceName(item:LinkedRecord) {
  const r=item.record;
  return `${kindNames[item.kind] || item.kind} · ${String(r.title || r.name || r.id || recordLabel(r.action || r.phase))}`;
}

function developmentPlans(items:LinkedRecord[]) {
  const seen=new Set<string>();
  return items.flatMap(item=>{
    const plans=nonempty(item.record.plan)?[{path:'',value:item.record.plan}]:fields(item.record,'plan');
    return plans.filter(plan=>{
      if(typeof plan.value==='string' && !plan.value.trim())return false;
      const key=JSON.stringify(plan.value);
      if(seen.has(key))return false;
      seen.add(key);return true;
    }).map(plan=>({item,...plan}));
  });
}

export function RecordChecks({items}:{items:LinkedRecord[]}) {
  const seen=new Set<string>();
  const groups=items.flatMap(item=>['acceptance','checks'].flatMap(field=>fields(item.record,field).flatMap(found=>{
    const key=field+JSON.stringify(found.value);
    if(seen.has(key))return [];
    seen.add(key);
    return [{item,field,...found}];
  })));
  if(!groups.length)return <><Alert type="info" showIcon message="当前记录及直接关联记录未提供逐项验收或检查明细" description="下方展示已记录的结论；总体结果不代表每个检查项目均已验证。"/><RecordValue value={items[0]?.record}/></>;
  return <Space direction="vertical" style={{width:'100%'}}>{groups.map((group,index)=><Card size="small" key={index} title={`${sourceName(group.item)} · ${group.field==='checks'?'检查结果':'验收条件'}`}>
    {group.field==='checks'?<CheckList value={group.value}/>:<RecordValue value={group.value}/>}
  </Card>)}</Space>;
}

export function RecordReceipts({items,receipt}:{items:LinkedRecord[];receipt?:RecordData}) {
  const seen=new Set<string>();
  const groups=items.flatMap(item=>{
    let values=fields(item.record,'receipts').flatMap(f=>Array.isArray(f.value)?f.value:[]);
    // 证据、审查本身就是一次结果，不应要求它再嵌套 receipts。
    if(!values.length && ['evidence','code_reviews','reviews'].includes(item.kind)) {
      const result=item.record.details ?? item.record.result ?? item.record.suggestion;
      const packet=unpack(item.record.packet);
      if(nonempty(result))values=[{call_id:item.record.call_id,action:item.record.phase || item.record.title || (object(packet)?packet.phase:undefined),at:item.record.at ?? item.record.created,result}];
    }
    if(item===items[0] && receipt)values=[receipt,...values];
    values=values.filter(value=>{
      const r=object(value)?value:{};
      const key=String(r.call_id || JSON.stringify([r.action,r.at,r.result]));
      if(seen.has(key))return false;
      seen.add(key);return true;
    });
    return values.length?[{item,values}]:[];
  });
  if(!groups.length)return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前记录及直接关联记录尚无执行回执"/>;
  return <Space direction="vertical" style={{width:'100%'}}>{groups.map((group,index)=><Card size="small" key={index} title={sourceName(group.item)}><ReceiptList value={group.values}/></Card>)}</Space>;
}

export default function RecordInspector({record,kind,receipt,statusLabel}:{record:RecordData;kind:string;receipt?:RecordData;statusLabel?:string}) {
  const [linked,setLinked]=useState<LinkedRecord>();
  const [image,setImage]=useState('');
  const [imageError,setImageError]=useState('');
  const current=linked || {kind,record};
  const monitorEvidence=!linked && kind==='monitoring' && typeof receipt?.evidence_id==='string'?receipt.evidence_id:undefined;
  const queryKind=monitorEvidence?'evidence':current.kind;
  const queryId=monitorEvidence || current.record.id;
  const query=useQuery<Context>({queryKey:['record-context',queryKind,queryId],
    queryFn:()=>api(`/${queryKind}/${encodeURIComponent(String(queryId))}/context`),
    enabled:contextKinds.has(queryKind) && !!queryId,refetchInterval:5000,retry:false});
  const selected=monitorEvidence?current.record:query.data?.record || current.record;
  const related=[...(monitorEvidence && query.data?[{kind:'evidence',record:query.data.record}]:[]),...(query.data?.related || [])];
  const items=[{kind:current.kind,record:selected},...related];
  const {receipts:_,checks:__,acceptance:___,plan:____,...summary}=selected;
  const plans=developmentPlans(items);
  const progress=currentProgress(selected,undefined,related.find(item=>item.kind==='reviews' && item.record.id===selected.review_id)?.record);
  const previous=progress.running?{reason:summary.reason,result:summary.result}:undefined;
  if(previous){delete summary.reason;delete summary.result;}
  const screenshot=async()=>{
    try {setImageError('');setImage((await api<{data_url:string}>(`/evidence/${encodeURIComponent(String(selected.id))}/screenshot`)).data_url);}
    catch(error){setImageError(serializeError(error));}
  };
  // 页签由组件显式控制：数据刷新不再重挂 Tabs，只在查看的记录真正变化时回到概况页。
  const [tab,setTab]=useState('summary');
  const identity=`${current.kind}:${String(current.record.id ?? '')}`;
  useEffect(()=>{setTab('summary');},[identity]);
  return <>
    {linked && <Space wrap><Button onClick={()=>setLinked(undefined)}>返回原记录</Button><strong>{sourceName(current)}</strong></Space>}
    {query.isLoading && <Spin size="small"/>}
    {query.error && <Alert type="warning" showIcon message="关联记录加载失败，当前仅展示已取得的内容" description={<ErrorNotice value={query.error}/>} action={<Button onClick={()=>void query.refetch()}>重试</Button>}/>}
    {current.kind==='runs' && <Alert type="info" message={`基础返修 ${Math.max(0,Number(selected.revisions||0)-Number(selected.extra_revisions||0))} 次 · 额外返修 ${Number(selected.extra_revisions||0)} 次 · 连续无进展 ${Number(selected.coordination_no_progress||0)} 轮`} description={String(selected.coordination_wait || selected.coordination_reason || '协调 Agent 将依据新证据判断是否需要帮助')}/>}
    <Tabs key={`${current.kind}:${String(selected.id || '')}`} activeKey={tab} onChange={setTab} items={[
      {key:'summary',label:'概况与判断',children:<>{progress.running?<Alert type="info" showIcon message={progress.label} description="本轮执行尚未结束，等待回执。"/>:!linked && statusLabel && <p>{statusLabel}</p>}<RecordValue value={summary}/>{previous && (previous.reason || previous.result)?<details><summary>上次结果</summary><RecordValue value={previous}/></details>:null}<Space wrap>
        {['sessions','reviews'].includes(current.kind) && !!selected.id && <Link to={`/${current.kind}?id=${encodeURIComponent(String(selected.id))}`}>打开完整{current.kind==='sessions'?'会话':'审查'}</Link>}
        {current.kind!=='reviews' && [selected.review_id,selected.acceptance_review_id].filter((id,index,all)=>typeof id==='string' && all.indexOf(id)===index).map(id=><Link key={String(id)} to={`/reviews?id=${encodeURIComponent(String(id))}`}>查看关联审查 {String(id).slice(0,8)}</Link>)}
        {current.kind==='evidence' && !!selected.screenshot && <Button onClick={()=>void screenshot()}>查看过程截图</Button>}
      </Space>{imageError && <Alert type="error" message={<ErrorNotice value={imageError}/>}/>}</>},
      ...(['runs','requirements'].includes(current.kind) || plans.length?[{key:'plan',label:'开发方案',children:plans.length?<div className="record-plans">{plans.map((plan,index)=><Card size="small" key={`${plan.item.kind}:${plan.item.record.id}:${plan.path}:${index}`} title={sourceName(plan.item)}><DevelopmentPlan value={plan.value}/></Card>)}</div>:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={query.isLoading?'正在加载开发方案':'尚未生成开发方案'}/>}]:[]),
      ...(current.kind==='runs' && selected.product_id && selected.id?[{key:'collaboration',label:'协作记录',children:<CollaborationPanel pid={String(selected.product_id)} runId={String(selected.id)}/>}]:[]),
      ...(current.kind==='requirements' && selected.confirmation_required?[{key:'confirmation',label:'需求确认',children:<RequirementCard row={selected as never} onChanged={()=>window.location.reload()}/>}]:[]),
      {key:'acceptance',label:'验收与检查',children:<RecordChecks items={items}/>},
      {key:'receipts',label:'执行回执',children:<RecordReceipts items={items} receipt={!linked?receipt:undefined}/>},
      {key:'related',label:`关联记录 (${related.length})`,children:<>
        {!!query.data?.missing.length && <Alert type="warning" message="部分引用记录不存在或不属于当前项目" description={query.data.missing.map(m=>`${kindNames[m.kind]} ${m.id}`).join('、')}/>}
        {related.length?<Space direction="vertical" style={{width:'100%'}}>{related.map(item=><Card key={`${item.kind}:${item.record.id}`} size="small" title={sourceName(item)} extra={<Button onClick={()=>setLinked(item)}>查看关联详情</Button>}><RecordValue value={{status:item.record.status,reason:item.record.reason,summary:item.record.summary,actual:item.record.actual}}/></Card>)}</Space>:<Empty description={query.error?'关联内容加载失败':query.isLoading?'正在加载关联内容':'尚无直接关联记录'}/>}
      </>},
      {key:'raw',label:'原始记录',children:<RawRecord value={selected}/>},
    ]}/>
    <Image style={{display:'none'}} src={image} preview={{visible:!!image,onVisibleChange:visible=>{if(!visible)setImage('');}}}/>
  </>;
}
