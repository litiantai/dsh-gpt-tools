import { ErrorNotice, errorText, serializeError } from './errors';
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Card, Descriptions, Empty, Image, Modal, Space, Spin, Table, Tabs } from 'antd';
import { api } from './api';
import { ReceiptList, RecordValue, recordLabel } from './RecordDetails';
import RecordInspector from './RecordInspector';

type Item = {id:string;title?:string;name?:string;status:string;[key:string]:unknown};
type Task = {run:Item;requirement:Partial<Item>;signals:Item[];inspections:Item[];evidence:Item[];prs:{label:string;pr_url:string}[]};
type Usage = {tokens:number;sources:{id:string;action:string;tokens:number;collected:boolean;budget_kind:string}[];day:string;project_tokens_used:number;project_tokens_limit:number;execution_seconds?:number;receipts:unknown[];evidence:Item[];reviews:Item[]};
const title = (task:Task)=>task.run.title || task.run.name || task.requirement.title || task.run.id;

function TaskUsage({runId}:{runId:string}) {
  const query=useQuery<Usage>({queryKey:['task-usage',runId],queryFn:()=>api(`/runs/${runId}/usage`),refetchInterval:5000});
  if(query.error)return <Alert type="error" message={<ErrorNotice value={query.error}/>}/>;
  if(!query.data)return <Spin/>;
  const data=query.data;
  const recorded=data.sources.some(s=>s.collected);
  return <>
    <Descriptions column={2} items={[
      {key:'tokens',label:'任务已记录消耗',children:recorded?`${data.tokens.toLocaleString()} Token`:'尚无可归属的用量记录'},
      {key:'duration',label:'累计执行耗时',children:data.execution_seconds==null?'未记录':`${Math.round(data.execution_seconds)} 秒`},
      {key:'budget',label:`项目开发额度 · ${data.day}（北京时间）`,span:2,children:`${data.project_tokens_used.toLocaleString()} / ${data.project_tokens_limit?data.project_tokens_limit.toLocaleString():'不限'} Token`},
    ]}/>
    <p className="muted">任务消耗包含已归属的执行与审查用量；共享巡检、发现及整批交付消耗不分摊。数据以用量账本采集结果为准，缺失记录不代表零消耗。</p>
    <Tabs items={[
      {key:'usage',label:'消耗明细',children:<Table rowKey="id" size="small" pagination={false} dataSource={data.sources} columns={[
        {title:'执行阶段',dataIndex:'action',render:recordLabel},
        {title:'额度分类',dataIndex:'budget_kind',render:(value:string)=>({tokens:'开发额度',code_delivery_tokens:'代码交付额度',daily_report_tokens:'日报独立额度'}[value] || value)},
        {title:'已采集 Token',render:(_,row)=>row.collected?row.tokens.toLocaleString():'等待采集'},
      ]}/>},
      {key:'process',label:'过程数据',children:<><ReceiptList value={data.receipts}/><RecordValue value={data.evidence}/><RecordValue value={data.reviews}/></>},
    ]}/>
  </>;
}

export default function DeliveryTasks({deliveryId,day}:{deliveryId:string;day?:string}) {
  const query=useQuery<{tasks:Task[];day?:string;missing_ids:string[]}>({queryKey:['delivery-tasks',deliveryId],queryFn:()=>api(`/deliveries/${deliveryId}/tasks`),refetchInterval:5000});
  const [selected,setSelected]=useState<{task:Task;view:string}>();
  const [image,setImage]=useState('');
  const [error,setError]=useState('');
  const screenshot=async(id:string)=>{
    try {setError('');setImage((await api<{data_url:string}>(`/evidence/${id}/screenshot`)).data_url);}
    catch(e){setError(serializeError(e));}
  };
  const task=selected?.task;
  return <div className="delivery-tasks" style={{width:'calc(100cqi - 32px)',maxWidth:'100%'}}>
    <p className="muted">{(day || query.data?.day)?.replace(/^(\d{4})(\d{2})(\d{2})$/, '$1-$2-$3') || '当日'} 交付批次处理的问题</p>
    {query.error && <Alert type="error" message={<ErrorNotice value={query.error}/>}/>}
    {!!query.data?.missing_ids.length && <Alert type="warning" message={`${query.data.missing_ids.length} 条关联任务记录缺失，暂无法展示`}/>}
    <Table<Task> rowKey={row=>row.run.id} size="small" loading={query.isLoading} dataSource={query.data?.tasks || []} pagination={false} scroll={{x:760}} locale={{emptyText:'该交付批次尚未关联任务'}} columns={[
      {title:'任务名称',render:(_,row)=>title(row)},
      {title:'状态',width:150,render:(_,row)=>recordLabel(row.run.status)},
      {title:'操作',width:410,render:(_,row)=><Space wrap>{[['task','任务详情'],['discovery','发现过程（巡检记录）'],['pr','PR 详情'],['usage','查看用量']].map(([view,label])=><Button size="small" key={view} onClick={()=>{setError('');setSelected({task:row,view});}}>{label}</Button>)}</Space>},
    ]}/>
    <Modal title={task?title(task):''} open={!!selected} onCancel={()=>{setSelected(undefined);setImage('');}} footer={null} width={900} destroyOnHidden>
      {task && <Tabs activeKey={selected?.view} onChange={view=>setSelected({task,view})} items={[
        {key:'task',label:'任务详情',children:<><RecordValue value={task.requirement}/><RecordInspector key={task.run.id} kind="runs" record={task.run}/></>},
        {key:'discovery',label:'发现过程（巡检记录）',children:<>
          <RecordValue value={{evidence:task.requirement.evidence,reproduction:task.requirement.reproduction,impact:task.requirement.impact}}/>
          {task.signals.map(signal=><Card key={signal.id} size="small" title={String(signal.summary || '发现信号')}><RecordValue value={signal}/></Card>)}
          {!task.inspections.length && !task.evidence.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无关联的结构化巡检记录；发现依据见上方需求与信号"/>}
          {task.inspections.map(entry=><Card key={entry.id} size="small" title={entry.title}><RecordValue value={entry}/></Card>)}
          {task.evidence.map(entry=><Card key={entry.id} size="small" title={entry.title}><RecordValue value={entry}/>{!!entry.screenshot && <Button onClick={()=>void screenshot(entry.id)}>查看过程截图</Button>}</Card>)}
          {error && <Alert type="error" message={<ErrorNotice value={error}/>}/>}
        </>},
        {key:'pr',label:'PR 详情',children:task.prs.length?<Space direction="vertical">{task.prs.map((pr,i)=><div key={`${pr.pr_url}-${i}`}>{pr.label}：{pr.pr_url.startsWith('https://github.com/')?<a href={pr.pr_url} target="_blank" rel="noreferrer">{pr.pr_url}</a>:'PR 地址不可用'}</div>)}</Space>:<Empty description="该任务尚未关联 PR"/>},
        {key:'usage',label:'查看用量',children:selected?.view==='usage'?<TaskUsage key={task.run.id} runId={task.run.id}/>:null},
      ]}/>}
    </Modal>
    <Image style={{display:'none'}} src={image} preview={{visible:!!image,onVisibleChange:visible=>{if(!visible)setImage('');}}}/>
  </div>;
}
