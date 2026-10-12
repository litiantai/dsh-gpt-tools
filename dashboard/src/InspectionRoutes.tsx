import {useState} from 'react';
import {Alert, Button, Descriptions, Modal, Space, Switch, Table, Tag} from 'antd';
import {api} from './api';
import {time, useData} from './components';
import {errorText} from './errors';
import {RecordValue} from './RecordDetails';
type Route={id:string;version:number;title:string;business_line:string;environment:string;enabled:boolean;status:string;coverage_reason?:string;definition:unknown};
type Plan={id:string;status:string;reason?:string;result?:{coverage_gaps?:string[]}};
export default function InspectionRoutes({pid}:{pid:string}) {
  const path=`/products/${pid}/inspection-routes`;
  const query=useData<{design_tokens:number;inspection_tokens:number;routes:Route[];plans:Plan[];rounds:{id:string;status:string;created:number}[]}>(path);
  const [selected,setSelected]=useState<Route>();const [busy,setBusy]=useState(false);const [error,setError]=useState('');
  const act=async(url:string,body:Record<string,unknown>)=>{setBusy(true);try{await api(url,body);setError('');await query.refetch();}catch(e){setError(errorText(e));}finally{setBusy(false);}};
  const plan=query.data?.plans.at(-1);
  return <>
    <Space><Button type="primary" disabled={busy||['queued','running'].includes(plan?.status||'')} onClick={()=>void act(path+'/generate',{})}>重新生成巡检路线</Button><Button disabled={busy} onClick={()=>void act(path+'/rescan',{})}>重新扫描</Button><span>每轮执行全部启用路线；异常每日北京时间 09:00 复扫。</span></Space>
    {(error||query.error)&&<Alert type="error" message={error||errorText(query.error)}/>}
    {plan&&<p>路线设计：{({queued:'排队中',running:'设计中',completed:'已完成',blocked:'阻塞'} as Record<string,string>)[plan.status]||plan.status} {plan.reason}</p>}
    {!!plan?.result?.coverage_gaps?.length&&<Alert type="warning" message="覆盖缺口" description={plan.result.coverage_gaps.join('；')}/>}
    <Table rowKey="id" dataSource={query.data?.routes||[]} columns={[
      {title:'业务线',dataIndex:'business_line',filters:[...new Set(query.data?.routes.map(r=>r.business_line))].map(value=>({text:value,value})),onFilter:(value,row)=>row.business_line===value},
      {title:'路线',dataIndex:'title',render:(value:string,row:Route)=><Button type="link" onClick={()=>setSelected(row)}>{value}</Button>},
      {title:'环境',dataIndex:'environment',render:(value:string)=>value==='master'?'主实例只读':'隔离测试'},
      {title:'状态',render:(_,row)=><><Tag>{row.status==='ready'?'可执行':'待核对'}</Tag>{row.coverage_reason}</>},
      {title:'启用',render:(_,row)=><Switch checked={row.enabled} disabled={busy} onChange={enabled=>void act(`${path}/${row.id}/toggle`,{version:row.version,enabled})}/>},
    ]}/>
    <p>今日路线设计用量：{query.data?.design_tokens||0} Token · 巡检执行用量：{query.data?.inspection_tokens||0} Token</p>
    <h3>最近巡检轮次</h3><Descriptions column={1} items={(query.data?.rounds||[]).slice(-5).reverse().map(r=>({key:r.id,label:time(r.created),children:r.status}))}/>
    <Modal title={selected?.title} open={!!selected} onCancel={()=>setSelected(undefined)} footer={null} width={900}><RecordValue value={selected?.definition}/><pre style={{whiteSpace:'pre-wrap'}}>{JSON.stringify(selected?.definition,null,2)}</pre></Modal>
  </>;
}
