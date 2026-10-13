import { ErrorNotice, errorText, serializeError } from './errors';
import { useState } from 'react';
import { Alert, Button, Card, Descriptions, Empty, Image, Modal, Space, Table, Tabs, Tag } from 'antd';
import { RecordValue, ResultDetails, RawRecord } from './RecordDetails';
import RecordInspector from './RecordInspector';
import InspectionRoutes from './InspectionRoutes';
import DailyReports from './DailyReports';
import { api } from './api';
import { time, useData } from './components';

interface Evidence {
  [key:string]:unknown;
  id:string; product_id:string; title:string; status:string; created:number; inspection_id?:string;
  method?:string; environment?:string; judgement?:string; action?:string; expected?:string;
  actual?:string; screenshot?:string; run_id?:string; provider?:string; model?:string;
  details?:unknown; steps?:string[]; results?:{run_id:string;title:string;status:string;reason?:string}[]; passed?:number;blocked?:number;target?:number;
}
const statusNames:Record<string,string>={verifying:'验证中',developing:'开发中',planning:'制定方案',plan_review:'方案审查',acceptance_review:'最终验收',accepted:'验收通过', pass:'通过',fail:'失败',blocked:'阻塞',running:'巡查中',completed:'已完成',observed:'已记录，待判断'};
const phases:Record<string,string>={daily_attribution:'晚间统一归因',daily_acceptance:'晚间逐项复验',daily_retrospective:'日报与复盘',probe:'运行监测',inspect:'隔离巡检',discover:'需求发现',plan:'开发方案',develop:'Harness 实现',verify:'独立验证',idle:'发布时机',publish:'发布',observe:'上线观察'};

export default function ProjectEvidence({productId,tab,onTabChange}:{productId?:string;tab:string;onTabChange:(key:string)=>void}) {
  const inspections=useData<Evidence[]>('/inspections');
  const evidence=useData<Evidence[]>('/evidence');
  const evaluations=useData<Evidence[]>('/evaluations');
  const signals=useData<Evidence[]>('/signals');
  const exceptions=signals.data?.filter(e=>e.product_id===productId && e.source==='inspection' &&
    ['blocked','fail','failed','error','abnormal'].includes(String((e.evidence as {status?:string})?.status))) ?? [];
  const [exception,setException]=useState<Evidence>();
  const [selection,setSelected]=useState<Evidence>();
  const selected=inspections.data?.find(row=>row.id===selection?.id && row.product_id===productId) || selection;
  const [image,setImage]=useState('');
  const [error,setError]=useState('');
  const [rescanning,setRescanning]=useState(false);
  const [rescanNotice,setRescanNotice]=useState('');
  const rescan=async()=>{
    if(!productId)return;
    setRescanning(true);
    try{
      const result=await api<{id:string;status:string}>(`/products/${productId}/inspection-routes/rescan`,{});
      setRescanNotice(`重新扫描已${result.status==='running'?'开始':'入队'}，轮次 ${result.id.slice(0,8)}；完成后保存逐路线证据并参与归因。`);
      setError('');await signals.refetch();
    }catch(err){setError(serializeError(err));}finally{setRescanning(false);}
  };
  const entries=evidence.data?.filter(e=>e.product_id===productId) ?? [];
  const list=inspections.data?.filter(e=>e.product_id===productId) ?? [];
  const color=(s:string)=>s==='pass'?'success':s==='fail'?'error':s==='blocked'?'warning':'processing';
  const screenshot=async(e:Evidence)=>{
    try {setImage((await api<{data_url:string}>(`/evidence/${e.id}/screenshot`)).data_url);}
    catch(err){setError(serializeError(err));}
  };
  const detail=(entry:Evidence)=><Card key={entry.id} size="small" className="evidence-step" title={<Space><Tag color={color(entry.status)}>{statusNames[entry.status] || entry.status}</Tag>{phases[entry.title] || entry.title}{!!(entry.details as {phase?:string})?.phase && <Tag>{({before:'操作前',after:'操作后',settled:'结果就绪',timeout:'等待超时',response:'接口响应',interrupted:'执行中断'} as Record<string,string>)[(entry.details as {phase:string}).phase] || (entry.details as {phase:string}).phase}</Tag>}</Space>} extra={time(entry.created)}>
    <Descriptions size="small" column={1} items={[
      ...['action','expected','actual','judgement'].filter(k=>entry[k as keyof Evidence]).map(k=>({key:k,label:({action:'巡查操作',expected:'预期结果',actual:'实际结果',judgement:'判断依据'} as Record<string,string>)[k],children:<RecordValue value={entry[k as keyof Evidence]}/>})),
      ...(entry.provider?[{key:'actor',label:'执行角色',children:`${entry.provider} · ${entry.model || '继承配置'}`}]:[]),
    ]}/>
    <Space>{entry.screenshot && <Button onClick={()=>void screenshot(entry)}>查看过程截图</Button>}{entry.run_id && <Tag>任务 {entry.run_id.slice(0,8)}</Tag>}</Space>
    {entry.details!==undefined && <details><summary>完整回执与检查明细</summary><ResultDetails value={entry.details}/></details>}
  </Card>;
  return <>
    {(error || inspections.error || evidence.error || evaluations.error || signals.error) && <Alert type="error" message={<ErrorNotice value={error || inspections.error || evidence.error || evaluations.error || signals.error}/>}/>}
    <Tabs activeKey={tab} onChange={onTabChange} onTabClick={onTabChange} animated={false} items={[
      {key:'inspection_routes',label:'巡检路线',children:productId?<InspectionRoutes pid={productId}/>:null},
      {key:'daily',label:'日报与复盘',children:<DailyReports productId={productId}/>},
      {key:'inspection_exceptions',label:`巡检异常 (${exceptions.length})`,children:<>
        <Alert type="info" showIcon message="异常每日北京时间 09:00 自动复扫，执行障碍与复扫证据参与归因；原始记录保留。" description="服务停机时恢复后补跑。只有真实断言通过才标记已恢复，取证完成仍需独立判断。"/>
        <Space style={{margin:'12px 0'}}><Button type="primary" loading={rescanning} disabled={!productId} onClick={()=>void rescan()}>重新扫描</Button>{rescanNotice}</Space>
        <Table rowKey="id" dataSource={exceptions} pagination={{pageSize:10}} columns={[
          {title:'巡查场景',render:(_,r)=><Button type="link" onClick={()=>setException(r)}>{String(r.summary || r.title || r.id)}</Button>},
          {title:'状态',render:()=> <Tag color="error">异常</Tag>},
          {title:'原因',render:(_,r)=><ErrorNotice value={r.evidence || r.reason}/>},
          {title:'复扫结论',render:(_,r)=><><Tag>{({queued:'已排队',recovered:'已恢复',collected:'已取证，待归因',blocked:'仍受阻'} as Record<string,string>)[String(r.rescan_status)] || '待复扫'}</Tag>{r.rescan_result!==undefined&&<ResultDetails value={r.rescan_result}/>}</>},
          {title:'归因',render:(_,r)=><RecordValue value={r.attribution || (r.attribution_pending?'待归因':'尚未归因')}/>},
          {title:'时间',dataIndex:'updated',render:time},
          {title:'证据',render:(_,r)=><Button onClick={()=>setException(r)}>详情</Button>},
        ]}/>
      </>},
      {key:'inspections',label:`巡查记录 (${list.length})`,children:<Table rowKey="id" dataSource={list} pagination={{pageSize:10}} scroll={{x:700}} columns={[
        {title:'巡查场景',dataIndex:'title',render:(title:string,row:Evidence)=><Button type="link" onClick={()=>setSelected(row)}>{title}</Button>},
        {title:'业务线',dataIndex:'business_line',filters:[...new Set(list.map(r=>String(r.business_line||'历史巡检')))].map(value=>({text:value,value})),onFilter:(value,row)=>String(row.business_line||'历史巡检')===value},
        {title:'路线',dataIndex:'route_id',filters:[...new Set(list.flatMap(r=>r.route_id?[String(r.route_id)]:[]))].map(value=>({text:list.find(r=>r.route_id===value)?.title||value,value})),onFilter:(value,row)=>row.route_id===value,render:(_,r)=>r.route_id?r.title:'历史巡检'},
        {title:'巡查方式',dataIndex:'method'},
        {title:'执行环境',dataIndex:'environment',render:(s:string)=>s==='isolated'?'隔离测试环境':s},
        {title:'结果',dataIndex:'status',render:(s:string)=><Tag color={color(s)}>{statusNames[s] || s}</Tag>},
        {title:'时间',dataIndex:'created',render:time},
        {title:'证据',render:(_,r)=><Button onClick={()=>setSelected(r)}>{r.steps?.length ?? 0} 个步骤</Button>},
      ]}/>},
      {key:'timeline',label:`全部证据 (${entries.length})`,children:entries.length?<div className="evidence-timeline">{entries.map(detail)}</div>:<Empty description="尚无执行回执；真实巡查与任务执行后会自动记录"/>},
      {key:'evaluations',label:'实践评测',children:<div className="evidence-timeline">{(evaluations.data?.filter(e=>e.product_id===productId) ?? []).map(e=><Card key={e.id} title={e.title}><Tag>{statusNames[e.status] || (e.status==='preparing'?'准备与需求发现':e.status)}</Tag><p>{e.judgement || '需求实践仍在进行；尚未生成最终评测结论。'}</p>{e.results && <Table rowKey="run_id" pagination={false} dataSource={e.results} columns={[{title:'需求',dataIndex:'title'},{title:'验收状态',dataIndex:'status',render:(value:string)=><Tag color={value==='accepted'?'success':value==='blocked'?'error':'processing'}>{statusNames[value] || value}</Tag>},{title:'当前说明',dataIndex:'reason',render:(value:string)=>value?<ErrorNotice value={value}/>:'—'}]}/>}<details><summary>评测标准与进度记录</summary><RecordValue value={e}/><RawRecord value={e}/></details></Card>)}</div>},
    ]}/>
    <Modal title={selected?.title} open={!!selected} onCancel={()=>setSelected(undefined)} footer={null} width={1000} destroyOnHidden>
      <p><strong>巡查方式：</strong>{selected?.method}</p>
      <p><strong>判断结论：</strong>{selected?.judgement || '巡查仍在进行中'}</p>
      <div className="evidence-timeline">{entries.filter(e=>e.inspection_id===selected?.id).sort((a,b)=>a.created-b.created).map(detail)}</div>
      {selected && <RecordInspector key={selected.id} kind="inspections" record={selected}/>}
    </Modal>
    <Image style={{display:'none'}} src={image} preview={{visible:!!image,onVisibleChange:v=>{if(!v)setImage('');}}}/>
    <Modal title="巡检异常记录" open={!!exception} onCancel={()=>setException(undefined)} footer={null} width={1000} destroyOnHidden>
      {exception && <RecordInspector key={exception.id} kind="signals" record={exception}/>}
    </Modal>
  </>;
}
